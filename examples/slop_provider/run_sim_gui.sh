#!/usr/bin/env bash
# Launch the Reachy Mini MuJoCo simulator with the GUI viewer.
#
# On macOS the MuJoCo viewer must run under `mjpython` (main-thread GUI). With a
# uv-managed Python, mjpython can't find libpython on its own, so we point
# DYLD_FALLBACK_LIBRARY_PATH at the interpreter's lib dir.
#
# Pass extra daemon args through, e.g. ./run_sim_gui.sh --scene minimal
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
source .venv/bin/activate

LIBDIR="$(python -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR"))')"

echo "[run_sim_gui] launching MuJoCo GUI sim (mjpython); libdir=$LIBDIR"
exec env DYLD_FALLBACK_LIBRARY_PATH="$LIBDIR" \
  mjpython -m reachy_mini.daemon.app.main --sim "$@"
