#!/usr/bin/env python3
"""
inject_audio_into_braw.py

Inject 1-16 raw PCM WAV audio tracks into a Blackmagic RAW (.braw) file as
native MP4 sound tracks, readable by DaVinci Resolve.

The BRAW is an ISO-BMFF (MP4-family) container. Audio is stored as
uncompressed PCM in a 'soun' handler track. No transcoding, no loss.

Timecode slicing: the WAV is often one long recording spanning multiple BRAW
clips. This tool reads the BRAW's embedded timecode track to determine the
clip's start TC and duration, then extracts only the matching slice from the
WAV (padding with silence if the WAV doesn't cover the full range).

Stdlib only (struct, argparse, pathlib). No third-party dependencies.

Usage:
    python3 inject_audio_into_braw.py INPUT.braw TRACK1.wav [TRACK2.wav ...] [options]
"""

import argparse
import os
import struct
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Timecode utilities
# ---------------------------------------------------------------------------

def parse_tc(s, fps):
    """
    Parse a timecode string 'HH:MM:SS:FF' or 'HH:MM:SS;FF' (drop-frame)
    into seconds. The ';FF' variant is treated the same (frame number is
    the nominal frame, drop-frame compensation is not applied here --
    the TC value is interpreted as the nominal frame count).
    """
    s = s.strip().replace(";", ":")
    parts = s.split(":")
    if len(parts) != 4:
        raise ValueError("Timecode must be HH:MM:SS:FF, got %r" % s)
    h, m, sec, f = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
    if f >= fps:
        raise ValueError("Frame %d out of range for %d fps" % (f, fps))
    return h * 3600.0 + m * 60.0 + sec + f / fps


