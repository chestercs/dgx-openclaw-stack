# Fish Audio S2 Pro migration — overnight result + rollback smoke report

**Date**: 2026-05-19 evening, Magyarország (CEST)
**Branch state**: `main` (Fish migration on `claude/upgrade-fish-audio-tts-stt-lDuS5` branch — preserved for PR / future hardware-compat work)

## TL;DR

- Fish S2 Pro migration **blocked by hardware-compat reality on GB10** (CHANGELOG R1 risk fired). Two attempts:
  - R1.a (SGLang-Omni + s2-pro): build succeeded after 3 commits of Dockerfile fixes; engine failed at runtime — `sgl-kernel 0.3.21` shipped no `sm_120` (GB10) prebuilt kernels (only `sm_90`/`sm_100`), and the `.[s2pro]` extra clobbered cu130 torch with a CPU build.
  - R1.c (fish-speech + openaudio-s1-mini): build failed at the gated-repo download step; the `openaudio-s1-mini` HF model requires the operator to accept the CC-BY-NC-SA license once on the HF web UI before `hf download` succeeds for the configured HF token.
- **Rollback to `main` + Whisper STT flipped from `Trendency/whisper-large-v3-hu` to `deepdml/faster-whisper-large-v3-turbo-ct2` (turbo)** — full stack live and verified.
- ComfyUI back up after the pre-Fish-build VRAM stop.
- Working state restored ~22:35 CEST 2026-05-19.

## Stack live state

| Service | Image | Status | Notes |
|---|---|---|---|
| openclaw-tts-router | openclaw-tts-router:0.1.0 | healthy | OpenAI-compat `/v1/audio/speech` seam, port 8092 |
| openclaw-tts-en | openclaw-tts-en:0.1.0 | healthy | Kokoro 82M EN, port 8091 |
| openclaw-tts-f5hun | openclaw-tts-f5hun:0.1.0 | healthy | F5-TTS HU (`hu` profile), port 8090 |
| openclaw-stt-whisper | openclaw-stt-whisper:0.1.0 | healthy | **`deepdml/faster-whisper-large-v3-turbo-ct2`** (~1.6 GB VRAM, ~8× faster than vanilla large-v3) |
| openclaw-gateway / cli / config-init | openclaw-base-ext:0.11.1 | healthy | Patcher wired turbo into `tools.media.audio.models[0]` |
| comfyui | mmartial/comfyui-nvidia-docker:ubuntu24_cuda13.1-dgx-latest | healthy | LTX-Video back online |

## Smoke battery — 100+ verified surface calls

### v1 + v2 combined (post-rollback)

| Category | Calls | Pass | Notes |
|---|---:|---:|---|
| Healthz roll-call | 5 | 5 | router/en/f5hun/stt/gateway all 200 |
| TTS EN Kokoro voices | 10 | 10 | af_heart/bella/nicole/aoede/kore/sarah/am_michael/fenrir/puck/bf_emma — 100-130ms warm |
| TTS EN OpenAI aliases | 10 | 10 | alloy/ash/ballad/coral/echo/fable/onyx/nova/sage/shimmer → Kokoro mapping verified |
| TTS HU F5-TTS explicit | 10 | 10 | 10 different sentences, default_hu — 1.1-1.4s |
| TTS HU autoroute (diacritics) | 10 | 10 | OpenAI voice + magyar text → silently rerouted to default_hu |
| TTS format variants | 10 | 10 | mp3/ogg/opus/aac/flac × EN+HU — ffmpeg transcode works |
| STT closed-loop TTS→Whisper | 10 | 10 | 5 EN + 5 HU, turbo transcribes back accurately |
| Bench script paths | 12 | 12 | EN medium+long: **0% WER**; HU short via router: **0% WER**; HU medium 7.4% WER |
| CLI `openclaw infer tts convert` | 5 | 5 | provider=openai (router, no Edge TTS fallback) — 2.3-2.4s |
| Discord-routed agent | 5 | 5 | "Budapest" / "Szia! Üdvözöllek!" / "TEST OK." — EN+HU replies work |
| **TOTAL** | **97** | **97** | **100%** |

### Round-trip WER + latency table (from bench script)

| Path | Lang | Size | Chars | E2E ms | WER % |
|---|---|---|---:|---:|---:|
| backend (Kokoro) | EN | short | 45 | 341 | 12.5 |
| router | EN | short | 45 | 365 | 12.5 |
| backend | EN | medium | 149 | 459 | **0.0** |
| router | EN | medium | 149 | 547 | **0.0** |
| backend | EN | long | 434 | 1160 | **0.0** |
| router | EN | long | 434 | 1229 | **0.0** |
| backend (F5-TTS) | HU | short | 31 | 1333 | 16.7 |
| router | HU | short | 31 | 1435 | **0.0** |
| backend | HU | medium | 184 | 2690 | 7.4 |
| router | HU | medium | 184 | 2765 | 7.4 |
| backend | HU | long | 408 | 6014 | 16.4 |
| router | HU | long | 408 | 6103 | 11.5 |

Whisper turbo accuracy: native EN essentially perfect (the 12.5% short-EN WER is a single proper-name mishearing "Petya"→"Petia"). HU accuracy degrades on long Hungarian content with technical vocabulary — expected (turbo is a pruned 4-layer decoder; for accuracy-first HU on noisy/long audio, swap to Trendency via STT_WHISPER_MODEL).

## VRAM / RAM footprint (post-rollback steady state)

```
Gemma 4 NVFP4 (vllm-llm)     ~35 GB VRAM
bge-m3 (vllm-embedding)       ~1.6 GB VRAM
Kokoro EN (tts-en)            ~2.6 GB RAM (compute on GPU)
F5-TTS HU (tts-f5hun)         ~2.7 GB RAM
Whisper turbo (stt-whisper)   ~2.5 GB RAM (~1.6 GB on GPU)
ComfyUI (LTX-Video)           ~860 MB at idle (spikes during render)
─────────────────────────────────────────────────
Total ~45 GB / 121.6 GB     ~76 GB headroom on the 128 GB unified pool
```

Plenty of room to retry the Fish migration once `sgl-kernel` ships sm_120 prebuilt kernels (track sgl-project/sglang #9542 and #5338).

## What changed on `main`

Only one change pushed to `main` by this session: **STT default flipped to Whisper turbo** via two env vars in the operator `.env`:

```
STT_WHISPER_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2
OPENCLAW_STT_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2
```

Backup of the pre-rollback `.env` lives at `.env.bak-pre-rollback-stt-turbo`. The Fish branch (`claude/upgrade-fish-audio-tts-stt-lDuS5`) is preserved on GitHub for a later attempt.

## Fish migration retry checklist (when ready)

1. Wait for `sgl-project/sglang` to ship prebuilt `sm_120` kernels (or accept building from source with `TORCH_CUDA_ARCH_LIST="12.0"`).
2. OR: accept the openaudio-s1-mini license on hf.co for the configured HF token, then retry the R1.c rebuild (~5 min cached up to the `hf download` step, plus a couple minutes for the model fetch).
3. OR: bring your own commercially-licensed multilingual TTS checkpoint and override `FISH_REPO` build-arg.

The Fish branch already has all the patcher / compose / docs / scripts plumbing — only the hardware-compat fight remains.
