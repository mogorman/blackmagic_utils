# blackmagic_utils

Tools for reading and writing **gyroscope / accelerometer (IMU) data inside
Blackmagic RAW (`.braw`) files**, so that [Gyroflow](https://gyroflow.com/) can
stabilize footage shot on Blackmagic cameras (e.g. the Ursa Micro / Mini Pro)
that have an external IMU logger (M5Stack, ESP32, etc.) recording alongside the
video.

Blackmagic cameras do not record gyro data natively, but the BRAW container is
an ISO-BMFF (MP4-family) file, and Gyroflow's BRAW parser
(`telemetry-parser/src/blackmagic/mod.rs`) *will* read an IMU if it is present
as an MP4 **metadata track**. These scripts let you:

- **inject** gyro/accel data from a Gyroflow `.gcsv` log into a `.braw` file
  (so Gyroflow reads it natively, with no sidecar file), and
- **extract** the IMU data back out of a `.braw` file into a standard `.gcsv`.

Both scripts are **pure Python standard library** (`struct`, `csv`, `argparse`,
`pathlib`) — no third-party dependencies, no compilation. They work on Linux,
macOS, and Windows.

## Why this exists

Gyroflow normally pairs a video file with a sidecar IMU log (`.gcsv`,
`.mov`-embedded, etc.). For Blackmagic BRAW footage there is no built-in IMU,
so the usual workflow is to keep a separate logger running and feed its `.gcsv`
to Gyroflow as a sidecar. That works, but:

- the sidecar must be kept in sync with the clip by hand, and
- some pipelines (e.g. automated stabilization) prefer the IMU to be *inside*
  the media file so there is a single self-contained input.

These tools let you **bake the IMU into the BRAW** (and reverse the process),
which is useful for testing, for self-contained clips, and for verifying that a
BRAW reader is actually consuming the embedded track.

## Files

| File | Purpose |
|---|---|
| [`bm_utils`](bm_utils) | The single entry point: `bm_utils inject_gyro \| extract_gyro \| install`. Run it straight from the repo, or via the nix package. |
| [`completions/bm_utils`](completions/bm_utils) | Bash tab-completion for `bm_utils` (subcommands + file names). |
| [`inject_gyro_into_braw.py`](inject_gyro_into_braw.py) | `.gcsv` → `.braw`: embed a Gyroflow IMU log into a BRAW file as a native MP4 metadata track. |
| [`extract_gyro_from_braw.py`](extract_gyro_from_braw.py) | `.braw` → `.gcsv`: read the IMU embedded in a BRAW file and write a standard Gyroflow `.gcsv`. |
| [`scripts/`](scripts/) | Fusion post-render hooks installed by `bm_utils install` into `~/.local/share/DaVinciResolve/Fusion/Scripts`. |

## How it works (the short version)

A BRAW file is an MP4 container. Its gyro data (if any) lives in a track whose
handler type is `meta`. Each IMU sample is a 20-byte box:

```
[u32 size][4CC "mogy"|"moac"][f32 x][f32 y][f32 z]     (little-endian)
```

- `mogy` = a gyroscope sample (x, y, z in **rad/s**)
- `moac` = an accelerometer sample (x, y, z in **m/s²**)

Two quirks of the BRAW parser that the scripts account for:

1. **The parser negates the accelerometer axes on read**
   (`blackmagic/mod.rs:148-153`). So the injector negates the accel values on
   *write*, and the extractor negates them back on *read*, to recover the true
   sensor readings.
2. **The parser assumes an axis orientation of `yxz`** for any camera model it
   does not special-case (`blackmagic/mod.rs:169-172`). The injector remaps the
   GCSV's native axis order into `yxz` before writing; the extractor remaps it
   back into whatever orientation you request.

Sample timestamps come from the track's `stts` (sample-to-time) table combined
with the `mdhd` timescale. The injector sets the timescale to 1000 (1 tick = 1
ms) and emits a per-sample `stts` delta table so the exact GCSV timestamps are
preserved.

## Usage

Everything is driven through the single `bm_utils` command (add `--help` to any
subcommand for its options). The two Python files above are the subcommands'
engines; you don't call them directly.

### Inject a GCSV into a BRAW

```bash
bm_utils inject_gyro INPUT.braw INPUT.gcsv [options]
```

By default this writes `INPUT_injected.braw` (the original is left untouched).

Options:

| Option | Meaning |
|---|---|
| `-o, --output PATH` | Output BRAW path (default: `<input>_injected.braw`). |
| `--track-id N` | MP4 track ID to use for the metadata track (default `4`). |
| `--timescale N` | Track timescale in ticks/second (default `1000`, i.e. 1 ms). |
| `--dry-run` | Parse and validate everything, report what *would* be written, but do not write any file. |

The injector validates the result by re-parsing the written file and confirming
every sample round-trips, and it refuses to clobber the input file.

### Extract a GCSV from a BRAW

```bash
bm_utils extract_gyro INPUT.braw [options]
```

By default this writes `INPUT.gcsv`.

Options:

| Option | Meaning |
|---|---|
| `-o, --output PATH` | Output GCSV path (default: `<input>.gcsv`). |
| `--orientation STR` | GCSV orientation to emit (default `YxZ`). The BRAW's `yxz` axes are remapped into this order. Use `XYZ` to match a standard M5Stack/ESP logger. |
| `--float` | Write sample values as floats with `tscale`/`gscale`/`ascale = 1` (lossless, simpler). Default writes integer counts with auto-picked scales. |
| `--no-accel` | Omit the accelerometer columns (gyro only). |
| `--id STR` | GCSV `id` field (default: auto from the BRAW's `camera_type` metadata). |
| `--fwversion STR` | GCSV `fwversion` field. |
| `--videofilename STR` | GCSV `videofilename` field (default: the input filename). |

The produced `.gcsv` can be loaded directly in Gyroflow as a sidecar, or fed
back through the injector.

## Installing & running

**Straight from the repo** (no nix needed) — `bm_utils` is a self-locating
bash script, so it works when run in place. It needs `python3` on the `PATH`:

```bash
./bm_utils inject_gyro in.braw in.gcsv
```

**As a nix package** — this is a flake. Build it and `bm_utils` lands on your
`PATH` (along with the inject/extract engines and the `scripts/` payload):

```bash
nix build                       # or: nix run .#blackmagic-utils
bm_utils install               # install the Fusion post-render scripts
```

`nix develop` gives a shell with `bm_utils` (and the completion) already loaded.

### Tab completion

`completions/bm_utils` is a standard bash-completion file. It is installed to
`share/bash-completion/completions/bm_utils`, so it loads automatically if you
have the [`bash-completion`](https://github.com/bash-git/bash-completion)
package enabled. To use it in a shell without that, source it:

```bash
. /path/to/completions/bm_utils
```

Afterwards `bm_utils <TAB>` completes the subcommands and `<TAB>` on an
argument completes file names.

## Round-trip

The two scripts are exact inverses. The full circle

```
original .gcsv  →  inject  →  .braw  →  extract  →  .gcsv
```

is lossless to the precision of the BRAW's `float32` sample storage (relative
error ~1e-7). Timestamps round-trip exactly.

## Notes & caveats

- **Only the IMU track is touched.** The injector performs MP4 "box surgery": it
  rewrites the `moov` atom (adding the new track and updating `mvhd`) and
  appends the new sample data, leaving the original video/audio/timecode tracks
  byte-for-byte intact. The output is a valid BRAW that plays normally.
- **Large files (> 4 GB):** the injector uses the `co64`/`largesize` MP4
  mechanisms so chunk offsets and box sizes stay correct for big clips.
- **Orientation:** if your logger's GCSV uses a different axis convention than
  the one you pass to `--orientation`, the extracted data will be permuted
  accordingly. Match the orientation to your logger's actual convention.
- **Not a general MP4 editor:** these scripts are purpose-built for the BRAW
  IMU track. They are not a general-purpose MP4 muxer.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
