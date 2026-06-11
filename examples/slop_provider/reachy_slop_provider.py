#!/usr/bin/env python3
"""SLOP provider that wraps the Reachy Mini SDK.

Exposes the robot as a SLOP state tree with affordances, served over a Unix
domain socket using NDJSON. A SLOP consumer (e.g. the `sloppy` runtime) connects
to the socket, observes joint state, and invokes movement/behavior affordances.

Discovery: on startup this writes a descriptor to ``/tmp/slop/providers/reachy.json``
(a default sloppy discovery path) and removes it on exit, so sloppy auto-loads the
provider with no config changes. The descriptor and its directory follow the spec's
filesystem hardening rules (0700 dir, 0600 file, atomic rename, pid for staleness).

Scope: movement + behaviors. Emotion/emote sounds play through the robot's
speaker when the SDK's local GStreamer audio backend is available (the default,
``--media-backend local``). Pass ``--media-backend no_media`` to run without any
GStreamer/audio stack (e.g. sim on a machine without GStreamer); if the local
backend fails to initialise, the provider falls back to ``no_media`` on its own.

State tree::

    /reachy
    ├── /status     props: connected, mode, busy, current_action,
    │               head_joints, antenna_joints  (live, polled)
    ├── /head       actions: goto_pose, set_pose, set_antennas
    └── /behavior   actions: wake_up, goto_sleep, list_emotions, play_emotion,
                    enable_wobbling, disable_wobbling, stop (visible while busy)

Long-running motion (goto_pose, wake_up, goto_sleep, play_emotion) is async per
the SLOP async-actions extension: the invoke returns ``status: "accepted"``
immediately, ``/status`` shows ``busy`` + ``current_action``, and an
``action-finished`` event is emitted on completion. While busy, ``/behavior``
swaps its motion affordances for ``stop`` (dynamic affordances), and any
conflicting motion invoke fails with ``code: "conflict"``.

Run::

    # 1. start the simulator daemon (visible MuJoCo GUI; macOS needs mjpython):
    #    mjpython -m reachy_mini.daemon.app.main --sim
    # 2. python reachy_slop_provider.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import threading
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable

from reachy_mini import ReachyMini
from reachy_mini.motion.recorded_move import RecordedMoves
from reachy_mini.utils import create_head_pose
from slop_ai import SlopServer
from slop_ai.transports.unix import listen as listen_unix

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("reachy-slop")

DEFAULT_SOCKET = "/tmp/slop/reachy.sock"
DEFAULT_DESCRIPTOR = "/tmp/slop/providers/reachy.json"
DEFAULT_EMOTIONS_DATASET = "pollen-robotics/reachy-mini-emotions-library"
DAEMON_STATUS_URL = "http://localhost:8000/api/daemon/status"
POLL_INTERVAL_S = 0.2  # ~5 Hz state poll
JOINT_ROUNDING = 4  # decimals — stabilises float jitter so we don't emit constant patches

# Must mirror the hello message sent by slop_ai's SlopServer.handle_connection,
# so discovery and handshake advertise the same surface.
SLOP_CAPABILITIES = [
    "state",
    "patches",
    "affordances",
    "attention",
    "windowing",
    "async",
    "content_refs",
]


class SlopActionError(Exception):
    """Action failure carrying a SLOP result error code.

    slop_ai reads the ``code`` attribute off raised exceptions and uses it as
    ``result.error.code`` (falling back to ``internal``).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecordedMoveLibrary:
    """Lazy loader for a Hugging Face recorded-move dataset."""

    def __init__(self, dataset_name: str) -> None:
        self.dataset_name = dataset_name
        self._moves: RecordedMoves | None = None
        self._lock = threading.Lock()

    def load(self) -> RecordedMoves:
        with self._lock:
            if self._moves is None:
                logger.info("loading recorded moves dataset: %s", self.dataset_name)
                self._moves = RecordedMoves(self.dataset_name)
            return self._moves

    def list_moves(self) -> list[str]:
        return sorted(self.load().list_moves())


