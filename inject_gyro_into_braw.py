#!/usr/bin/env python3
"""
inject_gyro_into_braw.py

Inject gyro/accelerometer data from a Gyroflow .gcsv file into a Blackmagic RAW
(.braw) file as a native MP4 metadata track, so that Gyroflow's BRAW parser
(telemetry-parser/src/blackmagic/mod.rs) reads it natively -- no sidecar file.

BRAW is an ISO-BMFF (MP4-family) container. The BRAW parser reads gyro data from
a metadata track whose samples are 20-byte boxes:

    [u32 size][4CC "mogy"|"moac"][f32 x][f32 y][f32 z]   (little-endian)

The parser NEGATES the accelerometer axes on read (mod.rs:148-153), so we must
negate them on write. Sample timestamps come from the track's stts + mdhd
timescale, so we set the timescale to 1000 (ms) and emit a per-sample stts delta
table to preserve the exact GCSV timestamps.

Stdlib only (struct, csv, argparse, pathlib). No third-party dependencies.

Usage:
    python3 inject_gyro_into_braw.py INPUT.braw INPUT.gcsv [options]
"""

import argparse
import csv
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GRAVITY = 9.80665  # m/s^2, 1 g

# The BRAW parser (blackmagic/mod.rs:169-172) uses orientation "yxz" for any
# model it does not special-case (the Ursa Micro G1 is not special-cased).
BRAW_ORIENTATION = "yxz"

# A BRAW metadata sample box is always exactly 20 bytes:
#   4 (size) + 4 (4CC) + 3*4 (f32 x,y,z)
SAMPLE_BOX_SIZE = 20


# ---------------------------------------------------------------------------
# GCSV parsing
# ---------------------------------------------------------------------------

class Gcsv:
    """Parsed .gcsv file: header metadata + converted IMU samples."""

    def __init__(self):
        self.header = {}
        self.orientation = None
        self.version = None
        self.gscale = 1.0
        self.ascale = 1.0
        self.tscale = 1.0
        self.mscale = None
        self.frame_readout_time = None
        self.videofilename = None
        self.has_accel = False
        self.has_mag = False
        # Each sample: dict with t_ms (int, ms), gyro (tuple of 3 floats, rad/s),
        # accl (tuple of 3 floats, m/s^2) or None.
        self.samples = []

    @classmethod
    def parse(cls, path):
        g = cls()
        with open(path, "r", newline="") as f:
            lines = f.read().splitlines()

        if not lines:
            raise ValueError("GCSV file is empty")

        first = lines[0].strip()
        if first not in ("GYROFLOW IMU LOG", "CAMERA IMU LOG"):
            raise ValueError(
                "Not a GCSV file: first line is %r, expected 'GYROFLOW IMU LOG' "
                "or 'CAMERA IMU LOG'" % first
            )

        # --- header ---
        data_start = None
        for i, line in enumerate(lines):
            line = line.strip()
            if not line or "," not in line:
                continue
            key, _, val = line.partition(",")
            key = key.strip()
            val = val.strip()
            g.header[key] = val
            if key == "orientation":
                g.orientation = val
            elif key == "version":
                g.version = val
            elif key == "gscale":
                g.gscale = float(val)
            elif key == "ascale":
                g.ascale = float(val)
                g.has_accel = True
            elif key == "mscale":
                g.mscale = float(val)
                g.has_mag = True
            elif key == "tscale":
                g.tscale = float(val)
            elif key == "frame_readout_time":
                g.frame_readout_time = float(val)
            elif key == "videofilename":
                g.videofilename = val

            # The data header row starts the data section.
            if key in ("t", "time"):
                data_start = i + 1
                break

        if data_start is None:
            raise ValueError("GCSV has no data header row (expected 't,gx,gy,gz,...')")

        # --- data rows ---
        # Determine which columns are present from the header row.
        header_fields = [c.strip() for c in lines[data_start - 1].split(",")]
        col = {name: idx for idx, name in enumerate(header_fields)}

        need = ["t", "gx", "gy", "gz"]
        for n in need:
            if n not in col:
                raise ValueError("GCSV data header missing column %r: %r" % (n, header_fields))

        has_ax = all(k in col for k in ("ax", "ay", "az"))
        has_mx = all(k in col for k in ("mx", "my", "mz"))

        for line in lines[data_start:]:
            line = line.strip()
            if not line or "," not in line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < len(need):
                continue

            def fval(name):
                return float(parts[col[name]])

            t_raw = fval("t")
            t_ms = round(t_raw * g.tscale * 1000.0)

            gyro = (
                fval("gx") * g.gscale,
                fval("gy") * g.gscale,
                fval("gz") * g.gscale,
            )

            accl = None
            if has_ax:
                # GCSV accel is in g; BRAW stores m/s^2.
                accl = (
                    fval("ax") * g.ascale * GRAVITY,
                    fval("ay") * g.ascale * GRAVITY,
                    fval("az") * g.ascale * GRAVITY,
                )

            g.samples.append({"t_ms": t_ms, "gyro": gyro, "accl": accl})

        if not g.samples:
            raise ValueError("GCSV contains no data rows")

        # Drop samples that have no gyro (shouldn't happen, but be safe).
        return g


