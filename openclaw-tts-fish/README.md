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

### Full request-parameter reference

Everything except `voice` passes through to the SGLang-Omni engine verbatim
(`voice` is resolved by the shim into a `references[]` clone pair). Upstream
defaults as of 2026-06:

| Field | Type | Upstream default | Notes |
|-------|------|------------------|-------|
| `input` | string | *(required)* | Text to synthesize |
| `voice` | string | `TTS_FISH_DEFAULT_VOICE` | Resolved to `/app/voices/<voice>.{wav,txt}` |
| `response_format` | string | `wav` | `mp3`/`ogg`/`opus`/`aac`/`flac` transcoded by the shim |
| `speed` | float | `1.0` | Playback speed multiplier |
| `stream` | bool | `false` | SSE audio chunks; the onset silence pad is skipped when streaming |
| `temperature` | float | `0.8` | Sampling temperature |
| `top_p` | float | `0.8` | Nucleus sampling |
| `top_k` | int | `30` | **Must be `-1` or `1..30`** — out-of-range fails the S2 Pro pipeline with a 500, not a clean 4xx (upstream limitation) |
| `repetition_penalty` | float | `1.1` | |
| `seed` | int | *(unset)* | Set for reproducible audio (regression tests) |
| `max_new_tokens` | int | `2048` | Semantic-token cap — raise for very long passages |

Deploy-wide baselines for the sampling fields can be set via `TTS_FISH_*` env
vars (see table below); per-request values always win.

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

Bundled in the repo at `server/voices/` and baked into the image's
`/app/voices_seed/`; the shim copies them into `/app/voices/` on first start
without overwriting user voices. (The old HF-dataset fetcher was removed
2026-06-10 — it soft-failed on every aarch64 build because `datasets` audio
decoding needs torchcodec, which has no aarch64 wheel, so images shipped with
an empty seed dir.)

| Voice | Timbre | Source | License |
|-------|--------|--------|---------|
| `default_en` | US female, warm | Kokoro 82M `af_heart` synthesis | Apache-2.0 (generated) |
| `bella` | US female, bright | Kokoro `af_bella` | Apache-2.0 (generated) |
| `nicole` | US female, soft | Kokoro `af_nicole` | Apache-2.0 (generated) |
| `michael` | US male, neutral | Kokoro `am_michael` | Apache-2.0 (generated) |
| `fenrir` | US male, low register | Kokoro `am_fenrir` | Apache-2.0 (generated) |
| `emma` | UK female | Kokoro `bf_emma` | Apache-2.0 (generated) |
| `default_hu` | HU female | KTH/hungarian-single-speaker-tts — Diana Majlinger, "Egri csillagok" | Public domain (LibriVox) |

Patcher step 11 maps friendly aliases on top: `english`/`narrator` →
`default_en`, `female` → `bella`, `male` → `michael`, `british` → `emma`,
`deep` → `fenrir`, `soft` → `nicole`, `magyar`/`hungarian` → `default_hu`.

## Why we build from source (and the two wheels that bit us)

The upstream reference image is `lmsysorg/sglang-omni:dev` — amd64-only as of
2026-06. GB10 is aarch64 + Blackwell sm_121 + CUDA 13, so we build on a
`nvidia/cuda:13.0.0-cudnn-devel-ubuntu24.04` base. Two dependency-resolution
traps shaped the Dockerfile — both produced a container whose engine died
before ready while the shim's `/healthz` still answered:

