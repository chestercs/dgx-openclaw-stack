# Fish Audio S2 Pro switchover — overnight result + smoke report

**Date**: 2026-06-10 overnight → morning, Magyarország (CEST)
**Branch state**: `main` (fast-forwarded from `poc/fish-s2pro-cu130`; backup of the
pre-switchover live state on `backup/tts-stt-live-state-20260610`)

## TL;DR

- **Fish Audio S2 Pro is LIVE on the GB10** — first successful synthesis ever on
  this hardware (the 2026-05-19 attempt died on hardware compat; the CHANGELOG R1
  risk that fired then is now fully retired).
- Five distinct root causes were peeled and fixed, each with a build-time assert
  so regressions fail the build, not the 2 am runtime.
- The production container runs via compose with hard containment (cgroup
  mem/cpu caps + BLAS thread caps) after an uncontained engine start livelocked
  the whole host twice (~1.5 h frozen userspace; all production services
  survived, no reboot was needed).
- End-to-end verified: shim-direct synthesis (HU+EN, 7 voices, mp3/wav,
  sampling overrides), Whisper STT roundtrip (near-word-perfect), gateway
  provider path via `openclaw infer tts convert`, patcher state
  (`voiceId: default_hu` + 9 aliases).

## The five-layer failure onion (all fixed in `openclaw-tts-fish/server/Dockerfile`)

| # | Root cause | Symptom | Fix |
|---|---|---|---|
| 1 | PyPI `sgl_kernel` aarch64 wheel is a CUDA 12 build | `libnvrtc.so.12: cannot open shared object file` | `0.3.21+cu130` wheel from sgl-project/whl GitHub releases; provenance assert via pip `direct_url.json` (internal version metadata is identical to the cu12 wheel) |
| 2 | PyPI torch aarch64 wheels are CPU-only; omni resolve swapped cu130 torch out | engine: "CPU/No GPU detected" | exact `+cu130` pins + `--index-strategy unsafe-best-match` + `--no-deps --force-reinstall` re-pin + build assert |
| 3 | Upstream `load_audio_decoder` materializes the full 5B model fp32-random-init (~20 GB) + ckpt (~11 GB) + bf16 copy | host-wide reclaim livelock at "Loading Fish audio decoder" (wedged the box twice) | build patch: `torch_dtype=bf16 + low_cpu_mem_usage=True` → decoder load ~28 s |
| 4 | Triton's bundled ptxas predates sm_121a | `ptxas fatal: Value 'sm_121a' is not defined` on first synth | `TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas` (CUDA 13 system ptxas) |
| 5 | Fish decoder imports FlashAttention-3 directly from `sgl_kernel` (Hopper-only SASS, bypasses `attention_backend`) | `no kernel image is available for execution on the device` | `fish_sdpa_attn_fallback.py` — torch-SDPA reimplementation of `flash_attn_with_kvcache` (in-place cache append, bottom-right causal, GQA) |

Supporting changes: GB10-calibrated pipeline yaml (`mem_fraction_static` 0.5,
no startup torch-compile/CUDA-graph capture, `attention_backend: triton`,
`max_running_requests` 4), persistent CUDA-PTX-JIT + Triton compile-cache
volumes, `SGLANG_OMNI_STARTUP_TIMEOUT=3000` under
`FISH_ENGINE_READY_DEADLINE_S=3300`, soundfile reference loader (no
torch-2.9-paired torchcodec exists for aarch64).

## Stack live state (post-switchover)

| Service | Image | Status |
|---|---|---|
| openclaw-tts-fish | openclaw-tts-fish:0.2.0 | healthy — engine ready, 7 voices |
| openclaw-stt-whisper | openclaw-stt-whisper:0.1.0 | healthy (turbo CT2, untouched) |
| openclaw-gateway / cli / config-init | (unchanged) | healthy; patcher no-op after voiceId/alias sync |
| vllm-llm / vllm-embedding / searxng | (unchanged) | healthy — survived both livelocks |

Voice library (baked seed + volume): `default_en`, `bella`, `nicole`,
`michael`, `fenrir`, `emma` (Kokoro 82M clones), `default_hu` (LibriVox PD).
Gateway aliases: `english`/`narrator`/`female`/`male`/`british`/`deep`/`soft`/
`magyar`/`hungarian`. `OPENCLAW_TTS_DEFAULT_VOICE=default_hu` in the live
`.env` (was a dead Kokoro leftover `af_heart`).