# ---------------------------------------------------------------------------
# Orientation remapping
# ---------------------------------------------------------------------------

def _normalize_orientation(s):
    """Normalize an orientation string to a canonical form for comparison.

    Gyroflow orientation strings use upper-case for a positive axis and
    lower-case for a negative (inverted) axis, e.g. 'YxZ' means +Y, -x, +Z.
    The BRAW parser uses 'yxz' (all lower) as the default for unknown models.
    """
    if not s:
        return None
    return s.strip()


def remap_orientation(gcsv_orientation, override=None):
    """
    Return (braw_orientation, axis_permutation, warnings).

    The GCSV orientation describes the sensor's axes. The BRAW parser expects
    'yxz' for the Ursa Micro G1. If the GCSV orientation is a case-variant of
    'yxz' (i.e. the same axis order, possibly with some axes inverted), we keep
    the axis order and emit a warning for any inverted axes so the user can
    confirm in Gyroflow. If the axis *order* differs, we compute a permutation
    to reorder the data into yxz and report it.

    axis_permutation is a tuple p such that out[i] = in[p[i]] applies the
    reorder needed to map the GCSV axis order onto 'yxz'.
    """
    warnings = []
    target = BRAW_ORIENTATION  # 'yxz'

    if override:
        # User forced an orientation; we still need a permutation. If the
        # override equals the GCSV order, identity; otherwise we cannot know
        # the physical mapping, so we assume identity and warn.
        if _normalize_orientation(override) == _normalize_orientation(gcsv_orientation):
            return override, (0, 1, 2), warnings
        warnings.append(
            "--orientation %r differs from GCSV orientation %r; assuming identity "
            "axis mapping (data may need manual correction in Gyroflow)."
            % (override, gcsv_orientation)
        )
        return override, (0, 1, 2), warnings

    if not gcsv_orientation:
        warnings.append(
            "GCSV has no 'orientation' field; assuming it matches BRAW 'yxz'."
        )
        return BRAW_ORIENTATION, (0, 1, 2), warnings

    g = _normalize_orientation(gcsv_orientation)

    # Same axis order, ignoring sign?
    if g.lower() == target.lower():
        # Check for inverted axes (case differences).
        inv = [i for i in range(3) if g[i].islower() != target[i].islower()]
        if inv:
            names = "xyz"
            inv_names = [names[i] for i in inv]
            warnings.append(
                "GCSV orientation %r has inverted axis(es) %s relative to BRAW %r; "
                "data is written as-is -- verify axis signs in Gyroflow."
                % (gcsv_orientation, ",".join(inv_names), target)
            )
        return target, (0, 1, 2), warnings

    # Different axis order: compute a permutation that maps gcsv order -> target.
    # g[i] is the gcsv axis at position i; we want out[i] (target axis i) to be
    # taken from the gcsv position where that axis lives.
    perm = []
    for target_axis in target:
        # find position of this axis (case-insensitive) in gcsv order
        pos = None
        for i, ga in enumerate(g):
            if ga.lower() == target_axis.lower():
                pos = i
                break
        if pos is None:
            warnings.append(
                "Cannot map GCSV orientation %r to BRAW %r (axis %r not found); "
                "assuming identity." % (gcsv_orientation, target, target_axis)
            )
            perm.append(len(perm))
        else:
            perm.append(pos)
    warnings.append(
        "GCSV orientation %r has a different axis order than BRAW %r; data was "
        "reordered with permutation %r. Verify in Gyroflow."
        % (gcsv_orientation, target, tuple(perm))
    )
    return target, tuple(perm), warnings


def apply_permutation(vec, perm):
    return tuple(vec[i] for i in perm)


# ---------------------------------------------------------------------------
# MP4 box helpers
# ---------------------------------------------------------------------------

