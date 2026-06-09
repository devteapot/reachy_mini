#!/usr/bin/env bash
# Launch the built sloppy runtime against the minimal demo config in ./demo,
# isolated from your global ~/.sloppy instance.
#
# This starts a single session runtime in the background, exposes it over a
# local WebSocket, then attaches the TUI to that same WebSocket endpoint.
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
# WebSocket knobs:
#   SLOPPY_WS_LAN=1
#   SLOPPY_WS_HOST=127.0.0.1
#   SLOPPY_WS_PORT=8787
#   SLOPPY_WS_PATH=/slop
#   SLOPPY_WS_PUBLIC_HOST=<lan-ip-or-dns-name>
#   SLOPPY_WS_PUBLIC_URL=ws://<lan-ip-or-dns-name>:8787/slop
#   SLOPPY_WS_ALLOW_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
#   SLOPPY_WS_TOKEN=<optional token; auto-generated in LAN mode>
#
# Args are forwarded to the TUI (e.g. --yolo).
set -euo pipefail

truthy() {
  case "${1:-}" in
    1 | true | TRUE | yes | YES | on | ON) return 0 ;;
    *) return 1 ;;
  esac
}

detect_lan_ip() {
  local iface ip

  if command -v route >/dev/null 2>&1 && command -v ipconfig >/dev/null 2>&1; then
    iface="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
    if [[ -n "$iface" ]]; then
      ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
      if [[ -n "$ip" ]]; then
        printf '%s\n' "$ip"
        return 0
      fi
    fi
    for iface in en0 en1; do
      ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
      if [[ -n "$ip" ]]; then
        printf '%s\n' "$ip"
        return 0
      fi
    done
  fi

  if command -v ip >/dev/null 2>&1; then
    ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}')"
    if [[ -n "$ip" ]]; then
      printf '%s\n' "$ip"
      return 0
    fi
  fi

  if command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    if [[ -n "$ip" ]]; then
      printf '%s\n' "$ip"
      return 0
    fi
  fi
}

append_token_query() {
  local url="$1"
  local token="$2"
  if [[ "$url" == *\?* ]]; then
    printf '%s&token=%s\n' "$url" "$token"
  else
    printf '%s?token=%s\n' "$url" "$token"
  fi
}

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
if truthy "${SLOPPY_WS_LAN:-0}"; then
  WS_HOST="${SLOPPY_WS_HOST:-0.0.0.0}"
else
  WS_HOST="${SLOPPY_WS_HOST:-127.0.0.1}"
