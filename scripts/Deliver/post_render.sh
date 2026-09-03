#!/usr/bin/env bash
#
# post_render.sh -- called by post-render-hook.py when a render finishes.
#
#   $1  = full path to the file Resolve just rendered
#   $2  = that file's directory
#
# The default action below repairs the known DaVinci MP4 bug: the AAC track's
# "esds" is written without a profile, so some devices/players reject it. If the
# file's AAC profile is not "LC", we re-mux it: video is STREAM-COPIIED (lossless,
# instant) and only the audio is re-encoded to a proper AAC-LC. The original is
# kept alongside as  <name>.orig.mp4
#
# Replace the "DEFAULT ACTION" section with whatever you actually want to do
# (upload, copy, notify, run your own esds patcher, etc.).
set -u

FILE="${1:-}"
DIR="${2:-}"
[ -z "$FILE" ] && { echo "post_render.sh: no file argument" >&2; exit 1; }
[ -z "$DIR" ] && DIR="$(dirname "$FILE")"

LOG="${POST_RENDER_LOG:-$HOME/.local/share/DaVinciResolve/Fusion/Scripts/Deliver/post_render.log}"
log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG" 2>/dev/null; }
log "post_render: got render output: $FILE"

# Tool resolution (override with $FFMPEG / $FFPROBE if they're not on PATH).
FFMPEG="${FFMPEG:-$(command -v ffmpeg || true)}"

# ---------------------------------------------------------------------------
# DEFAULT ACTION -- swap this out for whatever you want to happen per export
# ---------------------------------------------------------------------------
if [ -z "$FFMPEG" ]; then
  log "  ffmpeg not on PATH; skipping AAC fix (set \$FFMPEG to enable)"
else
  log " Prepping file for generic encoding"
  base="$(basename "$FILE")"; base="${base%.*}"   # strip the extension
  tmp="${DIR}/$(basename "$base")_signal.mp4"
  log " tmp is $tmp"
  if "$FFMPEG" -y -v error -i "$FILE" -c:v libx264 -crf 23 -preset medium -pix_fmt yuv420p   -c:a aac -b:a 128k -ac 2   -movflags +faststart   -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2"   -maxrate 8M -bufsize 16M   "$tmp" 2>>"$LOG"; then
    log "  created signal file"
  else
    log "  ffmpeg failed; leaving $FILE untouched"
    rm -f "$tmp"
  fi
fi