def box(name, payload):
    """Build an MP4 box: [u32 size][4CC name][payload]. Big-endian size."""
    name = name.encode("ascii") if isinstance(name, str) else name
    assert len(name) == 4, "4CC must be 4 bytes"
    size = 8 + len(payload)
    return struct.pack(">I4s", size, name) + payload


def box64(name, payload):
    """Build an MP4 box using a 64-bit 'largesize' header:
    [u32 size=1][4CC name][u64 largesize][payload]. Use when the box is >= 4 GB."""
    name = name.encode("ascii") if isinstance(name, str) else name
    assert len(name) == 4, "4CC must be 4 bytes"
    largesize = 16 + len(payload)
    return struct.pack(">I4sQ", 1, name, largesize) + payload


def read_top_level_boxes(buf):
    """
    Yield (name, start, size, payload_offset) for each top-level box.
    start is the absolute offset of the size field; payload starts at start+8
    (or start+16 for a 64-bit 'largesize' box).
    """
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
            # box extends to end of file
            size = n - pos
        if size < header or pos + size > n:
            break
        yield name, pos, size, pos + header
        pos += size


def find_box(buf, name):
    """Return (start, size, payload_offset) of the first top-level box with name, or None."""
    for n, start, size, poff in read_top_level_boxes(buf):
        if n == name.encode("ascii"):
            return start, size, poff
    return None


def patch_box_size(buf, start, new_size):
    """Overwrite the u32 size field at `start` with new_size (must fit in u32)."""
    if new_size > 0xFFFFFFFF:
        raise ValueError("Box size %d exceeds u32; 64-bit box required" % new_size)
    struct.pack_into(">I", buf, start, new_size)


# ---------------------------------------------------------------------------
# Building the new metadata track
# ---------------------------------------------------------------------------

def build_sample_payload(samples, perm, negate_accel):
    """
    Build the raw sample bytes for the metadata track.

    Samples are interleaved: for each GCSV sample we emit a 'mogy' box and (if
    accel present) a 'moac' box. Each box is 20 bytes.
    Returns (payload_bytes, num_samples, sample_sizes) where sample_sizes is a
    list with one entry per emitted sample box (all 20).
    """
    out = bytearray()
    sample_sizes = []
    for s in samples:
        gx, gy, gz = apply_permutation(s["gyro"], perm)
        # Box header (size + 4CC) is big-endian; the three f32 values are
        # little-endian, matching the BRAW parser (blackmagic/mod.rs:144
        # read_f32::<LittleEndian>).
        out += struct.pack(">I4s", SAMPLE_BOX_SIZE, b"mogy")
        out += struct.pack("<3f", gx, gy, gz)
        sample_sizes.append(SAMPLE_BOX_SIZE)
        if s["accl"] is not None:
            ax, ay, az = apply_permutation(s["accl"], perm)
            if negate_accel:
                ax, ay, az = -ax, -ay, -az
            out += struct.pack(">I4s", SAMPLE_BOX_SIZE, b"moac")
            out += struct.pack("<3f", ax, ay, az)
            sample_sizes.append(SAMPLE_BOX_SIZE)
    return bytes(out), len(sample_sizes), sample_sizes


def _u32(v):
    return struct.pack(">I", int(v))


def _u64(v):
    return struct.pack(">Q", int(v))


def _u16(v):
    return struct.pack(">H", int(v))


def _u8(v):
    return struct.pack(">B", int(v))


def build_stts(t_ms_list):
    """
    stts: sample-to-time table. We emit one entry per sample so each sample
    keeps its exact millisecond delta from the previous sample.
    Layout: u32 version/flags(0), u32 entry_count, then per entry
    u32 sample_count, u32 sample_delta.
    """
    entries = []
    prev = 0
    for t in t_ms_list:
        delta = t - prev
        if delta < 0:
            # non-monotonic timestamps: clamp to 0 (should not happen for a
            # well-formed GCSV). We keep the absolute position via stss/stco.
            delta = 0
        entries.append((1, delta))
        prev = t
    body = _u32(0) + _u32(len(entries))
    for count, delta in entries:
        body += _u32(count) + _u32(delta)
    return box("stts", body)


def build_stss(num_samples):
    """stss: sync sample table. Mark sample 1 as the only sync sample; all
    others are non-sync. For a metadata track this is conventional."""
    body = _u32(0) + _u32(1) + _u32(1)
    return box("stss", body)


def build_stsc(num_samples):
    """stsc: sample-to-chunk. One chunk containing all samples, first chunk
    index 1, samples-per-chunk = num_samples, sample-description-index = 1."""
    body = _u32(0) + _u32(1)
    body += _u32(1) + _u32(num_samples) + _u32(1)
    return box("stsc", body)