class RobotState:
    """Cache of the latest robot readings, updated by the background poll task.

    SLOP ``@node`` functions read from here (cheap, non-blocking). They must NOT
    call the synchronous ReachyMini SDK directly — every SDK read is a network
    round-trip that would stall the asyncio socket server.
    """

    def __init__(self) -> None:
        self.connected: bool = False
        self.mode: str = "unknown"  # "sim" | "real" | "unknown" (from the daemon status API)
        self.audio: bool = False  # whether the SDK client has a working audio backend
        self.busy_action: str | None = None  # name of the in-flight long motion, if any
        self.head_joints: list[float] = []
        self.antenna_joints: list[float] = []
        # Last orientation we commanded (degrees / mm), for the /head node props.
        self.last_pose: dict[str, float] = {"pitch": 0.0, "roll": 0.0, "yaw": 0.0, "z": 0.0}

    def snapshot_key(self) -> tuple:
        """Value used to decide whether a poll produced a visible change."""
        return (self.connected, tuple(self.head_joints), tuple(self.antenna_joints))


def build_server(mini: ReachyMini, state: RobotState) -> SlopServer:
    """Construct the SLOP server: nodes read the cache, actions drive the robot."""
    slop = SlopServer("reachy", "Reachy Mini")
    emotions = RecordedMoveLibrary(DEFAULT_EMOTIONS_DATASET)
    background_tasks: set[asyncio.Task] = set()

    # --- Busy / conflict policy ---------------------------------------------------
    #
    # One long motion at a time. Long actions go through start_motion(): the invoke
    # is answered "accepted" immediately and the motion runs in a background task,
    # so the connection's message loop stays responsive (the consumer can keep
    # querying state and can barge in with behavior/stop). While a motion runs,
    # the node descriptors hide conflicting affordances (spec: presence is the
    # signal) and any conflicting invoke that still arrives fails with "conflict".

    def ensure_not_busy(action: str) -> None:
        if state.busy_action is not None:
            raise SlopActionError(
                "conflict",
                f"Robot is busy with {state.busy_action!r}. Wait for the "
                "action-finished event (or status.busy == false), or cancel a "
                "playing move with the behavior 'stop' affordance.",
            )

    def start_motion(name: str, work_factory: Callable[[], Awaitable[None]]) -> dict[str, Any]:
        ensure_not_busy(name)
        state.busy_action = name

        async def runner() -> None:
            ok, error = True, None
            try:
                await work_factory()
            except Exception as e:
                ok, error = False, str(e)
                logger.warning("background action %r failed", name, exc_info=True)
            finally:
                state.busy_action = None
                slop.refresh()  # re-show hidden affordances, flip status.busy
                event: dict[str, Any] = {"action": name, "ok": ok}
                if error:
                    event["error"] = error
                slop.emit_event("action-finished", event)

        task = asyncio.get_running_loop().create_task(runner())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        # slop_ai turns "__async": True into result status "accepted".
        return {"__async": True, "action": name}

    # --- Nodes (read cache only) ------------------------------------------------
    #
    # Defined here but registered at the bottom of build_server, AFTER the action
    # decorators: a node descriptor that declares an explicit "actions" subset
    # (behavior_node) is merged against already-registered action handlers on
    # every rebuild, and @slop.node triggers a rebuild immediately — registering
    # the node first would reference handlers that don't exist yet.

    def status_node() -> dict[str, Any]:
        desc: dict[str, Any] = {
            "type": "context",
            "props": {
                "connected": state.connected,
                "mode": state.mode,
                "audio": state.audio,
                "busy": state.busy_action is not None,
                "current_action": state.busy_action,
                "head_joints": state.head_joints,
                "antenna_joints": state.antenna_joints,
            },
            "summary": (
                f"Reachy Mini ({state.mode}). Live head + antenna joint positions, in radians. "
                "head_joints = [body_yaw, stewart_1..6]; antenna_joints = [right, left]. "
                "busy/current_action track in-flight long motions."
            ),
        }
        if not state.connected:
            desc["meta"] = {
                "salience": 1.0,
                "reason": "Robot daemon is unreachable — motion affordances will fail.",
            }
        return desc

    def head_node() -> dict[str, Any]:
        # Head affordances stay visible while a long motion plays (the SDK's
        # descriptor/decorator merge cannot express an empty action set);
        # conflicting invokes are rejected with code "conflict" instead.
        return {
            "type": "control",
            "props": {"last_commanded_pose": state.last_pose},
            "summary": (
                "Head orientation and antennas. Use goto_pose to look around (angles in "
                "degrees, height in mm); use set_pose for real-time animation; "
                "use set_antennas to move the ears (radians). While status.busy is "
                "true these return a conflict error."
            ),
        }

    def behavior_node() -> dict[str, Any]:
        desc: dict[str, Any] = {
            "type": "control",
            "summary": (
                "High-level behaviors: wake_up and goto_sleep emotes, default recorded "
                "emotion moves, and audio-reactive head wobbling (visible motion only "
                "when audio is playing)."
            ),
        }
        if state.busy_action is not None:
            # While a motion plays: only non-motion actions, plus stop to cancel it.
            desc["actions"] = {
                "stop": {},
                "list_emotions": {},
                "enable_wobbling": {},
                "disable_wobbling": {},
            }
        else:
            # Idle: everything except stop (nothing to cancel).
            desc["actions"] = {
                "wake_up": {},
                "goto_sleep": {},
                "list_emotions": {},
                "play_emotion": {},
                "enable_wobbling": {},
                "disable_wobbling": {},
            }
        return desc

    # --- Affordances (drive the robot; SDK calls run off the event loop) --------

    @slop.action(
        "head",
        "goto_pose",
        params={
            "pitch": {
                "type": "number",
                "description": "Head pitch in degrees, range ±40 (+ looks down, - looks up).",
            },
            "roll": {
                "type": "number",
                "description": "Head roll in degrees, range ±40 (+ tilts right).",
            },
            "yaw": {
                "type": "number",
                "description": "Head yaw in degrees, range ±180 (+ turns left).",
            },
            "z": {"type": "number", "description": "Head vertical offset in millimetres."},
            "duration": {"type": "number", "description": "Movement duration in seconds."},
        },
        label="Move head to pose",
        description=(
            "Move the head to an absolute orientation (degrees) and height (mm), "
            "smoothly interpolated over `duration` seconds. All params are required; "
            "out-of-range angles are clamped by the SDK. Returns 'accepted' and runs "
            "in the background — watch status.busy / the action-finished event."
        ),
        estimate="async",
    )
    async def goto_pose(
        pitch: float, roll: float, yaw: float, z: float, duration: float
    ) -> dict[str, Any]:
        pose = create_head_pose(z=z, roll=roll, pitch=pitch, yaw=yaw, mm=True, degrees=True)

        async def work() -> None:
            await asyncio.to_thread(mini.goto_target, head=pose, duration=duration)
            state.last_pose = {"pitch": pitch, "roll": roll, "yaw": yaw, "z": z}

        result = start_motion("goto_pose", work)
        result["pose"] = {"pitch": pitch, "roll": roll, "yaw": yaw, "z": z}
        return result

    @slop.action(
        "head",
        "set_pose",
        params={
            "pitch": {
                "type": "number",
                "description": "Head pitch in degrees, range ±40 (+ looks down, - looks up).",
            },
            "roll": {
                "type": "number",
                "description": "Head roll in degrees, range ±40 (+ tilts right).",
            },
            "yaw": {
                "type": "number",
                "description": "Head yaw in degrees, range ±180 (+ turns left).",
            },
            "z": {"type": "number", "description": "Head vertical offset in millimetres."},
        },
        label="Set head pose",
        description=(
            "Set the head orientation (degrees) and height (mm) immediately, without "
            "interpolation. Fast and non-blocking — intended for real-time animation "
            "(e.g. talking motion) at ~10 Hz, unlike goto_pose which interpolates. "
            "All params are required; out-of-range angles are clamped by the SDK."
        ),
        estimate="fast",
    )
    async def set_pose(pitch: float, roll: float, yaw: float, z: float) -> dict[str, Any]:
        ensure_not_busy("set_pose")
        pose = create_head_pose(z=z, roll=roll, pitch=pitch, yaw=yaw, mm=True, degrees=True)
        await asyncio.to_thread(mini.set_target, head=pose)
        state.last_pose = {"pitch": pitch, "roll": roll, "yaw": yaw, "z": z}
        # slop_ai auto-rebuilds the tree after every invoke, so each call emits a
        # small last_commanded_pose patch; at ~10 Hz that is within the spec's
        # 50-100 ms coalescing guidance.
        return {"ok": True, "pose": state.last_pose}

    @slop.action(
        "head",
        "set_antennas",
        params={
            "right": {"type": "number", "description": "Right antenna angle in radians."},
            "left": {"type": "number", "description": "Left antenna angle in radians."},
        },
        label="Set antennas",
        description="Set the right and left antenna angles (radians). Both params required.",
        estimate="fast",
    )
    async def set_antennas(right: float, left: float) -> dict[str, Any]:
        ensure_not_busy("set_antennas")
        await asyncio.to_thread(mini.set_target, antennas=[right, left])
        return {"ok": True, "antennas": [right, left]}

    @slop.action("behavior", "wake_up", label="Wake up", estimate="async")
    async def wake_up() -> dict[str, Any]:
        async def work() -> None:
            await asyncio.to_thread(mini.wake_up)

        return start_motion("wake_up", work)

    @slop.action("behavior", "goto_sleep", label="Go to sleep", estimate="async")
    async def goto_sleep() -> dict[str, Any]:
        async def work() -> None:
            await asyncio.to_thread(mini.goto_sleep)

        return start_motion("goto_sleep", work)

    @slop.action(
        "behavior",
        "list_emotions",
        label="List emotions",
        description=(
            "List recorded emotion move names from the default Reachy Mini emotions "
            "library. The first call may download/cache the dataset."
        ),
        estimate="slow",
    )
    async def list_emotions() -> dict[str, Any]:
        moves = await asyncio.to_thread(emotions.list_moves)
        return {"ok": True, "dataset": emotions.dataset_name, "emotions": moves}

    @slop.action(
        "behavior",
        "play_emotion",
        params={
            "name": {
                "type": "string",
                "description": "Emotion move name returned by list_emotions.",
            },
        },
        label="Play emotion",
        description=(
            "Play one recorded move from the default Reachy Mini emotions library. "
            "The robot first moves to the recording's initial pose over 1 second; "
            "the move's bundled sound plays when status.audio is true. "
            "Returns 'accepted' and plays in the background — cancel with stop, "
            "watch status.busy / the action-finished event."
        ),
        estimate="async",
    )
    async def play_emotion(name: str) -> dict[str, Any]:
        ensure_not_busy("play_emotion")  # fail fast before the (possibly slow) validation
        library = await asyncio.to_thread(emotions.load)
        available = sorted(library.list_moves())
        if name not in available:
            raise SlopActionError(
                "invalid_params",
                f"Unknown emotion {name!r}. Available emotions: {', '.join(available)}",
            )
        move = await asyncio.to_thread(library.get, name)

        async def work() -> None:
            await mini.async_play_move(move, initial_goto_duration=1.0, sound=state.audio)

        result = start_motion("play_emotion", work)
        result.update({"emotion": name, "dataset": emotions.dataset_name})
        return result

    @slop.action(
        "behavior",
        "stop",
        label="Stop current motion",
        description=(
            "Cancel the currently playing recorded move (play_emotion, or the "
            "wake_up/goto_sleep emotes). An in-flight goto_pose interpolation "
            "completes on its own. Only offered while a motion is playing."
        ),
        idempotent=True,
        estimate="instant",
    )
    async def stop() -> dict[str, Any]:
        cancelled = state.busy_action
        await asyncio.to_thread(mini.cancel_move)
        return {"ok": True, "cancelled": cancelled}

    @slop.action("behavior", "enable_wobbling", label="Enable wobbling", estimate="instant")
    async def enable_wobbling() -> dict[str, Any]:
        await asyncio.to_thread(mini.enable_wobbling)
        return {"ok": True}

    @slop.action("behavior", "disable_wobbling", label="Disable wobbling", estimate="instant")
    async def disable_wobbling() -> dict[str, Any]:
        await asyncio.to_thread(mini.disable_wobbling)
        return {"ok": True}

    # Register nodes last — see the comment above status_node.
    slop.node("status")(status_node)
    slop.node("head")(head_node)
    slop.node("behavior")(behavior_node)

    return slop


