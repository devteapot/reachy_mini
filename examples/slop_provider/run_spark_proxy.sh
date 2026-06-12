#!/usr/bin/env bash
# Localhost TCP forwarders for the DGX Spark voice endpoints (STT :8002,
# TTS :8091). sloppy's voice network policy auto-starts the hands-free
# conversation loop only when both speech endpoints are local (no-auth +
# localhost baseUrl) — and in `session serve` mode nothing can invoke the
# start_listening approval flow for remote endpoints (the agent is blind to
# its own session provider, and the TUI has no control for it). Fronting the
# Spark on 127.0.0.1 restores the same invariant the Phase-2 voice gateway
# provided, without the gateway.
#
# Usage:
#   ./run_spark_proxy.sh                 # forward from SPARK_HOST (default below)
#   SPARK_HOST=192.168.1.42 ./run_spark_proxy.sh
#
# Run this before (or alongside) run_sloppy_demo.sh. Ctrl-C stops both forwards.
set -euo pipefail

SPARK_HOST="${SPARK_HOST:-192.168.1.103}"
STT_PORT="${STT_PORT:-8002}"
TTS_PORT="${TTS_PORT:-8091}"

if ! command -v socat > /dev/null 2>&1; then
  echo "[run_spark_proxy] socat is required:  sudo apt install socat" >&2
  exit 1
fi

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

for port in "$STT_PORT" "$TTS_PORT"; do
  if curl -sf --max-time 1 "http://127.0.0.1:${port}/" > /dev/null 2>&1 \
    || socat -u OPEN:/dev/null "TCP:127.0.0.1:${port},connect-timeout=1" 2>/dev/null; then
    echo "[run_spark_proxy] 127.0.0.1:${port} is already in use — reusing whatever serves it." >&2
    continue
  fi
  socat "TCP-LISTEN:${port},fork,reuseaddr,bind=127.0.0.1" "TCP:${SPARK_HOST}:${port}" &
  pids+=($!)
  echo "[run_spark_proxy] 127.0.0.1:${port} -> ${SPARK_HOST}:${port}"
done

if [[ ${#pids[@]} -eq 0 ]]; then
  echo "[run_spark_proxy] nothing to forward — exiting."
  exit 0
fi

echo "[run_spark_proxy] forwarding active (Ctrl-C to stop)."
wait
