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
    ├── /status     props: connected, mode, audio, busy, current_action,
    │               power_state, listen_mode, wake_word_active, audio_gate_open,
    │               head_joints, antenna_joints  (live, polled)
    ├── /head       actions: goto_pose, set_pose, set_antennas
    ├── /behavior   props: emotions (move names, prefetched at startup)
    │               actions: wake_up, goto_sleep, list_emotions, play_emotion,
    │               enable_wobbling, disable_wobbling, stop (visible while busy)
    ├── /audio      props: available, volume, microphone_volume  (polled ~5 s)
    │               actions: set_volume, set_microphone_volume, test_sound,
    │               set_listen_mode
    └── /camera     props: available, resolution, last capture metadata
                    actions: capture_frame — result carries a content_ref
                    (file:// JPEG); rejected while asleep

Power states (see ``audio_router.py`` for the microphone side):

- ``live`` + listen_mode ``realtime``: open mic — every frame streams to the
  consumer's voice pipeline.
- ``live`` + listen_mode ``wake``: the mic stream stays silent until the wake
  word is heard, then flows until end of speech.
- ``sleep`` (after goto_sleep): motors off, mic stream silent, wake word armed.
  Saying the wake word (or invoking wake_up) brings the robot back to live;
  a voice wake forwards the speech right after the wake word as the first
  utterance and lands in listen_mode ``wake``.

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
import io
import json
import logging
import os
import signal
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import numpy as np

try:  # camera capture needs Pillow for JPEG encode + downscale (see README)
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover — capture degrades to unavailable
    PILImage = None

from reachy_mini import ReachyMini
from reachy_mini.motion.recorded_move import RecordedMoves
from reachy_mini.utils import create_head_pose
from slop_ai import SlopServer
from slop_ai.transports.unix import listen as listen_unix

from audio_router import (
    DEFAULT_SOCKET as DEFAULT_AUDIO_SOCKET,
    DEFAULT_WAKE_MODEL,
    AudioRouter,
    AudioRouterConfig,
    RouterMode,
    WakeEvent,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("reachy-slop")

DEFAULT_SOCKET = "/tmp/slop/reachy.sock"
DEFAULT_DESCRIPTOR = "/tmp/slop/providers/reachy.json"
DEFAULT_EMOTIONS_DATASET = "pollen-robotics/reachy-mini-emotions-library"
DAEMON_API_BASE = "http://localhost:8000/api"
DAEMON_STATUS_URL = f"{DAEMON_API_BASE}/daemon/status"
POLL_INTERVAL_S = 0.2  # ~5 Hz state poll
VOLUME_POLL_EVERY = 25  # poll volume every Nth state poll (~5 s) — it rarely changes
DEFAULT_INITIAL_VOLUME = 100  # speaker volume applied at startup (host-configurable)
JOINT_ROUNDING = 4  # decimals — stabilises float jitter so we don't emit constant patches

# Camera captures: JPEGs written to a small ring on disk; the consumer resolves
# the returned file:// content_ref (provider and sloppy share the host).
DEFAULT_FRAMES_DIR = "/tmp/slop/camera"
FRAME_RING_SIZE = 8
CAPTURE_MAX_DIM_DEFAULT = 800  # px — plenty for a vision model, cheap to encode
CAPTURE_MAX_DIM_RANGE = (64, 1600)
CAPTURE_JPEG_QUALITY = 80
CAPTURE_READ_ATTEMPTS = 3  # GStreamerCamera.read() returns None on appsink timeout
CAPTURE_READ_RETRY_S = 0.2

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
        self.camera: bool = False  # camera backend AND Pillow both available
        self.camera_resolution: list[int] | None = None  # [width, height] px
        # Metadata of the newest captured frame (path, width, height, size_bytes,
        # captured_at). Not in snapshot_key(): only capture_frame changes it, and
        # that action calls slop.refresh() itself.
        self.last_capture: dict[str, Any] | None = None
        # Emotion move names, filled once by the startup prefetch (None until
        # loaded). Not in snapshot_key(): prefetch_emotions refreshes explicitly.
        self.emotions: list[str] | None = None
        self.busy_action: str | None = None  # name of the in-flight long motion, if any
        self.power_state: str = "live"  # "live" | "sleep" — drives motion guards + mic gating
        self.listen_mode: str = "wake"  # "realtime" | "wake" — mic routing while live
        self.wake_word_active: bool = False  # router armed AND wake model loaded
        self.gate_open: bool = False  # audio currently flowing to the consumer
        self.head_joints: list[float] = []
        self.antenna_joints: list[float] = []
        # Speaker / mic volume (0-100) from the daemon volume API; None = unknown.
        self.volume: int | None = None
        self.microphone_volume: int | None = None
        # Last orientation we commanded (degrees / mm), for the /head node props.
        self.last_pose: dict[str, float] = {"pitch": 0.0, "roll": 0.0, "yaw": 0.0, "z": 0.0}

    def snapshot_key(self) -> tuple:
        """Value used to decide whether a poll produced a visible change."""
        return (
            self.connected,
            tuple(self.head_joints),
            tuple(self.antenna_joints),
            self.volume,
            self.microphone_volume,
            self.power_state,
            self.listen_mode,
            self.wake_word_active,
            self.gate_open,
        )


def build_server(
    mini: ReachyMini,
    state: RobotState,
    router: AudioRouter | None = None,
    emotions: RecordedMoveLibrary | None = None,
) -> SlopServer:
    """Construct the SLOP server: nodes read the cache, actions drive the robot."""
    slop = SlopServer("reachy", "Reachy Mini")
    emotions = emotions or RecordedMoveLibrary(DEFAULT_EMOTIONS_DATASET)
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

    def ensure_awake(action: str) -> None:
        if state.power_state == "sleep":
            raise SlopActionError(
                "conflict",
                f"Robot is asleep (motor torque off) — {action} is unavailable. "
                "Invoke the behavior 'wake_up' affordance first.",
            )

    def ensure_camera(action: str) -> None:
        if not state.camera:
            raise SlopActionError(
                "conflict",
                "Camera is unavailable (no media backend camera, or Pillow is "
                "not installed in the provider environment).",
            )
        if state.power_state == "sleep":
            # Same policy as the muted microphone: asleep means deaf AND blind.
            raise SlopActionError(
                "conflict",
                f"Robot is asleep — {action} is unavailable. "
                "Invoke the behavior 'wake_up' affordance first.",
            )

    # The camera appsink keeps a single buffer (drop=true, max-buffers=1):
    # concurrent reads race for it and one gets None. Serialize captures.
    capture_lock = asyncio.Lock()

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

    # --- Power state (sleep/live) + microphone routing -----------------------------
    #
    # power_state gates motion (sleep = torque off, so motion affordances are
    # rejected) and drives the audio router: asleep the mic stream is muted with
    # the wake word armed; live it follows listen_mode (open mic vs wake-gated).
    # See audio_router.py. Without a router (stub runs, --no-audio-router, no
    # audio backend) the state machine still works — only voice wake is missing.

    def apply_audio_state() -> None:
        if router is None:
            return
        if state.power_state == "sleep":
            router.set_mode(RouterMode.MUTED)
        elif state.listen_mode == "realtime":
            router.set_mode(RouterMode.REALTIME)
        else:
            router.set_mode(RouterMode.WAKE)

    def set_power_state(value: str) -> None:
        if state.power_state == value:
            return
        state.power_state = value
        apply_audio_state()
        slop.emit_event("power-state-changed", {"power_state": value})
        slop.refresh()

    def wake_motion_work() -> Callable[[], Awaitable[None]]:
        async def work() -> None:
            # Mirror the daemon's wake sequence: torque on, then the emote.
            await asyncio.to_thread(mini.enable_motors)
            await asyncio.to_thread(mini.wake_up)

        return work

    def on_wake_word(event: WakeEvent) -> None:
        """Router callback (asyncio loop): the wake word was just heard.

        The router has already opened its gate from the capture thread, so the
        speech following the wake word is streaming regardless of how long the
        wake motion takes.
        """
        was_asleep = state.power_state == "sleep"
        slop.emit_event(
            "wake-word-detected",
            {"model": event.model, "score": round(event.score, 3), "asleep": was_asleep},
        )
        if not was_asleep:
            slop.refresh()  # gate_open changed; nothing else to do while live
            return
        logger.info("voice wake from sleep (score=%.2f)", event.score)
        state.power_state = "live"
        state.listen_mode = "wake"  # one-breath wake lands in the gated mode
        slop.emit_event("power-state-changed", {"power_state": "live"})
        try:
            start_motion("wake_up", wake_motion_work())
        except SlopActionError:
            # A motion is somehow in flight; stay live without the emote.
            logger.info("skipping wake emote: %s", state.busy_action)
        slop.refresh()

    if router is not None:
        router.set_wake_handler(on_wake_word)

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
                "power_state": state.power_state,
                "listen_mode": state.listen_mode,
                "wake_word_active": state.wake_word_active,
                "audio_gate_open": state.gate_open,
                "head_joints": state.head_joints,
                "antenna_joints": state.antenna_joints,
            },
            "summary": (
                f"Reachy Mini ({state.mode}). Live head + antenna joint positions, in radians. "
                "head_joints = [body_yaw, stewart_1..6]; antenna_joints = [right, left]. "
                "busy/current_action track in-flight long motions. While power_state "
                "is 'sleep', motors are off and motion affordances are rejected; the "
                "microphone is muted with the wake word armed (wake_word_active). "
                "listen_mode 'wake' streams mic audio only after the wake word "
                "(audio_gate_open shows when it flows); 'realtime' streams everything."
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
                "when audio is playing). After goto_sleep, motor torque is off — head "
                "and antenna commands will not move the robot until wake_up. "
                "Available emotion names are listed in props.emotions once loaded; "
                "play_emotion accepts an optional boolean sound param (default true)."
            ),
        }
        if state.emotions is not None:
            desc["props"] = {"emotions": state.emotions}
        if state.busy_action is not None:
            # While a motion plays: only non-motion actions, plus stop to cancel it.
            desc["actions"] = {
                "stop": {},
                "list_emotions": {},
                "enable_wobbling": {},
                "disable_wobbling": {},
            }
        elif state.power_state == "sleep":
            # Asleep: torque is off, so only waking (and harmless reads) make sense.
            desc["actions"] = {
                "wake_up": {},
                "list_emotions": {},
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
        ensure_awake("goto_pose")
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
        ensure_awake("set_pose")
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
        ensure_awake("set_antennas")
        ensure_not_busy("set_antennas")
        await asyncio.to_thread(mini.set_target, antennas=[right, left])
        return {"ok": True, "antennas": [right, left]}

    @slop.action(
        "behavior",
        "wake_up",
        label="Wake up",
        description=(
            "Enable motor torque and play the wake-up emote (with sound). From "
            "sleep this also restores microphone streaming (per listen_mode)."
        ),
        estimate="async",
    )
    async def wake_up() -> dict[str, Any]:
        result = start_motion("wake_up", wake_motion_work())
        set_power_state("live")
        return result

    @slop.action(
        "behavior",
        "goto_sleep",
        label="Go to sleep",
        description=(
            "Play the sleep emote (with sound) and disable motor torque. The "
            "microphone stream mutes immediately, with the wake word armed — the "
            "robot stays asleep until wake_up is invoked or the wake word is heard."
        ),
        estimate="async",
    )
    async def goto_sleep() -> dict[str, Any]:
        ensure_not_busy("goto_sleep")  # don't mute the mic on a doomed invoke

        async def work() -> None:
            # Mirror the daemon's sleep sequence: torque on, sleep pose, torque off.
            await asyncio.to_thread(mini.enable_motors)
            await asyncio.to_thread(mini.goto_sleep)
            await asyncio.to_thread(mini.disable_motors)

        result = start_motion("goto_sleep", work)
        # Mute before the emote finishes: once sleep is requested, no real audio
        # leaves the robot (the wake-word veto for the emote itself is in
        # allow_wake, keyed on busy_action).
        set_power_state("sleep")
        return result

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

    # sound is intentionally NOT in the params schema — slop_ai marks every
    # declared param as required (same constraint as capture_frame's max_width).
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
            "the move's bundled sound plays when status.audio is true. Accepts an "
            "optional boolean param sound (default true) — pass false to play the "
            "motion silently, e.g. while other audio is speaking. "
            "Returns 'accepted' and plays in the background — cancel with stop, "
            "watch status.busy / the action-finished event."
        ),
        estimate="async",
    )
    async def play_emotion(name: str, sound: bool = True) -> dict[str, Any]:
        ensure_awake("play_emotion")
        ensure_not_busy("play_emotion")  # fail fast before the (possibly slow) validation
        library = await asyncio.to_thread(emotions.load)
        available = sorted(library.list_moves())
        if name not in available:
            raise SlopActionError(
                "invalid_params",
                f"Unknown emotion {name!r}. Available emotions: {', '.join(available)}",
            )
        move = await asyncio.to_thread(library.get, name)
        effective_sound = bool(sound) and state.audio

        async def work() -> None:
            await mini.async_play_move(move, initial_goto_duration=1.0, sound=effective_sound)

        result = start_motion("play_emotion", work)
        result.update({"emotion": name, "dataset": emotions.dataset_name, "sound": effective_sound})
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
        ensure_awake("enable_wobbling")
        await asyncio.to_thread(mini.enable_wobbling)
        return {"ok": True}

    @slop.action("behavior", "disable_wobbling", label="Disable wobbling", estimate="instant")
    async def disable_wobbling() -> dict[str, Any]:
        await asyncio.to_thread(mini.disable_wobbling)
        return {"ok": True}

    # --- Audio node + affordances (volume via the daemon REST API) --------------

    def audio_node() -> dict[str, Any]:
        desc: dict[str, Any] = {
            "type": "control",
            "props": {
                "available": state.audio,
                "volume": state.volume,
                "microphone_volume": state.microphone_volume,
            },
            "summary": (
                "Robot speaker and microphone. Volumes are 0-100, controlled through "
                "the daemon. set_volume also plays a short confirmation sound; "
                "test_sound checks the speaker without changing anything."
                + (
                    " set_listen_mode picks how the mic streams while live: "
                    "'realtime' (open mic) or 'wake' (only after the wake word)."
                    if router is not None
                    else ""
                )
            ),
        }
        actions = {"set_volume": {}, "set_microphone_volume": {}, "test_sound": {}}
        if router is not None:
            actions["set_listen_mode"] = {}
        desc["actions"] = actions
        return desc

    @slop.action(
        "audio",
        "set_volume",
        params={"volume": {"type": "integer", "description": "Speaker volume, 0-100."}},
        label="Set speaker volume",
        description=(
            "Set the robot speaker volume (0-100). The daemon plays a short "
            "confirmation sound at the new level."
        ),
        estimate="fast",
    )
    async def set_volume(volume: int) -> dict[str, Any]:
        if not 0 <= volume <= 100:
            raise SlopActionError("invalid_params", "volume must be between 0 and 100")
        data = await asyncio.to_thread(_daemon_post, "/volume/set", {"volume": volume})
        state.volume = int(data.get("volume", volume))
        return {"ok": True, "volume": state.volume}

    @slop.action(
        "audio",
        "set_microphone_volume",
        params={"volume": {"type": "integer", "description": "Microphone volume, 0-100."}},
        label="Set microphone volume",
        description="Set the robot microphone input volume (0-100).",
        estimate="fast",
    )
    async def set_microphone_volume(volume: int) -> dict[str, Any]:
        if not 0 <= volume <= 100:
            raise SlopActionError("invalid_params", "volume must be between 0 and 100")
        data = await asyncio.to_thread(_daemon_post, "/volume/microphone/set", {"volume": volume})
        state.microphone_volume = int(data.get("volume", volume))
        return {"ok": True, "microphone_volume": state.microphone_volume}

    @slop.action(
        "audio",
        "test_sound",
        label="Play test sound",
        description=(
            "Play a short test sound through the provider's audio path — the same "
            "path emotion sounds use. Fails with conflict when audio is unavailable."
        ),
        idempotent=True,
        estimate="fast",
    )
    async def test_sound() -> dict[str, Any]:
        if not state.audio:
            raise SlopActionError(
                "conflict", "audio is unavailable (provider is running without a media backend)"
            )
        await asyncio.to_thread(mini.media.play_sound, "impatient1.wav")
        return {"ok": True}

    @slop.action(
        "audio",
        "set_listen_mode",
        params={
            "mode": {
                "type": "string",
                "description": (
                    "'realtime' streams every mic frame to the voice pipeline; "
                    "'wake' streams only after the wake word, until end of speech."
                ),
            },
        },
        label="Set listen mode",
        description=(
            "Choose how the microphone streams while the robot is live. Takes "
            "effect immediately when live; while asleep it is stored and applied "
            "on wake_up."
        ),
        estimate="instant",
    )
    async def set_listen_mode(mode: str) -> dict[str, Any]:
        if mode not in ("realtime", "wake"):
            raise SlopActionError("invalid_params", "mode must be 'realtime' or 'wake'")
        state.listen_mode = mode
        apply_audio_state()
        slop.refresh()
        return {"ok": True, "listen_mode": mode, "power_state": state.power_state}

    # --- Camera node + capture (frames from the daemon's local video tee) -------

    def camera_node() -> dict[str, Any]:
        # No node-level content_ref: the capture_frame RESULT carries the
        # file:// ref (the consumer ingests it from there); a ref on the node
        # would dangle once the ring prunes the file and would tempt the
        # model with a URI it cannot fetch.
        cap = state.last_capture
        return {
            "type": "sensor",
            "props": {
                "available": state.camera,
                "resolution": state.camera_resolution,
                "last_capture_at": cap["captured_at"] if cap else None,
                "last_capture_size": [cap["width"], cap["height"]] if cap else None,
            },
            "summary": (
                "Robot head camera. Invoke capture_frame to take a still photo: "
                "the result carries a content_ref with a file:// URI to a JPEG "
                "on the shared host. Capture is rejected while the robot is "
                "asleep — wake_up first."
            ),
            # capture_frame stays visible while asleep/unavailable (the SDK's
            # descriptor/decorator merge cannot express an empty action set —
            # same constraint as head_node); invokes are rejected with
            # "conflict" by ensure_camera instead.
        }

    # max_width is intentionally NOT in the params schema: slop_ai marks every
    # declared param as required (descriptor._normalize_params), and optional
    # params aren't expressible. Undeclared invoke params still reach the
    # handler (server._wrap_handler filters by signature).
    @slop.action(
        "camera",
        "capture_frame",
        label="Capture camera frame",
        description=(
            "Take a still photo with the head camera. Returns frame metadata "
            "plus a content_ref whose file:// URI points at the JPEG. Accepts "
            "an optional integer param max_width — largest output dimension in "
            f"px ({CAPTURE_MAX_DIM_RANGE[0]}-{CAPTURE_MAX_DIM_RANGE[1]}, "
            f"default {CAPTURE_MAX_DIM_DEFAULT}; never upscales)."
        ),
        estimate="fast",
    )
    async def capture_frame(max_width: int | None = None) -> dict[str, Any]:
        ensure_camera("capture_frame")
        max_dim = CAPTURE_MAX_DIM_DEFAULT if max_width is None else int(max_width)
        lo, hi = CAPTURE_MAX_DIM_RANGE
        if not lo <= max_dim <= hi:
            raise SlopActionError("invalid_params", f"max_width must be between {lo} and {hi}")
        async with capture_lock:
            # DEFAULT_FRAMES_DIR resolved at call time so test scaffolds can
            # point the ring at a scratch directory.
            cap = await asyncio.to_thread(_capture_and_store, mini, max_dim, DEFAULT_FRAMES_DIR)
        state.last_capture = cap
        slop.refresh()
        return {
            "ok": True,
            "width": cap["width"],
            "height": cap["height"],
            "size_bytes": cap["size_bytes"],
            "captured_at": cap["captured_at"],
            "content_ref": _capture_content_ref(cap),
        }

    # Register nodes last — see the comment above status_node.
    slop.node("status")(status_node)
    slop.node("head")(head_node)
    slop.node("behavior")(behavior_node)
    slop.node("audio")(audio_node)
    slop.node("camera")(camera_node)

    return slop


