# Voxtral TTS on NVIDIA DGX Spark with vLLM-Omni

This note documents a working setup for serving `mistralai/Voxtral-4B-TTS-2603`
on an NVIDIA DGX Spark / GB10 machine, then using it from a separate
speech-to-speech client over an OpenAI-compatible `/v1/audio/speech` endpoint.

Tested shape:

- Host: NVIDIA DGX Spark, Linux `aarch64`
- Docker with NVIDIA GPU runtime
- Base image: `ghcr.io/timothystewart6/vllm-gb10:v0.20.1-gb10.0`
- Python package: `vllm-omni==0.20.0`
- Model: `mistralai/Voxtral-4B-TTS-2603`
- API port: `8091`

## Why A Custom Deploy Config Is Needed

The default Voxtral TTS vLLM-Omni deploy config is tuned for larger/more typical
GPU setups. On DGX Spark / GB10, the first stage can reserve too much shared GPU
memory for KV cache, leaving the second TTS/audio stage unable to initialize.

The important Spark-specific tuning is:

- stage 0 `gpu_memory_utilization: 0.45`
- stage 1 `gpu_memory_utilization: 0.1`
- reduced `max_num_seqs`
- reduced `max_num_batched_tokens`
- both stages on GPU `0`

Also note that `--omni` is required. Without it, the model architecture
`VoxtralTTSForConditionalGeneration` is not registered by plain vLLM.

## Directory Layout

On the Spark:

```bash
mkdir -p ~/vllm-omni-voxtral
cd ~/vllm-omni-voxtral
```

## Dockerfile

Create `Dockerfile`:

```dockerfile
FROM ghcr.io/timothystewart6/vllm-gb10:v0.20.1-gb10.0

RUN python3 -m pip install --no-cache-dir vllm-omni==0.20.0

COPY voxtral_tts_spark.yaml /opt/vllm-omni/voxtral_tts_spark.yaml

EXPOSE 8091

CMD ["vllm-omni", "serve", "mistralai/Voxtral-4B-TTS-2603", "--host", "0.0.0.0", "--port", "8091", "--omni", "--deploy-config", "/opt/vllm-omni/voxtral_tts_spark.yaml"]
```

## Spark Deploy Config

Create `voxtral_tts_spark.yaml`:

```yaml
async_chunk: true

connectors:
  connector_of_shared_memory:
    name: SharedMemoryConnector
    extra:
      shm_threshold_bytes: 65536
      codec_streaming: true
      connector_get_sleep_s: 0.01
      connector_get_max_wait_first_chunk: 3000
      connector_get_max_wait: 300
      codec_chunk_frames: 25
      codec_chunk_frames_at_begin: 5
      codec_left_context_frames: 25

stages:
  - stage_id: 0
    max_num_batched_tokens: 8192
    max_num_seqs: 4
    gpu_memory_utilization: 0.45
    enforce_eager: false
    trust_remote_code: true
    enable_prefix_caching: false
    async_scheduling: true
    max_model_len: 4096
    devices: "0"
    output_connectors:
      to_stage_1: connector_of_shared_memory
    default_sampling_params:
      temperature: 0.0
      top_p: 1.0
      top_k: -1
      max_tokens: 1024
      seed: 42
      repetition_penalty: 1.1
      extra_args:
        cfg_alpha: 1.2
    tokenizer_mode: mistral
    config_format: mistral
    load_format: mistral
    skip_mm_profiling: true
    enable_chunked_prefill: false

  - stage_id: 1
    max_num_seqs: 4
    gpu_memory_utilization: 0.1
    enforce_eager: true
    trust_remote_code: true
    enable_prefix_caching: false
    async_scheduling: false
    max_num_batched_tokens: 8192
    max_model_len: 8192
    devices: "0"
    input_connectors:
      from_stage_0: connector_of_shared_memory
    default_sampling_params:
      temperature: 0.9
      top_p: 0.8
      top_k: 40
      max_tokens: 1024
      seed: 42
      repetition_penalty: 1.05
    tokenizer_mode: mistral
    config_format: mistral
    load_format: mistral
    skip_mm_profiling: true
```

## Build The Image

```bash
docker build -t spark-agents/vllm-omni-voxtral:0.20.0-spark .
```

## Run The Server

If your Hugging Face token/cache is on the host, mount it into the container:

```bash
docker run -d \
  --name voxtral-tts-spark \
  --gpus all \
  --ipc=host \
  -p 8091:8091 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  spark-agents/vllm-omni-voxtral:0.20.0-spark
```

For an ad-hoc run using an external config file instead of the baked `CMD`:

