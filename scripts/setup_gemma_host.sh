#!/usr/bin/env bash
# Bootstrap a remote host (dual RTX 4500 Ada) to run scripts/label_photos.py.
# Idempotent: safe to re-run.

set -euo pipefail

MODEL="${MODEL:-gemma3:27b}"

echo "==> Checking NVIDIA driver"
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "!! nvidia-smi not found. Install the NVIDIA driver for RTX 4500 Ada first." >&2
  exit 1
fi
nvidia-smi --query-gpu=index,name,memory.total --format=csv

echo "==> Installing Ollama (skipped if already installed)"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
else
  echo "   ollama already installed: $(ollama --version)"
fi

echo "==> Starting Ollama server if not running"
if ! pgrep -x ollama >/dev/null 2>&1; then
  # Spread model layers across both GPUs when the model doesn't require it,
  # so a second concurrent request can use the idle GPU.
  OLLAMA_SCHED_SPREAD=1 nohup ollama serve >/tmp/ollama.log 2>&1 &
  sleep 3
fi

echo "==> Pulling model: $MODEL"
ollama pull "$MODEL"

echo "==> Installing Python deps"
python3 -m pip install --upgrade pip
python3 -m pip install -r "$(dirname "$0")/requirements.txt"

echo "==> Smoke test"
python3 - <<'PY'
import ollama
client = ollama.Client()
print("models:", [m["model"] for m in client.list()["models"]])
PY

echo "==> Done. Run the labeler with:"
echo "    python3 scripts/label_photos.py --model $MODEL"
