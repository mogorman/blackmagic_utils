#!/usr/bin/env python3
"""
extract_gyro_from_braw.py

Reverse of inject_gyro_into_braw.py: read the gyro/accelerometer data that is
embedded in a Blackmagic RAW (.braw) file's MP4 metadata track and write it out
as a standard Gyroflow .gcsv sidecar file.

The .braw stores the IMU in an MP4 metadata track whose samples are 20-byte
boxes:

    [u32 size][4CC "mogy"|"moac"][f32 x][f32 y][f32 z]   (little-endian)

The BRAW parser (telemetry-parser/src/blackmagic/mod.rs) reads these as:
  * gyro  -> rad/s
  * accel -> m/s^2, with all three axes NEGATED on read (mod.rs:148-153)
  * orientation "yxz" for unknown models (mod.rs:169-172)

This script reverses that:
  * reads the f32 values (little-endian),
  * negates the accel back to recover the stored (positive) values,
  * remaps the axes from the BRAW "yxz" convention back to the GCSV
    orientation you choose (default "YxZ", the common M5Stack/ESP logger
    convention; use --orientation to change it),
  * converts gyro rad/s -> the GCSV raw counts and accel m/s^2 -> g, using
    scales you can set (or let it auto-pick), and
  * writes a .gcsv with a proper header.

Usage:
    python3 extract_gyro_from_braw.py INPUT.braw [options]

Options:
    -o, --output PATH      output .gcsv path (default: <input>.gcsv)
    --orientation STR      GCSV orientation string to emit (default: YxZ).
                           This is the sensor-native orientation the GCSV
                           should claim. The script remaps the BRAW 'yxz'
                           axes into this order.
    --float                write the sample values as floats with tscale/gscale/
                           ascale = 1 (lossless, simpler). Default: write
                           integer counts with auto-picked scales (conventional).
    --no-accel             omit the accelerometer columns (gyro only).
    --id STR               GCSV 'id' field (default: auto from BRAW camera_type)
    --videofilename STR    GCSV 'videofilename' field (default: input filename)

The output .gcsv can be loaded directly in Gyroflow as a sidecar, or fed back
through inject_gyro_into_braw.py.
"""

import argparse
import struct
import sys
from pathlib import Path

GRAVITY = 9.80665  # m/s^2 per g

# The BRAW parser's default IMU orientation for unknown models (blackmagic/mod.rs).
BRAW_ORIENTATION = "yxz"


# ---------------------------------------------------------------------------
# BRAW / MP4 box walking (mirrors inject_gyro_into_braw.py)
# ---------------------------------------------------------------------------

def read_top_level_boxes(buf):
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


def find_box(buf, name):
    for n, start, size, poff in read_top_level_boxes(buf):
        if n == name.encode("ascii"):
            return start, size, poff
    return None


def _walk(buf, start, end, want):
    """Find a child box named `want` directly inside [start,end). Return (pos,size) or None."""
    p = start
    while p + 8 <= end:
        size = struct.unpack(">I", buf[p:p + 4])[0]
        name = buf[p + 4:p + 8]
        if size < 8 or p + size > end:
            break
        if name == want:
            return p, size
        p += size
    return None


def find_metadata_track(buf):
    """
    Locate the metadata track (hdlr type 'meta') in the file's moov and return
    (track_pos, track_size). Returns None if no metadata track is present.
    """
    moov = find_box(buf, "moov")
    if moov is None:
        return None
    moov_start, moov_size, moov_poff = moov

    p = moov_poff
    end = moov_poff + (moov_size - 8)
    while p + 8 <= end:
        size = struct.unpack(">I", buf[p:p + 4])[0]
        name = buf[p + 4:p + 8]
        if size < 8 or p + size > end:
            break
        if name == b"trak":
            if _track_is_metadata(buf, p, p + size):
                return p, size
        p += size
    return None


