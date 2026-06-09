# Reachy Mini → SLOP provider

A small [SLOP](https://github.com/agnt-gg/slop) provider that wraps the Reachy Mini
Python SDK and exposes the robot as a **state tree + affordances** over a Unix socket.
A SLOP consumer — e.g. the [`sloppy`](https://github.com/devteapot/sloppy) agent runtime —
connects, observes joint state, and invokes movement/behavior actions. No bespoke tool
registry; the agent sees the robot the same SLOP-native way it sees everything else.

This is the **Phase 1 tracer**: movement + behaviors only, verified in the MuJoCo
simulator. Audio (mic/STT, speaker/TTS) is a later phase.

```
sloppy (TS SLOP consumer)
  └─ discovers /tmp/slop/providers/reachy.json
      └─ connects unix:/tmp/slop/reachy.sock  (NDJSON)
          └─ reachy_slop_provider.py  (SlopServer + ReachyMini SDK)
              └─ ws://localhost:8000/ws/sdk
                  └─ reachy-mini-daemon --sim   (MuJoCo)
```

## State tree

```
/reachy
├── /status     props: connected, mode, head_joints, antenna_joints   (live, ~5 Hz)
├── /head       actions: goto_pose(pitch,roll,yaw,z,duration), set_antennas(right,left)
└── /behavior   actions: wake_up, goto_sleep, list_emotions, play_emotion,
                enable_wobbling, disable_wobbling
```

- `goto_pose` — head orientation in **degrees**, height `z` in **mm**, `duration` in seconds.
- `set_antennas` — `right`/`left` angles in **radians**.
- `list_emotions` / `play_emotion(name)` — default recorded emotions from
  `pollen-robotics/reachy-mini-emotions-library`. The first call may cache the
  dataset from Hugging Face. Motion plays without bundled sounds while this
  provider runs with `media_backend="no_media"`.

## Setup

Python ≥ 3.10. Using [`uv`](https://docs.astral.sh/uv/):

```bash
uv venv
source .venv/bin/activate

# Reachy SDK with simulation extras (editable from this clone keeps versions in sync)
uv pip install -e "/Users/carlid/dev/reachy_mini[mujoco]"

# SLOP SDK from PyPI (0.2 line — matches sloppy's @slop-ai/* 0.2.0)
uv pip install "slop-ai>=0.2"
```

## Demo: GUI sim + isolated sloppy (3 terminals)

Two helper scripts wrap the fiddly bits (macOS `mjpython` + config isolation).

**1 — Simulator daemon with the visible MuJoCo window:**

```bash
./run_sim_gui.sh                 # add --scene minimal for a table + objects
```

On macOS the MuJoCo viewer must run under `mjpython`; with a uv-managed Python it
also needs `DYLD_FALLBACK_LIBRARY_PATH` pointed at the interpreter's lib dir — the
script sets that for you. (Headless equivalent: `reachy-mini-daemon --sim --headless`.)

**2 — The SLOP provider** (connects to the daemon, serves the SLOP surface):

```bash
python reachy_slop_provider.py
# connects to localhost:8000, writes /tmp/slop/providers/reachy.json,
# and listens on /tmp/slop/reachy.sock
```

**3 — sloppy, with the minimal demo config** (isolated from your global `~/.sloppy`):

```bash
./run_sloppy_demo.sh             # session runtime + WebSocket + attached TUI
```

`run_sloppy_demo.sh` starts one `sloppy session serve` runtime with `HOME` pointed
at `./demo`, exposes that same runtime at `ws://127.0.0.1:8787/slop`, then attaches
the TUI to the WebSocket. It loads `./demo/.sloppy/config.yaml` and ignores your
global instance. It sets `SLOPPY_CODEX_AUTH_PATH` so your `codex login` still works
under the HOME override.

Set `SLOPPY_DIR` if your sloppy checkout isn't at `~/dev/sloppy`. The launcher uses
`src/bin/sloppy.ts` when present so local runtime changes are picked up without a
rebuild; set `SLOPPY_ENTRYPOINT=~/dev/sloppy/dist/bin/sloppy.js` to force the built
entrypoint.

WebSocket overrides:

```bash
SLOPPY_WS_PORT=8788 ./run_sloppy_demo.sh
SLOPPY_WS_ALLOW_ORIGINS=http://localhost:5173,http://127.0.0.1:5173 ./run_sloppy_demo.sh
SLOPPY_WS_TOKEN=... SLOPPY_WS_HOST=0.0.0.0 ./run_sloppy_demo.sh
```

To access the runtime from another PC on the same LAN, run the launcher on the
robot/laptop machine with LAN mode:

```bash
SLOPPY_WS_LAN=1 ./run_sloppy_demo.sh
```

LAN mode binds the WebSocket listener to `0.0.0.0`, auto-detects the LAN IP for
the advertised URL, and auto-generates a temporary token if `SLOPPY_WS_TOKEN` is
not already set. Use the printed `websocket connect url` from the other PC. If
auto-detection picks the wrong interface, override it:

```bash
SLOPPY_WS_LAN=1 SLOPPY_WS_PUBLIC_HOST=192.168.1.42 ./run_sloppy_demo.sh
```

Prompt mode (`-p` / `--prompt`) is intentionally disabled in this launcher; enter
prompts in the attached TUI so the TUI and web clients share the same runtime.

### Connecting the robot (manual, by design)

External providers are **not** auto-connected. The discovered robot shows up in the
`apps` surface as an available, unloaded app; the agent connects it via the
`apps` `load_provider` affordance. So a good first prompt is:

> *"Load the Reachy Mini app, then make it look up and to the left and wobble its antennas."*

The agent will `apps → load_provider(reachy)`, after which the `/reachy` affordances
(`goto_pose`, `set_antennas`, `wake_up`, `goto_sleep`, wobbling) become available.

- **State check** — once loaded, `query_state` / `focus_state` on `/reachy` shows live
  `head_joints` (proves the SLOP handshake: hello → subscribe → snapshot → patch).
- **Control check** — the head and antennas move in the MuJoCo window. Try
  `wake_up` / `goto_sleep` too.
- **Emotion check** — ask: "Load the Reachy Mini app, list the default emotions,
  then play a small happy/default emotion." The agent should call
  `behavior.list_emotions` before `behavior.play_emotion(name)`.

`Ctrl-C` the provider to clean up the socket and descriptor.

## Notes

- The provider runs the robot with `media_backend="no_media"`, so no GStreamer/audio
  stack is needed for Phase 1. `wake_up`/`goto_sleep` still perform their head emotes;
  their sound effects are silently skipped without audio.
- Every SDK call is blocking, so actions run it via `asyncio.to_thread` and a background
  task polls joint state into a cache — node functions never call the SDK directly.
- `goto_pose` returns only after the interpolated motion completes (it's marked
  `estimate: "slow"`).
