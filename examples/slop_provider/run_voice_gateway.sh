#!/usr/bin/env bash
# Local OpenAI-compatible voice gateway for the sloppy Reachy voice demo:
#   - Parakeet-TDT STT (in-process via mlx-audio), and
#   - a reverse proxy of /v1/audio/speech to the Voxtral TTS server on the spark.
# Both are served on http://localhost:8090/v1 so sloppy's voice policy treats them
# as local (no per-turn approval prompts).
#
# Runs in the hf-speech-to-speech venv, which already has mlx-audio + the Parakeet
# model cached. Override HF_S2S_DIR if that checkout lives elsewhere.
#
# Pass-through args, e.g. ./run_voice_gateway.sh --port 8090 \
#   --tts-upstream http://slopinator-s-1.local:8091/v1
set -euo pipefail
cd "$(dirname "$0")"

HF_S2S_DIR="${HF_S2S_DIR:-$HOME/dev/hf-speech-to-speech}"
PY="$HF_S2S_DIR/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "[run_voice_gateway] $PY not found — set HF_S2S_DIR to your hf-speech-to-speech checkout." >&2
  exit 1
fi

echo "[run_voice_gateway] python=$PY"
exec "$PY" voice_gateway.py "$@"