def _track_is_metadata(buf, trak_pos, trak_end):
    """Check whether a trak's mdia/hdlr has handler_type 'meta'."""
    mdia = _walk(buf, trak_pos + 8, trak_end, b"mdia")
    if mdia is None:
        return False
    mpos, msize = mdia
    hdlr = _walk(buf, mpos + 8, mpos + msize, b"hdlr")
    if hdlr is None:
        return False
    hpos, hsize = hdlr
    # hdlr layout: [size][hdlr][ver/flags(4)][pre_defined(4)][handler_type(4)]...
    # handler_type is at hpos+8 (after size+4CC) + 4 (ver/flags) + 4 (pre_defined)
    ht = buf[hpos + 8 + 4 + 4:hpos + 8 + 4 + 4 + 4]
    return ht == b"meta"


def _get_stbl_boxes(buf, trak_pos, trak_size):
    """Return a dict of the stbl child boxes (stts, stsz, stco/co64, stss) for a trak."""
    mdia = _walk(buf, trak_pos + 8, trak_pos + trak_size, b"mdia")
    if mdia is None:
        return None
    mpos, msize = mdia
    minf = _walk(buf, mpos + 8, mpos + msize, b"minf")
    if minf is None:
        return None
    ipos, isize = minf
    stbl = _walk(buf, ipos + 8, ipos + isize, b"stbl")
    if stbl is None:
        return None
    sp, ssize = stbl
    out = {}
    p = sp + 8
    end = sp + ssize
    while p + 8 <= end:
        size = struct.unpack(">I", buf[p:p + 4])[0]
        name = buf[p + 4:p + 8]
        if size < 8 or p + size > end:
            break
        if name in (b"stts", b"stsz", b"stco", b"co64", b"stss"):
            out[name] = (p + 8, size - 8)  # payload offset, payload length
        p += size
    return out


def parse_stts(buf, off, ln):
    """Return list of absolute sample timestamps (in timescale units) from an stts box."""
    entry_count = struct.unpack(">I", buf[off + 4:off + 8])[0]
    ts = []
    p = off + 8
    acc = 0
    for _ in range(entry_count):
        count = struct.unpack(">I", buf[p:p + 4])[0]
        delta = struct.unpack(">I", buf[p + 4:p + 8])[0]
        for _ in range(count):
            acc += delta
            ts.append(acc)
        p += 8
    return ts


def parse_stsz(buf, off, ln):
    """Return (sample_size, sample_sizes_list, sample_count) from an stsz box."""
    sample_size = struct.unpack(">I", buf[off + 4:off + 8])[0]
    sample_count = struct.unpack(">I", buf[off + 8:off + 12])[0]
    if sample_size == 0:
        # per-sample sizes follow
        sizes = []
        p = off + 12
        for _ in range(sample_count):
            sizes.append(struct.unpack(">I", buf[p:p + 4])[0])
            p += 4
        return 0, sizes, sample_count
    return sample_size, None, sample_count


def parse_chunk_offsets(buf, off, ln, is_co64):
    """Return list of chunk data offsets from stco (u32) or co64 (u64)."""
    chunk_count = struct.unpack(">I", buf[off + 4:off + 8])[0]
    offsets = []
    p = off + 8
    fmt = ">Q" if is_co64 else ">I"
    step = 8 if is_co64 else 4
    for _ in range(chunk_count):
        offsets.append(struct.unpack(fmt, buf[p:p + step])[0])
        p += step
    return offsets


def parse_stsc(buf, off, ln):
    """Return list of (first_chunk, samples_per_chunk, sample_desc_index) from stsc."""
    entry_count = struct.unpack(">I", buf[off + 4:off + 8])[0]
    entries = []
    p = off + 8
    for _ in range(entry_count):
        first_chunk = struct.unpack(">I", buf[p:p + 4])[0]
        samples_per_chunk = struct.unpack(">I", buf[p + 4:p + 8])[0]
        desc_index = struct.unpack(">I", buf[p + 8:p + 12])[0]
        entries.append((first_chunk, samples_per_chunk, desc_index))
        p += 12
    return entries


