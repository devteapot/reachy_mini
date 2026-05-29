#!/usr/bin/env python3
"""SLOP provider that wraps the Reachy Mini SDK.

Exposes the robot as a SLOP state tree with affordances, served over a Unix
domain socket using NDJSON. A SLOP consumer (e.g. the `sloppy` runtime) connects
to the socket, observes joint state, and invokes movement/behavior affordances.

Discovery: on startup this writes a descriptor to ``/tmp/slop/providers/reachy.json``
(a default sloppy discovery path) and removes it on exit, so sloppy auto-loads the
provider with no config changes.

Scope (Phase 1 tracer): movement + behaviors only, no audio. The robot is driven
with ``media_backend="no_media"`` so no GStreamer/audio stack is required.

State tree::

    /reachy
    ├── /status     props: connected, mode, head_joints, antenna_joints  (live, polled)
    ├── /head       actions: goto_pose, set_antennas
    └── /behavior   actions: wake_up, goto_sleep, enable_wobbling, disable_wobbling

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
from pathlib import Path
from typing import Any

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose
from slop_ai import SlopServer
from slop_ai.transports.unix import listen as listen_unix

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("reachy-slop")

DEFAULT_SOCKET = "/tmp/slop/reachy.sock"
DEFAULT_DESCRIPTOR = "/tmp/slop/providers/reachy.json"
POLL_INTERVAL_S = 0.2  # ~5 Hz state poll
JOINT_ROUNDING = 4  # decimals — stabilises float jitter so we don't emit constant patches


class RobotState:
    """Cache of the latest robot readings, updated by the background poll task.

    SLOP ``@node`` functions read from here (cheap, non-blocking). They must NOT
    call the synchronous ReachyMini SDK directly — every SDK read is a network
    round-trip that would stall the asyncio socket server.
    """

    def __init__(self) -> None:
        self.connected: bool = False
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

    # --- Nodes (read cache only) ------------------------------------------------

    @slop.node("status")
    def status_node() -> dict[str, Any]:
        return {
            "type": "context",
            "props": {
                "connected": state.connected,
                "mode": "sim",
                "head_joints": state.head_joints,
                "antenna_joints": state.antenna_joints,
            },
            "summary": (
                "Reachy Mini (simulated). Live head + antenna joint positions, in radians. "
                "head_joints = [body_yaw, stewart_1..6]; antenna_joints = [right, left]."
            ),
        }

    @slop.node("head")
    def head_node() -> dict[str, Any]:
        return {
            "type": "control",
            "props": {"last_commanded_pose": state.last_pose},
            "summary": (
                "Head orientation and antennas. Use goto_pose to look around (angles in "
                "degrees, height in mm); use set_antennas to move the ears (radians)."
            ),
        }

    @slop.node("behavior")
    def behavior_node() -> dict[str, Any]:
        return {
            "type": "control",
            "summary": (
                "High-level behaviors: wake_up and goto_sleep emotes, and audio-reactive "
                "head wobbling (visible motion only when audio is playing)."
            ),
        }

    # --- Affordances (drive the robot; SDK calls run off the event loop) --------

    @slop.action(
        "head",
        "goto_pose",
        params={
            "pitch": {"type": "number", "description": "Head pitch in degrees (+ looks up)."},
            "roll": {"type": "number", "description": "Head roll in degrees (+ tilts right)."},
            "yaw": {"type": "number", "description": "Head yaw in degrees (+ turns left)."},
            "z": {"type": "number", "description": "Head vertical offset in millimetres."},
            "duration": {"type": "number", "description": "Movement duration in seconds."},
        },
        label="Move head to pose",
        description=(
            "Move the head to an absolute orientation (degrees) and height (mm), "
            "smoothly interpolated over `duration` seconds."
        ),
        estimate="slow",
    )
    async def goto_pose(
        pitch: float, roll: float, yaw: float, z: float, duration: float
    ) -> dict[str, Any]:
        pose = create_head_pose(z=z, roll=roll, pitch=pitch, yaw=yaw, mm=True, degrees=True)
        await asyncio.to_thread(mini.goto_target, head=pose, duration=duration)
        state.last_pose = {"pitch": pitch, "roll": roll, "yaw": yaw, "z": z}
        slop.refresh()
        return {"ok": True, "pose": state.last_pose}

    @slop.action(
        "head",
        "set_antennas",
        params={
            "right": {"type": "number", "description": "Right antenna angle in radians."},
            "left": {"type": "number", "description": "Left antenna angle in radians."},
        },
        label="Set antennas",
        description="Set the right and left antenna angles (radians).",
        estimate="fast",
    )
    async def set_antennas(right: float, left: float) -> dict[str, Any]:
        await asyncio.to_thread(mini.set_target, antennas=[right, left])
        slop.refresh()
        return {"ok": True, "antennas": [right, left]}

    @slop.action("behavior", "wake_up", label="Wake up", estimate="slow")
    async def wake_up() -> dict[str, Any]:
        await asyncio.to_thread(mini.wake_up)
        slop.refresh()
        return {"ok": True}

    @slop.action("behavior", "goto_sleep", label="Go to sleep", estimate="slow")
    async def goto_sleep() -> dict[str, Any]:
        await asyncio.to_thread(mini.goto_sleep)
        slop.refresh()
        return {"ok": True}

    @slop.action("behavior", "enable_wobbling", label="Enable wobbling", estimate="instant")
    async def enable_wobbling() -> dict[str, Any]:
        await asyncio.to_thread(mini.enable_wobbling)
        return {"ok": True}

    @slop.action("behavior", "disable_wobbling", label="Disable wobbling", estimate="instant")
    async def disable_wobbling() -> dict[str, Any]:
        await asyncio.to_thread(mini.disable_wobbling)
        return {"ok": True}

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
            state.connected = False
        await asyncio.sleep(POLL_INTERVAL_S)


def write_descriptor(path: str, socket_path: str) -> None:
    descriptor = {
        "id": "reachy",
        "name": "Reachy Mini",
        "transport": {"type": "unix", "path": socket_path},
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(descriptor, indent=2))
    logger.info("wrote discovery descriptor: %s", path)


async def run(socket_path: str, descriptor_path: str) -> None:
    logger.info("connecting to Reachy Mini daemon (media_backend=no_media)...")
    mini = ReachyMini(media_backend="no_media")
    logger.info("connected.")

    state = RobotState()
    slop = build_server(mini, state)

    server = await listen_unix(slop, socket_path)
    write_descriptor(descriptor_path, socket_path)
    logger.info("SLOP provider listening on unix:%s", socket_path)

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
    args = parser.parse_args()
    asyncio.run(run(args.socket, args.descriptor))


if __name__ == "__main__":
    main()
