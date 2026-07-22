#!/usr/bin/env bash
# Launch the built sloppy runtime against the minimal demo config in ./demo,
# isolated from your global ~/.sloppy instance.
#
# This starts a single session runtime in the background, then attaches the TUI
# to its typed Unix socket. The current sloppy runtime exposes WebSockets only
# through `sloppy gateway` backed by a Supervisor; `session serve` is Unix-only.
#
# How the isolation works:
#   - HOME is pointed at ./demo, so sloppy loads ./demo/.sloppy/config.yaml as
#     its home+workspace config and ignores your real ~/.sloppy/config.yaml.
#   - SLOPPY_CODEX_AUTH_PATH keeps your real Codex (`codex login`) auth working
#     despite the HOME override.
#
# Override SLOPPY_DIR if your sloppy checkout lives elsewhere. Override
# SLOPPY_ENTRYPOINT to force a specific sloppy entrypoint.
#
# Args are forwarded to the TUI (e.g. --yolo).
set -euo pipefail

REAL_HOME="$HOME"
DEMO_DIR="$(cd "$(dirname "$0")/demo" && pwd)"
SLOPPY_DIR="${SLOPPY_DIR:-$REAL_HOME/dev/sloppy}"
SLOPPY_SRC_ENTRYPOINT="$SLOPPY_DIR/src/bin/sloppy.ts"
SLOPPY_DIST_ENTRYPOINT="$SLOPPY_DIR/dist/bin/sloppy.js"
SLOPPY_ENTRYPOINT="${SLOPPY_ENTRYPOINT:-}"
if [[ -z "$SLOPPY_ENTRYPOINT" ]]; then
  if [[ -f "$SLOPPY_SRC_ENTRYPOINT" ]]; then
    SLOPPY_ENTRYPOINT="$SLOPPY_SRC_ENTRYPOINT"
  else
    SLOPPY_ENTRYPOINT="$SLOPPY_DIST_ENTRYPOINT"
  fi
fi
SESSION_SOCKET="${SLOPPY_SESSION_SOCKET:-/tmp/slop/sloppy-reachy-demo.sock}"
SESSION_LOG="$DEMO_DIR/.sloppy/session-server.log"

if [[ ! -f "$SLOPPY_ENTRYPOINT" ]]; then
  echo "[run_sloppy_demo] sloppy entrypoint not found: $SLOPPY_ENTRYPOINT" >&2
  echo "    Set SLOPPY_DIR or SLOPPY_ENTRYPOINT, or build sloppy:" >&2
  echo "    (cd \"$SLOPPY_DIR\" && bun run build)" >&2
  exit 1
fi

if [[ "$*" == *"--prompt"* || "$*" == *" -p "* || "${1:-}" == "-p" ]]; then
  echo "[run_sloppy_demo] prompt mode is not supported by the demo launcher." >&2
  echo "[run_sloppy_demo] run without -p/--prompt and enter the prompt in the TUI." >&2
  exit 2
fi

mkdir -p "$DEMO_DIR/.sloppy"
cd "$DEMO_DIR"

server_args=(
  session serve
  --socket "$SESSION_SOCKET"
)

cleanup() {
  if [[ -n "${session_pid:-}" ]] && kill -0 "$session_pid" 2>/dev/null; then
    kill "$session_pid" 2>/dev/null || true
    wait "$session_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[run_sloppy_demo] HOME=$DEMO_DIR  config=$DEMO_DIR/.sloppy/config.yaml"
echo "[run_sloppy_demo] sloppy=$SLOPPY_ENTRYPOINT"
echo "[run_sloppy_demo] session socket=$SESSION_SOCKET"
echo "[run_sloppy_demo] log=$SESSION_LOG"

env \
  HOME="$DEMO_DIR" \
  SLOPPY_CODEX_AUTH_PATH="$REAL_HOME/.codex/auth.json" \
  bun "$SLOPPY_ENTRYPOINT" "${server_args[@]}" >"$SESSION_LOG" 2>&1 &
session_pid=$!

ready=0
for _ in {1..80}; do
  if ! kill -0 "$session_pid" 2>/dev/null; then
    echo "[run_sloppy_demo] session server exited early; log follows:" >&2
    sed -n '1,200p' "$SESSION_LOG" >&2 || true
    exit 1
  fi
  if [[ -S "$SESSION_SOCKET" ]]; then
    ready=1
    break
  fi
  sleep 0.1
done

if [[ "$ready" -ne 1 ]]; then
  echo "[run_sloppy_demo] timed out waiting for session socket; log follows:" >&2
  sed -n '1,200p' "$SESSION_LOG" >&2 || true
  exit 1
fi

echo "[run_sloppy_demo] running: bun $SLOPPY_ENTRYPOINT tui --socket $SESSION_SOCKET $*"
env \
  HOME="$DEMO_DIR" \
  SLOPPY_CODEX_AUTH_PATH="$REAL_HOME/.codex/auth.json" \
  bun "$SLOPPY_ENTRYPOINT" tui --socket "$SESSION_SOCKET" "$@"