## Verified end-to-end

- Shim-direct: HU + EN synthesis → HTTP 200 RIFF WAVE; `voice=michael` (library
  voice); `response_format=mp3` transcode; `speed/seed/temperature` overrides.
- STT roundtrip: HU "Szia Petya! …GB10-en" → "Szia Petja! …GBA 10-en"
  (phonetically exact); EN word-perfect.
- Gateway path: `openclaw infer tts convert --voice default_hu` → 23.2 s, audio
  delivered. NOTE: the `infer` CLI passes the voice string raw to the shim —
  `voiceAliases` resolve on the chat/Discord surface, use raw ids in CLI tests.
- Patcher: `messages.tts.providers.openai.voiceId = default_hu`, 9 aliases, then
  clean no-op on re-run.

## Bench (TTS → STT roundtrip, `scripts/bench_tts_stt_roundtrip.py`)

| Path | Lang | Size | Chars | Words | TTS ms | STT ms | E2E ms | Audio KB | WER % |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| fish | EN | short | 45 | 8 | 20811 | 688 | 21500 | 257 | **0.0** |
| fish | EN | medium | 149 | 25 | 36347 | 778 | 37125 | 821 | **0.0** |
| fish | EN | long | 434 | 75 | 72668 | 1052 | 73721 | 2145 | **0.0** |
| fish | HU | short | 31 | 6 | 18011 | 366 | 18377 | 213 | 16.7¹ |
| fish | HU | medium | 184 | 27 | 38417 | 569 | 38986 | 1005 | **3.7** |
| fish | HU | long | 371 | 55 | 68107 | 855 | 68963 | 2145 | **0.0** |

¹ The single HU-short "error" is `Petya` → `Petja` — phonetically identical;
one proper noun in a six-word sentence dominates the percentage.

**Accuracy vs the legacy stack** (2026-05-19 report, same corpus): EN
12.5/0/0 → **0/0/0**; HU 0-16.7/7.4/11.5-16.4 → **16.7¹/3.7/0.0**. The HU
long-passage WER drop (16.4 → 0.0) is the headline — Fish S2 Pro with a
clean HU reference clip reads long technical Hungarian essentially
perfectly. **Latency** went the other way (legacy EN long 1.2 s → 72.7 s):
the AR decode + per-request CPU reference encode is the price of the
quality; see limitations.

## Known limitations / follow-ups

1. **Latency**: ~15-30 s per sentence warm (the reference clip is VQ-encoded on
   CPU per request; CUDA graphs + torch-compile disabled pending sm_121
   maturity). Fine for tagged Discord TTS, not for real-time voice channels.
   Future: cache reference VQ codes per voice (upstream accepts precomputed
   codes in `references[]`), re-enable CUDA graph once sglang ships sm_121
   kernels.
2. **Gateway 30 s TTS fetch ceiling** (`fetchWithSsrFGuard` generic default):
   long final replies on the Discord tagged path may exceed it. The first
   request after a cold recreate compiles Triton kernels and WILL exceed it
   once (caches persist on volumes now, so this is one request per image bump).
   TODO: find the config knob via the WebGUI schema oracle.
3. **Memory steady state is tight**: ~117/121 GB with vLLM + Fish + Whisper +
   ComfyUI resident. The fish container is capped (26 g / 12 cpus / 4-thread
   BLAS) so it can only OOM itself, never wedge the host again.
4. The legacy Kokoro/F5/router images remain on the host for instant rollback
   (`openclaw-tts-en:0.1.0`, `openclaw-tts-f5hun:0.1.0`,
   `openclaw-tts-router:0.1.0` + `backup/tts-stt-live-state-20260610`).
   Remove with `docker rmi` once Fish has a week of production mileage.
5. The TTS shim Bearer token appeared in a maintenance transcript (LAN-only
   exposure). Rotate with `./rotate-secrets.sh` at the next convenient window.

## Incident log (for the record)

- 2× host livelock during PoC runs 1-2 (uncontained engine start, root cause
  #3 above): userspace frozen ~1.5 h total, kernel/ICMP alive, zero OOM kills,
  all services healthy after recovery, no reboot. Fixed by the low-mem patch;
  cgroup + thread containment added as defense-in-depth (also on the compose
  service).
- ~20 min router DNS outage (192.168.111.1 SERVFAIL) mid-build; recovered on
  its own. Diagnostic: `nslookup x 8.8.8.8` works while plain fails.
