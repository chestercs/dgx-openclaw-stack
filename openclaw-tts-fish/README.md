# openclaw-tts-fish

OpenAI-compatible Text-to-Speech service backed by **Fish Audio S2 Pro**,
self-hosted on NVIDIA GB10 (Grace-Blackwell, sm_120, aarch64, CUDA 13) via
**SGLang-Omni**. Replaces the legacy 3-service Kokoro EN + F5-TTS HU + router
pipeline with one container that handles 80+ languages out of one checkpoint,
including English and Hungarian, with reference-audio voice cloning.

## License

> **Fish Audio Research License — non-commercial use only.**
>
> Model weights (`fishaudio/s2-pro`, ~11 GB) are pulled at build time. By
> building this image you accept the upstream license. Commercial deployments
> require a separate license from Fish Audio (`business@fish.audio`) or a
> swap to a commercially-licensed checkpoint via the `FISH_REPO` build arg.
>
> Wrapper code in `./server/` is MIT.

## What it is

- Single self-hosted container.
- OpenAI-compatible endpoint: `POST /v1/audio/speech` (Bearer-auth via
  `TTS_API_TOKEN`).
- Two-process inside: a FastAPI shim on `:8080` (auth + voice→references
  mapping + onset silence pad) wrapping the SGLang-Omni native HTTP server
  on loopback `:9090`.
- Voice cloning: any 10-30 s mono WAV + transcript at `/app/voices/<name>.{wav,txt}`.

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET    | `/healthz` | none | engine readiness + voice catalog + GPU compat |
| GET    | `/v1/voices` | Bearer | list mounted voices |
| POST   | `/v1/audio/speech` | Bearer | synthesize speech |

`POST /v1/audio/speech` accepts the OpenAI shape:

```json
{ "input": "Hello world.", "voice": "default_en", "response_format": "wav" }
```

Optional pass-through fields: `speed`, `stream`, `temperature`, `top_p`,
`top_k`, `repetition_penalty`, `seed`, `max_new_tokens`.

## Voice cloning workflow

The shim maps the OpenAI `voice` string to two files in `/app/voices/`:

```
/app/voices/myclone.wav   # 10-30 s clean mono, 16-24 kHz preferred
/app/voices/myclone.txt   # verbatim transcript of myclone.wav
```

Add a voice without restarting the container:

```bash
docker cp myclone.wav ${CONTAINER_NAME_PREFIX:-}openclaw-tts-fish:/app/voices/
docker cp myclone.txt ${CONTAINER_NAME_PREFIX:-}openclaw-tts-fish:/app/voices/
```

Then request it:

```bash
curl -H "Authorization: Bearer $TTS_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"input":"Hello world.","voice":"myclone"}' \
     http://127.0.0.1:8091/v1/audio/speech --output out.wav
```

## Default voices shipped

| Voice | Source | License |
|-------|--------|---------|
| `default_en` | LibriSpeech (LibriVox excerpt) | Public domain |
| `default_hu` | KTH/hungarian-single-speaker-tts — Diana Majlinger, "Egri csillagok" | Public domain (LibriVox) |

Both are seeded at build time via `fetch_default_voices.py`. They land in
`/app/voices_seed/` inside the image and get copied into `/app/voices/` on
first start without overwriting user voices.

## Why we build from source

The upstream reference image is `frankleeeee/sglang-omni:dev` — amd64-only as
of 2026-05. GB10 is aarch64 + Blackwell sm_120 + CUDA 13, so we build on a
`nvidia/cuda:13.0.0-cudnn-devel-ubuntu24.04` base and let SGLang-Omni
compile `sgl-kernel` from source against the cu130 torch wheels. First build
is long (~15-30 min including the 11 GB model download); subsequent builds
hit the layer cache.

## Env vars

| Var | Default | Purpose |
|-----|---------|---------|
| `TTS_API_TOKEN` | *(required)* | Bearer auth — startup fails fast if unset |
| `TTS_FISH_DEFAULT_VOICE` | `default_en` | Voice used when client omits `voice` |
| `TTS_FISH_DEVICE` | `cuda` | Set to `cpu` for slow CPU fallback |
| `TTS_FISH_LEADING_SILENCE_MS` | `300` | Onset pad to defend against Whisper STT clip ("Szia" → "Zia"). Set 0 to skip |
| `FISH_ENGINE_PORT` | `9090` | Internal loopback for the SGLang-Omni child process |
| `FISH_ENGINE_READY_DEADLINE_S` | `600` | How long startup waits for SGLang-Omni's /health to flip |

## Troubleshooting

- **`{"status":"starting"}` for >10 minutes**: SGLang-Omni cold load on first
  start can take several minutes (model decompress + CUDA kernels JIT-compile).
  Watch `docker compose logs -f openclaw-tts-fish`.
- **`404 voice 'x' not found`**: `docker exec openclaw-tts-fish ls /app/voices`
  to confirm the file pair landed; check the `.txt` file exists alongside the
  `.wav`.
- **Garbled / silent audio on Hungarian**: S2 Pro is multilingual but
  Hungarian is "Tier 3". Voice cloning with a clean magyar reference clip
  (5-15 s, single speaker, no music/noise) usually fixes it.
- **OOM during build or first synth**: check that other GPU consumers
  (ComfyUI, vLLM tweaks) are not pinning the GB10's unified memory. Stop
  ComfyUI compose before first build/start, then bring it back once Fish is
  healthy.
