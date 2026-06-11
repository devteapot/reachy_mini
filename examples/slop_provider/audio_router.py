#!/usr/bin/env python3
"""Provider-owned microphone router with local wake-word gating.

The router is the single owner of the robot microphone. It pulls raw frames
from the SDK media backend (daemon GStreamer appsink, 16 kHz stereo float32),
downmixes to mono PCM16, and serves a continuous stream on a Unix socket that
sloppy's voice plugin reads via its ``streamCommand`` (see
``audio_stream_client.py``). What goes out depends on the routing mode:

- ``REALTIME``: every frame is forwarded as-is (open mic).
- ``WAKE``: silence until the wake word is detected, then the pre-roll ring
  plus live frames until end-of-speech (energy VAD), then silence again.
- ``MUTED``: silence, wake detection armed — a detection here is the
  voice-wake-from-sleep path: the gate opens immediately (so the trailing
  speech of "hey ... <message>" is not lost while motors spin up) and the
  ``on_wake`` callback lets the provider run its wake-up transition.

Silence frames substitute 1:1 for real frames, so the consumer always sees an
uninterrupted real-time stream and its STT VAD simply never fires; no real
audio leaves the process while gated.

Wake-word detection uses openWakeWord (onnx). The model loads lazily in a
background thread; until it is ready (or if the import fails) gating still
works, only wake-by-voice is unavailable.

Debug from the mic, without SLOP::

    python audio_router.py --mode wake --wake-model hey_jarvis
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import numpy as np

logger = logging.getLogger("reachy-slop.audio")

SAMPLE_RATE = 16_000  # the SDK media backend records at a fixed 16 kHz
FRAME_SAMPLES = 640  # 40 ms — matches sloppy's streamChunkMs default
FRAME_BYTES = FRAME_SAMPLES * 2  # mono S16LE
SILENCE_FRAME = b"\x00" * FRAME_BYTES
OWW_CHUNK_SAMPLES = 1280  # openWakeWord's recommended 80 ms prediction chunk
MAX_CLIENT_BUFFER = 64 * 1024  # drop clients that stop reading — capture never blocks

DEFAULT_SOCKET = "/tmp/slop/reachy_audio.sock"
DEFAULT_WAKE_MODEL = "hey_jarvis"  # pretrained stand-in until a custom model exists


class RouterMode(Enum):
    MUTED = "muted"  # sleep: silence out, wake word armed
    WAKE = "wake"  # live, wake-word gated
    REALTIME = "realtime"  # live, open mic


@dataclass
class AudioRouterConfig:
    socket_path: str = DEFAULT_SOCKET
    wake_model: str = DEFAULT_WAKE_MODEL  # openWakeWord model name or path to a custom .onnx
    wake_threshold: float = 0.5
    vad_threshold: float = 0.012  # RMS on float32 mono frames
    silence_stop_s: float = 1.5  # close the gate after this much non-voice
    max_utterance_s: float = 30.0  # safety cap on an open gate
    preroll_s: float = 0.3  # audio replayed on gate-open (wake word ends before detection)
    refractory_s: float = 2.0  # ignore re-detections this close to the last one


@dataclass
class WakeEvent:
    model: str
    score: float


@dataclass
class GateDecision:
    forward: bool
    flush_preroll: bool = False
    wake: WakeEvent | None = None
    closed: bool = False


class EnergyVad:
    """RMS-threshold voice activity, used only to close an open gate.

    Deliberately simple: no extra dependency, identical behaviour in sim and
    on the Pi. If field use shows it closing on quiet speech, the ReSpeaker's
    hardware flag (``mini.media.get_DoA()[1]``) can be OR-ed in here.
    """

    def __init__(self, threshold: float) -> None:
        self.threshold = threshold

    def is_voice(self, mono_f32: np.ndarray) -> bool:
        return float(np.sqrt(np.mean(np.square(mono_f32)))) >= self.threshold


class GateFsm:
    """Pure gating state machine; one ``on_frame`` call per 40 ms frame.

    Clock-injected (callers pass ``now``) and detector-agnostic (callers pass
    the frame's wake score), so it is testable with scripted inputs.
    """

    def __init__(self, mode: RouterMode, config: AudioRouterConfig, model_name: str) -> None:
        self.mode = mode
        self.gate_open = False
        self._config = config
        self._model_name = model_name
        self._opened_at = 0.0
        self._last_voice_at = 0.0
        self._last_detection_at: float | None = None

    def set_mode(self, mode: RouterMode) -> None:
        self.mode = mode
        self.gate_open = False

    def on_frame(self, now: float, score: float, is_voice: bool) -> GateDecision:
        if self.mode is RouterMode.REALTIME:
            return GateDecision(forward=True)

        detected = score >= self._config.wake_threshold and (
            self._last_detection_at is None
            or now - self._last_detection_at >= self._config.refractory_s
        )
        if detected:
            self._last_detection_at = now

        if not self.gate_open:
            if not detected:
                return GateDecision(forward=False)
            # Open immediately, even from MUTED: the wake-from-sleep speech
            # ("hey ... <message>") must flow while the provider transitions.
            self.mode = RouterMode.WAKE
            self.gate_open = True
            self._opened_at = now
            self._last_voice_at = now
            return GateDecision(
                forward=True,
                flush_preroll=True,
                wake=WakeEvent(model=self._model_name, score=score),
            )

        # Check the hangover before crediting this frame's voice, so the gate
        # closes even when no quiet frames flowed during the silence (capture
        # gaps); a fresh detection re-opens it.
        if (
            now - self._last_voice_at > self._config.silence_stop_s
            or now - self._opened_at > self._config.max_utterance_s
        ):
            self.gate_open = False
            return GateDecision(forward=False, closed=True)
        if is_voice:
            self._last_voice_at = now
        return GateDecision(forward=True)


class WakeWordDetector:
    """Lazy-loading openWakeWord wrapper.

    ``feed`` accumulates mono int16 samples and predicts on 80 ms chunks.
    Always uses the onnx framework — openWakeWord's tflite default has no
    wheels for current Pythons (notably on the Pi).
    """

    def __init__(self, model: str) -> None:
        self.model_name = Path(model).stem if model.endswith(".onnx") else model
        self._model_arg = model
        self._oww: Any = None
        self._buf = np.zeros(0, dtype=np.int16)
        self._load_started = False

    @property
    def loaded(self) -> bool:
        return self._oww is not None

    def start_loading(self) -> None:
        if self._load_started:
            return
        self._load_started = True
        threading.Thread(target=self._load, name="wake-model-load", daemon=True).start()

    def _load(self) -> None:
        try:
            import openwakeword
            from openwakeword.model import Model

            if not self._model_arg.endswith(".onnx"):
                # Idempotent; fetches the named model + shared feature models.
                openwakeword.utils.download_models(model_names=[self._model_arg])
            oww = Model(wakeword_models=[self._model_arg], inference_framework="onnx")
            self._oww = oww
            logger.info("wake-word model %r ready", self.model_name)
        except Exception:
            logger.warning(
                "wake-word model %r failed to load — wake-by-voice disabled "
                "(install with: uv pip install 'openwakeword>=0.6' onnxruntime)",
                self._model_arg,
                exc_info=True,
            )

    def feed(self, pcm_i16: np.ndarray) -> float:
        if self._oww is None:
            return 0.0
        self._buf = np.concatenate([self._buf, pcm_i16])
        score = 0.0
        while len(self._buf) >= OWW_CHUNK_SAMPLES:
            chunk, self._buf = self._buf[:OWW_CHUNK_SAMPLES], self._buf[OWW_CHUNK_SAMPLES:]
            scores = self._oww.predict(chunk)
            score = max(score, max(scores.values(), default=0.0))
        return score

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)
        if self._oww is not None and hasattr(self._oww, "reset"):
            self._oww.reset()


class AudioRouter:
    """Capture thread + gating FSM + Unix-socket PCM16 broadcast server.

    ``media`` needs ``start_recording`` / ``stop_recording`` /
    ``get_audio_sample`` (the SDK media backend, or a fake in tests).
    ``on_wake`` runs on the asyncio loop for every accepted detection.
    ``allow_wake`` (checked on the capture thread) lets the provider veto
    detections, e.g. while the goto_sleep emote is still playing.
    """

    def __init__(
        self,
        media: Any,
        config: AudioRouterConfig,
        on_wake: Callable[[WakeEvent], None] | None = None,
        allow_wake: Callable[[], bool] | None = None,
        detector: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._media = media
        self._config = config
        self._on_wake: Callable[[WakeEvent], None] = on_wake or (
            lambda e: logger.info("wake word detected but no handler attached")
        )
        self._allow_wake = allow_wake or (lambda: True)
        self._detector = detector if detector is not None else WakeWordDetector(config.wake_model)
        self._clock = clock
        self._vad = EnergyVad(config.vad_threshold)
        self._fsm = GateFsm(RouterMode.WAKE, config, self._detector.model_name)
        self._fsm_lock = threading.Lock()
        self._preroll: deque[bytes] = deque(maxlen=max(1, round(config.preroll_s / 0.04)))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- status, read by the provider's nodes -----------------------------------

    @property
    def mode(self) -> str:
        return self._fsm.mode.value

    @property
    def gate_open(self) -> bool:
        return self._fsm.gate_open

    @property
    def wake_ready(self) -> bool:
        return self._detector.loaded

    def set_mode(self, mode: RouterMode) -> None:
        with self._fsm_lock:
            if self._fsm.mode is not mode:
                logger.info("audio router mode -> %s", mode.value)
            self._fsm.set_mode(mode)
            self._detector.reset()

    def set_wake_handler(self, on_wake: Callable[[WakeEvent], None]) -> None:
        """Attach the wake callback after construction.

        The provider's handler closes over its SLOP server, which is itself
        built with the router — hence the two-step wiring.
        """
        self._on_wake = on_wake

    # --- lifecycle ---------------------------------------------------------------

    async def start(self, initial_mode: RouterMode) -> None:
        self._loop = asyncio.get_running_loop()
        self._fsm.set_mode(initial_mode)
        self._detector.start_loading()
        self._media.start_recording()

        socket_path = Path(self._config.socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(socket_path.parent, 0o700)
        socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(socket_path))
        os.chmod(socket_path, 0o600)

        self._thread = threading.Thread(target=self._capture_loop, name="audio-capture", daemon=True)
        self._thread.start()
        logger.info(
            "audio router serving PCM16 mono %d Hz on unix:%s (mode=%s, wake_model=%s)",
            SAMPLE_RATE,
            socket_path,
            initial_mode.value,
            self._detector.model_name,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        # Close clients before wait_closed(): since 3.12 it waits for connection
        # handlers, which sit in reader.read() until their client goes away.
        for writer in list(self._clients):
            writer.close()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        try:
            self._media.stop_recording()
        except Exception:
            logger.debug("stop_recording failed", exc_info=True)
        Path(self._config.socket_path).unlink(missing_ok=True)

    # --- socket server -------------------------------------------------------------

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # sloppy respawns the stream client around every transcript/TTS cycle,
        # so connects are frequent; clients only read, never write.
        self._clients.add(writer)
        logger.debug("audio client connected (%d total)", len(self._clients))
        try:
            await reader.read()  # returns on client EOF/disconnect
        finally:
            self._clients.discard(writer)
            writer.close()
            logger.debug("audio client disconnected (%d total)", len(self._clients))

    def _broadcast(self, data: bytes) -> None:
        for writer in list(self._clients):
            transport = writer.transport
            if transport.is_closing():
                self._clients.discard(writer)
                continue
            if transport.get_write_buffer_size() > MAX_CLIENT_BUFFER:
                logger.warning("audio client not reading — disconnecting it")
                self._clients.discard(writer)
                writer.close()
                continue
            writer.write(data)

    # --- capture thread --------------------------------------------------------------

    def _capture_loop(self) -> None:
        acc = np.zeros(0, dtype=np.float32)
        while not self._stop.is_set():
            try:
                chunk = self._media.get_audio_sample()  # blocks ≤ 20 ms
            except Exception:
                logger.warning("audio capture failed; retrying", exc_info=True)
                time.sleep(0.5)
                continue
            if chunk is None:
                continue
            acc = np.concatenate([acc, chunk.mean(axis=1).astype(np.float32)])
            while len(acc) >= FRAME_SAMPLES:
                frame, acc = acc[:FRAME_SAMPLES], acc[FRAME_SAMPLES:]
                self._process_frame(frame)

    def _process_frame(self, mono: np.ndarray) -> None:
        pcm16 = (np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")
        data = pcm16.tobytes()
        self._preroll.append(data)

        with self._fsm_lock:
            score = 0.0
            if self._fsm.mode is not RouterMode.REALTIME and self._allow_wake():
                score = self._detector.feed(pcm16)
            decision = self._fsm.on_frame(self._clock(), score, self._vad.is_voice(mono))

        if decision.flush_preroll:
            # The ring already contains this frame; replay it all (short burst,
            # recovers the audio between phrase end and detection).
            out = b"".join(self._preroll)
            self._preroll.clear()
        elif decision.forward:
            out = data
        else:
            out = SILENCE_FRAME

        if decision.wake is not None:
            logger.info("wake word detected (score=%.2f)", decision.wake.score)
        if decision.closed:
            logger.info("end of speech — audio gate closed")
            self._detector.reset()

        loop = self._loop
        if loop is not None and not loop.is_closed():
            if decision.wake is not None:
                loop.call_soon_threadsafe(self._on_wake, decision.wake)
            loop.call_soon_threadsafe(self._broadcast, out)


# --- mic debug mode -------------------------------------------------------------


def _debug_main() -> None:
    """Run the router against the real SDK media backend and print activity."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--mode", default="wake", choices=[m.value for m in RouterMode])
    parser.add_argument("--wake-model", default=DEFAULT_WAKE_MODEL)
    parser.add_argument("--wake-threshold", type=float, default=0.5)
    parser.add_argument("--vad-threshold", type=float, default=0.012)
    parser.add_argument("--silence-stop-seconds", type=float, default=1.5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from reachy_mini import ReachyMini

    mini = ReachyMini(media_backend="local")
    config = AudioRouterConfig(
        socket_path=args.socket,
        wake_model=args.wake_model,
        wake_threshold=args.wake_threshold,
        vad_threshold=args.vad_threshold,
        silence_stop_s=args.silence_stop_seconds,
    )

    async def main() -> None:
        router = AudioRouter(
            mini.media,
            config,
            on_wake=lambda e: print(f"*** WAKE ({e.model}, score={e.score:.2f}) ***", flush=True),
        )
        await router.start(RouterMode(args.mode))
        print(f"listening; stream with: python3 audio_stream_client.py --socket {args.socket}")
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            await router.stop()

    asyncio.run(main())


if __name__ == "__main__":
    _debug_main()