def extract_imu_samples(buf):
    """
    Extract the IMU samples from the BRAW's metadata track.

    Returns (samples, timescale) where samples is a list of dicts:
        {"t_ms": int, "gyro": (x,y,z) or None, "accl": (x,y,z) or None}
    gyro in rad/s, accl in m/s^2 (already with the BRAW parser's negation
    REVERSED, i.e. the raw stored values).
    """
    track = find_metadata_track(buf)
    if track is None:
        raise RuntimeError("No metadata (hdlr 'meta') track found in this BRAW file. "
                           "It may not contain embedded gyro data.")
    trak_pos, trak_size = track
    boxes = _get_stbl_boxes(buf, trak_pos, trak_size)
    if boxes is None:
        raise RuntimeError("Metadata track found but no stbl box could be parsed.")

    if b"stsz" not in boxes or b"stts" not in boxes:
        raise RuntimeError("Metadata track is missing stsz/stts boxes.")

    # stco or co64
    if b"co64" in boxes:
        co_off, co_len = boxes[b"co64"]
        offsets = parse_chunk_offsets(buf, co_off, co_len, is_co64=True)
    elif b"stco" in boxes:
        co_off, co_len = boxes[b"stco"]
        offsets = parse_chunk_offsets(buf, co_off, co_len, is_co64=False)
    else:
        raise RuntimeError("Metadata track is missing stco/co64 (chunk offsets).")

    stsz_off, stsz_len = boxes[b"stsz"]
    sample_size, per_sample_sizes, sample_count = parse_stsz(buf, stsz_off, stsz_len)

    stts_off, stts_len = boxes[b"stts"]
    # timescale comes from mdhd; read it to convert timestamps to seconds.
    timescale = _read_mdhd_timescale(buf, trak_pos, trak_size)

    # Build the list of (absolute_offset) for each sample by walking stsc + stco.
    stsc = parse_stsc(buf, boxes[b"stsc"][0], boxes[b"stsc"][1]) if b"stsc" in boxes else [(1, sample_count, 1)]
    sample_offsets = _sample_offsets_from_stsc(stsc, offsets, sample_size, per_sample_sizes, sample_count)

    # Timestamps: stts gives per-RAW-sample deltas in timescale units.
    # The track stores 2 raw samples per GCSV sample (mogy then moac), so
    # raw sample j has timestamp ts_units[j]. GCSV sample i = raw samples
    # (2*i, 2*i+1); we use the gyro's (2*i) timestamp for the GCSV row.
    ts_units = parse_stts(buf, stts_off, stts_len)
    if len(ts_units) != sample_count:
        sys.stderr.write("warning: stts sample count (%d) != stsz count (%d); using stts as-is\n"
                         % (len(ts_units), sample_count))

    # Decode each raw sample box at its offset.
    gyro = []
    accl = []
    for i in range(sample_count):
        off = sample_offsets[i]
        b = buf[off:off + 20]
        if len(b) < 20:
            break
        bname = b[4:8]
        x, y, z = struct.unpack("<3f", b[8:20])
        if bname == b"mogy":
            gyro.append((x, y, z))
        elif bname == b"moac":
            # The BRAW parser negates accel on read; the stored value is the
            # negative of the physical value. We recover the physical value by
            # negating here so the GCSV gets the true sensor reading.
            accl.append((-x, -y, -z))

    # Pair raw samples into GCSV samples: gyro[k] + accl[k] = GCSV sample k.
    # The track stores them interleaved as mogy, moac, mogy, moac, ... so
    # gyro[k] and accl[k] correspond to the same GCSV sample.
    n = max(len(gyro), len(accl))
    samples = []
    for i in range(n):
        # Use the gyro raw sample's timestamp (raw index 2*i) if available,
        # else the accel's (2*i+1), else fall back to i.
        raw_idx = 2 * i
        if raw_idx < len(ts_units):
            t_ms = int(round(ts_units[raw_idx] / timescale * 1000.0))
        elif raw_idx + 1 < len(ts_units):
            t_ms = int(round(ts_units[raw_idx + 1] / timescale * 1000.0))
        else:
            t_ms = i
        samples.append({
            "t_ms": t_ms,
            "gyro": gyro[i] if i < len(gyro) else None,
            "accl": accl[i] if i < len(accl) else None,
        })

    return samples, timescale