async def prefetch_emotions(
    emotions: RecordedMoveLibrary, state: RobotState, slop: SlopServer
) -> None:
    """Warm the emotions dataset and publish the move names as /behavior props.

    Loading the HF dataset is slow on first run (download) — doing it at startup
    means the first play_emotion doesn't stall, and the LLM sees the emotion
    vocabulary in the state tree without a list_emotions round trip. Quiet on
    failure (e.g. offline with a cold cache): props.emotions stays absent and
    play_emotion still validates names on invoke.
    """
    try:
        names = await asyncio.to_thread(emotions.list_moves)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("emotions prefetch failed — props.emotions unavailable", exc_info=True)
        return
    state.emotions = names
    slop.refresh()
    logger.info("emotions prefetched: %d moves", len(names))


async def poll_state(
    mini: ReachyMini, state: RobotState, slop: SlopServer, router: AudioRouter | None = None
) -> None:
    """Refresh the cached joint state ~5 Hz; emit patches only when it changes."""
    tick = 0
    while True:
        try:
            head_joints, antenna_joints = await asyncio.to_thread(
                mini.get_current_joint_positions
            )
            prev = state.snapshot_key()
            if tick % VOLUME_POLL_EVERY == 0:
                # Volume can change outside SLOP (dashboard, curl) — keep it honest.
                await asyncio.to_thread(fetch_volumes, state)
            if router is not None:
                # The router mutates these from its capture thread; mirror them
                # into the cache so gate transitions show up as patches.
                state.wake_word_active = router.wake_ready and router.mode != "realtime"
                state.gate_open = router.gate_open
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
        tick += 1
        await asyncio.sleep(POLL_INTERVAL_S)


