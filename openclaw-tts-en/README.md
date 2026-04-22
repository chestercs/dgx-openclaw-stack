# openclaw-tts-en

OpenAI-compatible `/v1/audio/speech` service backed by
[Kokoro 82M](https://huggingface.co/hexgrad/Kokoro-82M) — the Apache 2.0 open-
weights TTS model that sits at the top of TTS Arena's open-weights leaderboard.

This service is the English half of the bilingual TTS surface in this stack.
The router (`openclaw-tts-router`) sits in front of it and exposes a single
endpoint to OpenClaw via `messages.tts.providers.openai.baseUrl`.

## Why a custom thin wrapper

The popular `remsky/Kokoro-FastAPI` image is broken on DGX Spark / ARM64 +
Blackwell (sm_100 / sm_120) at the time of writing — see upstream
[issue #401](https://github.com/remsky/Kokoro-FastAPI/issues/401) ("no kernel
image available for execution on device" / "exec format error") and the
unmerged ARM64 nightly-torch wrapper PR
[#403](https://github.com/remsky/Kokoro-FastAPI/pull/403).

The cu130 wheel pattern used by the rest of this stack's vLLM services is the
proven path on GB10. ~80 lines of FastAPI on top of Kokoro's `KPipeline` is
the simpler answer than waiting on upstream.

## Model + voice catalog

- **Model**: `hexgrad/Kokoro-82M`, 82M params, Apache 2.0.
- **Sample rate**: 24 kHz mono.
- **Languages**: US English (`a*` voice prefix), UK English (`b*` voice prefix).
  `KPipeline` is initialized lazily per language and stays resident after
  first use.
- **Voices baked into the image**: only the A / A- / B- grade voices from
  Kokoro's `VOICES.md` ship — `af_heart` (A, default), `af_bella`, `af_nicole`,
  `af_aoede`, `af_kore`, `af_sarah`, `am_michael`, `am_fenrir`, `am_puck`,
  `bf_emma`. Add more by extending `allow_patterns` in the Dockerfile and
  rebuilding (each voice is ~523 KB).

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/audio/speech` | Bearer (`TTS_API_TOKEN`) | OpenAI Audio API. Returns wav / flac / ogg / pcm. |
| `GET` | `/v1/voices` | Bearer | Lists baked voices with their language hint. |
| `GET` | `/healthz` | _none_ | Reports checkpoint dir presence + pipelines loaded. |

The router transcodes wav to mp3 / opus / aac downstream — this service stays
deliberately wav-only to avoid a second ffmpeg dependency on the GPU image.

## Resource footprint

| Resource | Steady state |
|---|---|
| VRAM | ~500 MB – 1 GB (single English pipeline). |
| RAM | ~1.5 – 2 GB (model weights + Python). |
| Disk | ~1.5 GB (cu130 torch + voices + base image). |
| Latency | ~1.5–2× real-time on GB10 GPU; Kokoro is intentionally a small fast model. |

Coexists comfortably with the Gemma-4 NVFP4 LLM on the same GB10 GPU.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TTS_API_TOKEN` | yes | — | Bearer token clients must present. The router uses this as `EN_API_TOKEN`. |
| `DEFAULT_VOICE` | no | `af_heart` | Voice used when the request omits one. |
| `KOKORO_DEVICE` | no | `cuda` | Set to `cpu` to run without a GPU (~10× slower). |
| `KOKORO_LOCAL_DIR` | no | `/opt/checkpoints/kokoro` | Where the baked voice + weight files live in the image. |
| `KOKORO_REPO` | no | `hexgrad/Kokoro-82M` | HF repo id used at build time. |

## License

This wrapper is MIT-licensed. The Kokoro model weights are Apache 2.0 — safe
for commercial use under the model's license terms. See
<https://huggingface.co/hexgrad/Kokoro-82M> for details.
