#!/usr/bin/env bash
# One-time setup per session for engine/reel.py: voice model, logo + icon libraries.
set -euo pipefail
cd "$(dirname "$0")"
pip install --break-system-packages -q kokoro-onnx soundfile pillow numpy >/dev/null 2>&1 || pip install --break-system-packages kokoro-onnx soundfile pillow numpy
mkdir -p .cache
REL=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
[ -s .cache/kokoro.onnx ] || curl -sSfL -o .cache/kokoro.onnx "$REL/kokoro-v1.0.onnx"
[ -s .cache/voices.bin ]  || curl -sSfL -o .cache/voices.bin  "$REL/voices-v1.0.bin"
[ -d node_modules/simple-icons ] && [ -d node_modules/lucide-static ] || npm install --silent --no-audit --no-fund
python3 -c "import playwright, kokoro_onnx" && command -v ffmpeg >/dev/null
echo "reel engine ready"
