#!/usr/bin/env python3
"""Verification harness: serve the provider's SLOP surface with a STUBBED robot.

This reuses the real ``build_server`` and ``poll_state`` from
``reachy_slop_provider.py`` but swaps the ReachyMini SDK for a fake that records
calls and reports synthetic joint state. It lets us validate the SLOP provider
(state tree, affordances, patches) and cross-language interop with a TypeScript
consumer WITHOUT a running robot/simulator.

Not part of the integration — a test scaffold only.
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("rsp", HERE / "reachy_slop_provider.py")
rsp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rsp)

SOCKET = "/tmp/slop/reachy.sock"
DESCRIPTOR = "/tmp/slop/providers/reachy.json"


class FakeMini:
    """Records affordance calls; returns moving joint state so patches fire."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._head = [0.0] * 7
        self._antennas = [0.0, 0.0]

    def get_current_joint_positions(self):
        return list(self._head), list(self._antennas)

    def goto_target(self, head=None, duration=0.5, **kw):
        self.calls.append(("goto_target", duration))
        # Pretend the move landed: nudge joint 1 so the consumer sees a patch.
        self._head[1] = round(self._head[1] + 0.1, 4)

    def set_target(self, antennas=None, **kw):
        self.calls.append(("set_target", antennas))
        if antennas:
            self._antennas = [round(float(a), 4) for a in antennas]

    def wake_up(self):
        self.calls.append(("wake_up",))

    def goto_sleep(self):
        self.calls.append(("goto_sleep",))

    def play_move(self, move, initial_goto_duration=0.0, sound=True):
        self.calls.append(
            ("play_move", getattr(move, "description", ""), initial_goto_duration, sound)
        )
        self._head[1] = round(self._head[1] + 0.2, 4)

    def enable_wobbling(self):
        self.calls.append(("enable_wobbling",))

    def disable_wobbling(self):
        self.calls.append(("disable_wobbling",))


async def main() -> None:
    mini = FakeMini()
    state = rsp.RobotState()
    slop = rsp.build_server(mini, state)
    server = await rsp.listen_unix(slop, SOCKET)
    rsp.write_descriptor(DESCRIPTOR, SOCKET)
    print(f"[stub] serving on unix:{SOCKET}", flush=True)
    poll = asyncio.create_task(rsp.poll_state(mini, state, slop))
    try:
        await asyncio.sleep(20)  # serve long enough for the consumer to run
    finally:
        poll.cancel()
        server.close()
        await server.wait_closed()
        print(f"[stub] recorded calls: {mini.calls}", flush=True)
        Path(DESCRIPTOR).unlink(missing_ok=True)
        Path(SOCKET).unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