def build_stco(offset, num_samples):
    """stco: chunk offset. Single chunk at `offset`, one chunk for all samples.
    Uses 32-bit offsets; only valid when offset < 4 GB."""
    body = _u32(0) + _u32(1) + _u32(offset)
    return box("stco", body)


def build_co64(offset, num_samples):
    """co64: 64-bit chunk offset. Use when the chunk offset exceeds 4 GB
    (i.e. the file is large enough that a u32 offset cannot address the payload)."""
    body = _u32(0) + _u32(1) + _u64(offset)
    return box("co64", body)


def build_stsz(num_samples, sample_size):
    """stsz: sample size. uniform size for all samples."""
    body = _u32(0) + _u32(sample_size) + _u32(num_samples)
    return box("stsz", body)


def build_stsd():
    """
    stsd: sample description. One entry: a metadata sample description.
    We use a minimal 4CC 'mogy' entry. The BRAW parser does not inspect the
    stsd entry type for metadata tracks (it reads samples directly), so a
    minimal valid entry is sufficient.
    Layout: u32 version/flags, u32 entry_count, then per entry:
      u32 size, 4CC, u16 data_reference_index, u16 reserved, u16 pre_defined,
      u16 reserved, u32 reserved, u16 reserved, u16 reserved, u16 sample_size
    """
    entry_body = (
        _u16(0)  # data_reference_index
        + _u16(0)  # reserved
        + _u16(0)  # pre_defined
        + _u16(0)  # reserved
        + _u32(0)  # reserved
        + _u16(0)  # reserved
        + _u16(0)  # reserved
        + _u16(0)  # sample_size
    )
    entry = box("mogy", entry_body)
    body = _u32(0) + _u32(1) + entry
    return box("stsd", body)


def build_smhd():
    """smhd: sound media header (required child of minf for some parsers; we
    include it for structural completeness). balance=0, reserved=0."""
    body = _u32(0) + _u16(0) + _u16(0)
    return box("smhd", body)


def build_hdlr():
    """hdlr: handler. pre_defined=0, handler_type='meta', reserved x3, name=''."""
    body = (
        _u32(0)
        + _u32(0)
        + b"meta"
        + _u32(0) + _u32(0) + _u32(0)
        + b""  # empty name
    )
    return box("hdlr", body)


def build_mdhd(timescale, duration):
    """mdhd: media header. version 0: u32 ver/flags, u32 creation, u32
    modification, u32 timescale, u32 duration."""
    body = (
        _u32(0)
        + _u32(0)  # creation_time
        + _u32(0)  # modification_time
        + _u32(timescale)
        + _u32(duration)
    )
    return box("mdhd", body)


def build_tkhd(track_id, timescale, duration):
    """tkhd: track header, version 0.

    IMPORTANT: the layout must match what mp4parse's read_tkhd() (version 0)
    consumes, which is 84 bytes of content:
        ver/flags(4) creation(4) modification(4) track_id(4) reserved(4)
        duration(4) [16 bytes: reserved(4) reserved(2) volume(2) reserved(4)
        + 4 padding] matrix(36) width(4) height(4)

    A spec-minimal tkhd is only 76 bytes, but mp4parse's reader expects the
    84-byte form (the one ffmpeg and Blackmagic emit). Writing the shorter form
    misaligns every following box and makes read_mp4 fail with UnexpectedEOF,
    so we emit the 84-byte form.
    """
    flags = 0x000003  # enabled | movie_switch
    # 3x3 transformation matrix (identity), 9 x i32 = 36 bytes.
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
        # 16 bytes: reserved(4) + reserved(2) + volume(2) + reserved(4) + pad(4)
        + _u32(0)
        + _u16(0)
        + _u16(0)
        + _u32(0)
        + _u32(0)
        + transform
        + _u32(0)  # width (u32, as read by mp4parse)
        + _u32(0)  # height (u32, as read by mp4parse)
    )
    return box("tkhd", body)


def build_track(track_id, timescale, duration, stbl_boxes):
    """Assemble trak > mdia > minf > stbl with the given stbl child boxes."""
    stbl = box("stbl", b"".join(stbl_boxes))
    minf = box("minf", build_smhd() + stbl)
    mdia = box("mdia", build_mdhd(timescale, duration) + build_hdlr() + minf)
    trak = box("trak", build_tkhd(track_id, timescale, duration) + mdia)
    return trak


# ---------------------------------------------------------------------------
# Splicing into the container
# ---------------------------------------------------------------------------

