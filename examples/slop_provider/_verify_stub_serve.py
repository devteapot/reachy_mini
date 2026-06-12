#!/usr/bin/env python3
"""Verification harness: serve the provider's SLOP surface with a STUBBED robot.

This reuses the real ``build_server`` and ``poll_state`` from
``reachy_slop_provider.py`` but swaps the ReachyMini SDK for a fake that records
calls and reports synthetic joint state, plus a fake media/wake-word pair for
the audio router. It lets us validate the SLOP provider (state tree,
affordances, patches, sleep/wake state machine) and cross-language interop with
a TypeScript consumer WITHOUT a running robot/simulator.

Two modes::

    python _verify_stub_serve.py           # serve 20 s for an external consumer
    python _verify_stub_serve.py --check   # in-process SlopConsumer assertions

Not part of the integration — a test scaffold only.
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

spec = importlib.util.spec_from_file_location("rsp", HERE / "reachy_slop_provider.py")
rsp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rsp)

from _verify_audio_router import FakeDetector, FakeMedia  # noqa: E402
from audio_router import AudioRouter, AudioRouterConfig, RouterMode  # noqa: E402

SOCKET = "/tmp/slop/reachy.sock"
DESCRIPTOR = "/tmp/slop/providers/reachy.json"
AUDIO_SOCKET = "/tmp/slop/_verify_reachy_audio.sock"
FRAMES_DIR = "/tmp/slop/_verify_camera_frames"  # scratch ring, not the real one


class FakeMini:
    """Records affordance calls; returns moving joint state so patches fire."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._head = [0.0] * 7
        self._antennas = [0.0, 0.0]
        self.media = FakeMedia()

    def get_current_joint_positions(self):
        return list(self._head), list(self._antennas)

    def goto_target(self, head=None, duration=0.5, **kw):
        self.calls.append(("goto_target", duration))
        # Hold for the requested duration (runs in to_thread) so the provider's
        # busy window is observable, then pretend the move landed.
        time.sleep(min(float(duration), 2.0))
        self._head[1] = round(self._head[1] + 0.1, 4)

    def set_target(self, antennas=None, **kw):
        self.calls.append(("set_target", antennas))
        if antennas:
            self._antennas = [round(float(a), 4) for a in antennas]

    def enable_motors(self, ids=None):
        self.calls.append(("enable_motors", ids))

    def disable_motors(self, ids=None):
        self.calls.append(("disable_motors", ids))

    def wake_up(self):
        self.calls.append(("wake_up",))

    def goto_sleep(self):
        self.calls.append(("goto_sleep",))

    def play_move(self, move, initial_goto_duration=0.0, sound=True):
        self.calls.append(
            ("play_move", getattr(move, "description", ""), initial_goto_duration, sound)
        )
        self._head[1] = round(self._head[1] + 0.2, 4)

    async def async_play_move(
        self, move, play_frequency=100.0, initial_goto_duration=0.0, sound=True
    ):
        self.calls.append(
            ("async_play_move", getattr(move, "description", ""), initial_goto_duration, sound)
        )
        await asyncio.sleep(0.2)
        self._head[1] = round(self._head[1] + 0.2, 4)

    def cancel_move(self):
        self.calls.append(("cancel_move",))

    def enable_wobbling(self):
        self.calls.append(("enable_wobbling",))

    def disable_wobbling(self):
        self.calls.append(("disable_wobbling",))


class FakeMove:
    def __init__(self, name: str) -> None:
        self.description = name


class FakeMoveLibrary:
    """Duck-typed RecordedMoveLibrary: no HF download, fixed move names."""

    dataset_name = "fake/emotions"

    def load(self):
        return self

    def list_moves(self):
        return ["fake1", "fake2"]

    def get(self, name: str) -> FakeMove:
        return FakeMove(name)


def build_stub():
    mini = FakeMini()
    state = rsp.RobotState()
    # The real provider sets these in run() from the SDK media manager; the stub
    # bypasses run(), so mirror it here (FakeMedia.get_frame serves the frames).
    state.camera = rsp.PILImage is not None
    state.camera_resolution = [1280, 720]
    state.audio = True  # so play_emotion's `sound and state.audio` is observable
    rsp.DEFAULT_FRAMES_DIR = FRAMES_DIR  # capture_frame reads this at call time
    detector = FakeDetector()
    router = AudioRouter(
        mini.media,
        AudioRouterConfig(socket_path=AUDIO_SOCKET, refractory_s=0.05),
        allow_wake=lambda: state.busy_action not in ("goto_sleep", "wake_up"),
        detector=detector,
    )
    emotions = FakeMoveLibrary()
    slop = rsp.build_server(mini, state, router, emotions)
    return mini, state, detector, router, slop, emotions