def _sample_offsets_from_stsc(stsc, offsets, sample_size, per_sample_sizes, sample_count):
    """
    Compute the absolute file offset of each sample, given the stsc (sample-to-chunk)
    table, the chunk offsets, and the sample sizes.

    stsc entries: (first_chunk, samples_per_chunk, desc_index). A chunk's sample
    count is determined by the stsc entry whose first_chunk <= chunk_id < next first_chunk.
    """
    # Build a per-chunk sample count list.
    # Sort stsc entries by first_chunk.
    entries = sorted(stsc, key=lambda e: e[0])
    chunk_counts = []
    for idx, (first_chunk, spc, _di) in enumerate(entries):
        start = first_chunk - 1  # 0-based chunk id
        end = (entries[idx + 1][0] - 1) if idx + 1 < len(entries) else len(offsets)
        for c in range(start, end):
            chunk_counts.append(spc)

    # Now walk chunks, emitting sample offsets.
    result = []
    si = 0  # global sample index
    for c, chunk_off in enumerate(offsets):
        count = chunk_counts[c] if c < len(chunk_counts) else 0
        pos = chunk_off
        for _ in range(count):
            if si >= sample_count:
                return result
            if per_sample_sizes is not None:
                sz = per_sample_sizes[si]
            else:
                sz = sample_size
            result.append(pos)
            pos += sz
            si += 1
        if si >= sample_count:
            break
    return result


def _read_mdhd_timescale(buf, trak_pos, trak_size):
    mdia = _walk(buf, trak_pos + 8, trak_pos + trak_size, b"mdia")
    if mdia is None:
        return 1000
    mpos, msize = mdia
    mdhd = _walk(buf, mpos + 8, mpos + msize, b"mdhd")
    if mdhd is None:
        return 1000
    hpos, hsize = mdhd
    # mdhd v0: [size][mdhd][ver/flags(4)][creation(4)][modification(4)][timescale(4)][duration(4)]
    ver = buf[hpos + 8] >> 4  # version is top nibble of the version/flags byte
    if ver == 1:
        # v1: creation(8) modification(8) timescale(4) duration(8)
        ts = struct.unpack(">I", buf[hpos + 8 + 4 + 8 + 8:hpos + 8 + 4 + 8 + 8 + 4])[0]
    else:
        ts = struct.unpack(">I", buf[hpos + 8 + 4 + 4 + 4:hpos + 8 + 4 + 4 + 4 + 4])[0]
    return ts if ts > 0 else 1000