def inject(braw_path, gcsv, args):
    buf = bytearray(Path(braw_path).read_bytes())

    # --- orientation / permutation ---
    perm = (0, 1, 2)
    if not args.no_remap:
        _, perm, orient_warnings = remap_orientation(gcsv.orientation, args.orientation)
        for w in orient_warnings:
            print("  [orientation] " + w, file=sys.stderr)

    # --- build sample payload ---
    # Each GCSV sample yields up to two 20-byte boxes (mogy, moac) sharing the
    # same timestamp. stts is per emitted box, so we expand the timestamp list.
    emitted_t = []
    for s in gcsv.samples:
        emitted_t.append(s["t_ms"])
        if s["accl"] is not None:
            emitted_t.append(s["t_ms"])

    payload, num_samples, _sample_sizes = build_sample_payload(
        gcsv.samples, perm, not args.no_accel_negate
    )

    # --- locate moov and mdat ---
    moov = find_box(buf, "moov")
    if moov is None:
        raise RuntimeError("No 'moov' box found in BRAW file")
    moov_start, moov_size, moov_poff = moov

    mdat = find_box(buf, "mdat")
    if mdat is None:
        raise RuntimeError("No 'mdat' box found in BRAW file")
    mdat_start, mdat_size, mdat_poff = mdat

    # Pick a track id that does not collide with existing tracks.
    track_id = _pick_track_id(buf, moov_poff, moov_size)

    timescale = args.timescale
    duration = max(emitted_t) if emitted_t else 0

    # ------------------------------------------------------------------
    # Splice strategy
    # ------------------------------------------------------------------
    # We preserve the original top-level box order and *replace* the existing
    # moov in place with a grown one (old moov payload + new trak). The new
    # IMU sample payload is appended to the end of the file, and the mdat box
    # is grown to span up to and including that payload.
    #
    # Because the original file has exactly one moov (at the end, after mdat),
    # the new file layout is:
    #   [boxes before mdat] [mdat' (grown)] [moov' (grown)]
    #
    # where mdat' = old mdat payload + appended payload, and moov' = old moov
    # payload + new trak. The stco chunk offset points to the start of the
    # appended payload inside mdat'.

    old_mdat_payload = bytes(buf[mdat_poff:mdat_start + mdat_size])
    old_moov_payload = bytes(buf[moov_poff:moov_start + moov_size])
    before_mdat = bytes(buf[:mdat_start])
    after_moov = bytes(buf[moov_start + moov_size:])

    new_mdat_payload = old_mdat_payload + payload

    # The mdat box header is 8 bytes (u32 size + 4CC) normally, but 16 bytes
    # (u32 size=1 + 4CC + u64 largesize) when the box is >= 4 GB. We need the
    # header size to (a) compute the payload's absolute offset and (b) emit the
    # correct box header, so decide it up front.
    new_mdat_total = 8 + len(new_mdat_payload)
    mdat_uses_largesize = new_mdat_total > 0xFFFFFFFF
    mdat_header_size = 16 if mdat_uses_largesize else 8

    # The appended payload begins at:
    #   mdat' start + mdat header size + len(old_mdat_payload)
    # mdat' start = len(before_mdat)  (mdat' replaces mdat at the same position)
    mdat_final_start = len(before_mdat)
    payload_abs_offset = mdat_final_start + mdat_header_size + len(old_mdat_payload)

    def make_trak(chunk_offset):
        # Use 64-bit chunk offsets (co64) when the offset exceeds the u32 range,
        # otherwise the standard 32-bit stco.
        if chunk_offset > 0xFFFFFFFF:
            chunk_box = build_co64(chunk_offset, num_samples)
        else:
            chunk_box = build_stco(chunk_offset, num_samples)
        stbl = [
            build_stsd(),
            build_stts(emitted_t),
            build_stss(num_samples),
            build_stsc(num_samples),
            chunk_box,
            build_stsz(num_samples, SAMPLE_BOX_SIZE),
        ]
        return build_track(track_id, timescale, duration, stbl)

    new_moov_payload = old_moov_payload + make_trak(payload_abs_offset)

    # Emit mdat with the header size we already decided (8 or 16 bytes). Emit
    # moov with a 64-bit 'largesize' header only if it itself exceeds the u32
    # range (rare; moov is small unless the original had a huge number of tracks).
    if mdat_uses_largesize:
        new_mdat_box = box64("mdat", new_mdat_payload)
    else:
        new_mdat_box = box("mdat", new_mdat_payload)

    if 8 + len(new_moov_payload) > 0xFFFFFFFF:
        new_moov_box = box64("moov", new_moov_payload)
    else:
        new_moov_box = box("moov", new_moov_payload)

    out = bytearray()
    out += before_mdat
    out += new_mdat_box
    out += new_moov_box
    out += after_moov

    out_path = Path(args.output) if args.output else _default_output(braw_path)
    out_path.write_bytes(bytes(out))

    print("Wrote %s (%d bytes, %d IMU samples, track id 0x%02X, timescale %d)"
          % (out_path, len(out), len(gcsv.samples), track_id, timescale))
    print("  mogy/moac sample boxes: %d (each 20 bytes)" % num_samples)
    actual_mdat_size = (16 if mdat_uses_largesize else 8) + len(new_mdat_payload)
    print("  payload at file offset %d (mdat now %d bytes, %s header)"
          % (payload_abs_offset, actual_mdat_size, "64-bit" if mdat_uses_largesize else "32-bit"))

    if not args.no_verify:
        ok = verify(out_path, gcsv, perm, not args.no_accel_negate)
        if not ok:
            print("  [verify] FAILED -- see messages above", file=sys.stderr)
            return 1
    return 0


