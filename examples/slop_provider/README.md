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
└── /behavior   actions: wake_up, goto_sleep, enable_wobbling, disable_wobbling
```

- `goto_pose` — head orientation in **degrees**, height `z` in **mm**, `duration` in seconds.
- `set_antennas` — `right`/`left` angles in **radians**.

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

## Run & verify (3 terminals)

**1 — Simulator daemon (visible MuJoCo window).** On macOS the MuJoCo GUI requires
`mjpython` rather than the `reachy-mini-daemon` console script:

```bash
mjpython -m reachy_mini.daemon.app.main --sim
# Linux: reachy-mini-daemon --sim
# add --scene minimal for a table + objects
```

**2 — The SLOP provider:**

```bash
python reachy_slop_provider.py
# connects to localhost:8000, writes /tmp/slop/providers/reachy.json,
# and listens on /tmp/slop/reachy.sock
```

**3 — sloppy:** start the agent normally. `/tmp/slop/providers` is a default discovery
path, so the `reachy` provider auto-loads (no config change). Then:

- **State check** — have the agent `query_state` / `focus_state` on `/reachy`; the
  `status` node should show live `head_joints`. This proves the SLOP handshake
  (hello → subscribe → snapshot → patch) works end to end.
- **Control check** — ask: *"look up and to the left, then wobble the antennas."* The
  head and antennas should move in the MuJoCo window. Try `wake_up` / `goto_sleep` too.

`Ctrl-C` the provider to clean up the socket and descriptor.

## Notes

- The provider runs the robot with `media_backend="no_media"`, so no GStreamer/audio
  stack is needed for Phase 1. `wake_up`/`goto_sleep` still perform their head emotes;
  their sound effects are silently skipped without audio.
- Every SDK call is blocking, so actions run it via `asyncio.to_thread` and a background
  task polls joint state into a cache — node functions never call the SDK directly.
- `goto_pose` returns only after the interpolated motion completes (it's marked
  `estimate: "slow"`).