async def serve() -> None:
    """Serve for 20 s so an external (TypeScript) consumer can poke the tree."""
    mini, state, detector, router, slop, _emotions = build_stub()
    server = await rsp.listen_unix(slop, SOCKET)
    rsp.write_descriptor(DESCRIPTOR, SOCKET)
    await router.start(RouterMode.WAKE)
    print(f"[stub] serving on unix:{SOCKET} (audio on unix:{AUDIO_SOCKET})", flush=True)
    poll = asyncio.create_task(rsp.poll_state(mini, state, slop, router))
    try:
        await asyncio.sleep(20)  # serve long enough for the consumer to run
    finally:
        poll.cancel()
        await router.stop()
        server.close()
        await server.wait_closed()
        print(f"[stub] recorded calls: {mini.calls}", flush=True)
        Path(DESCRIPTOR).unlink(missing_ok=True)
        Path(SOCKET).unlink(missing_ok=True)


# --- in-process state-machine checks over real SLOP --------------------------------


def ok(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok' if condition else 'FAIL'}] {name}", flush=True)
    if not condition:
        sys.exit(f"check failed: {name} {detail}")


async def wait_for(predicate, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    sys.exit(f"timed out waiting for {what}")


async def check() -> None:
    from slop_ai import SlopConsumer
    from slop_ai.transports.unix_client import UnixClientTransport

    mini, state, detector, router, slop, emotions = build_stub()
    server = await rsp.listen_unix(slop, SOCKET)
    await router.start(RouterMode.WAKE)
    poll = asyncio.create_task(rsp.poll_state(mini, state, slop, router))

    consumer = SlopConsumer(UnixClientTransport(SOCKET))
    events: list[tuple[str, dict]] = []
    consumer.on_event(lambda name, payload: events.append((name, payload)))
    await consumer.connect()
    await consumer.subscribe("/")

    def event_names() -> list[str]:
        return [n for n, _ in events]

    def affordance_names(node) -> set[str]:
        return {a.action for a in (node.affordances or [])}

    try:
        status = await consumer.query("/status")
        ok(
            "live status props",
            status.properties["power_state"] == "live"
            and status.properties["listen_mode"] == "wake"
            and status.properties["audio_gate_open"] is False,
        )
        behavior = await consumer.query("/behavior")
        ok("goto_sleep offered while live", "goto_sleep" in affordance_names(behavior))
        audio = await consumer.query("/audio")
        ok("set_listen_mode offered with router", "set_listen_mode" in affordance_names(audio))

        # --- emotions: prefetch props, play_emotion sound param, conflicts ------
        ok("no emotions prop before prefetch", "emotions" not in (behavior.properties or {}))
        await rsp.prefetch_emotions(emotions, state, slop)
        behavior = await consumer.query("/behavior")
        ok(
            "emotions prop after prefetch",
            behavior.properties.get("emotions") == ["fake1", "fake2"],
            str(behavior.properties),
        )

        res = await consumer.invoke("/behavior", "play_emotion", {"name": "fake1"})
        ok("play_emotion accepted", res.get("status") == "accepted", str(res))
        ok("play_emotion result reports sound", res.get("data", {}).get("sound") is True, str(res))
        await wait_for(lambda: state.busy_action is None, what="emotion to finish")
        ok("emotion played with sound by default", ("async_play_move", "fake1", 1.0, True) in mini.calls)
        await wait_for(lambda: "action-finished" in event_names(), what="action-finished event")

        res = await consumer.invoke("/behavior", "play_emotion", {"name": "fake1", "sound": False})
        ok("muted play_emotion accepted", res.get("status") == "accepted", str(res))
        await wait_for(lambda: state.busy_action is None, what="muted emotion to finish")
        ok("sound=false muted the move", ("async_play_move", "fake1", 1.0, False) in mini.calls)

        res = await consumer.invoke("/behavior", "play_emotion", {"name": "nope"})
        ok(
            "unknown emotion rejected",
            res.get("status") == "error" and res.get("error", {}).get("code") == "invalid_params",
            str(res),
        )

        res = await consumer.invoke("/behavior", "play_emotion", {"name": "fake2"})
        ok("first of two emotions accepted", res.get("status") == "accepted", str(res))
        # While busy, play_emotion is hidden from the descriptor, so slop_ai
        # rejects a second invoke before the provider's conflict guard runs.
        res = await consumer.invoke("/behavior", "play_emotion", {"name": "fake1"})
        ok("second emotion rejected while busy", res.get("status") == "error", str(res))
        # Head affordances stay visible while busy — those hit the conflict guard.
        res = await consumer.invoke("/head", "set_pose", {"pitch": 0, "roll": 0, "yaw": 0, "z": 0})
        ok(
            "set_pose conflicts while emotion plays",
            res.get("status") == "error" and res.get("error", {}).get("code") == "conflict",
            str(res),
        )
        behavior = await consumer.query("/behavior")
        names = affordance_names(behavior)
        ok("busy: stop offered, play_emotion hidden", "stop" in names and "play_emotion" not in names)
        await wait_for(lambda: state.busy_action is None, what="conflicting emotion to finish")

        # --- camera: capture, content_ref, ring pruning -------------------------
        if rsp.PILImage is None:
            print("[stub] Pillow not installed — skipping camera checks", flush=True)
        else:
            shutil.rmtree(FRAMES_DIR, ignore_errors=True)
            camera = await consumer.query("/camera")
            ok("camera available, no capture yet", camera.properties["available"] is True)
            ok("capture_frame offered while live", "capture_frame" in affordance_names(camera))

            res = await consumer.invoke("/camera", "capture_frame")
            cap = res.get("data", {})
            ok("capture_frame ok", res.get("status") == "ok" and cap.get("ok") is True, str(res))
            ok("capture downscaled to default", cap["width"] <= 800 and cap["height"] <= 800)
            frame_path = Path(cap["content_ref"]["uri"].removeprefix("file://"))
            ok("frame file exists 0600", frame_path.is_file() and (frame_path.stat().st_mode & 0o777) == 0o600)
            ok("frame is a JPEG", frame_path.read_bytes()[:2] == b"\xff\xd8")
            ok("content_ref size matches file", cap["content_ref"]["size"] == frame_path.stat().st_size)

            camera = await consumer.query("/camera")
            ok(
                "node shows capture metadata but no content_ref",
                # The ref lives only in the action result: a node-level ref
                # would dangle once the ring prunes the file.
                camera.content_ref is None
                and camera.properties["last_capture_at"] == cap["captured_at"]
                and camera.properties["last_capture_size"] == [cap["width"], cap["height"]],
            )

            res = await consumer.invoke("/camera", "capture_frame", {"max_width": 32})
            ok(
                "invalid max_width rejected",
                res.get("status") == "error" and res.get("error", {}).get("code") == "invalid_params",
                str(res),
            )

            for _ in range(10):
                await consumer.invoke("/camera", "capture_frame", {"max_width": 200})
            n_frames = len(list(Path(FRAMES_DIR).glob("frame-*.jpg")))
            ok("frame ring pruned", n_frames <= rsp.FRAME_RING_SIZE, f"{n_frames} files")

        # --- goto_sleep: mutes the mic, hides motion, rejects motion invokes ----
        res = await consumer.invoke("/behavior", "goto_sleep")
        ok("goto_sleep accepted", res.get("status") == "accepted", str(res))
        ok("mic muted as soon as sleep is requested", router.mode == "muted")
        await wait_for(lambda: state.busy_action is None, what="sleep emote to finish")
        ok("sleep sequence ran", ("goto_sleep",) in mini.calls and ("disable_motors", None) in mini.calls)

        behavior = await consumer.query("/behavior")
        names = affordance_names(behavior)
        ok("asleep: goto_sleep hidden, wake_up offered", "goto_sleep" not in names and "wake_up" in names)

        res = await consumer.invoke(
            "/head", "set_pose", {"pitch": 0, "roll": 0, "yaw": 0, "z": 0}
        )
        ok(
            "motion rejected while asleep",
            res.get("status") == "error" and res.get("error", {}).get("code") == "conflict",
            str(res),
        )

        if rsp.PILImage is not None:
            # capture_frame stays visible while asleep (empty action sets are
            # inexpressible — see camera_node); the invoke must conflict.
            res = await consumer.invoke("/camera", "capture_frame")
            ok(
                "capture rejected while asleep",
                res.get("status") == "error" and res.get("error", {}).get("code") == "conflict",
                str(res),
            )

        # --- voice wake from sleep ------------------------------------------------
        detector.arm(0.9)
        mini.media.feed_frames(5, 0.5)
        await wait_for(lambda: state.power_state == "live", what="voice wake")
        ok("wake-word-detected emitted", "wake-word-detected" in event_names())
        ok("power-state-changed emitted", "power-state-changed" in event_names())
        ok("voice wake lands in wake listen mode", state.listen_mode == "wake")
        await wait_for(lambda: state.busy_action is None, what="wake emote to finish")
        ok("wake emote ran", ("wake_up",) in mini.calls)
        ok("gate open after voice wake", router.gate_open)

        # --- listen mode switching --------------------------------------------------
        res = await consumer.invoke("/audio", "set_listen_mode", {"mode": "realtime"})
        ok("set_listen_mode realtime", res.get("result", {}).get("listen_mode") == "realtime" or router.mode == "realtime", str(res))
        ok("router follows listen mode", router.mode == "realtime")
        res = await consumer.invoke("/audio", "set_listen_mode", {"mode": "nope"})
        ok(
            "invalid listen mode rejected",
            res.get("status") == "error"
            and res.get("error", {}).get("code") == "invalid_params",
            str(res),
        )

        # --- affordance wake path (from a fresh sleep) ------------------------------
        await consumer.invoke("/behavior", "goto_sleep")
        await wait_for(lambda: state.busy_action is None, what="second sleep")
        ok("asleep again, mic muted", state.power_state == "sleep" and router.mode == "muted")
        res = await consumer.invoke("/behavior", "wake_up")
        ok("wake_up accepted from sleep", res.get("status") == "accepted", str(res))
        ok("wake_up restores listen mode", state.power_state == "live" and router.mode == "realtime")
        await wait_for(lambda: state.busy_action is None, what="wake emote")

        print(f"\n[stub] all checks passed ({len(events)} events seen)", flush=True)
    finally:
        consumer.disconnect()
        poll.cancel()
        await router.stop()
        server.close()
        await server.wait_closed()
        Path(SOCKET).unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(check() if "--check" in sys.argv else serve())