def _default_output(braw_path):
    p = Path(braw_path)
    return p.with_name(p.stem + "_injected" + p.suffix)


def _pick_track_id(buf, moov_poff, moov_size):
    """Pick a track id that does not collide with existing tracks in moov."""
    # Walk the moov payload for 'trak' boxes and collect their track ids.
    used = set()
    pos = moov_poff
    end = moov_poff + (moov_size - 8)
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        name = buf[pos + 4:pos + 8]
        if size < 8 or pos + size > end:
            break
        if name == b"trak":
            # tkhd is the first child of trak; track_id is at offset
            # 8 (trak header) + 4 (tkhd size) + 4 (tkhd name) + 4 (ver/flags)
            # + 4 (creation) + 4 (modification) = +24 from trak start.
            tkhd_start = pos + 8
            # find tkhd
            if buf[tkhd_start + 4:tkhd_start + 8] == b"tkhd":
                ver_flags = struct.unpack(">I", buf[tkhd_start + 8:tkhd_start + 12])[0]
                version = ver_flags >> 24
                # For version 0: after ver/flags(4) creation(4) modification(4)
                # track_id(4)
                off = tkhd_start + 8 + 4 + 4 + 4
                if version == 1:
                    off = tkhd_start + 8 + 4 + 8 + 8  # ver/flags, creation(8), mod(8)
                tid = struct.unpack(">I", buf[off:off + 4])[0]
                used.add(tid)
        pos += size

    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


# ---------------------------------------------------------------------------
# Verification: re-parse the output the way the BRAW parser does
# ---------------------------------------------------------------------------