async def poll_state(mini: ReachyMini, state: RobotState, slop: SlopServer) -> None:
    """Refresh the cached joint state ~5 Hz; emit patches only when it changes."""
    while True:
        try:
            head_joints, antenna_joints = await asyncio.to_thread(
                mini.get_current_joint_positions
            )
            prev = state.snapshot_key()
            state.connected = True
            state.head_joints = [round(float(v), JOINT_ROUNDING) for v in head_joints]
            state.antenna_joints = [round(float(v), JOINT_ROUNDING) for v in antenna_joints]
            if state.snapshot_key() != prev:
                slop.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("state poll failed", exc_info=True)
            if state.connected:
                # Broadcast the disconnect once; stay quiet while it lasts.
                state.connected = False
                slop.refresh()
        await asyncio.sleep(POLL_INTERVAL_S)


def fetch_daemon_mode(status_url: str = DAEMON_STATUS_URL) -> str:
    """Ask the daemon whether it runs a simulated or a real robot."""
    try:
        with urllib.request.urlopen(status_url, timeout=5) as resp:
            status = json.loads(resp.read())
        if status.get("simulation_enabled") or status.get("mockup_sim_enabled"):
            return "sim"
        return "real"
    except Exception:
        logger.warning("could not read daemon status from %s", status_url, exc_info=True)
        return "unknown"


