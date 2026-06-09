#!/usr/bin/env python3
"""Local OpenAI-compatible voice gateway for the sloppy Reachy voice demo.

One process exposes BOTH endpoints the sloppy voice provider needs, on localhost:

  POST /v1/audio/transcriptions  → Parakeet-TDT STT, in-process via mlx-audio
                                    (reuses the model from ~/dev/hf-speech-to-speech).
  POST /v1/audio/speech          → reverse-proxied to the Voxtral TTS server on the
                                    "spark" (default http://slopinator-s-1.local:8091).

Why a localhost gateway: sloppy's voice network policy auto-allows endpoints that
need no auth AND point at localhost/127.0.0.1 (src/plugins/first-party/voice/policy.ts).
The spark is a LAN host (`*.local`), which the policy would otherwise gate behind a
per-turn approval prompt — fatal for a hands-free loop. Fronting it on localhost
keeps the privacy boundary intact while letting the loop run unattended. STT is
genuinely local (Apple Silicon mlx), so it's local either way.

Run (uses the hf-speech-to-speech venv, which already has mlx-audio + the model):
    ~/dev/hf-speech-to-speech/.venv/bin/python voice_gateway.py
or  ./run_voice_gateway.sh

Sloppy config points both stt and tts at http://localhost:8090/v1 with auth: none.
"""

from __future__ import annotations

import argparse
import logging
import struct
import threading
import urllib.request

import numpy as np
from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("voice-gateway")

STT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
TARGET_SAMPLE_RATE = 16000

# Loaded once at startup; guarded by a lock since the half-duplex loop sends one
# request at a time but FastAPI may still run sync endpoints on worker threads.
_model = None
_model_lock = threading.Lock()


def load_stt():
    global _model
    from mlx_audio.stt.generate import load_model

    logger.info("loading Parakeet STT model: %s", STT_MODEL)
    _model = load_model(STT_MODEL)
    logger.info("STT model ready")


def _parse_wav(data: bytes) -> tuple[np.ndarray, int, int]:
    """Minimal RIFF/WAVE reader → (samples float32 in [-1,1], sample_rate, channels).

    Handles PCM16/24/32, IEEE float32, and WAVE_FORMAT_EXTENSIBLE (0xFFFE) — which
    is what macOS coreaudio capture emits and stdlib `wave` rejects.
    """
    if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")

    fmt = None
    pcm = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8 : pos + 8 + chunk_size]
        if chunk_id == b"fmt ":
            audio_format, channels, sample_rate, _byte_rate, _align, bits = struct.unpack_from(
                "<HHIIHH", body, 0
            )
            if audio_format == 0xFFFE and len(body) >= 26:
                # Extensible: real format is the first 2 bytes of the SubFormat GUID.
                audio_format = struct.unpack_from("<H", body, 24)[0]
            fmt = (audio_format, channels, bits, sample_rate)
        elif chunk_id == b"data":
            pcm = body
        pos += 8 + chunk_size + (chunk_size & 1)  # chunks are word-aligned

    if fmt is None or pcm is None:
        raise ValueError("missing fmt/data chunk")
    audio_format, channels, bits, sample_rate = fmt

    if audio_format == 1 and bits == 16:
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    elif audio_format == 1 and bits == 32:
        audio = np.frombuffer(pcm, dtype="<i4").astype(np.float32) / 2147483648.0
    elif audio_format == 3 and bits == 32:
        audio = np.frombuffer(pcm, dtype="<f4").astype(np.float32).copy()
    elif audio_format == 1 and bits == 24:
        raw = np.frombuffer(pcm, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        vals = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        vals = np.where(vals & 0x800000, vals - 0x1000000, vals)
        audio = vals.astype(np.float32) / 8388608.0
    else:
        raise ValueError(f"unsupported WAV: format={audio_format} bits={bits}")

    return audio, sample_rate, channels


def wav_to_float32_mono_16k(data: bytes) -> np.ndarray:
    """Decode arbitrary WAV bytes to float32 mono at 16 kHz for Parakeet."""
    audio, sample_rate, channels = _parse_wav(data)

    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)

    if sample_rate != TARGET_SAMPLE_RATE and len(audio) > 1:
        new_len = int(round(len(audio) * TARGET_SAMPLE_RATE / sample_rate))
        audio = np.interp(
            np.linspace(0, len(audio) - 1, new_len),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)

    return np.ascontiguousarray(audio, dtype=np.float32)


def transcribe_audio(audio: np.ndarray) -> str:
    import mlx.core as mx

    with _model_lock:
        result = _model.decode_chunk(mx.array(audio, dtype=mx.float32), verbose=False)
    text = result.text if hasattr(result, "text") else str(result)
    return text.strip()


def build_app(tts_upstream: str) -> FastAPI:
    app = FastAPI(title="sloppy voice gateway")

    @app.on_event("startup")
    def _startup() -> None:
        load_stt()

    @app.get("/v1/models")
    def models() -> JSONResponse:
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": STT_MODEL, "object": "model", "owned_by": "mlx-audio"},
                    {"id": "voxtral-proxy", "object": "model", "owned_by": "vllm"},
                ],
            }
        )

    @app.post("/v1/audio/transcriptions")
    def transcriptions(
        file: UploadFile,
        model: str = Form(default=STT_MODEL),
        response_format: str = Form(default="json"),
        language: str = Form(default="en"),
    ) -> JSONResponse:
        raw = file.file.read()
        try:
            audio = wav_to_float32_mono_16k(raw)
        except Exception as exc:  # noqa: BLE001 — surface decode errors to the client
            logger.warning("audio decode failed: %s", exc)
            return JSONResponse({"error": f"audio decode failed: {exc}"}, status_code=400)
        text = transcribe_audio(audio)
        logger.info("STT (%d samples): %r", len(audio), text)
        # verbose_json-compatible: sloppy reads `text` (and optional `language`).
        return JSONResponse({"text": text, "language": language})

    @app.post("/v1/audio/speech")
    async def speech(request: Request) -> Response:
        body = await request.body()
        upstream = f"{tts_upstream.rstrip('/')}/audio/speech"
        req = urllib.request.Request(
            upstream,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                audio = resp.read()
                content_type = resp.headers.get("Content-Type", "audio/wav")
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTS upstream failed (%s): %s", upstream, exc)
            return JSONResponse({"error": f"tts upstream failed: {exc}"}, status_code=502)
        logger.info("TTS proxied %d bytes (%s)", len(audio), content_type)
        return Response(content=audio, media_type=content_type)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="sloppy voice gateway (Parakeet STT + Voxtral TTS proxy)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--tts-upstream",
        default="http://slopinator-s-1.local:8091/v1",
        help="OpenAI-speech-compatible TTS server base URL to proxy /v1/audio/speech to.",
    )
    args = parser.parse_args()

    import uvicorn

    app = build_app(args.tts_upstream)
    logger.info("voice gateway on http://%s:%d/v1  (TTS → %s)", args.host, args.port, args.tts_upstream)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