```bash
docker run -d \
  --name voxtral-tts-spark \
  --gpus all \
  --ipc=host \
  -p 8091:8091 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v "$PWD":/config:ro \
  spark-agents/vllm-omni-voxtral:0.20.0-spark \
  vllm-omni serve mistralai/Voxtral-4B-TTS-2603 \
    --host 0.0.0.0 \
    --port 8091 \
    --omni \
    --deploy-config /config/voxtral_tts_spark.yaml
```

Watch startup logs:

```bash
docker logs -f voxtral-tts-spark
```

Expected successful startup includes:

```text
Orchestrator ready with 2 stages
Supported tasks: {'speech', 'generate'}
Route: /v1/audio/speech, Methods: POST
Route: /v1/audio/speech/stream, Endpoint: streaming_speech
Route: /v1/audio/voices, Methods: GET
Application startup complete.
```

## Smoke Tests

Health:

```bash
curl -i http://127.0.0.1:8091/health
```

Voices:

```bash
curl -sS http://127.0.0.1:8091/v1/audio/voices
```

Expected voices:

```json
{
  "voices": [
    "ar_male",
    "casual_female",
    "casual_male",
    "cheerful_female",
    "de_female",
    "de_male",
    "es_female",
    "es_male",
    "fr_female",
    "fr_male",
    "hi_female",
    "hi_male",
    "it_female",
    "it_male",
    "neutral_female",
    "neutral_male",
    "nl_female",
    "nl_male",
    "pt_female",
    "pt_male"
  ],
  "uploaded_voices": []
}
```

Generate a WAV:

```bash
curl -sS --max-time 180 \
  -X POST http://127.0.0.1:8091/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mistralai/Voxtral-4B-TTS-2603",
    "input": "Hello from Voxtral running locally on DGX Spark.",
    "voice": "casual_male",
    "response_format": "wav"
  }' \
  --output voxtral-test.wav

file voxtral-test.wav
```

Expected file shape:

```text
RIFF (little-endian) data, WAVE audio, Microsoft PCM, 16 bit, mono 24000 Hz
```

Generate raw PCM:

```bash
curl -sS --max-time 180 \
  -w "ttfb=%{time_starttransfer} total=%{time_total} bytes=%{size_download} status=%{http_code}\n" \
  -X POST http://127.0.0.1:8091/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mistralai/Voxtral-4B-TTS-2603",
    "input": "Short latency test.",
    "voice": "casual_male",
    "response_format": "pcm",
    "stream": true
  }' \
  --output /dev/null
```

One observed warm result:

```text
ttfb=0.002343 total=2.430056 bytes=138240 status=200
```

## Use From A Speech-To-Speech Client

From another machine on the same network, point the TTS backend at:

```text
http://<spark-hostname-or-ip>:8091/v1
```

Example with `hf-speech-to-speech` using an OpenAI-compatible speech endpoint:

```bash
speech-to-speech \
  --mode local \
  --device mps \
  --stt parakeet-tdt \
  --parakeet_tdt_device mps \
  --llm_backend mlx-lm \
  --model_name mlx-community/Qwen3-4B-Instruct-2507-bf16 \
  --tts openai-speech \
  --openai_speech_base_url "http://<spark-hostname-or-ip>:8091/v1" \
  --openai_speech_model "mistralai/Voxtral-4B-TTS-2603" \
  --openai_speech_voice casual_male \
  --openai_speech_response_format pcm \
  --openai_speech_stream true
```

## Troubleshooting

### Plain `vllm serve` says Voxtral TTS architecture is unsupported

Use `vllm-omni serve ... --omni`, not plain `vllm serve`.

The failure looks like:

```text
Model architectures ['VoxtralTTSForConditionalGeneration'] are not supported
```

### Stage 1 dies with not enough free memory

The failure looks like:

```text
Free memory on device cuda:0 (...) on startup is less than desired GPU memory utilization
StageEngineCoreProc died during READY
```

Use the Spark-specific deploy config above. The key setting is reducing stage 0
from the default `gpu_memory_utilization: 0.8` to `0.45`.

### `/health` resets while booting

During first startup, the server may accept the port before both stages are
ready. Wait for:

```text
Application startup complete.
```

Then retry:

```bash
curl -i http://127.0.0.1:8091/health
```

### `ffmpeg` warning

The container may log:

```text
Couldn't find ffmpeg or avconv
```

For the tested `wav` and raw `pcm` responses, this warning did not block
generation.

## References

- Voxtral TTS model: <https://huggingface.co/mistralai/Voxtral-4B-TTS-2603>
- vLLM-Omni speech API docs: <https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/speech_api/>
- NVIDIA DGX Spark vLLM guide: <https://build.nvidia.com/spark/vllm/i>