def read_meta_string(buf, key):
    """
    Best-effort: find a metadata key's string value in the file's meta box.

    The BRAW meta box contains a 'keys' box (mapping index -> 'mdta:<name>')
    and an 'ilst' box (mapping index -> value). We look up the index whose key
    name ends with the requested `key`, then read the corresponding ilst value.
    Returns the decoded string, or None if not found.
    """
    moov = find_box(buf, "moov")
    if moov is None:
        return None
    moov_start, moov_size, moov_poff = moov
    meta = _walk(buf, moov_poff, moov_poff + (moov_size - 8), b"meta")
    if meta is None:
        return None
    mpos, msize = meta

    # Parse the 'keys' box: [size][keys][ver/flags(4)][count(4)] then
    # count entries of [entry_size(4)][key_name_bytes...].
    keys = _walk(buf, mpos + 8, mpos + msize, b"keys")
    if keys is None:
        return None
    kpos, ksize = keys
    count = struct.unpack(">I", buf[kpos + 12:kpos + 16])[0]
    keymap = {}  # index -> key name
    p = kpos + 16
    for idx in range(count):
        if p + 4 > kpos + ksize:
            break
        entry_size = struct.unpack(">I", buf[p:p + 4])[0]
        if entry_size < 4 or p + entry_size > kpos + ksize:
            break
        name_bytes = buf[p + 4:p + entry_size]
        name = name_bytes.decode("utf-8", "replace")
        # Strip the 'mdta:' prefix if present.
        if name.startswith("mdta:"):
            name = name[len("mdta:"):]
        keymap[idx + 1] = name  # ilst indices are 1-based
        p += entry_size

    # Find the index whose key matches.
    target_idx = None
    for idx, name in keymap.items():
        if name == key or name.endswith(":" + key) or name == "mdta:" + key:
            target_idx = idx
            break
    if target_idx is None:
        return None

    # Parse the 'ilst' box: [size][ilst] then entries of
    # [entry_size(4)][index(4)][value_size(4)][value...].
    ilst = _walk(buf, mpos + 8, mpos + msize, b"ilst")
    if ilst is None:
        return None
    ipos, isize = ilst
    p = ipos + 8
    end = ipos + isize
    while p + 12 <= end:
        entry_size = struct.unpack(">I", buf[p:p + 4])[0]
        idx = struct.unpack(">I", buf[p + 4:p + 8])[0]
        value_size = struct.unpack(">I", buf[p + 8:p + 12])[0]
        if entry_size < 12 or p + entry_size > end:
            break
        if idx == target_idx:
            val = buf[p + 12:p + 12 + value_size]
            # The value may be a 'data' sub-box; strip a leading 'data' box if present.
            if val[:4] == b"data":
                # [data(4)][data_size(4)][index(4)][type(4)][payload...]
                if len(val) >= 16:
                    payload = val[16:]
                    # payload is the actual value; for strings it's UTF-8.
                    s = payload.split(b"\x00")[0]
                    s = bytes(b for b in s if 32 <= b < 127)
                    return s.decode("ascii", "replace") if s else None
            s = val.split(b"\x00")[0]
            s = bytes(b for b in s if 32 <= b < 127)
            return s.decode("ascii", "replace") if s else None
        p += entry_size
    return None


# ---------------------------------------------------------------------------
# Orientation remapping (reverse of the injector)
# ---------------------------------------------------------------------------

def _normalize(orient):
    return orient.strip() if orient else None


def remap_to_gcsv_orientation(braw_values, braw_orient, gcsv_orient):
    """
    Remap a 3-tuple of values from the BRAW axis order (braw_orient, e.g. 'yxz')
    to the GCSV axis order (gcsv_orient, e.g. 'YxZ').

    Returns the remapped tuple. If the orientations have the same axis order
    (ignoring case), the values pass through unchanged (only the sign convention
    may differ, which the caller handles). If the order differs, we permute.
    """
    b = _normalize(braw_orient) or BRAW_ORIENTATION
    g = _normalize(gcsv_orient) or BRAW_ORIENTATION
    if b.lower() == g.lower():
        return braw_values
    # Compute permutation: for each axis position in the target (gcsv) order,
    # find where that axis sits in the source (braw) order.
    perm = []
    for target_axis in g:
        pos = None
        for i, sa in enumerate(b):
            if sa.lower() == target_axis.lower():
                pos = i
                break
        if pos is None:
            pos = len(perm)  # unknown; keep position
        perm.append(pos)
    return tuple(braw_values[i] for i in perm)


# ---------------------------------------------------------------------------
# GCSV writing
# ---------------------------------------------------------------------------