def write_descriptor(path: str, socket_path: str) -> None:
    """Write the discovery descriptor per spec/core/transport.md §Local discovery.

    Hardening (mirrors slop_ai.transports.unix._register_provider): the providers
    directory is 0700, the descriptor is 0600 and written atomically via a
    same-directory temp file + rename, and includes pid for stale detection.
    """
    descriptor = {
        "id": "reachy",
        "name": "Reachy Mini",
        "slop_version": "0.1",
        "transport": {"type": "unix", "path": socket_path},
        "pid": os.getpid(),
        "capabilities": SLOP_CAPABILITIES,
    }
    final_path = Path(path)
    providers_dir = final_path.parent
    providers_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(providers_dir, 0o700)
    tmp_path = providers_dir / f"{final_path.name}.tmp.{os.getpid()}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(descriptor, indent=2))
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, final_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    logger.info("wrote discovery descriptor: %s", path)


async def run(socket_path: str, descriptor_path: str, media_backend: str) -> None:
    logger.info("connecting to Reachy Mini daemon (media_backend=%s)...", media_backend)
    try:
        mini = ReachyMini(media_backend=media_backend)
    except Exception:
        if media_backend == "no_media":
            raise
        logger.warning(
            "media backend %r failed to initialise (GStreamer missing?); "
            "falling back to no_media — emotion sounds will be skipped",
            media_backend,
            exc_info=True,
        )
        mini = ReachyMini(media_backend="no_media")
    logger.info("connected.")

    state = RobotState()
    state.audio = getattr(mini.media_manager, "audio", None) is not None
    state.mode = await asyncio.to_thread(fetch_daemon_mode)
    logger.info("mode=%s audio=%s", state.mode, state.audio)
    slop = build_server(mini, state)

    server = await listen_unix(slop, socket_path)
    write_descriptor(descriptor_path, socket_path)
    logger.info("SLOP provider listening on unix:%s (mode=%s)", socket_path, state.mode)

    poll_task = asyncio.create_task(poll_state(mini, state, slop))

    # Shut down cleanly on SIGINT/SIGTERM.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (e.g. Windows)
            pass

    try:
        await stop.wait()
    finally:
        logger.info("shutting down...")
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()
        slop.stop()
        Path(descriptor_path).unlink(missing_ok=True)
        Path(socket_path).unlink(missing_ok=True)
        try:
            mini.media_manager.close()
            mini.client.disconnect()
        except Exception:
            logger.debug("error during robot disconnect", exc_info=True)
        logger.info("done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reachy Mini SLOP provider")
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="Unix socket path to serve on")
    parser.add_argument(
        "--descriptor", default=DEFAULT_DESCRIPTOR, help="Discovery descriptor path to write"
    )
    parser.add_argument(
        "--media-backend",
        default="local",
        choices=["local", "no_media", "webrtc"],
        help=(
            "SDK media backend for the provider's robot client. 'local' (default) "
            "plays emotion/emote sounds on the robot speaker via GStreamer and "
            "falls back to 'no_media' if it can't initialise."
        ),
    )
    args = parser.parse_args()
    asyncio.run(run(args.socket, args.descriptor, args.media_backend))


if __name__ == "__main__":
    main()
