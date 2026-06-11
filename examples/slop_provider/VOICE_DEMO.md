# Reachy Mini voice demo (Phase 2) — run order

Hands-free voice conversation in the MuJoCo sim:

```
mic ──► provider audio router (sleep/wake + wake-word gate, audio_router.py)
    ──► audio_stream_client.py (unix socket → stdout, sloppy streamCommand)
    ──► voice /stt (Parakeet, gateway :8090)
    ──► agent turn (gpt-5.4-mini via OpenAI Codex — cloud, fast)
    ──► voice /tts (Voxtral on the spark, via gateway :8090 proxy)
    ──► play (sox)  +  runtime-driven head motion on the simulated Reachy
```

The provider owns the microphone: in the default `wake` listen mode nothing
streams to STT until the wake word (`hey jarvis` for now) is heard; after
`goto_sleep` the stream is muted entirely with the wake word armed, so saying
*"hey jarvis, wake up"* (one breath) wakes the robot and the words after the
wake word become the first utterance. `audio.set_listen_mode(realtime)` gives
the old open-mic behaviour.

All components are local except the Voxtral TTS, which runs on the spark
(`slopinator-s-1.local:8091`) and is fronted on localhost by the gateway so
sloppy's voice policy treats it as local (no per-turn approval prompts).

## One-time prerequisites

- `brew install sox` (`play` is sloppy's default playback command; mic capture
  now goes through the provider's audio router instead of sox).
- Wake-word detection in the provider venv:
  `uv pip install "openwakeword>=0.6" onnxruntime` (first run downloads the
  `hey_jarvis` model; without the packages everything still works except
  wake-by-voice).
- Grant your terminal **Microphone** permission (System Settings → Privacy &
  Security → Microphone). The provider's GStreamer capture will prompt.
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

1. With all four up, say **"hey jarvis"** and your request in one breath. The
   provider opens its audio gate (a short pre-roll keeps the words right after
   the wake word), streams until ~1.5 s of silence, Parakeet transcribes, the
   agent replies, Voxtral speaks it, and the sim head/antennas animate during
   the reply. Then it re-arms. For an open mic (no wake word), ask the agent to
   set listen mode to `realtime`, or start the provider with
   `--listen-mode realtime`.
2. Say *"go to sleep"* (the agent invokes `goto_sleep`): motors relax and the
   mic stream mutes — only the local wake-word detector keeps listening. *"hey
   jarvis, wake up"* (or invoking `wake_up`) brings it back.
3. **Loading the robot:** discovered providers aren't auto-connected — the agent
   loads `reachy` via the `apps` provider's `load_provider` affordance. So an
   early utterance like *"wake up and look around"* gets the robot online; head
   animation then works on subsequent replies. (Before it's loaded, embodiment is
   a quiet no-op — the audio loop still works.)

## Notes / knobs

- Config: `demo/.sloppy/config.yaml` (loaded in isolation via `HOME=demo/`; your
  global `~/.sloppy` is untouched).
- Mic input is sloppy's `streamCommand` → `audio_stream_client.py` → the
  provider's socket (`/tmp/slop/reachy_audio.sock`, override with the provider's
  `--audio-socket`). The client exits non-zero on EOF on purpose: that is what
  makes sloppy's continuous mode reconnect after a provider restart.
- Wake tuning on the provider: `--wake-model` (an openWakeWord name or a path
  to a custom .onnx — drop in a "hey sloppytron" model here later),
  `--wake-threshold`, `--silence-stop-seconds`, `--preroll-seconds`,
  `--vad-threshold`. `python audio_router.py` runs the router standalone and
  prints scores/gate transitions for tuning.
- Long motions (`goto_pose`, `play_emotion`, …) now return `accepted` and run in
  the background; while one plays, `head.set_pose` returns a `conflict` error, so
  talking animation pauses during emotions and resumes after the `action-finished`
  event.
- Override hosts/ports: gateway flags `--port` / `--tts-upstream`; LLM model via
  the `llm` block in `demo/.sloppy/config.yaml`.
```
