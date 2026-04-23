# openclaw-stt-whisper

Self-built speech-to-text service for the DGX OpenClaw stack. OpenAI-compatible
`/v1/audio/transcriptions` + `/v1/audio/translations` on top of
[`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) running
`Systran/faster-whisper-large-v3` by default.

## Why a custom wrapper instead of `ghcr.io/speaches-ai/speaches`

Upstream speaches publishes CUDA-12.6.3 variants. On a GB10 (Grace-Blackwell,
sm_120) the bundled CTranslate2 rejects every low-precision compute type
(`float16` / `int8_float16` / `int8_bfloat16` → _"target device or backend do
not support efficient ... computation"_), and the `float32` fallback runs on
CUDA cores at real-time factor ~1× while also destabilizing numerically
(Whisper generation collapsing into compression-ratio loops). A `CUDA 13.0.0`
base with the cu130 PyTorch wheels and current `faster-whisper` (≥ 1.2) is the
proven path on GB10 — the surrounding vLLM and Kokoro services already use it.

If the speaches upstream later publishes a Blackwell-tensor-core image
(e.g. `latest-cuda-13.x`), swapping `build:` back to `image:` in
`docker-compose.yml` is a 15-line diff; this wrapper is ~150 LOC and easy to
retire.

## License

- **Wrapper code** (`Dockerfile`, `server/app.py`, `server/requirements.txt`):
  MIT, matching the rest of this repo.
- **Model weights** (`Systran/faster-whisper-large-v3` and the other official
  Systran CT2 rehosts): **MIT**, inherited from OpenAI's Whisper release.
  No CC-BY-NC component — this service ships in the default profile with no
  opt-in gate.

## Configuration

Environment variables (all read at service start — restart the container to
apply changes):

| Variable | Default | Purpose |
|---|---|---|
| `STT_API_TOKEN` | _(empty)_ | Bearer token; when set, every `/v1/*` request must carry `Authorization: Bearer <token>`. `/health` always bypasses auth for Docker healthchecks. |
| `WHISPER_MODEL` | `Systran/faster-whisper-large-v3` | HuggingFace model id. Swap to `deepdml/faster-whisper-large-v3-turbo-ct2` for 8× speed / half VRAM; Hungarian WER on turbo is unvalidated. |
| `WHISPER_DEVICE` | `cuda` | `cuda` or `cpu`. CPU is ~50× slower but a workable fallback on GPU-less hosts. |
| `WHISPER_COMPUTE_TYPE` | `float16` | One of `float16`, `bfloat16`, `int8_float16`, `int8_bfloat16`, `int8`, `float32`. See fallback ladder below. |
| `WHISPER_LANGUAGE` | _(empty → autodetect)_ | ISO-639-1 code (e.g. `hu`, `en`). Leave empty so Whisper detects per request. |

## `compute_type` fallback ladder on Blackwell

If the tensor-core kernels for your chosen precision aren't compiled into
CTranslate2 for sm_120, the service will raise `ValueError` at the first
transcribe. Try these in order until one loads:

1. `float16` (default — fastest, ~3 GB VRAM, full precision).
2. `bfloat16` (same speed class on Hopper+Blackwell, numerically more robust).
3. `int8_float16` (~1.5 GB VRAM, ~5-10% WER cost).
4. `int8_bfloat16` (same VRAM as above, different kernel class).
5. `int8` (CPU-style quant; slow on GPU, reliable fallback).
6. `float32` (~6 GB VRAM, slowest on the GPU but no quantization or fused
   kernels — lossless baseline).

## Endpoints

- `GET /health` — always unauth. Returns `{status, model, device, compute_type, loaded}`.
- `GET /v1/models` — Bearer-auth. Returns the configured model in OpenAI-list shape.
- `POST /v1/audio/transcriptions` — Bearer-auth. Multipart `file` + optional
  `language`, `response_format` (one of `json` / `verbose_json` / `text` /
  `srt` / `vtt`), `temperature`. Returns the transcript in the requested
  format.
- `POST /v1/audio/translations` — same shape as transcriptions, but Whisper's
  translate task (source language → English text).

## First-boot behavior

No model weights are baked into the image — the ~3 GB large-v3 CT2 weights
download from HuggingFace on the first `/v1/audio/transcriptions` request
(or on an explicit warm-up call). Subsequent boots reuse the
`stt-whisper-hf-cache` Docker volume, so the download happens exactly once
per host.
