#!/usr/bin/env bash
# Local OpenAI-compatible LLM server for the sloppy Reachy voice demo, serving
# mlx-community/Qwen3-4B-Instruct-2507-bf16 via mlx-lm on http://localhost:8080/v1.
# The demo's .sloppy/config.yaml points its `mlx-local` LLM endpoint here.
#
# Runs in the hf-speech-to-speech venv (has mlx-lm + the model cached).
# Override HF_S2S_DIR / MODEL / PORT as needed.
set -euo pipefail
cd "$(dirname "$0")"

HF_S2S_DIR="${HF_S2S_DIR:-$HOME/dev/hf-speech-to-speech}"
PY="$HF_S2S_DIR/.venv/bin/python"
MODEL="${MODEL:-mlx-community/Qwen3-4B-Instruct-2507-bf16}"
PORT="${PORT:-8080}"

if [[ ! -x "$PY" ]]; then
  echo "[run_llm_server] $PY not found — set HF_S2S_DIR to your hf-speech-to-speech checkout." >&2
  exit 1
fi

echo "[run_llm_server] serving $MODEL on http://localhost:$PORT/v1 (mlx-lm)"
exec "$PY" -m mlx_lm server --model "$MODEL" --port "$PORT"
