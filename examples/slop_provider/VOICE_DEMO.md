# Reachy Mini voice demo (Phase 2) — run order

Hands-free voice conversation in the MuJoCo sim:

```
mic (sox) ──► voice /stt (Parakeet, gateway :8090)
          ──► agent turn (gpt-5.4-mini via OpenAI Codex — cloud, fast)
          ──► voice /tts (Voxtral on the spark, via gateway :8090 proxy)
          ──► afplay  +  runtime-driven head motion on the simulated Reachy
```

All components are local except the Voxtral TTS, which runs on the spark
(`slopinator-s-1.local:8091`) and is fronted on localhost by the gateway so
sloppy's voice policy treats it as local (no per-turn approval prompts).

## One-time prerequisites

- `brew install sox` (mic capture). `afplay` is built into macOS (playback).
- Grant your terminal **Microphone** permission (System Settings → Privacy &
  Security → Microphone). The first `sox` capture will prompt.
- Rebuild sloppy after pulling Phase 2: `(cd ~/dev/sloppy && bun run build)`.
- The Voxtral TTS server must be running on the spark:
  `vllm-omni serve mistralai/Voxtral-4B-TTS-2603 --host 0.0.0.0 --port 8091 --omni`
- LLM is OpenAI Codex (`gpt-5.4-mini`) — needs a working `codex login`
  (`~/.codex/auth.json`); `run_sloppy_demo.sh` passes it through the HOME override.
- STT reuses the `~/dev/hf-speech-to-speech` venv (mlx-audio + Parakeet cached;
  `python-multipart` was added there).
  (`run_llm_server.sh` for local Qwen3 via mlx-lm is kept as an offline alternative
  — not used in this config.)

## Terminals (ports: reachy 8000 · gateway 8090 · spark TTS 8091)

```
# A — MuJoCo sim GUI (macOS needs mjpython)
./run_sim_gui.sh

# B — Reachy SLOP provider (head/antennas/behaviors + the new fast head.set_pose)
source .venv/bin/activate && python reachy_slop_provider.py

# C — voice gateway: Parakeet STT + Voxtral TTS proxy on http://localhost:8090/v1
./run_voice_gateway.sh

# D — sloppy (interactive; the voice-conversation loop arms at startup)
./run_sloppy_demo.sh
```

(LLM is cloud Codex now — no local LLM terminal. For an offline LLM instead, run
`./run_llm_server.sh` and point the `llm` block back at a local `openai-chat` endpoint.)

Run D **without** `-p` — the conversation loop runs as a session plugin
(`onStartup` → listen) and must stay resident. With `-p` it would run one turn
and exit.

## Using it

1. With all five up, just **speak** into the mic. Capture ends after ~1.2 s of
   silence (sox VAD), Parakeet transcribes, Qwen replies, Voxtral speaks it, and
   the sim head/antennas animate during the reply. Then it listens again.
2. **Loading the robot:** discovered providers aren't auto-connected — the agent
   loads `reachy` via the `apps` provider's `load_provider` affordance. So an
   early utterance like *"wake up and look around"* gets the robot online; head
   animation then works on subsequent replies. (Before it's loaded, embodiment is
   a quiet no-op — the audio loop still works.)

## Notes / knobs

- Config: `demo/.sloppy/config.yaml` (loaded in isolation via `HOME=demo/`; your
  global `~/.sloppy` is untouched).
- TTS must be `format: wav` (afplay needs a container; raw pcm won't play).
- `voice.tts … autospeak: false` — the loop synthesizes replies itself; autospeak
  would double the TTS call.
- Long motions (`goto_pose`, `play_emotion`, …) now return `accepted` and run in
  the background; while one plays, `head.set_pose` returns a `conflict` error, so
  talking animation pauses during emotions and resumes after the `action-finished`
  event.
- Override hosts/ports: gateway flags `--port` / `--tts-upstream`; LLM model via
  the `llm` block in `demo/.sloppy/config.yaml`.
```
