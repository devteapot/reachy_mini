#!/usr/bin/env python3
"""Bridge the audio router's Unix-socket PCM stream to stdout for sloppy.

sloppy's voice plugin spawns this as its ``streamCommand`` and reads mono
S16LE PCM from stdout. It substitutes the active STT sample rate for
``{rate}``; the router only produces 16 kHz, so anything else exits 2 rather
than silently producing resampled-sounding audio.

Exit codes matter: sloppy treats a stream subprocess that exits 0 as a clean
end of audio and strands its listen loop, while a non-zero exit raises and
triggers the continuous-mode restart backoff. So every failure path here —
including plain EOF when the provider shuts down — exits 1. The connect retry
below bridges short provider restarts faster than sloppy's backoff alone.

Stdlib only; runnable with any python3.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

ROUTER_SAMPLE_RATE = 16_000
CONNECT_ATTEMPTS = 10
CONNECT_RETRY_S = 0.5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/tmp/slop/reachy_audio.sock")
    parser.add_argument("--rate", type=int, default=ROUTER_SAMPLE_RATE)
    args = parser.parse_args()

    if args.rate != ROUTER_SAMPLE_RATE:
        print(
            f"audio_stream_client: router streams {ROUTER_SAMPLE_RATE} Hz but "
            f"{args.rate} Hz was requested — refusing to mislabel audio",
            file=sys.stderr,
        )
        return 2

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            sock.connect(args.socket)
            break
        except OSError as e:
            if attempt == CONNECT_ATTEMPTS - 1:
                print(f"audio_stream_client: cannot connect to {args.socket}: {e}", file=sys.stderr)
                return 1
            time.sleep(CONNECT_RETRY_S)

    out = sys.stdout.buffer
    try:
        while True:
            data = sock.recv(4096)
            if not data:  # provider went away mid-stream
                print("audio_stream_client: stream ended (provider stopped?)", file=sys.stderr)
                return 1
            out.write(data)
            out.flush()
    except BrokenPipeError:
        return 1  # sloppy closed our stdout (normal kill path); exit quietly but non-zero
    except OSError as e:
        print(f"audio_stream_client: stream error: {e}", file=sys.stderr)
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
