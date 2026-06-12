#!/usr/bin/env python3
"""Verification: capture a real frame through a RUNNING provider.

Run on the machine hosting the daemon + provider (e.g. the Pi, with
``run_robot_usb.sh`` already up)::

    python _verify_camera.py [--socket /tmp/slop/reachy.sock]

Connects as a SLOP consumer, queries /camera, invokes capture_frame, then
re-opens the returned file:// JPEG with Pillow to prove the daemon's video
tee -> provider -> content_ref path works end to end.

Not part of the integration — a test scaffold only.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from PIL import Image
from slop_ai import SlopConsumer
from slop_ai.transports.unix_client import UnixClientTransport

DEFAULT_SOCKET = "/tmp/slop/reachy.sock"


def ok(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'ok' if condition else 'FAIL'}] {name}", flush=True)
    if not condition:
        sys.exit(f"check failed: {name} {detail}")


async def main(socket_path: str) -> None:
    consumer = SlopConsumer(UnixClientTransport(socket_path))
    await consumer.connect()
    try:
        camera = await consumer.query("/camera")
        print(f"[camera] props: {camera.properties}", flush=True)
        ok("camera available", camera.properties.get("available") is True)

        res = await consumer.invoke("/camera", "capture_frame")
        ok("capture_frame ok", res.get("status") == "ok", str(res))
        cap = res.get("data", {})
        ref = cap.get("content_ref", {})
        path = ref.get("uri", "").removeprefix("file://")
        ok("content_ref has a file:// uri", bool(path), str(ref))

        with Image.open(path) as img:
            img.load()
            ok(
                "JPEG decodes and matches metadata",
                img.format == "JPEG" and [img.width, img.height] == [cap["width"], cap["height"]],
                f"{img.format} {img.size} vs {cap}",
            )
        print(
            f"\n[camera] captured {cap['width']}x{cap['height']} "
            f"({cap['size_bytes']} bytes) -> {path}",
            flush=True,
        )
    finally:
        consumer.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="Provider unix socket path")
    args = parser.parse_args()
    asyncio.run(main(args.socket))
