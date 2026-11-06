#!/usr/bin/env bash
# Usage: bash engine/get_media.sh SLUG [MAX_SECONDS]
# Waits for the bruce-media-fetch Action to publish media for SLUG on the media-cache branch,
# then unpacks it to engine/.cache/media/SLUG/ and builds a contact sheet (candidates.jpg) to review.
set -euo pipefail
cd "$(dirname "$0")/.."
SLUG=$1; MAX=${2:-480}; URL=https://raw.githubusercontent.com/Eddysailab/bruce-media/media-cache/$SLUG/manifest.json
for ((t=0; t<MAX; t+=15)); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "$URL?t=$t")" = 200 ] && break; sleep 15
done
[ "$(curl -s -o /dev/null -w '%{http_code}' "$URL?t=final")" = 200 ] || { echo "media not ready after ${MAX}s (check the bruce-media-fetch Action)"; exit 1; }
git fetch -q origin media-cache
DEST=engine/.cache/media; mkdir -p "$DEST"; rm -rf "$DEST/$SLUG"
git archive origin/media-cache "$SLUG" | tar -x -C "$DEST"
python3 - "$DEST/$SLUG" <<'PY'
import json, sys, pathlib
from PIL import Image, ImageDraw
d = pathlib.Path(sys.argv[1]); m = json.loads((d / "manifest.json").read_text())
print("\n".join(m["log"]))
items = [f for f in m["files"]]
if not items: sys.exit(0)
w, h = 300, 460; cols = 5; rows = (len(items) + cols - 1) // cols
sheet = Image.new("RGB", (cols * (w + 10) + 10, rows * (h + 40) + 10), "#777"); dr = ImageDraw.Draw(sheet)
for k, f in enumerate(items):
    p = d / (f["file"].replace(".mp4", ".thumb.jpg"))
    im = Image.open(p).convert("RGB")
    im = im.crop((0, 0, im.width, min(im.height, int(im.width * h / w)))) if f["type"] == "shot" else im
    im.thumbnail((w, h)); x, y = 10 + (k % cols) * (w + 10), 10 + (k // cols) * (h + 40)
    sheet.paste(im, (x, y)); dr.text((x, y + h + 6), f"{f['file']} [{f['type']}]", fill="white")
sheet.save(d / "candidates.jpg", quality=85); print("contact sheet:", d / "candidates.jpg")
PY