1. **PyPI `sgl_kernel` aarch64 wheel is a CUDA 12 build.** Inside a CUDA 13
   image its `common_ops.abi3.so` dlopen-fails with `libnvrtc.so.12: cannot
   open shared object file`. Fix: install the `+cu130` aarch64 wheel from the
   sgl-project/whl GitHub release page (the documented CUDA 13 path in sglang
   v0.5.8's install.md). The wheel ships sm90/sm100 SASS + embedded PTX — on
   GB10 (sm_121) the driver JIT-compiles the PTX, the same mechanism the
   official `lmsysorg/sglang:spark` image relies on.
2. **PyPI torch wheels for aarch64 are CPU-only.** The sglang-omni dependency
   resolution silently replaced the cu130 torch with `2.9.1+cpu`, after which
   the engine reported "CPU/No GPU detected". Fix: exact `+cu130` pre-pins,
   `--extra-index-url` + `--index-strategy unsafe-best-match` on the omni
   install, and a surgical `--no-deps --force-reinstall` re-pin afterwards
   with a build-time assert (provenance-checked via pip's `direct_url.json`,
   because the cu130 wheel's *internal* version metadata is identical to the
   cu12 one).

First build is long (~15-30 min including the 11 GB model download);
subsequent builds hit the layer cache.

## Env vars

| Var | Default | Purpose |
|-----|---------|---------|
| `TTS_API_TOKEN` | *(required)* | Bearer auth — startup fails fast if unset |
| `TTS_FISH_DEFAULT_VOICE` | `default_en` | Voice used when client omits `voice` |
| `TTS_FISH_DEVICE` | `cuda` | Set to `cpu` for slow CPU fallback |
| `TTS_FISH_LEADING_SILENCE_MS` | `300` | Onset pad to defend against Whisper STT clip ("Szia" → "Zia"). Set 0 to skip |
| `TTS_FISH_TEMPERATURE` | *(unset)* | Deploy-wide sampling baseline (request wins) |
| `TTS_FISH_TOP_P` | *(unset)* | " |
| `TTS_FISH_TOP_K` | *(unset)* | " — must be `-1` or `1..30` |
| `TTS_FISH_REPETITION_PENALTY` | *(unset)* | " |
| `TTS_FISH_MAX_NEW_TOKENS` | *(unset)* | " |
| `TTS_FISH_SPEED` | *(unset)* | " |
| `TTS_FISH_SEED` | *(unset)* | " — set for reproducible regression audio |
| `FISH_S2PRO_CONFIG` | `/opt/configs/s2pro_tts_gb10.yaml` | SGLang-Omni pipeline config. Image default is GB10-calibrated (capped KV pool, no startup torch-compile / CUDA-graph capture — rationale inline in the yaml). Upstream defaults: `/opt/sglang-omni/examples/configs/s2pro_tts.yaml` |
| `SGLANG_OMNI_STARTUP_TIMEOUT` | `3000` (compose) / `600` (upstream) | Engine-internal stage-ready watchdog (seconds) |
| `FISH_ENGINE_PORT` | `9090` | Internal loopback for the SGLang-Omni child process |
| `FISH_ENGINE_READY_DEADLINE_S` | `3300` (compose) / `600` (image) | How long the shim waits for SGLang-Omni's /health to flip — keep above `SGLANG_OMNI_STARTUP_TIMEOUT` so the engine's own error surfaces first |

## Troubleshooting

- **First boot takes 10-30+ minutes, later boots are fast**: expected on GB10.
  The cu130 `sgl-kernel` wheel ships sm90/sm100 SASS only; every kernel is
  JIT-compiled from embedded PTX for sm_121 on first launch. The result
  persists on the `tts-fish-cuda-jit-cache` volume — don't wipe it unless you
  enjoy the marathon. Watch `docker compose logs -f openclaw-tts-fish`.
- **Engine wedges during "Loading Fish audio decoder" and times out**: the KV
  pool ate the headroom. sglang sizes its static pool from memory visible at
  startup — on the unified GB10 pool that's "whatever vLLM left", so the
  upstream `mem_fraction_static: 0.85` allocates a ~16 GB KV cache and leaves
  nothing for the decoder + vocoder. The bundled GB10 yaml caps it at 0.5;
  if you still wedge, stop ComfyUI/video workloads and recreate.
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
