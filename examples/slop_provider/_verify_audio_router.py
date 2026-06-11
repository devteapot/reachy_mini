#!/usr/bin/env python3
"""Verification harness for the audio router (no robot, no openWakeWord).

Drives ``GateFsm`` with a scripted clock, then runs a full ``AudioRouter``
against a fake media source and a fake detector, reading the result through
the real Unix socket — including one pass through ``audio_stream_client.py``
to pin down its exit-code contract with sloppy.

Not part of the integration — a test scaffold only. Run::

    python _verify_audio_router.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from audio_router import (  # noqa: E402
    FRAME_BYTES,
    FRAME_SAMPLES,
    AudioRouter,
    AudioRouterConfig,
    GateFsm,
    RouterMode,
)

SOCKET = "/tmp/slop/_verify_reachy_audio.sock"
CHECKS: list[str] = []


def ok(name: str, condition: bool, detail: str = "") -> None:
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {name}{f' — {detail}' if detail and not condition else ''}", flush=True)
    CHECKS.append(name) if condition else sys.exit(f"check failed: {name} {detail}")


# --- GateFsm with a scripted clock ------------------------------------------------


def verify_fsm() -> None:
    config = AudioRouterConfig(
        wake_threshold=0.5, silence_stop_s=1.5, max_utterance_s=10.0, refractory_s=2.0
    )

    fsm = GateFsm(RouterMode.REALTIME, config, "fake")
    ok("realtime always forwards", fsm.on_frame(0.0, 0.0, False).forward)

    fsm = GateFsm(RouterMode.WAKE, config, "fake")
    d = fsm.on_frame(0.0, 0.1, True)
    ok("wake mode gated below threshold", not d.forward and d.wake is None)
    d = fsm.on_frame(1.0, 0.9, True)
    ok("detection opens gate + flushes preroll", d.forward and d.flush_preroll and d.wake is not None)
    ok("gate stays open while voice", fsm.on_frame(2.0, 0.0, True).forward)
    d = fsm.on_frame(2.5, 0.9, True)
    ok("refractory ignores re-detection", d.wake is None and d.forward)
    d = fsm.on_frame(4.1, 0.0, False)  # last voice at 2.5 (+1.5s hangover)
    ok("gate closes after silence", not d.forward and d.closed)
    d = fsm.on_frame(10.0, 0.9, False)
    ok("re-detection after refractory", d.wake is not None and d.forward)
    # keep voice flowing; the safety cap should still close at opened_at + 10
    d = fsm.on_frame(20.5, 0.0, True)
    ok("max utterance cap closes gate", d.closed)

    fsm = GateFsm(RouterMode.MUTED, config, "fake")
    ok("muted is silent", not fsm.on_frame(0.0, 0.0, True).forward)
    d = fsm.on_frame(1.0, 0.9, True)
    ok(
        "muted detection wakes, opens gate, lands in wake mode",
        d.wake is not None and d.forward and d.flush_preroll and fsm.mode is RouterMode.WAKE,
    )


# --- full router over the real socket --------------------------------------------


class FakeMedia:
    """Queue-backed stand-in for the SDK media backend (stereo float32 chunks)."""

    def __init__(self) -> None:
        self._chunks: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self.recording = False

    def start_recording(self) -> None:
        self.recording = True

    def stop_recording(self) -> None:
        self.recording = False

    def get_audio_sample(self) -> np.ndarray | None:
        with self._lock:
            if self._chunks:
                return self._chunks.popleft()
        time.sleep(0.005)  # the real call blocks ≤ 20 ms when idle
        return None

    def feed_frames(self, n: int, amplitude: float) -> None:
        """Queue n exact 40 ms frames; odd-sized chunks exercise re-framing."""
        samples = np.full(n * FRAME_SAMPLES, amplitude, dtype=np.float32)
        stereo = np.stack([samples, samples], axis=1)
        with self._lock:
            # split unevenly so the router's accumulator has to re-frame
            cut = max(1, len(stereo) // 3)
            self._chunks.append(stereo[:cut])
            self._chunks.append(stereo[cut:])


class FakeDetector:
    """Scripted scores; `arm(score)` makes the next fed frame detect."""

    def __init__(self) -> None:
        self.model_name = "fake"
        self.loaded = True
        self.resets = 0
        self._next_score = 0.0

    def start_loading(self) -> None:
        pass

    def arm(self, score: float) -> None:
        self._next_score = score

    def feed(self, pcm_i16: np.ndarray) -> float:
        score, self._next_score = self._next_score, 0.0
        return score

    def reset(self) -> None:
        self.resets += 1


async def read_exactly(reader: asyncio.StreamReader, n: int, timeout: float = 5.0) -> bytes:
    return await asyncio.wait_for(reader.readexactly(n), timeout)


async def verify_router() -> None:
    config = AudioRouterConfig(
        socket_path=SOCKET,
        wake_threshold=0.5,
        vad_threshold=0.012,
        silence_stop_s=0.2,
        preroll_s=0.12,  # 3-frame ring
        refractory_s=0.05,  # effectively off — phases below re-detect quickly
    )
    media = FakeMedia()
    detector = FakeDetector()
    wakes: list = []
    router = AudioRouter(media, config, on_wake=wakes.append, detector=detector)
    await router.start(RouterMode.WAKE)
    ok("recording started", media.recording)

    reader, writer = await asyncio.open_unix_connection(SOCKET)
    await asyncio.sleep(0.1)  # let the server register the client before broadcasting

    # A: gated — loud input must come out as pure silence frames
    media.feed_frames(10, 0.5)
    data = await read_exactly(reader, 10 * FRAME_BYTES)
    ok("gated output is silence", data == b"\x00" * len(data))

    # B: detection — preroll ring (3 real frames) is flushed in a burst
    detector.arm(0.9)
    media.feed_frames(1, 0.5)
    data = await read_exactly(reader, 3 * FRAME_BYTES)
    ok("preroll flush carries real audio", any(b != 0 for b in data))
    ok("on_wake callback fired", len(wakes) == 1 and wakes[0].score == 0.9)
    ok("router status reflects open gate", router.gate_open and router.mode == "wake")

    # C: open gate — live frames pass through unmodified
    media.feed_frames(2, 0.25)
    data = await read_exactly(reader, 2 * FRAME_BYTES)
    expected = int(0.25 * 32767).to_bytes(2, "little", signed=True) * (2 * FRAME_SAMPLES)
    ok("open gate passes audio through", data == expected)

    # D: silence hangover closes the gate, output returns to silence
    media.feed_frames(1, 0.0)
    await read_exactly(reader, FRAME_BYTES)  # quiet but inside the hangover window
    await asyncio.sleep(0.3)  # > silence_stop_s
    media.feed_frames(3, 0.5)  # loud again, but no detection armed
    data = await read_exactly(reader, 3 * FRAME_BYTES)
    rest = data[FRAME_BYTES:]  # first frame is the one that closes the gate
    ok("gate closes after silence", rest == b"\x00" * len(rest))
    ok("detector reset on close", detector.resets >= 1)

    # E: realtime mode is a straight passthrough
    router.set_mode(RouterMode.REALTIME)
    media.feed_frames(2, 0.25)
    data = await read_exactly(reader, 2 * FRAME_BYTES)
    ok("realtime passes audio through", data == expected)

    # F: muted + detection = wake-from-sleep
    router.set_mode(RouterMode.MUTED)
    media.feed_frames(2, 0.5)
    data = await read_exactly(reader, 2 * FRAME_BYTES)
    ok("muted output is silence", data == b"\x00" * len(data))
    detector.arm(0.9)
    media.feed_frames(1, 0.5)
    data = await read_exactly(reader, 3 * FRAME_BYTES)  # preroll burst again
    ok("wake from muted flushes preroll", any(b != 0 for b in data))
    ok("wake from muted lands in wake mode", len(wakes) == 2 and router.mode == "wake")

    writer.close()

    # G: audio_stream_client.py — passthrough and the exit-1-on-EOF contract
    router.set_mode(RouterMode.REALTIME)  # deterministic passthrough for the client
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(HERE / "audio_stream_client.py"),
        "--socket",
        SOCKET,
        "--rate",
        "16000",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await asyncio.sleep(0.3)  # let it connect
    media.feed_frames(2, 0.25)
    data = await asyncio.wait_for(proc.stdout.readexactly(2 * FRAME_BYTES), 5.0)
    ok("stream client relays audio", data == expected)
    await router.stop()
    rc = await asyncio.wait_for(proc.wait(), 5.0)
    ok("stream client exits 1 on router EOF", rc == 1, f"exit={rc}")

    bad = await asyncio.create_subprocess_exec(
        sys.executable,
        str(HERE / "audio_stream_client.py"),
        "--socket",
        SOCKET,
        "--rate",
        "24000",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    rc = await asyncio.wait_for(bad.wait(), 5.0)
    ok("stream client refuses wrong sample rate", rc == 2, f"exit={rc}")


def main() -> None:
    verify_fsm()
    asyncio.run(verify_router())
    print(f"\nall {len(CHECKS)} checks passed", flush=True)


if __name__ == "__main__":
    main()
