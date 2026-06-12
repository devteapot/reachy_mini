# Reachy Mini → SLOP provider

A small [SLOP](https://github.com/devteapot/slop) provider that wraps the Reachy Mini
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
├── /status     props: connected, mode, audio, busy, current_action,
│               power_state, listen_mode, wake_word_active, audio_gate_open,
│               head_joints, antenna_joints                            (live, ~5 Hz)
├── /head       actions: goto_pose(pitch,roll,yaw,z,duration),
│               set_pose(pitch,roll,yaw,z), set_antennas(right,left)
├── /behavior   actions: wake_up, goto_sleep, list_emotions, play_emotion,
│               enable_wobbling, disable_wobbling, stop (visible while busy)
├── /audio      props: available, volume, microphone_volume           (polled ~5 s)
│               actions: set_volume(volume), set_microphone_volume(volume),
│               test_sound, set_listen_mode(mode)
└── /camera     props: available, resolution, last_capture_at, last_capture_size
                actions: capture_frame(max_width?) — the result carries a
                content_ref with a file:// URI to the JPEG
```

- `goto_pose` — head orientation in **degrees** (+pitch looks **down**, +roll tilts
  right, +yaw turns left), height `z` in **mm**, `duration` in seconds.
- `set_pose` — same units, immediate (no interpolation); for ~10 Hz animation.
- `set_antennas` — `right`/`left` angles in **radians**.
- `set_volume` / `set_microphone_volume` — speaker/mic volume 0-100, via the
  daemon's REST volume API; current values are polled into `/audio` props every
  ~5 s so out-of-band changes show up too. `test_sound` plays a short sound
  through the provider's own audio path (the same one emotions use).
  On startup the provider sets the speaker volume to 100 (before the wake-up
  emote, so it plays at that level); the host launching the provider picks the
  value with `--initial-volume 0-100`, or keeps the daemon's current volume by
  passing a negative value.
- `list_emotions` / `play_emotion(name)` — default recorded emotions from
  `pollen-robotics/reachy-mini-emotions-library`. The first call may cache the
  dataset from Hugging Face. The move's bundled sound plays on the robot speaker
  when `/status.audio` is true (default `--media-backend local`).

**Async motion + busy state.** Long motions (`goto_pose`, `wake_up`, `goto_sleep`,
`play_emotion`) follow the SLOP async-actions extension: the invoke returns
`status: "accepted"` immediately and the motion runs in the background, so the
connection stays responsive. `/status` shows `busy` + `current_action`; completion
is signalled by a patch (busy → false) and an `action-finished` event. While busy,
`/behavior` swaps its motion affordances for `stop` (cancels a playing recorded
move), and conflicting motion invokes fail with `error.code: "conflict"`.

**Sleep/wake + microphone routing.** The provider owns the robot microphone via
an audio router (`audio_router.py`) and serves a gated mono PCM16 stream on a
Unix socket (`--audio-socket`, default `/tmp/slop/reachy_audio.sock`); sloppy's
voice plugin reads it through `audio_stream_client.py` as its `streamCommand`.
`power_state` + `listen_mode` decide what flows:

| state | motors | mic stream | wake word |
|---|---|---|---|
| `live` + `listen_mode: realtime` | on | everything | off |
| `live` + `listen_mode: wake` (default) | on | after the wake word, until end of speech | armed |
| `sleep` (after `goto_sleep`) | off | silence only | armed |

While gated, silence frames substitute 1:1 for real ones, so the consumer sees
an uninterrupted stream and no real audio leaves the provider. Saying the wake
word while asleep wakes the robot (motors + emote) and streams the speech right
after the wake word as the first utterance — *"hey jarvis, what time is it"*
works in one breath (a short pre-roll covers the detection lag). While asleep,
motion affordances are hidden/rejected and `/behavior` offers `wake_up`.
Detection runs locally via [openWakeWord](https://github.com/dscripka/openWakeWord)
(`--wake-model`, default `hey_jarvis`; pass a path to a custom `.onnx` for your
own phrase). Events: `wake-word-detected`, `power-state-changed`.

**Camera.** `capture_frame` grabs a still from the daemon's local video tee
(the same `GStreamerCamera` IPC path the SDK's `local` media backend uses — no
WebRTC decode, no contention with the daemon's own WebRTC stream), downsizes it
to `max_width` px (optional, 64-1600, default 800), and writes a JPEG to
`/tmp/slop/camera/` (a ring of the last 8, dir `0700`/files `0600`). The result
carries a `content_ref` with a `file://` URI — the consumer runs on the same
host and reads the file directly (sloppy registers it into its images
provider); SLOP has no in-protocol content fetch, and the `/camera` node
deliberately carries no ref of its own since ring pruning would leave it
dangling. Capture follows the microphone's sleep policy: while `power_state` is
`sleep` the invoke is rejected with `conflict` (asleep = deaf *and* blind).
Requires [Pillow](https://pypi.org/project/pillow/); without it (or without a
camera) `/camera.available` is `false` and `capture_frame` errors.

For a live human-facing view (debugging from a browser/iPad), don't poll
`capture_frame` — the daemon already streams WebRTC unconditionally; connect to
its `webrtcsink` signalling server on `ws://<robot-host>:8443`.

## Setup

Python ≥ 3.10. Using [`uv`](https://docs.astral.sh/uv/):

**Linux/RPi first**: the SDK depends on `pygobject` (built from source on Linux)
and uses GStreamer for audio — install the system packages from
[`docs/source/SDK/gstreamer-installation.md`](../../docs/source/SDK/gstreamer-installation.md)
Step 1 (the `apt-get install` line; the Rust WebRTC plugin in Steps 2–3 is only
needed for browser/remote streaming). macOS/Windows get GStreamer via wheels.

Then, from this directory (`examples/slop_provider/`):

```bash
uv venv
source .venv/bin/activate

# Reachy SDK, editable from this clone (add [mujoco] only if you want the simulator)
uv pip install -e ../..

# SLOP SDK from PyPI (0.2 line — matches sloppy's @slop-ai/* 0.2.0)
uv pip install "slop-ai>=0.2"

# wake-word detection (optional — without it, wake-by-voice is disabled and
# everything else still works; first run downloads the model)
uv pip install "openwakeword>=0.6" onnxruntime

# camera capture (optional — without it /camera reports available: false and
# everything else still works)
uv pip install pillow
```

On **Linux with Python ≥ 3.12** (e.g. the Pi) that last install fails:
openwakeword hard-depends on `tflite-runtime` there, which has no wheels past
cp311. The provider only uses the onnx path, so drop the dep with an override:

```bash
echo "tflite-runtime; sys_platform == 'never'" > /tmp/oww-override.txt
uv pip install --override /tmp/oww-override.txt "openwakeword>=0.6" onnxruntime
```

If GStreamer audio can't initialise at provider startup, it falls back to
`no_media` (motion works, sounds are skipped, `/status.audio` is `false`).

## Physical robot (USB / Lite): one script

For a real robot connected over USB, one launcher starts both the daemon
(serial port auto-detect, headless) and the SLOP provider, and stops the daemon
again on Ctrl-C:

```bash
./run_robot_usb.sh                # extra args go to the daemon
```

If a daemon is already running on `:8000` it is reused (and left running on
exit). The **provider** wakes the robot on start and puts it to sleep on Ctrl-C —
with their emote sounds, through its own audio path (the daemon's sounds need the
GStreamer Rust webrtc plugin, so the launcher starts the daemon with
`--no-wake-up-on-start`). Use the provider's `--no-wake-on-start` /
`--no-sleep-on-exit` flags to opt out. `/status.mode` will report `real`.
Then start sloppy as in step 3 below.

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

- The provider defaults to `--media-backend local`: emotion moves and the
  `wake_up`/`goto_sleep` emotes play their bundled sounds on the robot speaker via
  GStreamer. If GStreamer isn't available (e.g. a sim-only machine) it falls back
  to `no_media` automatically — motion still works, sounds are skipped, and
  `/status.audio` reports `false`. Force it with `--media-backend no_media`.
- Every SDK call is blocking, so actions run it via `asyncio.to_thread` and a background
  task polls joint state into a cache — node functions never call the SDK directly.
- The discovery descriptor is written per the spec's hardening rules: `0700`
  providers dir, `0600` file, atomic temp-file + rename, and a `pid` field so
  consumers can detect a stale descriptor after a crash.
- `mode` in `/status` is read from `GET /api/daemon/status` at startup
  (`sim` / `real` / `unknown`).
