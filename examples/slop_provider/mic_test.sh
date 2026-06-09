#!/usr/bin/env bash
# Isolated test of the voice loop's capture → VAD → STT, independent of sloppy.
# Reproduces the exact sox capture command the conversation loop uses, then
# reports byte size + audio level, plays the clip back, and transcribes it via
# the local voice gateway (:8090).
#
# IMPORTANT: stop the sloppy session first (Ctrl-C terminal E) — only one process
# can hold the microphone.
#
# Tune with env vars, e.g.:  THRESH=1% STOP=1.5 ./mic_test.sh
set -euo pipefail
cd "$(dirname "$0")"

THRESH="${THRESH:-1%}"   # start/stop amplitude threshold (low — the Studio mic is quiet)
STOP="${STOP:-1.2}"      # seconds of trailing silence that ends capture
OUT=/tmp/mic_test.wav
GATEWAY="${GATEWAY:-http://127.0.0.1:8090}"

echo "[mic_test] device: $(osascript -e 'name of (get volume settings)' 2>/dev/null || true)"
echo "[mic_test] SPEAK NOW — capture stops after ${STOP}s of silence (threshold ${THRESH})."
echo "[mic_test] (if this hangs with no 'captured' line, the start threshold was never crossed — Ctrl-C and retry with THRESH=1%)"

sox -d -c 1 -r 16000 -b 16 -e signed-integer -t wav "$OUT" silence 1 0.1 "$THRESH" 1 "$STOP" "$THRESH"

echo "[mic_test] captured $(stat -f%z "$OUT") bytes"
echo "[mic_test] level:"; sox "$OUT" -n stat 2>&1 | grep -iE 'Maximum amplitude|RMS +amplitude' || true
echo "[mic_test] playing back…"; afplay "$OUT" || true
echo "[mic_test] transcribing via gateway $GATEWAY …"
curl -sS -m60 -X POST "$GATEWAY/v1/audio/transcriptions" \
  -F 'model=parakeet-tdt' -F 'response_format=verbose_json' -F 'language=en' \
  -F "file=@${OUT};type=audio/wav"
echo
