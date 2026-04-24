# OpenClaw STT — Whisper stack architecture

> Reference material: STT deployment details + schema.

## Overview

One service in the unified `llm/dgx-openclaw-stack/` compose:

| Service | Model | Port | VRAM | License | Profile |
|---|---|---|---|---|---|
| `openclaw-stt-whisper` | `Trendency/whisper-large-v3-hu` (default) / swap to `Systran/faster-whisper-large-v3` for the pure multilingual baseline | 8093 | ~3 GB (float16) | Apache-2.0 / MIT | default |

Self-built from `./openclaw-stt-whisper/server/` on `nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04`. ~150 LOC FastAPI wrapper around [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) exposing OpenAI-compatible `/v1/audio/transcriptions`, `/v1/audio/translations`, `/v1/models`, and `/health` endpoints.

## Why Whisper large-v3 + self-built CUDA 13 image

The backend choice was made 2026-04 against these candidates:

| Candidate | HU WER (FLEURS) | VRAM | Licence | OpenAI-compat server | Verdict |
|---|---|---|---|---|---|
| **Whisper large-v3 + self-built CUDA 13** | **14.1%** | ~3 GB | MIT + MIT | ✅ (this repo's wrapper) | ✅ chosen |
| Whisper large-v3-turbo (same wrapper) | not published | ~1.6 GB | MIT | ✅ | override (faster, unvalidated HU) |
| `ghcr.io/speaches-ai/speaches` (upstream) | 14.1% | ~3 GB | MIT | ✅ native | ❌ Blackwell sm_120 kernel gap |
| NVIDIA Parakeet-TDT 0.6B v3 | 15.72% | ~1.2 GB | CC-BY-4.0 | ❌ NeMo-only | wrapper burden |
| NVIDIA Canary-1B v2 | not published | ~2 GB | CC-BY-4.0 | ❌ NeMo-only | wrapper burden |
| Microsoft Phi-4 Multimodal | not supported | ~11 GB | MIT | ❌ | no Hungarian audio |
| Distil-Whisper | n/a | n/a | MIT | ✅ | English-only |

`14.1%` is from the [Whisper Notes benchmark post](https://whispernotes.app/blog/parakeet-v3-default-mac-model) (25-language FLEURS table) — the best validated Hungarian number among the OpenAI-compatible candidates.

### Why self-built instead of the speaches upstream image

On paper `ghcr.io/speaches-ai/speaches` is the ideal dependency: MIT license, active maintenance, ships OpenAI-compatible routes, zero custom code to maintain. The original 2026-04-23 plan selected it. In practice the upstream publishes only CUDA 12.6.3 variants, and on GB10 (sm_120) that image's bundled CTranslate2 rejects every low-precision compute type — `float16`, `int8_float16`, and `int8_bfloat16` all fail with _"target device or backend do not support efficient … computation"_. The `float32` fallback runs on CUDA cores at real-time factor ~1× (2 min audio = 2 min transcribe) while also destabilizing numerically (Whisper generation collapses into compression-ratio loops).

The fix is a CUDA 13 base + cu130 PyTorch wheels + current `faster-whisper` (≥ 1.2), which is the same wheel pattern the surrounding `vllm-llm` and `openclaw-tts-en` services already use successfully on GB10. The wrapper is ~150 LOC of FastAPI + a Bearer-auth middleware — trivial to retire if speaches upstream later publishes a Blackwell-tensor-core variant (swap `build:` back to `image:` in `docker-compose.yml`).

### `compute_type` fallback ladder on Blackwell

If the tensor-core kernels for your chosen precision aren't compiled into the CTranslate2 version that landed with `faster-whisper` at build time, the service raises `ValueError` on the first transcribe. Try these in order:

1. `float16` (default — fastest, ~3 GB VRAM, full precision).
2. `bfloat16` (same speed class on Blackwell, numerically more robust).
3. `int8_float16` (~1.5 GB VRAM, 5-10% WER cost).
4. `int8_bfloat16` (same VRAM as above, different kernel class).
5. `int8` (CPU-style quant; slow on GPU, reliable fallback).
6. `float32` (~6 GB VRAM, slowest on the GPU but no quantization or fused kernels — lossless baseline).

### When to switch back to upstream speaches

The moment `ghcr.io/speaches-ai/speaches` publishes a CUDA 13 variant (e.g. `latest-cuda-13.x`) that compiles CT2 tensor-core kernels for sm_120, the swap is a 15-line diff: replace the `build: ./openclaw-stt-whisper/server` + `image: openclaw-stt-whisper:0.1.0` lines with a single `image: ghcr.io/speaches-ai/speaches:<cuda-13-tag>`, re-map the env-var names (`STT_API_TOKEN` → `API_KEY`, `WHISPER_MODEL` → `WHISPER__MODEL`, etc.), and delete the `openclaw-stt-whisper/` directory. A GitHub watch on `speaches-ai/speaches` catches that release.

## Three voice surfaces in OpenClaw (easy to confuse)

OpenClaw has three speech-input paths; this service only backs two of them.

1. **Control UI realtime mic button** (`/chat?session=…`, mic icon in composer) — hard-wired to the browser's native `SpeechRecognition` / Web Speech API in `speech.ts`. Language support depends on the browser + OS; Chrome routes to Google STT, Firefox uses OS-native. **This service does NOT participate.** See [OpenClaw Chronicles 2026-04-18 mic fix](https://openclawchronicles.com/posts/openclaw-2026-4-18-control-ui-mic-fix/).
2. **Voice-note attachment** — drop a wav/mp3/m4a/opus into the chat composer, OpenClaw's `tools.media.audio` pipeline runs the file through the first matching `models[]` entry. The transcript replaces the message body (wrapped in an `[Audio]` block) and slash commands inside the transcript still execute. **This service backs this path.**
3. **Voicewake / Talk / VoiceCall nodes + Discord voice-channel** — the node pipelines (`docs.openclaw.ai/nodes/{talk,voicewake}`, `cli/voicecall`) use the same `tools.media.audio` configuration. Wake-word and push-to-talk voice interaction both route here. **This service backs this path.**

## OpenClaw wiring

`patch-config.mjs` step 14, env-gated by `STT_API_TOKEN`. Writes into `tools.media.audio`:

```json5
{
  "tools": {
    "media": {
      "audio": {
        "enabled": true,
        "models": [
          {
            "provider": "openai",
            "model": "Systran/faster-whisper-large-v3",
            "baseUrl": "http://openclaw-stt-whisper:8080/v1/",
            "headers": {
              "Authorization": "Bearer <STT_API_TOKEN>"
            }
          }
        ]
      }
    }
  }
}
```

**Why the Bearer lives in `headers.Authorization`, not `apiKey`:** the OpenClaw schema (see `docs.openclaw.ai/nodes/audio`) routes provider auth through the standard chain — `models.providers.openai.apiKey` or env vars or auth profiles. Writing the Whisper Bearer into the global `models.providers.openai` block would collide with any cloud OpenAI account the user might also configure. Per-entry `headers` overrides are explicitly supported by the schema and keep the STT token isolated.

**Upsert-by-`baseUrl`:** re-runs of the patcher don't clobber user-added entries. If the user has a Deepgram fallback or a local `whisper-cpp` CLI entry in `models[]`, they survive; only the entry whose `baseUrl` matches ours is updated.

**Env-gated skip:** unset `STT_API_TOKEN` leaves `tools.media.audio` alone. Combined with `profiles: ["never"]` on the service block, that is the full opt-out path.

## Model catalog

| Env override | VRAM | Trade-off |
|---|---|---|
| `STT_WHISPER_MODEL=Trendency/whisper-large-v3-hu` (default) | ~3 GB | Hungarian Whisper fine-tune (Apache-2.0). Published 11.26% CV19/20/21 WER with a clean train/eval split. 2026-04-24 GB10 validation: ~7-8 fewer mis-hearings per chapter than vanilla on LibriVox HU, holds up cleanly under telephone-bandpass + pink-noise degradation, and the JFK 11-sec EN reference clip transcribes identically to the vanilla baseline (no English-side regression at smoke scale). Auto-converts from HF transformers format to CT2 float16 on first boot (~3 min), cached in the stt-whisper-hf-cache volume thereafter. |
| `STT_WHISPER_MODEL=Systran/faster-whisper-large-v3` | ~3 GB | Pure multilingual baseline. MIT-licensed, FLEURS HU WER 14.1%. Pick this if your workload is English-heavy, you need the broadest out-of-language robustness, or you want the tightest licence (MIT vs Apache-2.0). On noisy HU this model fragments segments more (61 vs 36 on the 3-min stress test) but keeps the Hungarian proper nouns slightly more reliably. |
| `STT_WHISPER_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2` | ~1.6 GB | 8× faster, ~equal EN WER. HU WER NOT independently published — run your own samples before flipping. |
| `STT_WHISPER_COMPUTE_TYPE=int8_float16` | ~1.5 GB | 5-10% WER increase on any model. Safe VRAM fallback if the LLM + TTS squeeze the budget, or if a Blackwell sm_120 numerical issue appears. |

Community Hungarian fine-tunes we evaluated and **rejected**:

- **benmajor27/whisper-large-v3-hu_full** — published 8.86% CV17 WER looked promising, but 2026-04-24 validation showed it was trained on the full CV17 set and evaluated on CV17, so the published number is overfit. Out-of-distribution audio (LibriVox Petőfi, noisy synthetic phone-grade) causes the decoder to collapse into long compression-ratio loops (`"Tüz. Tüz. Tüz. ..."`, `"-‑-‑-‑-‑-‑"`, `"��������"`, random `"a the in the"` English tokens). Do not swap in this model.

### HuggingFace transformers → CT2 auto-conversion

When `STT_WHISPER_MODEL` points at a HuggingFace repo that does NOT ship CT2-format weights (e.g. most community Hungarian fine-tunes), the wrapper detects it on first boot and runs `ct2-transformers-converter --quantization float16` transparently. Cache lands in `/root/.cache/huggingface/ct2-converted/<safe-name>/` inside the `stt-whisper-hf-cache` volume. Subsequent boots skip the conversion and load straight into CT2.

Any HF repo id starting with `Systran/`, `deepdml/`, or `openai/` is treated as already-CT2 and loaded without conversion. Local paths (starting with `/` or `./`) are passed through unchanged.

## VRAM budget on GB10

At `LLM_GPU_MEM_UTIL=0.68` with Kokoro TTS running:

```
Gemma 4 NVFP4  ~96 GB
bge-m3          ~1.1 GB
Kokoro TTS      ~1 GB
Whisper large-v3 ~3 GB
────────────────────────
≈ 101 GB used, ~12 GB headroom on the 113 GB effective shared pool
```

Dropping to turbo + Kokoro leaves ~13.5 GB; adding F5-TTS HU on top consumes ~1 GB more.

## Environment variables

See `.env.example` "STT — faster-whisper large-v3" section. Key entries:

- `STT_API_TOKEN` — Bearer token (required to lock the service; leaving it empty disables Bearer auth).
- `STT_WHISPER_MODEL` — HuggingFace model id.
- `STT_WHISPER_COMPUTE_TYPE` — walk the fallback ladder above if `float16` isn't supported by your CT2 build.
- `STT_WHISPER_DEVICE` — `cuda` or `cpu`.
- `STT_WHISPER_BIND` / `STT_WHISPER_PORT` — host-side publish (loopback default).
- `OPENCLAW_STT_BASE_URL` / `OPENCLAW_STT_MODEL` / `OPENCLAW_STT_LANGUAGE` — patcher inputs.

## Verification

```bash
STT_KEY=$(grep '^STT_API_TOKEN=' .env | cut -d= -f2-)

# Health (unauth)
curl -s http://127.0.0.1:8093/health | jq .

# Models list (Bearer)
curl -s -H "Authorization: Bearer $STT_KEY" http://127.0.0.1:8093/v1/models | jq '.data[].id'

# Hungarian autodetect
curl -sS -X POST http://127.0.0.1:8093/v1/audio/transcriptions \
  -H "Authorization: Bearer $STT_KEY" \
  -F file=@/path/to/hungarian_sample.wav \
  -F model=Systran/faster-whisper-large-v3 \
  -F response_format=verbose_json | jq '.language, .text'
# expected: "hu" + accurate Hungarian diacritics

# Gateway-side config inspection
docker exec openclaw-cli jq '.tools.media.audio' $OPENCLAW_CONFIG_DIR/openclaw.json
```

## Troubleshooting

- **Cold start takes minutes on first boot** — ~3 GB of large-v3 CT2 weights download from HuggingFace. Subsequent boots reuse the `stt-whisper-hf-cache` volume.
- **401 Unauthorized from a gateway audio upload** — most likely `STT_API_TOKEN` drift between `.env` and the patched `openclaw.json`. Re-run `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli` to re-apply the patcher after rotating the token.
- **HU transcripts look rough on your actual audio** — the 14.1% FLEURS number is lab-clean narration. On noisy mic input, poorly-placed ékezetek, or heavy speaker accent, quality degrades. Options: (1) try `large-v3-turbo` for lower latency at comparable quality, (2) switch to `int8_float16` to see if precision matters on your audio, (3) look for a Hungarian Whisper fine-tune on HF, (4) file a follow-up with sample clips for investigation.
- **Blackwell / sm_120 kernel class unsupported** — first transcribe raises `ValueError: target device or backend do not support efficient <type> computation`. Walk the `compute_type` fallback ladder above (`float16` → `bfloat16` → `int8_float16` → `int8_bfloat16` → `int8` → `float32`). A `.env` line change + `docker compose up -d --force-recreate openclaw-stt-whisper` is enough; no rebuild.

## License

MIT Whisper weights (`Systran/faster-whisper-large-v3` is a CT2-quantized rehost of `openai/whisper-large-v3`, MIT) + MIT `faster-whisper` library + MIT wrapper code in this repo. No CC-BY-NC component — no opt-in profile gate needed, the service ships in the default profile.