def verify(out_path, gcsv, perm, negate_accel):
    """
    Re-open the output file, locate the metadata track we just wrote, decode its
    mogy/moac samples, and compare against the source GCSV (after the same
    permutation + accel negation the BRAW parser will apply on read).
    """
    print("  [verify] re-parsing %s" % out_path)
    buf = Path(out_path).read_bytes()

    # Find the moov, then the trak whose hdlr is 'meta' and whose stsz count
    # matches our sample count.
    moov = find_box(buf, "moov")
    if moov is None:
        print("    FAIL: no moov in output")
        return False
    moov_start, moov_size, moov_poff = moov

    # Walk moov children to find trak boxes.
    traks = []
    pos = moov_poff
    end = moov_poff + (moov_size - 8)
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        name = buf[pos + 4:pos + 8]
        if size < 8 or pos + size > end:
            break
        if name == b"trak":
            traks.append((pos, size))
        pos += size

    if not traks:
        print("    FAIL: no trak boxes found in moov")
        return False

    # Find the metadata track (hdlr type 'meta').
    meta_trak = None
    for tpos, tsize in traks:
        # walk trak children: tkhd, mdia
        cpos = tpos + 8
        cend = tpos + tsize
        hdlr_type = None
        while cpos + 8 <= cend:
            csize = struct.unpack(">I", buf[cpos:cpos + 4])[0]
            cname = buf[cpos + 4:cpos + 8]
            if csize < 8 or cpos + csize > cend:
                break
            if cname == b"mdia":
                # walk mdia children for hdlr
                mpos = cpos + 8
                mend = cpos + csize
                while mpos + 8 <= mend:
                    msize = struct.unpack(">I", buf[mpos:mpos + 4])[0]
                    mname = buf[mpos + 4:mpos + 8]
                    if msize < 8 or mpos + msize > mend:
                        break
                    if mname == b"hdlr":
                        # hdlr: ver/flags(4) pre_defined(4) handler_type(4)
                        ht = buf[mpos + 8 + 4 + 4:mpos + 8 + 4 + 4 + 4]
                        hdlr_type = ht
                    mpos += msize
            cpos += csize
        if hdlr_type == b"meta":
            meta_trak = (tpos, tsize)
            break

    if meta_trak is None:
        print("    FAIL: no metadata (hdlr 'meta') track found")
        return False

    tpos, tsize = meta_trak
    # Now extract stts, stsz, and the chunk-offset box (stco or co64) from this trak.
    stts = None
    stsz = None
    chunk_box = None   # (offset, len, is_co64)
    cpos = tpos + 8
    cend = tpos + tsize
    while cpos + 8 <= cend:
        csize = struct.unpack(">I", buf[cpos:cpos + 4])[0]
        cname = buf[cpos + 4:cpos + 8]
        if csize < 8 or cpos + csize > cend:
            break
        if cname == b"mdia":
            mpos = cpos + 8
            mend = cpos + csize
            while mpos + 8 <= mend:
                msize = struct.unpack(">I", buf[mpos:mpos + 4])[0]
                mname = buf[mpos + 4:mpos + 8]
                if msize < 8 or mpos + msize > mend:
                    break
                if mname == b"minf":
                    ipos = mpos + 8
                    iend = mpos + msize
                    while ipos + 8 <= iend:
                        isize = struct.unpack(">I", buf[ipos:ipos + 4])[0]
                        iname = buf[ipos + 4:ipos + 8]
                        if isize < 8 or ipos + isize > iend:
                            break
                        if iname == b"stbl":
                            sp = ipos + 8
                            send = ipos + isize
                            while sp + 8 <= send:
                                ssize = struct.unpack(">I", buf[sp:sp + 4])[0]
                                sname = buf[sp + 4:sp + 8]
                                if ssize < 8 or sp + ssize > send:
                                    break
                                if sname == b"stts":
                                    stts = (sp + 8, ssize - 8)
                                elif sname == b"stsz":
                                    stsz = (sp + 8, ssize - 8)
                                elif sname in (b"stco", b"co64"):
                                    chunk_box = (sp + 8, ssize - 8, sname == b"co64")
                                sp += ssize
                        ipos += isize
                mpos += msize
        cpos += csize

    if not (stts and stsz and chunk_box):
        print("    FAIL: could not find stts/stsz/chunk-offset box in metadata track")
        return False

    # Parse stsz: ver/flags(4) sample_size(4) sample_count(4)
    sz_off, sz_len = stsz
    sample_size = struct.unpack(">I", buf[sz_off + 4:sz_off + 8])[0]
    sample_count = struct.unpack(">I", buf[sz_off + 8:sz_off + 12])[0]

    # Parse the chunk-offset box (stco: u32 offset, co64: u64 offset).
    co_off, co_len, is_co64 = chunk_box
    chunk_count = struct.unpack(">I", buf[co_off + 4:co_off + 8])[0]
    if chunk_count != 1:
        print("    FAIL: expected 1 chunk, got %d" % chunk_count)
        return False
    if is_co64:
        data_offset = struct.unpack(">Q", buf[co_off + 8:co_off + 16])[0]
    else:
        data_offset = struct.unpack(">I", buf[co_off + 8:co_off + 12])[0]

    # Parse stts to get per-sample timestamps.
    ts_off, ts_len = stts
    entry_count = struct.unpack(">I", buf[ts_off + 4:ts_off + 8])[0]
    timestamps = []
    epos = ts_off + 8
    for _ in range(entry_count):
        count = struct.unpack(">I", buf[epos:epos + 4])[0]
        delta = struct.unpack(">I", buf[epos + 4:epos + 8])[0]
        for _ in range(count):
            timestamps.append(delta)
        epos += 8
    # Convert deltas to absolute times (ms).
    abs_t = []
    acc = 0
    for d in timestamps:
        acc += d
        abs_t.append(acc)

    # Now read the sample data from data_offset.
    mdat = find_box(buf, "mdat")
    if mdat is None:
        print("    FAIL: no mdat in output")
        return False
    mdat_start, mdat_size, mdat_poff = mdat
    # data_offset is absolute in the file; mdat payload starts at mdat_poff.
    # The sample data region is [data_offset, data_offset + sample_count*sample_size).
    region_start = data_offset
    region_end = data_offset + sample_count * sample_size
    if region_start < mdat_poff or region_end > mdat_start + mdat_size:
        print("    FAIL: sample data region outside mdat bounds")
        return False

    # Decode boxes.
    decoded_gyro = []
    decoded_accl = []
    p = region_start
    for i in range(sample_count):
        b = buf[p:p + sample_size]
        if len(b) < sample_size:
            break
        bsize = struct.unpack(">I", b[0:4])[0]
        bname = b[4:8]
        x, y, z = struct.unpack("<3f", b[8:20])
        if bname == b"mogy":
            decoded_gyro.append((x, y, z))
        elif bname == b"moac":
            decoded_accl.append((x, y, z))
        p += sample_size

    # Compare against source GCSV.
    # The BRAW parser negates accel on read, so the on-disk accel is
    # -gcsv_accl. We stored -gcsv_accl (negate_accel=True), so decoded_accl
    # should equal -gcsv_accl (permuted). Compare accordingly.
    src_gyro = [apply_permutation(s["gyro"], perm) for s in gcsv.samples]
    src_accl = [apply_permutation(s["accl"], perm) for s in gcsv.samples if s["accl"] is not None]

    ok = True
    if len(decoded_gyro) != len(src_gyro):
        print("    FAIL: gyro sample count mismatch: decoded %d vs source %d"
              % (len(decoded_gyro), len(src_gyro)))
        ok = False
    else:
        max_err = 0.0
        for d, s in zip(decoded_gyro, src_gyro):
            for a, b in zip(d, s):
                max_err = max(max_err, abs(a - b))
        if max_err > 1e-4:
            print("    FAIL: gyro values differ by up to %g (f32 rounding?)" % max_err)
            ok = False
        else:
            print("    OK: %d gyro samples match (max err %.2e)" % (len(decoded_gyro), max_err))

    if src_accl:
        if len(decoded_accl) != len(src_accl):
            print("    FAIL: accel sample count mismatch: decoded %d vs source %d"
                  % (len(decoded_accl), len(src_accl)))
            ok = False
        else:
            # decoded_accl is the on-disk (negated) value; source is positive.
            max_err = 0.0
            for d, s in zip(decoded_accl, src_accl):
                for a, b in zip(d, s):
                    max_err = max(max_err, abs(a - (-b)))
            if max_err > 1e-4:
                print("    FAIL: accel values differ by up to %g" % max_err)
                ok = False
            else:
                print("    OK: %d accel samples match (negated, max err %.2e)"
                      % (len(decoded_accl), max_err))

    # Timestamp sanity: first and last.
    if abs_t:
        print("    timestamps: first=%d ms, last=%d ms, count=%d"
              % (abs_t[0], abs_t[-1], len(abs_t)))

    return ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Inject gyro/accel from a .gcsv into a .braw file as a native "
                    "MP4 metadata track (no sidecar).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("braw", help="input .braw file")
    ap.add_argument("gcsv", help="input .gcsv file (matching the video)")
    ap.add_argument("-o", "--output", default=None,
                    help="output path (default: <input>_injected.braw)")
    ap.add_argument("--orientation", default=None,
                    help="override the BRAW IMU orientation string (default: auto from GCSV)")
    ap.add_argument("--no-accel-negate", action="store_true",
                    help="do NOT negate accelerometer axes on write (default: negates, matching the BRAW parser)")
    ap.add_argument("--no-remap", action="store_true",
                    help="do not remap GCSV axes into BRAW 'yxz' order (use when GCSV is already in BRAW order)")
    ap.add_argument("--timescale", type=int, default=1000,
                    help="metadata track timescale (default: 1000 = ms)")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the built-in re-parse verification")
    args = ap.parse_args(argv)

    if not Path(args.braw).exists():
        print("error: BRAW file not found: %s" % args.braw, file=sys.stderr)
        return 2
    if not Path(args.gcsv).exists():
        print("error: GCSV file not found: %s" % args.gcsv, file=sys.stderr)
        return 2

    print("Parsing GCSV: %s" % args.gcsv)
    try:
        gcsv = Gcsv.parse(args.gcsv)
    except Exception as e:
        print("error: failed to parse GCSV: %s" % e, file=sys.stderr)
        return 2
    print("  %d samples, orientation=%r, gscale=%g, ascale=%g, tscale=%g"
          % (len(gcsv.samples), gcsv.orientation, gcsv.gscale, gcsv.ascale, gcsv.tscale))
    if gcsv.frame_readout_time is not None:
        print("  frame_readout_time=%g ms (note: BRAW parser computes its own from meta; "
              "this value is not written into the BRAW)" % gcsv.frame_readout_time)

    print("Injecting into BRAW: %s" % args.braw)
    rc = inject(args.braw, gcsv, args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