def write_gcsv(path, samples, timescale, gcsv_orientation, args):
    has_accel = any(s["accl"] is not None for s in samples)
    if args.no_accel:
        has_accel = False

    # The GCSV 't' field is in units where (t * tscale) = seconds.
    # Our samples carry t_ms (milliseconds). We use tscale = 0.001 (1 ms) so
    # that t == t_ms directly (t = t_ms / 1000 / 0.001 = t_ms).
    tscale = 0.001

    if args.float:
        gscale = 1.0
        ascale = 1.0
    else:
        # Auto-pick gyro/accel scales so the raw counts are ~ +/- 100000.
        gscale = _pick_gscale([v for s in samples for v in (s["gyro"] or (0, 0, 0))])
        ascale = _pick_ascale([v for s in samples for v in (s["accl"] or (0, 0, 0))]) if has_accel else 1.0

    # Header.
    vid = args.videofilename or Path(args.braw).name
    if args.id:
        cam_id = args.id
    else:
        cam_id = _default_id(args.braw)

    lines = []
    lines.append("GYROFLOW IMU LOG")
    lines.append("version,1.3")
    lines.append("id,%s" % cam_id)
    lines.append("orientation,%s" % gcsv_orientation)
    if args.fwversion:
        lines.append("fwversion,%s" % args.fwversion)
    lines.append("videofilename,%s" % vid)
    lines.append("tscale,%s" % _fmt(tscale))
    lines.append("gscale,%s" % _fmt(gscale))
    if has_accel:
        lines.append("ascale,%s" % _fmt(ascale))
    if has_accel:
        lines.append("t,gx,gy,gz,ax,ay,az")
    else:
        lines.append("t,gx,gy,gz")

    for s in samples:
        # t in GCSV units: t = (t_ms / 1000) / tscale. With tscale=0.001 this is t_ms.
        t = int(round((s["t_ms"] / 1000.0) / tscale))
        if args.float:
            gx, gy, gz = (s["gyro"] or (0.0, 0.0, 0.0))
            if has_accel:
                ax, ay, az = (s["accl"] or (0.0, 0.0, 0.0))
                lines.append("%d,%s,%s,%s,%s,%s,%s" % (t, _fmt(gx), _fmt(gy), _fmt(gz), _fmt(ax), _fmt(ay), _fmt(az)))
            else:
                lines.append("%d,%s,%s,%s" % (t, _fmt(gx), _fmt(gy), _fmt(gz)))
        else:
            gx, gy, gz = (s["gyro"] or (0.0, 0.0, 0.0))
            gx_r, gy_r, gz_r = _to_raw(gx, gscale), _to_raw(gy, gscale), _to_raw(gz, gscale)
            if has_accel:
                ax, ay, az = (s["accl"] or (0.0, 0.0, 0.0))
                # GCSV accel is in g; our values are m/s^2, so divide by gravity.
                ax_g, ay_g, az_g = ax / GRAVITY, ay / GRAVITY, az / GRAVITY
                ax_r, ay_r, az_r = _to_raw(ax_g, ascale), _to_raw(ay_g, ascale), _to_raw(az_g, ascale)
                lines.append("%d,%d,%d,%d,%d,%d,%d" % (t, gx_r, gy_r, gz_r, ax_r, ay_r, az_r))
            else:
                lines.append("%d,%d,%d,%d" % (t, gx_r, gy_r, gz_r))

    Path(path).write_text("\n".join(lines) + "\n")
    return tscale, gscale, ascale


def _to_raw(value, scale):
    if scale == 0:
        return 0
    return int(round(value / scale))


def _fmt(x):
    # Compact float formatting that round-trips reasonably.
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return ("%.10g" % x)


def _pick_tscale(t_ms_list):
    # tscale such that t_raw * tscale = seconds. Use tscale = 0.001 (ms) by default
    # if timestamps look like ms; else 1.0.
    if not t_ms_list:
        return 0.001
    # If max is < 1000, likely ms; if < 1e6, likely ms; else could be us.
    mx = max(t_ms_list)
    if mx < 10000:
        return 0.001
    if mx < 10000000:
        return 0.001
    return 0.000001


def _pick_gscale(values):
    # Choose a scale so that raw counts are ~ +/- 100000 (fits comfortably in i32).
    if not values:
        return 1.0
    mx = max(abs(v) for v in values)
    if mx == 0:
        return 1.0
    # target raw magnitude ~ 100000
    scale = mx / 100000.0
    # Round scale to a nice number.
    return _nice(scale)


