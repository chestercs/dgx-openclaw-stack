# openclaw-tts-f5hun (Hungarian — opt-in, CC-BY-NC)

OpenAI-compatible `/v1/audio/speech` service backed by an
[F5-TTS](https://github.com/SWivid/F5-TTS) Hungarian fine-tune. Pairs with the
English [`openclaw-tts-en`](../openclaw-tts-en) service behind the
[`openclaw-tts-router`](../openclaw-tts-router) for a bilingual TTS surface.

## ⚠️ License — read this first

The wrapper code in this directory (Dockerfile, `app.py`,
`fetch_default_voice.py`, `requirements.txt`) is MIT-licensed (see the repo
root `LICENSE`).

The **model weights** the build pulls from HuggingFace —
[`sarpba/F5-TTS_V1_hun_v2`](https://huggingface.co/sarpba/F5-TTS_V1_hun_v2) —
are distributed under **CC-BY-NC-4.0** (Creative Commons,
**non-commercial only**). Using this service in any commercial deployment
without first replacing the checkpoint with one you have a commercial license
to is a license violation.

This repo ships **no model weights of any kind**. The Dockerfile is an
instruction set; building it is what triggers the download (and your
acceptance of the upstream model license). If you don't want to opt into
CC-BY-NC content, **don't build this service** — leave the HU profile parked
(see "Activation", below) and the rest of the stack runs EN-only.

For a commercial-grade Hungarian TTS, override `F5_CHECKPOINT` / `F5_VOCAB`
to point at a checkpoint with a license that fits your use case.

## Why not ship HU on by default

The router defaults to EN-only because the EN backend (Kokoro 82M) is Apache
2.0 — safe for any consumer of this repo. Hungarian deployments with
non-commercial usage opt in via the `hu` Docker Compose profile (the service
won't even start without it).

## Activation

Two pieces are required on a fresh stack:

1. **Set `F5HUN_API_TOKEN` in `.env`.** `bootstrap.sh` generates this
   automatically when you re-run it after pulling the HU support; if you
   want to set it by hand: `openssl rand -base64 64`.
2. **Activate the `hu` Compose profile.** Either of:
   - Add `COMPOSE_PROFILES=hu` to `.env` (then plain
     `docker compose up -d` brings the HU service up).
   - Or pass `--profile hu` on the command line: `docker compose --profile hu up -d`.

Without the profile, `openclaw-tts-f5hun` does not start; the router
auto-detects the missing backend and serves EN-only (HU voice ids return 404,
the diacritic autodetect is a no-op). With the profile and the token, the
router activates the HU voice ids (`default_hu`, `hu_diana`) and the diacritic
autodetect re-routes Hungarian-text requests to this backend transparently.

## Model + voice catalog

- **Default checkpoint**: `sarpba/F5-TTS_V1_hun_v2` (CC-BY-NC-4.0).
- **Sample rate**: 24 kHz mono.
- **Default reference voice**: Diana Majlinger reading "Egri csillagok"
  (Geza Gardonyi). Source: LibriVox public-domain recording, fetched at build
  time from the `KTH/hungarian-single-speaker-tts` HF dataset. Diana
  Majlinger is the speaker behind the CSS10 Hungarian corpus that fed the
  fine-tune's training data, so cloning quality is in-distribution.
- **Add your own reference voice**: drop `<name>.wav` (8–14 s clean speech,
  24 kHz mono preferred) and `<name>.txt` (its transcript, exact wording) into
  the runtime `/app/voices` bind-mount, then call the service with
  `voice: "<name>"`.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/audio/speech` | Bearer (`TTS_API_TOKEN`) | OpenAI Audio API. Returns wav / flac / ogg / pcm. |
| `GET` | `/v1/voices` | Bearer | Lists voices the runtime sees + the default. |
| `GET` | `/healthz` | _none_ | Reports checkpoint / vocab / default-voice / model-loaded. |

The router fronts this service and transcodes wav→mp3/opus/aac downstream;
this service stays deliberately wav-only to avoid a second ffmpeg dependency
on the GPU image.

## Resource footprint

| Resource | Steady state |
|---|---|
| VRAM | ~1 GB (F5-TTS V1 + vocoder, single stream). |
| RAM | ~2.5 GB (model + Python). |
| Disk | ~6 GB (cu130 torch + safetensors + base image). |
| Latency | ~1.18 s for a 3 s sentence on GB10 (~2.5× real-time). |

Coexists with the Gemma 4 NVFP4 LLM and the Kokoro EN service on the same GB10
GPU at the default `LLM_GPU_MEM_UTIL=0.68`.

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `TTS_API_TOKEN` | yes | — | Bearer token clients must present. The router uses this as `F5HUN_API_TOKEN`. |
| `DEFAULT_VOICE` | no | `default_hu` | Voice used when the request omits one. |
| `F5_DEVICE` | no | `cuda` | Set to `cpu` to run without a GPU (much slower). |
| `F5_CHECKPOINT` | no | `/opt/checkpoints/sarpba_v1_hun_v2/model_927900.safetensors` | Override to point at a different checkpoint (e.g. one with a commercial license). |
| `F5_VOCAB` | no | `/opt/checkpoints/sarpba_v1_hun_v2/vocab.txt` | Override to match the alternate checkpoint. |
| `F5_MODEL_NAME` | no | `F5TTS_v1_Base` | F5-TTS architecture preset. |

## License (wrapper)

MIT — see repo root `LICENSE`.

## License (model weights)

`sarpba/F5-TTS_V1_hun_v2` is **CC-BY-NC-4.0**. By building this image you
accept the upstream model license. Replace `F5_CHECKPOINT` / `F5_VOCAB` with
a commercially-licensed checkpoint if you need commercial use.
