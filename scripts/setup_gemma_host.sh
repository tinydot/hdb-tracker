#!/usr/bin/env bash
# Bootstrap a remote host (dual RTX 4500 Ada) to run scripts/label_photos.py.
# Starts one ollama serve instance per GPU so both cards label in parallel.
# Idempotent: safe to re-run.

set -euo pipefail

MODEL="${MODEL:-gemma4:31b}"
GPUS="${GPUS:-0,1}"               # comma-separated GPU indices
BASE_PORT="${BASE_PORT:-11434}"   # first instance uses this port; each next +1
KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-24h}"

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

# Stop any stray ollama daemon that would squat on port 11434 and both GPUs.
if pgrep -x ollama >/dev/null 2>&1; then
  echo "==> Stopping existing ollama processes"
  pkill -x ollama || true
  sleep 1
fi

echo "==> Starting one ollama serve per GPU"
IFS=',' read -ra GPU_LIST <<< "$GPUS"
HOSTS=()
for i in "${!GPU_LIST[@]}"; do
  gpu="${GPU_LIST[$i]}"
  port=$((BASE_PORT + i))
  log="/tmp/ollama-gpu${gpu}.log"
  echo "   GPU ${gpu} -> 127.0.0.1:${port} (log: ${log})"
  CUDA_VISIBLE_DEVICES="${gpu}" \
  OLLAMA_HOST="127.0.0.1:${port}" \
  OLLAMA_KEEP_ALIVE="${KEEP_ALIVE}" \
    nohup ollama serve >"${log}" 2>&1 &
  HOSTS+=("http://127.0.0.1:${port}")
done
sleep 3

echo "==> Pulling model: $MODEL (on each instance)"
for host in "${HOSTS[@]}"; do
  echo "   ${host}"
  OLLAMA_HOST="${host#http://}" ollama pull "$MODEL"
done

echo "==> Installing Python deps"
python3 -m pip install --upgrade pip
python3 -m pip install -r "$(dirname "$0")/requirements.txt"

echo "==> Smoke test"
for host in "${HOSTS[@]}"; do
  OLLAMA_HOST="${host#http://}" python3 - <<PY
import os, ollama
host = os.environ["OLLAMA_HOST"]
client = ollama.Client(host=f"http://{host}")
print(f"{host}: models =", [m["model"] for m in client.list()["models"]])
PY
done

joined=$(IFS=, ; echo "${HOSTS[*]}")
echo "==> Done. Run the labeler across both GPUs with:"
echo "    python3 scripts/label_photos.py --model $MODEL --hosts '${joined}'"
echo "or export it:"
echo "    export OLLAMA_HOSTS='${joined}'"
echo "    python3 scripts/label_photos.py --model $MODEL"
