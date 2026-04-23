# OpenClaw STT — Whisper stack architecture

> Reference material: STT deployment details + schema.

## Overview

One service in the unified `llm/dgx-openclaw-stack/` compose:

| Service | Model | Port | VRAM | License | Profile |
|---|---|---|---|---|---|
| `openclaw-stt-whisper` | `Systran/faster-whisper-large-v3` | 8093 | ~3 GB (float16) | MIT | default |

Upstream image: `ghcr.io/speaches-ai/speaches-cuda`. No custom Dockerfile, no wrapper code — the stack consumes the upstream OpenAI-compatible `/v1/audio/transcriptions`, `/v1/audio/translations`, `/v1/models`, and `/health` endpoints directly.

## Why Whisper large-v3 + speaches

The backend choice was made 2026-04 against these candidates:

| Candidate | HU WER (FLEURS) | VRAM | Licence | OpenAI-compat server | Verdict |
|---|---|---|---|---|---|
| **Whisper large-v3 + speaches** | **14.1%** | ~3 GB | MIT + MIT | ✅ native | ✅ chosen |
| Whisper large-v3-turbo + speaches | not published | ~1.6 GB | MIT | ✅ native | override (faster, unvalidated HU) |
| NVIDIA Parakeet-TDT 0.6B v3 | 15.72% | ~1.2 GB | CC-BY-4.0 | ❌ NeMo-only | wrapper burden |
| NVIDIA Canary-1B v2 | not published | ~2 GB | CC-BY-4.0 | ❌ NeMo-only | wrapper burden |
| Microsoft Phi-4 Multimodal | not supported | ~11 GB | MIT | ❌ | no Hungarian audio |
| Distil-Whisper | n/a | n/a | MIT | ✅ | English-only |

`14.1%` is from the [Whisper Notes benchmark post](https://whispernotes.app/blog/parakeet-v3-default-mac-model) (25-language FLEURS table) — the best validated Hungarian number among the OpenAI-compatible candidates.

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
            "baseUrl": "http://openclaw-stt-whisper:8000/v1/",
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
| `STT_WHISPER_MODEL=Systran/faster-whisper-large-v3` (default) | ~3 GB | Best validated HU WER (14.1%). Use in doubt. |
| `STT_WHISPER_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2` | ~1.6 GB | 8× faster, ~equal EN WER. HU WER NOT independently published — run your own samples before flipping. |
| `STT_WHISPER_COMPUTE_TYPE=int8_float16` | ~1.5 GB | 5-10% WER increase on any model. Safe VRAM fallback if the LLM + TTS squeeze the budget, or if a Blackwell sm_120 numerical issue appears. |

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

- `STT_API_TOKEN` — Bearer token (required for the service; skip to opt out).
- `STT_WHISPER_MODEL` — HuggingFace model id.
- `STT_WHISPER_COMPUTE_TYPE` — `float16` (default) or `int8_float16`.
- `STT_WHISPER_DEVICE` — `cuda` or `cpu`.
- `STT_WHISPER_TTL` — seconds of idle before VRAM unload (0 = resident).
- `STT_SPEACHES_TAG` — pin the upstream image once validated.
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
- **Blackwell / sm_120 numerical issues** — CTranslate2 inference is independent of cuBLAS GEMM, so the speaches upstream image is usually stable. If you see NaN spans or garbled Unicode, `STT_WHISPER_COMPUTE_TYPE=int8_float16` is the first mitigation; a self-built `nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04` + `faster-whisper` pip install Dockerfile is the last-resort fallback.

## License

MIT Whisper weights (`Systran/faster-whisper-large-v3` is a CT2-quantized rehost of `openai/whisper-large-v3`, MIT) + MIT speaches wrapper. No CC-BY-NC component — no opt-in profile gate needed, the service ships in the default profile.