def _daemon_get(path: str, timeout: float = 5.0) -> dict[str, Any]:
    """GET a daemon REST endpoint, return parsed JSON. Blocking — call via to_thread."""
    with urllib.request.urlopen(f"{DAEMON_API_BASE}{path}", timeout=timeout) as resp:
        return json.loads(resp.read())


def _daemon_post(path: str, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    """POST JSON to a daemon REST endpoint. Blocking — call via to_thread."""
    req = urllib.request.Request(
        f"{DAEMON_API_BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def fetch_daemon_mode() -> str:
    """Ask the daemon whether it runs a simulated or a real robot."""
    try:
        status = _daemon_get("/daemon/status")
        if status.get("simulation_enabled") or status.get("mockup_sim_enabled"):
            return "sim"
        return "real"
    except Exception:
        logger.warning("could not read daemon status", exc_info=True)
        return "unknown"


def fetch_volumes(state: RobotState) -> None:
    """Refresh cached speaker/mic volume from the daemon volume API.

    Quiet on failure (debug log only) — the volume API is unavailable when the
    daemon is down or in stub runs, and the joint poll already reports that.
    """
    for path, attr in (("/volume/current", "volume"), ("/volume/microphone/current", "microphone_volume")):
        try:
            setattr(state, attr, int(_daemon_get(path)["volume"]))
        except Exception:
            logger.debug("volume fetch failed for %s", path, exc_info=True)
            setattr(state, attr, None)


def _encode_jpeg(frame: np.ndarray, max_dim: int) -> tuple[bytes, int, int]:
    """JPEG-encode a BGR frame, downscaled to fit max_dim. Blocking — to_thread.

    Returns ``(jpeg_bytes, width, height)`` of the encoded image.
    """
    img = PILImage.fromarray(frame[:, :, ::-1])  # BGR -> RGB; fromarray copies
    img.thumbnail((max_dim, max_dim))  # aspect-preserving, never upscales
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=CAPTURE_JPEG_QUALITY)
    return buf.getvalue(), img.width, img.height


def _store_frame(data: bytes, frames_dir: str) -> Path:
    """Write a frame to the ring dir with the descriptor's filesystem hardening.

    0700 dir, 0600 file, atomic same-directory temp + rename. Timestamped names
    keep ring pruning a filename sort.
    """
    dir_path = Path(frames_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    os.chmod(dir_path, 0o700)
    final_path = dir_path / f"frame-{int(time.time() * 1000)}.jpg"
    tmp_path = dir_path / f"{final_path.name}.tmp.{os.getpid()}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, final_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return final_path


def _prune_frames(frames_dir: str, keep: int = FRAME_RING_SIZE) -> None:
    frames = sorted(Path(frames_dir).glob("frame-*.jpg"))
    for old in frames[:-keep] if keep else frames:
        old.unlink(missing_ok=True)


def _capture_and_store(
    mini: ReachyMini, max_dim: int, frames_dir: str = DEFAULT_FRAMES_DIR
) -> dict[str, Any]:
    """Grab a frame, encode, store, prune. Blocking — call via to_thread.

    Returns the ``last_capture`` metadata dict.
    """
    frame = None
    for attempt in range(CAPTURE_READ_ATTEMPTS):
        frame = mini.media.get_frame()
        if frame is not None:
            break
        if attempt + 1 < CAPTURE_READ_ATTEMPTS:
            time.sleep(CAPTURE_READ_RETRY_S)  # appsink timeout is transient
    if frame is None:
        raise RuntimeError("no frame from camera (daemon video tee not delivering)")
    data, width, height = _encode_jpeg(frame, max_dim)
    path = _store_frame(data, frames_dir)
    _prune_frames(frames_dir)
    return {
        "path": str(path),
        "width": width,
        "height": height,
        "size_bytes": len(data),
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _capture_content_ref(cap: dict[str, Any]) -> dict[str, Any]:
    """content_ref dict (spec §13) for a capture — used by node and action result."""
    return {
        "type": "binary",
        "mime": "image/jpeg",
        "summary": (
            f"Camera frame {cap['width']}x{cap['height']}, captured at {cap['captured_at']}"
        ),
        "size": cap["size_bytes"],
        "uri": f"file://{cap['path']}",
    }


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


async def run(
    socket_path: str,
    descriptor_path: str,
    media_backend: str,
    wake_on_start: bool = True,
    sleep_on_exit: bool = True,
    initial_state: str = "live",
    listen_mode: str = "wake",
    audio_router_enabled: bool = True,
    router_config: AudioRouterConfig | None = None,
    initial_volume: int | None = DEFAULT_INITIAL_VOLUME,
) -> None:
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
    camera = getattr(mini.media_manager, "camera", None)
    state.camera = camera is not None and PILImage is not None
    if camera is not None:
        state.camera_resolution = [int(v) for v in camera.resolution]
        if PILImage is None:
            logger.warning(
                "camera backend is up but Pillow is missing — capture_frame "
                "disabled. Install it with: uv pip install pillow"
            )
    state.mode = await asyncio.to_thread(fetch_daemon_mode)
    state.power_state = initial_state
    state.listen_mode = listen_mode
    if initial_volume is not None:
        # Before the wake-up emote, so it already plays at this level. Quiet on
        # failure — the volume API is down exactly when the daemon is, and the
        # joint poll reports that.
        try:
            await asyncio.to_thread(_daemon_post, "/volume/set", {"volume": initial_volume})
            logger.info("initial speaker volume set to %d", initial_volume)
        except Exception:
            logger.warning("could not set initial volume to %d", initial_volume, exc_info=True)
    await asyncio.to_thread(fetch_volumes, state)
    logger.info(
        "mode=%s audio=%s camera=%s volume=%s",
        state.mode,
        state.audio,
        state.camera,
        state.volume,
    )

    router: AudioRouter | None = None
    if audio_router_enabled and state.audio:
        router = AudioRouter(
            mini.media,
            router_config or AudioRouterConfig(),
            # The wake handler is attached in build_server; veto detections
            # while the sleep/wake emotes themselves play (their sounds and the
            # user echoing the word mid-transition must not re-trigger).
            allow_wake=lambda: state.busy_action not in ("goto_sleep", "wake_up"),
        )
    elif audio_router_enabled:
        logger.warning("audio backend unavailable — mic routing and wake word disabled")

    emotions = RecordedMoveLibrary(DEFAULT_EMOTIONS_DATASET)
    slop = build_server(mini, state, router, emotions)

    if router is not None:
        initial_mode = (
            RouterMode.MUTED
            if initial_state == "sleep"
            else RouterMode.REALTIME
            if listen_mode == "realtime"
            else RouterMode.WAKE
        )
        await router.start(initial_mode)

    if initial_state == "sleep":
        wake_on_start = False  # stay down: muted mic, wake word armed
    if wake_on_start:
        # The provider performs the wake-up (not the daemon) so the emote sound
        # plays through our working audio path — the daemon's media server needs
        # the GStreamer Rust webrtc plugin for its sounds. run_robot_usb.sh
        # starts the daemon with --no-wake-up-on-start accordingly.
        logger.info("waking up the robot...")
        try:
            # Torque first: with --no-wake-up-on-start the daemon never enables
            # motor control, so without this the emote is sound-only.
            await asyncio.to_thread(mini.enable_motors)
            await asyncio.to_thread(mini.wake_up)
        except Exception:
            logger.warning("wake-up on start failed", exc_info=True)

    server = await listen_unix(slop, socket_path)
    write_descriptor(descriptor_path, socket_path)
    logger.info("SLOP provider listening on unix:%s (mode=%s)", socket_path, state.mode)

    poll_task = asyncio.create_task(poll_state(mini, state, slop, router))
    prefetch_task = asyncio.create_task(prefetch_emotions(emotions, state, slop))

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
        if router is not None:
            # Stop the mic stream first: clients see EOF (and exit non-zero, so
            # the consumer backs off) and no shutdown audio leaks out.
            try:
                await router.stop()
            except Exception:
                logger.debug("audio router stop failed", exc_info=True)
        if sleep_on_exit:
            # Sleep with sound while our audio path is still open; the daemon's
            # own goto-sleep-on-stop stays enabled as a (silent) safety net.
            # Same sequence as the daemon: torque on, sleep pose, torque off.
            try:
                await asyncio.to_thread(mini.enable_motors)
                await asyncio.to_thread(mini.goto_sleep)
                await asyncio.to_thread(mini.disable_motors)
            except Exception:
                logger.warning("goto-sleep on exit failed", exc_info=True)
        for task in (poll_task, prefetch_task):
            task.cancel()
            try:
                await task
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
    parser.add_argument(
        "--wake-on-start",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wake the robot (with sound) when the provider starts.",
    )
    parser.add_argument(
        "--sleep-on-exit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Put the robot to sleep (with sound) when the provider shuts down.",
    )
    parser.add_argument(
        "--initial-state",
        default="live",
        choices=["live", "sleep"],
        help="Start live, or asleep with the wake word armed (implies --no-wake-on-start).",
    )
    parser.add_argument(
        "--listen-mode",
        default="wake",
        choices=["wake", "realtime"],
        help="Mic routing while live: gated behind the wake word, or open mic.",
    )
    parser.add_argument(
        "--initial-volume",
        type=int,
        default=DEFAULT_INITIAL_VOLUME,
        metavar="0-100",
        help=(
            "Speaker volume applied at startup through the daemon, before the "
            "wake-up emote (default %(default)s). Pass a negative value to keep "
            "the daemon's current volume."
        ),
    )
    parser.add_argument(
        "--audio-router",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Serve the gated microphone stream (and wake-word detection).",
    )
    parser.add_argument(
        "--audio-socket",
        default=DEFAULT_AUDIO_SOCKET,
        help="Unix socket path for the PCM16 microphone stream.",
    )
    parser.add_argument(
        "--wake-model",
        default=DEFAULT_WAKE_MODEL,
        help="openWakeWord model name, or a path to a custom .onnx model.",
    )
    parser.add_argument(
        "--wake-threshold",
        type=float,
        default=0.5,
        help="Wake-word detection score threshold (0-1).",
    )
    parser.add_argument(
        "--silence-stop-seconds",
        type=float,
        default=1.5,
        help="Close the wake-gated stream after this much silence.",
    )
    parser.add_argument(
        "--preroll-seconds",
        type=float,
        default=0.3,
        help="Audio replayed when the gate opens (covers the detection lag).",
    )
    parser.add_argument(
        "--vad-threshold",
        type=float,
        default=0.012,
        help="RMS voice-activity threshold used to detect end of speech.",
    )
    args = parser.parse_args()
    if args.initial_volume > 100:
        parser.error("--initial-volume must be 0-100 (or negative to keep the current volume)")
    router_config = AudioRouterConfig(
        socket_path=args.audio_socket,
        wake_model=args.wake_model,
        wake_threshold=args.wake_threshold,
        silence_stop_s=args.silence_stop_seconds,
        preroll_s=args.preroll_seconds,
        vad_threshold=args.vad_threshold,
    )
    asyncio.run(
        run(
            args.socket,
            args.descriptor,
            args.media_backend,
            wake_on_start=args.wake_on_start,
            sleep_on_exit=args.sleep_on_exit,
            initial_state=args.initial_state,
            listen_mode=args.listen_mode,
            audio_router_enabled=args.audio_router,
            router_config=router_config,
            initial_volume=args.initial_volume if args.initial_volume >= 0 else None,
        )
    )


if __name__ == "__main__":
    main()