fi
WS_PORT="${SLOPPY_WS_PORT:-8787}"
WS_PATH="${SLOPPY_WS_PATH:-/slop}"
if [[ "$WS_PATH" != /* ]]; then
  WS_PATH="/$WS_PATH"
fi

remote_ws=0
case "$WS_HOST" in
  127.0.0.1 | localhost | ::1 | "[::1]") ;;
  *) remote_ws=1 ;;
esac

if [[ "$remote_ws" -eq 1 && -z "${SLOPPY_WS_TOKEN:-}" ]]; then
  if ! command -v openssl >/dev/null 2>&1; then
    echo "[run_sloppy_demo] non-loopback websocket requires SLOPPY_WS_TOKEN." >&2
    echo "[run_sloppy_demo] Set SLOPPY_WS_TOKEN or install openssl for auto-generation." >&2
    exit 1
  fi
  export SLOPPY_WS_TOKEN="$(openssl rand -hex 24)"
fi

public_host="${SLOPPY_WS_PUBLIC_HOST:-}"
if [[ -z "${SLOPPY_WS_PUBLIC_URL:-}" && "$remote_ws" -eq 1 && -z "$public_host" ]]; then
  public_host="$(detect_lan_ip || true)"
  if [[ -z "$public_host" ]]; then
    echo "[run_sloppy_demo] could not auto-detect LAN IP for websocket public URL." >&2
    echo "[run_sloppy_demo] Set SLOPPY_WS_PUBLIC_HOST or SLOPPY_WS_PUBLIC_URL." >&2
    exit 1
  fi
fi

display_host="${public_host:-$WS_HOST}"
if [[ "$display_host" == "0.0.0.0" || "$display_host" == "::" || "$display_host" == "[::]" ]]; then
  display_host="localhost"
fi
ws_public_url="${SLOPPY_WS_PUBLIC_URL:-ws://$display_host:$WS_PORT$WS_PATH}"
ws_connect_url="$ws_public_url"
if [[ -n "${SLOPPY_WS_TOKEN:-}" ]]; then
  ws_connect_url="$(append_token_query "$ws_public_url" "$SLOPPY_WS_TOKEN")"
fi

if [[ -n "${SLOPPY_WS_ALLOW_ORIGINS+x}" ]]; then
  WS_ALLOW_ORIGINS="$SLOPPY_WS_ALLOW_ORIGINS"
else
  WS_ALLOW_ORIGINS="http://localhost:5173,http://127.0.0.1:5173"
  if [[ "$remote_ws" -eq 1 && -n "$public_host" ]]; then
    WS_ALLOW_ORIGINS="$WS_ALLOW_ORIGINS,http://$public_host:5173"
  fi
fi

if [[ ! -f "$SLOPPY_ENTRYPOINT" ]]; then
  echo "[run_sloppy_demo] sloppy entrypoint not found: $SLOPPY_ENTRYPOINT" >&2
  echo "    Set SLOPPY_DIR or SLOPPY_ENTRYPOINT, or build sloppy:" >&2
  echo "    (cd \"$SLOPPY_DIR\" && bun run build)" >&2
  exit 1
fi

if [[ "$*" == *"--prompt"* || "$*" == *" -p "* || "${1:-}" == "-p" ]]; then
  echo "[run_sloppy_demo] prompt mode is not supported by the websocket demo launcher." >&2
  echo "[run_sloppy_demo] run without -p/--prompt and enter the prompt in the TUI." >&2
  exit 2
fi

mkdir -p "$DEMO_DIR/.sloppy"
cd "$DEMO_DIR"

server_args=(
  session serve
  --socket "$SESSION_SOCKET"
  --ws-host "$WS_HOST"
  --ws-port "$WS_PORT"
  --ws-path "$WS_PATH"
)

if [[ -n "${SLOPPY_WS_TOKEN:-}" ]]; then
  server_args+=(--ws-token-env SLOPPY_WS_TOKEN)
fi

if [[ -n "${SLOPPY_WS_PUBLIC_URL:-}" || "$remote_ws" -eq 1 ]]; then
  server_args+=(--ws-public-url "$ws_public_url")
fi

if [[ -n "$WS_ALLOW_ORIGINS" ]]; then
  IFS=',' read -r -a origins <<< "$WS_ALLOW_ORIGINS"
  for origin in "${origins[@]}"; do
    origin="${origin#"${origin%%[![:space:]]*}"}"
    origin="${origin%"${origin##*[![:space:]]}"}"
    if [[ -n "$origin" ]]; then
      server_args+=(--ws-allow-origin "$origin")
    fi
  done
fi

check_host="$WS_HOST"
if [[ "$check_host" == "0.0.0.0" || "$check_host" == "::" || "$check_host" == "[::]" ]]; then
  check_host="127.0.0.1"
fi

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
echo "[run_sloppy_demo] websocket=$ws_public_url"
if [[ -n "${SLOPPY_WS_TOKEN:-}" ]]; then
  echo "[run_sloppy_demo] websocket token=$SLOPPY_WS_TOKEN"
  echo "[run_sloppy_demo] websocket connect url=$ws_connect_url"
fi
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
  if [[ -S "$SESSION_SOCKET" ]] && curl -fsS "http://$check_host:$WS_PORT/.well-known/slop" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.1
done

if [[ "$ready" -ne 1 ]]; then
  echo "[run_sloppy_demo] timed out waiting for socket + websocket; log follows:" >&2
  sed -n '1,200p' "$SESSION_LOG" >&2 || true
  exit 1
fi

echo "[run_sloppy_demo] running: bun $SLOPPY_ENTRYPOINT tui --socket $ws_connect_url $*"
env \
  HOME="$DEMO_DIR" \
  SLOPPY_CODEX_AUTH_PATH="$REAL_HOME/.codex/auth.json" \
  bun "$SLOPPY_ENTRYPOINT" tui --socket "$ws_connect_url" "$@"