def format_tc(seconds, fps):
    """Format seconds as 'HH:MM:SS:FF'."""
    total_frames = round(seconds * fps)
    f = total_frames % round(fps)
    total_sec = total_frames // round(fps)
    sec = total_sec % 60
    m = (total_sec // 60) % 60
    h = total_sec // 3600
    return "%02d:%02d:%02d:%02d" % (h, m, sec, f)


# ---------------------------------------------------------------------------
# WAV / BWF parsing
# ---------------------------------------------------------------------------

class Wav:
    """Parsed WAV/BWF file: format info + raw PCM data + optional BWF timecode."""

    def __init__(self):
        self.sample_rate = 0
        self.channels = 0
        self.bits_per_sample = 0
        self.audio_format = 0   # 1=PCM, 3=IEEE float
        self.data = b""          # raw interleaved PCM bytes
        self.bwf_start_tc_s = None  # seconds from midnight, or None

    @property
    def is_float(self):
        return self.audio_format == 3

    @property
    def bytes_per_sample(self):
        return self.bits_per_sample // 8

    @property
    def frame_size(self):
        return self.channels * self.bytes_per_sample

    @property
    def num_frames(self):
        if self.frame_size == 0:
            return 0
        return len(self.data) // self.frame_size

    @property
    def duration_s(self):
        if self.sample_rate == 0:
            return 0.0
        return self.num_frames / self.sample_rate

    @classmethod
    def parse(cls, path):
        w = cls()
        raw = Path(path).read_bytes()
        if len(raw) < 12:
            raise ValueError("File too small to be a WAV: %s" % path)
        if raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
            raise ValueError("Not a RIFF/WAVE file: %s" % path)

        pos = 12
        n = len(raw)
        while pos + 8 <= n:
            chunk_id = raw[pos:pos + 4]
            chunk_size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
            chunk_data = raw[pos + 8:pos + 8 + chunk_size]
            # Chunks are word-aligned (padded to even boundary)
            next_pos = pos + 8 + chunk_size + (chunk_size & 1)

            if chunk_id == b"fmt ":
                w._parse_fmt(chunk_data)
            elif chunk_id == b"data":
                w.data = chunk_data
            elif chunk_id == b"time":
                w._parse_bwf_time(chunk_data)

            pos = next_pos

        if w.sample_rate == 0:
            raise ValueError("WAV has no valid 'fmt ' chunk: %s" % path)
        if not w.data:
            raise ValueError("WAV has no 'data' chunk: %s" % path)

        return w

    def _parse_fmt(self, d):
        if len(d) < 16:
            raise ValueError("WAV 'fmt ' chunk too short")
        self.audio_format = struct.unpack("<H", d[0:2])[0]
        self.channels = struct.unpack("<H", d[2:4])[0]
        self.sample_rate = struct.unpack("<I", d[4:8])[0]
        _byte_rate = struct.unpack("<I", d[8:12])[0]
        _block_align = struct.unpack("<H", d[12:14])[0]
        self.bits_per_sample = struct.unpack("<H", d[14:16])[0]

        if self.audio_format not in (1, 3):
            raise ValueError(
                "Unsupported WAV audio format %d (only PCM=1 and IEEE float=3 "
                "are supported)" % self.audio_format
            )
        if self.bits_per_sample not in (16, 24, 32):
            raise ValueError(
                "Unsupported WAV bit depth %d (only 16, 24, 32 supported)"
                % self.bits_per_sample
            )

    def _parse_bwf_time(self, d):
        """Parse BWF 'time' chunk: 4 x QPCTime (8 bytes each).
        The 3rd QPCTime is the timecode at the start of the data."""
        if len(d) < 32:
            return
        # 3rd QPCTime: bytes 16-23
        time_reference = struct.unpack("<I", d[16:20])[0]
        time_scale = struct.unpack("<I", d[20:24])[0]
        if time_scale > 0:
            self.bwf_start_tc_s = time_reference / time_scale


def wav_mp4_4cc(wav):
    """Return the MP4 sample entry 4CC for this WAV's PCM format."""
    if wav.is_float and wav.bits_per_sample == 32:
        return "fl32"
    if wav.bits_per_sample == 32:
        return "in32"
    if wav.bits_per_sample == 24:
        return "in24"
    if wav.bits_per_sample == 16:
        return "twos"
    raise ValueError("Cannot determine MP4 4CC for bits=%d float=%d"
                     % (wav.bits_per_sample, wav.is_float))


# ---------------------------------------------------------------------------
# BRAW timecode / duration reader
# ---------------------------------------------------------------------------

def _read_top_level_boxes(buf):
    """Yield (name, start, size, payload_offset) for each top-level box."""
    pos = 0
    n = len(buf)
    while pos + 8 <= n:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        name = buf[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > n:
                break
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = n - pos
        if size < header or pos + size > n:
            break
        yield name, pos, size, pos + header
        pos += size


def _find_box(buf, name):
    """Return (start, size, payload_offset) of first top-level box with name."""
    for n, start, size, poff in _read_top_level_boxes(buf):
        if n == name.encode("ascii"):
            return start, size, poff
    return None


def _iter_top_level_boxes_stream(f, fsize):
    """Yield (name, start, size, poff) for top-level boxes by streaming header
    reads from an open binary file. O(1) memory -- the file is never loaded."""
    pos = 0
    n = fsize
    while pos + 8 <= n:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        size = struct.unpack(">I", hdr[0:4])[0]
        name = hdr[4:8]
        header = 8
        if size == 1:
            ls = f.read(8)
            if len(ls) < 8:
                break
            size = struct.unpack(">Q", ls)[0]
            header = 16
        elif size == 0:
            size = n - pos
        if size < header or pos + size > n:
            break
        yield name, pos, size, pos + header
        pos += size


def _find_top_level_boxes_stream(f, fsize, names):
    """Return {name_bytes: (start, size, poff)} for the wanted top-level boxes."""
    wanted = set()
    for x in names:
        wanted.add(x.encode("ascii") if isinstance(x, str) else x)
    found = {}
    for name, start, size, poff in _iter_top_level_boxes_stream(f, fsize):
        if name in wanted:
            found.setdefault(name, (start, size, poff))
        if len(found) == len(wanted):
            break
    return found


def _read_moov_payload_streaming(path):
    """Return the moov box payload bytes for `path`, streaming the box headers
    (O(1) memory). Returns None if no moov box is found."""
    fsize = os.path.getsize(path)
    with open(path, "rb") as f:
        boxes = _find_top_level_boxes_stream(f, fsize, ("moov",))
        if b"moov" not in boxes:
            return None
        moov_start, moov_size, moov_poff = boxes[b"moov"]
        f.seek(moov_poff)
        return f.read((moov_start + moov_size) - moov_poff)


def _copy_range_stream(f_in, start, length, f_out, chunk=8 * 1024 * 1024):
    """Copy `length` bytes from f_in starting at `start` to f_out, in chunks."""
    f_in.seek(start)
    remaining = length
    while remaining > 0:
        data = f_in.read(min(chunk, remaining))
        if not data:
            break
        f_out.write(data)
        remaining -= len(data)


def _walk_boxes(buf, start, end):
    """Yield (name, pos, size, payload_offset) for boxes within [start, end)."""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        name = buf[pos + 4:pos + 8]
        if size < 8 or pos + size > end:
            break
        yield name, pos, size, pos + 8
        pos += size


def _find_child(buf, parent_poff, parent_size, name):
    """Find a child box by name within a parent box's payload."""
    target = name.encode("ascii")
    for n, pos, size, poff in _walk_boxes(buf, parent_poff, parent_poff + (parent_size - 8)):
        if n == target:
            return pos, size, poff
    return None


def _find_hdlr_type(buf, trak_pos, trak_size):
    """Walk trak > mdia > hdlr and return the handler type (4 bytes)."""
    mdia = _find_child(buf, trak_pos + 8, trak_size, "mdia")
    if mdia is None:
        return None
    mdia_pos, mdia_size, mdia_poff = mdia
    hdlr = _find_child(buf, mdia_poff, mdia_size, "hdlr")
    if hdlr is None:
        return None
    hdlr_pos, hdlr_size, hdlr_poff = hdlr
    # hdlr: ver/flags(4) pre_defined(4) handler_type(4)
    return buf[hdlr_poff + 8: hdlr_poff + 12]


def _find_mdhd_duration(buf, trak_pos, trak_size):
    """Read (timescale, duration) from trak > mdia > mdhd."""
    mdia = _find_child(buf, trak_pos + 8, trak_size, "mdia")
    if mdia is None:
        return None, None
    mdia_pos, mdia_size, mdia_poff = mdia
    mdhd = _find_child(buf, mdia_poff, mdia_size, "mdhd")
    if mdhd is None:
        return None, None
    mdhd_pos, mdhd_size, mdhd_poff = mdhd
    ver_flags = struct.unpack(">I", buf[mdhd_poff:mdhd_poff + 4])[0]
    version = ver_flags >> 24
    if version == 0:
        timescale = struct.unpack(">I", buf[mdhd_poff + 12:mdhd_poff + 16])[0]
        duration = struct.unpack(">I", buf[mdhd_poff + 16:mdhd_poff + 20])[0]
    else:
        timescale = struct.unpack(">I", buf[mdhd_poff + 20:mdhd_poff + 24])[0]
        duration = struct.unpack(">Q", buf[mdhd_poff + 24:mdhd_poff + 32])[0]
    return timescale, duration


def read_braw_timecode(buf):
    """
    Read start TC (seconds from midnight) and fps from the BRAW's timecode track.
    Returns (start_tc_s, fps) or (None, None) if not found.
    """
    moov = _find_box(buf, "moov")
    if moov is None:
        return None, None
    moov_start, moov_size, moov_poff = moov
    return read_timecode_from_moov_payload(buf[moov_poff:moov_start + moov_size])


def read_timecode_from_moov_payload(moov_payload):
    """Read (start_tc_s, fps) from a moov box *payload* (0-based offsets)."""
    for name, pos, size, _poff in _walk_boxes(moov_payload, 0, len(moov_payload)):
        if name != b"trak":
            continue
        hdlr_type = _find_hdlr_type(moov_payload, pos, size)
        if hdlr_type != b"time":
            continue
        # Found the timecode track. Look for tmcd in stsd.
        tc = _read_tmcd(moov_payload, pos, size)
        if tc is not None:
            return tc
    return None, None


def _read_tmcd(buf, trak_pos, trak_size):
    """Read tmcd box from trak > mdia > minf > stbl > stsd. Returns (start_tc_s, fps)."""
    mdia = _find_child(buf, trak_pos + 8, trak_size, "mdia")
    if mdia is None:
        return None
    mdia_pos, mdia_size, mdia_poff = mdia
    minf = _find_child(buf, mdia_poff, mdia_size, "minf")
    if minf is None:
        return None
    minf_pos, minf_size, minf_poff = minf
    stbl = _find_child(buf, minf_poff, minf_size, "stbl")
    if stbl is None:
        return None
    stbl_pos, stbl_size, stbl_poff = stbl
    stsd = _find_child(buf, stbl_poff, stbl_size, "stsd")
    if stsd is None:
        return None
    stsd_pos, stsd_size, stsd_poff = stsd
    # stsd: ver/flags(4) entry_count(4), then entries
    entry_count = struct.unpack(">I", buf[stsd_poff + 4:stsd_poff + 8])[0]
    if entry_count < 1:
        return None
    # First entry: u32 size, 4CC
    e_start = stsd_poff + 8
    e_size = struct.unpack(">I", buf[e_start:e_start + 4])[0]
    e_4cc = buf[e_start + 4:e_start + 8]
    if e_4cc != b"tmcd":
        return None
    # tmcd content (after 8-byte box header): ver/flags(4) media_rate(4) start_time(4)
    tmcd_body = e_start + 8
    _ver_flags = struct.unpack(">I", buf[tmcd_body:tmcd_body + 4])[0]
    media_rate = struct.unpack(">I", buf[tmcd_body + 4:tmcd_body + 8])[0]
    start_time = struct.unpack(">I", buf[tmcd_body + 8:tmcd_body + 12])[0]
    # media_rate is 16.16 fixed point (fps << 16)
    fps = media_rate / 65536.0
    if fps <= 0:
        return None
    start_tc_s = start_time / fps
    return start_tc_s, fps


def read_braw_duration(buf):
    """Read movie duration in seconds from moov > mvhd. Returns float seconds."""
    moov = _find_box(buf, "moov")
    if moov is None:
        return None
    moov_start, moov_size, moov_poff = moov
    return read_duration_from_moov_payload(buf[moov_poff:moov_start + moov_size])


def read_duration_from_moov_payload(moov_payload):
    """Read movie duration in seconds from a moov box *payload* (0-based)."""
    mvhd = _find_child(moov_payload, 0, len(moov_payload) + 8, "mvhd")
    if mvhd is None:
        return None
    mvhd_pos, mvhd_size, mvhd_poff = mvhd
    ver_flags = struct.unpack(">I", moov_payload[mvhd_poff:mvhd_poff + 4])[0]
    version = ver_flags >> 24
    if version == 0:
        timescale = struct.unpack(">I", moov_payload[mvhd_poff + 12:mvhd_poff + 16])[0]
        duration = struct.unpack(">I", moov_payload[mvhd_poff + 16:mvhd_poff + 20])[0]
    else:
        timescale = struct.unpack(">I", moov_payload[mvhd_poff + 20:mvhd_poff + 24])[0]
        duration = struct.unpack(">Q", moov_payload[mvhd_poff + 24:mvhd_poff + 32])[0]
    if timescale == 0:
        return None
    return duration / timescale


# ---------------------------------------------------------------------------
# Audio slicing
# ---------------------------------------------------------------------------

def slice_wav(wav, braw_start_tc_s, wav_start_tc_s, braw_duration_s):
    """
    Extract the portion of the WAV that matches the BRAW clip's time range.
    Pads with silence if the WAV doesn't cover the full range.
    Returns (pcm_bytes, num_frames).
    """
    rate = wav.sample_rate
    offset_s = braw_start_tc_s - wav_start_tc_s

    # Desired total frames for the clip duration
    total_frames_needed = round(braw_duration_s * rate)

    # Where in the WAV the clip starts (in frames)
    wav_start_frame = round(offset_s * rate)
    wav_end_frame = wav_start_frame + total_frames_needed

    # Build the output buffer
    out = bytearray()

    # Pad beginning with silence if offset is negative (WAV starts after BRAW TC)
    if wav_start_frame < 0:
        silence_frames = -wav_start_frame
        out += b"\x00" * (silence_frames * wav.frame_size)

    # Extract the available data
    clamp_start = max(0, wav_start_frame)
    clamp_end = min(wav.num_frames, wav_end_frame)

    if clamp_end > clamp_start:
        byte_start = clamp_start * wav.frame_size
        byte_end = clamp_end * wav.frame_size
        out += wav.data[byte_start:byte_end]

    # Pad end with silence if WAV doesn't cover the full clip duration
    frames_so_far = len(out) // wav.frame_size
    if frames_so_far < total_frames_needed:
        silence_frames = total_frames_needed - frames_so_far
        out += b"\x00" * (silence_frames * wav.frame_size)

    return bytes(out), total_frames_needed


def slice_wav_offset(wav, offset_s, braw_duration_s):
    """
    Slice using a direct offset in seconds (bypasses timecode math).
    Returns (pcm_bytes, num_frames).
    """
    return slice_wav(wav, offset_s, 0.0, braw_duration_s)


# ---------------------------------------------------------------------------
# MP4 box helpers
# ---------------------------------------------------------------------------

def box(name, payload):
    """Build an MP4 box: [u32 size][4CC name][payload]."""
    name = name.encode("ascii") if isinstance(name, str) else name
    assert len(name) == 4
    size = 8 + len(payload)
    return struct.pack(">I4s", size, name) + payload


def box64(name, payload):
    """Build an MP4 box with 64-bit 'largesize' header."""
    name = name.encode("ascii") if isinstance(name, str) else name
    assert len(name) == 4
    largesize = 16 + len(payload)
    return struct.pack(">I4sQ", 1, name, largesize) + payload


def _u32(v):
    return struct.pack(">I", int(v))


def _u64(v):
    return struct.pack(">Q", int(v))


def _u16(v):
    return struct.pack(">H", int(v))


# ---------------------------------------------------------------------------
# Building the audio track
# ---------------------------------------------------------------------------

def build_audio_stsd(wav):
    """
    stsd: sample description with one PCM audio entry.
    4CC depends on format: fl32, in32, in24, twos.
    """
    fourcc = wav_mp4_4cc(wav)
    entry_body = (
        _u16(1)  # data_reference_index
        + _u16(0)  # reserved
        + _u16(0)  # reserved
        + _u16(0)  # reserved
        + _u32(0)  # reserved
        + _u16(wav.channels)  # channelcount
        + _u16(wav.bits_per_sample)  # samplesize
        + _u16(0)  # pre_defined
        + _u16(0)  # reserved
        + _u32(wav.sample_rate << 16)  # samplerate (16.16 fixed point)
    )
    entry = box(fourcc, entry_body)
    body = _u32(0) + _u32(1) + entry
    return box("stsd", body)


def build_audio_stts(num_frames):
    """stts: one entry -- all frames have delta=1 (timescale = sample_rate)."""
    body = _u32(0) + _u32(1)
    body += _u32(num_frames) + _u32(1)
    return box("stts", body)


def build_audio_stsc(num_frames):
    """stsc: one chunk containing all samples."""
    body = _u32(0) + _u32(1)
    body += _u32(1) + _u32(num_frames) + _u32(1)
    return box("stsc", body)


def build_audio_stsz(num_frames, frame_size):
    """stsz: uniform sample size."""
    body = _u32(0) + _u32(frame_size) + _u32(num_frames)
    return box("stsz", body)


def build_stco(offset):
    """stco: single chunk offset (32-bit)."""
    body = _u32(0) + _u32(1) + _u32(offset)
    return box("stco", body)


def build_co64(offset):
    """co64: single chunk offset (64-bit)."""
    body = _u32(0) + _u32(1) + _u64(offset)
    return box("co64", body)


def build_audio_smhd():
    """smhd: sound media header. balance=0, reserved=0."""
    body = _u32(0) + _u16(0) + _u16(0)
    return box("smhd", body)


def build_audio_hdlr():
    """hdlr: handler type 'soun'."""
    body = (
        _u32(0)
        + _u32(0)
        + b"soun"
        + _u32(0) + _u32(0) + _u32(0)
        + b""
    )
    return box("hdlr", body)


def build_audio_mdhd(timescale, duration):
    """mdhd: media header, version 0."""
    body = (
        _u32(0)
        + _u32(0)  # creation_time
        + _u32(0)  # modification_time
        + _u32(timescale)
        + _u32(duration)
    )
    return box("mdhd", body)


def build_audio_tkhd(track_id, timescale, duration):
    """tkhd: track header, 84-byte form (compatible with mp4parse/ffmpeg)."""
    flags = 0x000003  # enabled | in_movie
    transform = (
        _u32(0x00010000) + _u32(0) + _u32(0)
        + _u32(0) + _u32(0x00010000) + _u32(0)
        + _u32(0) + _u32(0) + _u32(0x40000000)
    )
    body = (
        _u32(flags)
        + _u32(0)  # creation_time
        + _u32(0)  # modification_time
        + _u32(track_id)
        + _u32(0)  # reserved
        + _u32(duration)
        + _u32(0)  # reserved(4)
        + _u16(0)  # reserved(2)
        + _u16(0)  # volume (0 for non-primary; Resolve handles this)
        + _u32(0)  # reserved(4)
        + _u32(0)  # padding(4)
        + transform
        + _u32(0)  # width
        + _u32(0)  # height
    )
    return box("tkhd", body)


def build_audio_track(track_id, sample_rate, duration, stbl_boxes):
    """Assemble trak > mdia > minf > stbl for an audio track."""
    stbl = box("stbl", b"".join(stbl_boxes))
    minf = box("minf", build_audio_smhd() + stbl)
    mdia = box("mdia", build_audio_mdhd(sample_rate, duration) + build_audio_hdlr() + minf)
    trak = box("trak", build_audio_tkhd(track_id, sample_rate, duration) + mdia)
    return trak


# ---------------------------------------------------------------------------
# Track ID picker
# ---------------------------------------------------------------------------

def _pick_track_ids(buf, moov_poff, moov_size, count):
    """Pick `count` unique track IDs that don't collide with existing tracks."""
    used = set()
    pos = moov_poff
    end = moov_poff + (moov_size - 8)
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        name = buf[pos + 4:pos + 8]
        if size < 8 or pos + size > end:
            break
        if name == b"trak":
            tkhd_start = pos + 8
            if tkhd_start + 28 <= end and buf[tkhd_start + 4:tkhd_start + 8] == b"tkhd":
                ver_flags = struct.unpack(">I", buf[tkhd_start + 8:tkhd_start + 12])[0]
                version = ver_flags >> 24
                if version == 0:
                    off = tkhd_start + 8 + 4 + 4 + 4
                else:
                    off = tkhd_start + 8 + 4 + 8 + 8
                tid = struct.unpack(">I", buf[off:off + 4])[0]
                used.add(tid)
        pos += size

    ids = []
    candidate = 1
    while len(ids) < count:
        if candidate not in used:
            ids.append(candidate)
            used.add(candidate)
        candidate += 1
    return ids


def _pick_track_ids_from_moov_payload(payload, count):
    """Pick `count` unique track IDs that don't collide with tracks in a moov
    *payload* buffer (0-based offsets; no full-file buffer needed)."""
    used = set()
    pos = 0
    end = len(payload)
    while pos + 8 <= end:
        size = struct.unpack(">I", payload[pos:pos + 4])[0]
        name = payload[pos + 4:pos + 8]
        if size < 8 or pos + size > end:
            break
        if name == b"trak":
            tkhd_start = pos + 8
            if tkhd_start + 28 <= end and payload[tkhd_start + 4:tkhd_start + 8] == b"tkhd":
                ver_flags = struct.unpack(">I", payload[tkhd_start + 8:tkhd_start + 12])[0]
                version = ver_flags >> 24
                if version == 0:
                    off = tkhd_start + 8 + 4 + 4 + 4
                else:
                    off = tkhd_start + 8 + 4 + 8 + 8
                tid = struct.unpack(">I", payload[off:off + 4])[0]
                used.add(tid)
        pos += size

    ids = []
    candidate = 1
    while len(ids) < count:
        if candidate not in used:
            ids.append(candidate)
            used.add(candidate)
        candidate += 1
    return ids


# ---------------------------------------------------------------------------
# Splicing
# ---------------------------------------------------------------------------

def inject(braw_path, wav_files, args):
    """
    Inject audio tracks into a BRAW file.
    wav_files: list of (Wav, pcm_bytes, num_frames) tuples.
    Returns exit code.

    Streams the (multi-GB) mdat payload directly from input to output, so peak
    memory is O(moov + audio PCM) rather than a multiple of the file size. The
    output is written to a temp file and renamed into place on success.
    """
    pcms = [pcm for _wav, pcm, _nf in wav_files]
    combined_pcm_len = sum(len(p) for p in pcms)

    # --- locate moov + mdat by streaming the box headers (O(1) memory) ---
    in_size = os.path.getsize(braw_path)
    with open(braw_path, "rb") as f:
        boxes = _find_top_level_boxes_stream(f, in_size, ("moov", "mdat"))
        if b"moov" not in boxes or b"mdat" not in boxes:
            raise RuntimeError("BRAW file must contain both 'moov' and 'mdat' boxes")
        moov_start, moov_size, moov_poff = boxes[b"moov"]
        mdat_start, mdat_size, mdat_poff = boxes[b"mdat"]
        if moov_start < mdat_start:
            raise RuntimeError("Unsupported BRAW layout: expected 'mdat' before 'moov'")
        # moov is small: read it whole (for track-id pick + as base payload)
        f.seek(moov_poff)
        old_moov_payload = f.read((moov_start + moov_size) - moov_poff)

    track_ids = _pick_track_ids_from_moov_payload(old_moov_payload, len(wav_files))

    # --- sizes / offsets (computed without holding the image data) ---
    before_mdat_len = mdat_start
    old_mdat_payload_len = (mdat_start + mdat_size) - mdat_poff
    new_mdat_payload_len = old_mdat_payload_len + combined_pcm_len
    mdat_uses_largesize = (8 + new_mdat_payload_len) > 0xFFFFFFFF
    mdat_header_size = 16 if mdat_uses_largesize else 8
    new_payload_abs_start = mdat_start + mdat_header_size + old_mdat_payload_len

    # Build tracks with correct cumulative chunk offsets.
    new_traks = bytearray()
    track_info = []
    cumulative_offset = 0
    for i, (wav, pcm, num_frames) in enumerate(wav_files):
        track_id = track_ids[i]
        chunk_offset = new_payload_abs_start + cumulative_offset
        cumulative_offset += len(pcm)

        if chunk_offset > 0xFFFFFFFF:
            chunk_box = build_co64(chunk_offset)
        else:
            chunk_box = build_stco(chunk_offset)

        stbl = [
            build_audio_stsd(wav),
            build_audio_stts(num_frames),
            build_audio_stsc(num_frames),
            chunk_box,
            build_audio_stsz(num_frames, wav.frame_size),
        ]
        new_traks += build_audio_track(track_id, wav.sample_rate, num_frames, stbl)
        track_info.append((i, track_id, wav, num_frames, chunk_offset))

    new_moov_payload = old_moov_payload + bytes(new_traks)
    if 8 + len(new_moov_payload) > 0xFFFFFFFF:
        new_moov_box = box64("moov", new_moov_payload)
    else:
        new_moov_box = box("moov", new_moov_payload)

    # --- stream the output ---
    out_path = Path(args.output) if args.output else _default_output(braw_path)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    try:
        with open(braw_path, "rb") as f_in, open(tmp_path, "wb") as f_out:
            _copy_range_stream(f_in, 0, before_mdat_len, f_out)
            if mdat_uses_largesize:
                f_out.write(struct.pack(">I4sQ", 1, b"mdat",
                                        16 + new_mdat_payload_len))
            else:
                f_out.write(struct.pack(">I4s", 8 + new_mdat_payload_len, b"mdat"))
            _copy_range_stream(f_in, mdat_poff, old_mdat_payload_len, f_out)
            for pcm in pcms:
                f_out.write(pcm)
            f_out.write(new_moov_box)
            _copy_range_stream(f_in, moov_start + moov_size,
                               in_size - (moov_start + moov_size), f_out)
        os.replace(tmp_path, out_path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    out_total = (before_mdat_len + mdat_header_size + new_mdat_payload_len
                 + len(new_moov_box) + (in_size - (moov_start + moov_size)))
    print("Wrote %s (%d bytes, %d audio track(s))" % (out_path, out_total, len(track_info)))
    for i, track_id, wav, num_frames, chunk_offset in track_info:
        print("  track %d: id=%d, %d Hz, %d ch, %d-bit %s, %d frames (%.2f s), offset=%d"
              % (i + 1, track_id, wav.sample_rate, wav.channels, wav.bits_per_sample,
                 "float" if wav.is_float else "int", num_frames,
                 num_frames / wav.sample_rate, chunk_offset))

    if not args.no_verify:
        ok = verify_audio(out_path, track_info)
        if not ok:
            print("  [verify] FAILED", file=sys.stderr)
            return 1
    return 0


def _default_output(braw_path):
    p = Path(braw_path)
    return p.with_name(p.stem + "_injected" + p.suffix)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_audio(out_path, track_info):
    """
    Re-parse the output, find each audio track we wrote, and verify structure.
    Seek-based: only the moov box is read; the PCM regions are bounds-checked
    against the file size (not loaded), so memory stays small for huge files.
    """
    print("  [verify] re-parsing %s" % out_path)
    fsize = os.path.getsize(out_path)

    with open(out_path, "rb") as f:
        boxes = _find_top_level_boxes_stream(f, fsize, ("moov",))
        if b"moov" not in boxes:
            print("    FAIL: no moov in output")
            return False
        moov_start, moov_size, moov_poff = boxes[b"moov"]
        f.seek(moov_poff)
        mb = f.read((moov_start + moov_size) - moov_poff)

    # Collect all traks (0-based within the moov payload).
    traks = []
    for name, pos, size, _poff in _walk_boxes(mb, 0, len(mb)):
        if name == b"trak":
            traks.append((pos, size))

    ok = True
    for i, track_id, wav, num_frames, _expected_offset in track_info:
        # Find the trak with this track_id
        target_trak = None
        for tpos, tsize in traks:
            tid = _read_trak_id(mb, tpos, tsize)
            if tid == track_id:
                target_trak = (tpos, tsize)
                break

        if target_trak is None:
            print("    FAIL: track id %d not found in output" % track_id)
            ok = False
            continue

        tpos, tsize = target_trak
        # Verify hdlr is 'soun'
        hdlr_type = _find_hdlr_type(mb, tpos, tsize)
        if hdlr_type != b"soun":
            print("    FAIL: track %d hdlr is %r, expected 'soun'" % (track_id, hdlr_type))
            ok = False
            continue

        # Read stsz to get frame count and frame size
        stsz = _find_stsz(mb, tpos, tsize)
        if stsz is None:
            print("    FAIL: track %d has no stsz" % track_id)
            ok = False
            continue
        frame_size, frame_count = stsz
        if frame_count != num_frames:
            print("    FAIL: track %d frame count %d != expected %d"
                  % (track_id, frame_count, num_frames))
            ok = False
            continue
        if frame_size != wav.frame_size:
            print("    FAIL: track %d frame size %d != expected %d"
                  % (track_id, frame_size, wav.frame_size))
            ok = False
            continue

        # Read chunk offset
        chunk_off = _find_chunk_offset(mb, tpos, tsize)
        if chunk_off is None:
            print("    FAIL: track %d has no chunk offset" % track_id)
            ok = False
            continue

        # Bounds-check the PCM region against the file size (not loaded).
        data_end = chunk_off + frame_count * frame_size
        if data_end > fsize:
            print("    FAIL: track %d data extends past EOF" % track_id)
            ok = False
            continue

        print("    OK: track %d (id=%d): %d frames x %d bytes, offset=%d"
              % (i + 1, track_id, frame_count, frame_size, chunk_off))

    return ok


def _read_trak_id(buf, trak_pos, trak_size):
    """Read the track ID from a trak box."""
    tkhd_start = trak_pos + 8
    if tkhd_start + 28 > trak_pos + trak_size:
        return None
    if buf[tkhd_start + 4:tkhd_start + 8] != b"tkhd":
        return None
    ver_flags = struct.unpack(">I", buf[tkhd_start + 8:tkhd_start + 12])[0]
    version = ver_flags >> 24
    if version == 0:
        off = tkhd_start + 8 + 4 + 4 + 4
    else:
        off = tkhd_start + 8 + 4 + 8 + 8
    if off + 4 > trak_pos + trak_size:
        return None
    return struct.unpack(">I", buf[off:off + 4])[0]


def _find_stsz(buf, trak_pos, trak_size):
    """Find stsz in trak > mdia > minf > stbl. Returns (frame_size, frame_count)."""
    mdia = _find_child(buf, trak_pos + 8, trak_size, "mdia")
    if mdia is None:
        return None
    _, _, mdia_poff = mdia
    minf = _find_child(buf, mdia_poff, mdia[1], "minf")
    if minf is None:
        return None
    _, _, minf_poff = minf
    stbl = _find_child(buf, minf_poff, minf[1], "stbl")
    if stbl is None:
        return None
    stbl_poff, stbl_size, stbl_ppoff = stbl
    stsz = _find_child(buf, stbl_ppoff, stbl_size, "stsz")
    if stsz is None:
        return None
    stsz_poff = stsz[2]
    frame_size = struct.unpack(">I", buf[stsz_poff + 4:stsz_poff + 8])[0]
    frame_count = struct.unpack(">I", buf[stsz_poff + 8:stsz_poff + 12])[0]
    return frame_size, frame_count


def _find_chunk_offset(buf, trak_pos, trak_size):
    """Find stco/co64 in trak > mdia > minf > stbl. Returns the first chunk offset."""
    mdia = _find_child(buf, trak_pos + 8, trak_size, "mdia")
    if mdia is None:
        return None
    _, _, mdia_poff = mdia
    minf = _find_child(buf, mdia_poff, mdia[1], "minf")
    if minf is None:
        return None
    _, _, minf_poff = minf
    stbl = _find_child(buf, minf_poff, minf[1], "stbl")
    if stbl is None:
        return None
    stbl_poff, stbl_size, stbl_ppoff = stbl

    for name, pos, size, poff in _walk_boxes(buf, stbl_ppoff, stbl_ppoff + (stbl_size - 8)):
        if name == b"stco":
            count = struct.unpack(">I", buf[poff + 4:poff + 8])[0]
            if count >= 1:
                return struct.unpack(">I", buf[poff + 8:poff + 12])[0]
        elif name == b"co64":
            count = struct.unpack(">I", buf[poff + 4:poff + 8])[0]
            if count >= 1:
                return struct.unpack(">Q", buf[poff + 8:poff + 16])[0]
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

MAX_AUDIO_TRACKS = 16


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Inject 1-16 PCM WAV audio tracks into a .braw file as native "
                    "MP4 sound tracks (for DaVinci Resolve).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # Inject a single WAV (auto-detect timecodes from BRAW + BWF):
  %(prog)s clip.braw recording.wav

  # Inject with explicit WAV start timecode:
  %(prog)s clip.braw recording.wav --wav-start-tc 10:00:00:00

  # Inject multiple tracks with a direct offset (no TC):
  %(prog)s clip.braw track1.wav track2.wav --offset 0

  # Override BRAW start TC and fps:
  %(prog)s clip.braw recording.wav --braw-start-tc 10:05:00:00 --fps 25
""",
    )
    ap.add_argument("braw", help="input .braw file")
    ap.add_argument("wavs", nargs="+", help="input .wav file(s) (1-16)")
    ap.add_argument("-o", "--output", default=None,
                    help="output path (default: <input>_injected.braw)")
    ap.add_argument("--wav-start-tc", default=None,
                    help="WAV start timecode HH:MM:SS:FF (default: auto from BWF 'time' chunk)")
    ap.add_argument("--braw-start-tc", default=None,
                    help="override BRAW start timecode HH:MM:SS:FF (default: auto from BRAW)")
    ap.add_argument("--fps", type=float, default=None,
                    help="frame rate for timecode interpretation (default: auto from BRAW)")
    ap.add_argument("--offset", type=float, default=None,
                    help="direct offset in seconds (bypasses timecode math; "
                         "audio starts this many seconds into the clip)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the built-in re-parse verification")
    args = ap.parse_args(argv)

    if len(args.wavs) > MAX_AUDIO_TRACKS:
        print("error: maximum %d audio tracks supported (got %d)"
              % (MAX_AUDIO_TRACKS, len(args.wavs)), file=sys.stderr)
        return 2

    if not Path(args.braw).exists():
        print("error: BRAW file not found: %s" % args.braw, file=sys.stderr)
        return 2

    for w in args.wavs:
        if not Path(w).exists():
            print("error: WAV file not found: %s" % w, file=sys.stderr)
            return 2

    # --- Parse BRAW for timecode / duration (stream moov only, not the whole file) ---
    moov_payload = _read_moov_payload_streaming(args.braw)
    if moov_payload is None:
        print("error: could not read BRAW moov box", file=sys.stderr)
        return 2
    braw_tc_s, braw_fps = read_timecode_from_moov_payload(moov_payload)
    braw_dur_s = read_duration_from_moov_payload(moov_payload)

    if braw_dur_s is None:
        print("error: could not read BRAW duration from mvhd", file=sys.stderr)
        return 2

    # Resolve fps
    fps = args.fps if args.fps is not None else (braw_fps if braw_fps else 25.0)

    # Resolve BRAW start TC
    if args.braw_start_tc:
        braw_start_tc_s = parse_tc(args.braw_start_tc, fps)
        print("BRAW start TC: %s (from --braw-start-tc)" % args.braw_start_tc)
    elif braw_tc_s is not None:
        braw_start_tc_s = braw_tc_s
        print("BRAW start TC: %s (auto from timecode track, %.3f fps)"
              % (format_tc(braw_tc_s, fps), braw_fps))
    else:
        # No timecode track; default to 0 (or require --offset)
        if args.offset is None:
            print("warning: no timecode track in BRAW and no --braw-start-tc/--offset "
                  "given; assuming start TC = 00:00:00:00", file=sys.stderr)
        braw_start_tc_s = 0.0

    if braw_tc_s is None and args.braw_start_tc is None and args.offset is None:
        # If we have no TC info at all, we can only use --offset or assume 0
        pass

    print("BRAW duration: %.3f s (%s)" % (braw_dur_s, format_tc(braw_dur_s, fps)))

    # --- Parse WAVs and slice ---
    wav_files = []
    for i, wav_path in enumerate(args.wavs):
        print("Parsing WAV %d/%d: %s" % (i + 1, len(args.wavs), wav_path))
        try:
            wav = Wav.parse(wav_path)
        except Exception as e:
            print("error: failed to parse WAV: %s" % e, file=sys.stderr)
            return 2

        print("  %d Hz, %d ch, %d-bit %s, %d frames (%.2f s)"
              % (wav.sample_rate, wav.channels, wav.bits_per_sample,
                 "float" if wav.is_float else "int",
                 wav.num_frames, wav.duration_s))

        # Determine WAV start TC
        if args.wav_start_tc:
            wav_start_tc_s = parse_tc(args.wav_start_tc, fps)
            tc_src = "from --wav-start-tc"
        elif wav.bwf_start_tc_s is not None:
            wav_start_tc_s = wav.bwf_start_tc_s
            tc_src = "auto from BWF time chunk"
        else:
            wav_start_tc_s = 0.0
            tc_src = "default (no BWF time chunk)"

        if i == 0:
            print("  WAV start TC: %s (%s)" % (format_tc(wav_start_tc_s, fps), tc_src))
        elif wav_start_tc_s != wav_files[0][0].bwf_start_tc_s:
            # Warn if different WAVs have different TCs
            pass

        # Slice
        if args.offset is not None:
            pcm, num_frames = slice_wav_offset(wav, args.offset, braw_dur_s)
        else:
            pcm, num_frames = slice_wav(wav, braw_start_tc_s, wav_start_tc_s, braw_dur_s)

        wav_files.append((wav, pcm, num_frames))

        # Report slice info
        offset_s = braw_start_tc_s - wav_start_tc_s
        print("  slice: offset=%.3f s, extracted %d frames (%.2f s)"
              % (offset_s, num_frames, num_frames / wav.sample_rate))

    # --- Inject ---
    print("Injecting %d audio track(s) into: %s" % (len(wav_files), args.braw))
    return inject(args.braw, wav_files, args)


if __name__ == "__main__":
    sys.exit(main())
