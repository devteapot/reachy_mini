#!/usr/bin/env bash
# Pi-friendly microphone loopback test: record DUR seconds from the robot mic
# (PipeWire default source — the Reachy Mini Audio input) and play it back
# through the robot speaker, printing the captured peak level.
#
# No daemon/provider/sloppy needed; safe to run alongside them (PipeWire
# shares the device). Usage:  DUR=5 ./mic_test_pi.sh
set -euo pipefail

DUR="${DUR:-5}"
OUT=/tmp/mic_test_pi.wav

echo "[mic_test_pi] SPEAK NOW — recording ${DUR}s from the default source…"
timeout -s INT "$DUR" gst-launch-1.0 -q -e pulsesrc ! audioconvert ! wavenc ! filesink location="$OUT" || true

python3 - "$OUT" <<'EOF'
import sys, wave
import numpy as np

w = wave.open(sys.argv[1])
pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
peak = int(np.abs(pcm).max()) if len(pcm) else 0
verdict = "SIGNAL — mic works" if peak > 500 else "SILENCE — mic path is dead"
print(f"[mic_test_pi] frames={w.getnframes()} rate={w.getframerate()} "
      f"channels={w.getnchannels()} peak={peak}/32767 -> {verdict}")
EOF

echo "[mic_test_pi] playing back through the speaker…"
gst-launch-1.0 -q playbin uri="file://$OUT" || true