def _pick_ascale(values):
    if not values:
        return 1.0
    mx = max(abs(v) for v in values)
    if mx == 0:
        return 1.0
    scale = mx / 100000.0
    return _nice(scale)


def _nice(x):
    """Round a scale to a 'nice' value (1, 2, or 5 times a power of 10)."""
    if x == 0:
        return 1.0
    import math
    exp = math.floor(math.log10(x))
    base = 10.0 ** exp
    for m in (1.0, 2.0, 5.0, 1.0):
        cand = m * base
        if abs(cand - x) / x < 0.5:
            return cand
    return x


def _default_id(braw_path):
    """Try to derive a GCSV id from the BRAW's camera_type metadata."""
    try:
        buf = Path(braw_path).read_bytes()
        cam = read_meta_string(buf, "camera_type")
        fw = read_meta_string(buf, "firmware_version")
        if cam:
            base = cam.strip().replace(" ", "_").lower()
            return base
    except Exception:
        pass
    return Path(braw_path).stem


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Extract gyro/accel data embedded in a .braw file and write a .gcsv.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("braw", help="input .braw file")
    ap.add_argument("-o", "--output", default=None, help="output .gcsv path (default: <input>.gcsv)")
    ap.add_argument("--orientation", default="YxZ",
                    help="GCSV orientation string to emit (default: YxZ). The BRAW 'yxz' axes are remapped into this order.")
    ap.add_argument("--float", action="store_true",
                    help="write sample values as floats with tscale/gscale/ascale=1 (lossless)")
    ap.add_argument("--no-accel", action="store_true", help="omit accelerometer columns (gyro only)")
    ap.add_argument("--id", default=None, help="GCSV 'id' field (default: auto from BRAW camera_type)")
    ap.add_argument("--fwversion", default=None, help="GCSV 'fwversion' field")
    ap.add_argument("--videofilename", default=None, help="GCSV 'videofilename' field (default: input filename)")
    args = ap.parse_args(argv)

    if not Path(args.braw).exists():
        print("error: BRAW file not found: %s" % args.braw, file=sys.stderr)
        return 2

    out_path = args.output or str(Path(args.braw).with_suffix(".gcsv"))

    print("Reading BRAW: %s" % args.braw)
    buf = Path(args.braw).read_bytes()
    print("  file size: %d bytes" % len(buf))

    try:
        samples, timescale = extract_imu_samples(buf)
    except Exception as e:
        print("error: %s" % e, file=sys.stderr)
        return 2

    if not samples:
        print("error: no IMU samples found in the metadata track", file=sys.stderr)
        return 2

    print("  found %d IMU samples, timescale=%d" % (len(samples), timescale))
    if samples[0]["gyro"] is not None:
        print("  first gyro (rad/s): %s" % _fmt3(samples[0]["gyro"]))
    if samples[0]["accl"] is not None:
        print("  first accl (m/s^2): %s" % _fmt3(samples[0]["accl"]))
    print("  timestamps: %d ms .. %d ms" % (samples[0]["t_ms"], samples[-1]["t_ms"]))

    # Remap axes from BRAW 'yxz' to the requested GCSV orientation.
    gcsv_orient = args.orientation
    for s in samples:
        if s["gyro"] is not None:
            s["gyro"] = remap_to_gcsv_orientation(s["gyro"], BRAW_ORIENTATION, gcsv_orient)
        if s["accl"] is not None:
            s["accl"] = remap_to_gcsv_orientation(s["accl"], BRAW_ORIENTATION, gcsv_orient)

    tscale, gscale, ascale = write_gcsv(out_path, samples, timescale, gcsv_orient, args)
    print("Wrote %s" % out_path)
    print("  orientation=%s  tscale=%s  gscale=%s  ascale=%s" % (gcsv_orient, _fmt(tscale), _fmt(gscale), _fmt(ascale)))
    return 0


def _fmt3(t):
    return "(%s, %s, %s)" % (_fmt(t[0]), _fmt(t[1]), _fmt(t[2]))


if __name__ == "__main__":
    sys.exit(main())
