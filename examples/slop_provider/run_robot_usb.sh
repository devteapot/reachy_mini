#!/usr/bin/env bash
# Launch the Reachy Mini daemon for a USB-connected robot (Lite) AND the SLOP
# provider, in one terminal.
#
#   ./run_robot_usb.sh                          # daemon (serial auto-detect) + provider
#   ./run_robot_usb.sh --no-wake-up-on-start    # extra args are passed to the daemon
#
# The daemon is started headless (it has no GUI for a real robot anyway) with
# serial port auto-detection, and is stopped again when you Ctrl-C the provider.
# If a daemon is already running on :8000, it is reused and left running.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
source .venv/bin/activate

STATUS_URL="http://localhost:8000/api/daemon/status"
DAEMON_LOG="/tmp/reachy_daemon_usb.log"
DAEMON_PID=""

daemon_up() {
  curl -sf --max-time 2 "$STATUS_URL" > /dev/null 2>&1
}

cleanup() {
  if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
    echo "[run_robot_usb] stopping daemon (pid $DAEMON_PID)..."
    kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if daemon_up; then
  echo "[run_robot_usb] daemon already running on :8000 — reusing it (will not stop it on exit)"
else
  echo "[run_robot_usb] starting reachy-mini-daemon (USB, serial auto-detect); log: $DAEMON_LOG"
  reachy-mini-daemon "$@" > "$DAEMON_LOG" 2>&1 &
  DAEMON_PID=$!

  # Wait for the REST API to come up (motor init + USB detection take a moment).
  for _ in $(seq 1 60); do
    if daemon_up; then break; fi
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
      echo "[run_robot_usb] daemon exited during startup — last log lines:" >&2
      tail -20 "$DAEMON_LOG" >&2
      exit 1
    fi
    sleep 1
  done
  if ! daemon_up; then
    echo "[run_robot_usb] daemon did not answer on $STATUS_URL after 60s — log: $DAEMON_LOG" >&2
    exit 1
  fi
  echo "[run_robot_usb] daemon is up."
fi

echo "[run_robot_usb] starting SLOP provider (Ctrl-C stops provider + daemon)"
python reachy_slop_provider.py
