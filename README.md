<!--
Keywords for discovery: NVIDIA DGX Spark, ASUS Ascent GB10, Grace-Blackwell,
GB10 Superchip, NVFP4, FP4 quantization, Gemma 4 31B, vLLM, local LLM,
self-hosted agent, OpenClaw, bge-m3, multilingual embeddings, RAG, tool calling,
128 GB unified memory, ARM64 AI workstation, edge AI, on-device AI, docker compose,
SearxNG, privacy-respecting web search, self-hosted meta-search, hybrid retrieval,
hybrid BM25 + vector search, MMR re-ranking, x86_64 GPU server, cloud LLM backend,
OpenAI compatible, Anthropic, OpenRouter, AWS Bedrock, hosted LLM, RTX 4090.
-->

# DGX OpenClaw Stack

> **A one-command, production-grade local AI agent stack** — OpenClaw + vLLM + bge-m3 multilingual embeddings + SearxNG private web search + hybrid (BM25 + vector) memory retrieval, wired together in a single `docker compose` file.
>
> **Calibrated** for the NVIDIA GB10 "Grace-Blackwell" Superchip (NVIDIA DGX Spark, ASUS Ascent GB10) running Gemma 4 31B in NVFP4. **Portable** to other hardware — swap the LLM for whatever fits your GPU, or point OpenClaw at a cloud LLM API and keep everything else.

The default profile's tuning decisions — NVFP4 quantization, GPU memory split between LLM and embedding, FP8 KV cache, concurrency bands, context-window budgeting — are calibrated to the GB10 Superchip's specific hardware profile: **128 GB of unified LPDDR5X**, **273 GB/s bandwidth**, and **native FP4 tensor-core acceleration** (`sm_120`/`sm_121`). On a DGX Spark or ASUS Ascent GB10 you get those numbers out of the box. On other hardware everything except the LLM service is reusable as-is.

[![Docker Compose](https://img.shields.io/badge/docker%20compose-24.0%2B-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![vLLM](https://img.shields.io/badge/vLLM-0.11%2B-7C3AED)](https://github.com/vllm-project/vllm)
[![Gemma 4](https://img.shields.io/badge/Gemma%204-31B%20NVFP4-4285F4)](https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4)
[![OpenClaw](https://img.shields.io/badge/OpenClaw-2026.4.15%2B-0A7F3F)](https://openclaw.ai)
[![Hardware](https://img.shields.io/badge/hardware-DGX%20Spark%20%7C%20ASUS%20GB10-76B900?logo=nvidia&logoColor=white)](#hardware-targets)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

---

## Who this is for

| You are… | What you get | Time to working stack |
|---|---|---|
| **A GB10 owner** (DGX Spark, ASUS Ascent GB10) | The calibrated reference profile. Boot the stack and run Gemma 4 31B NVFP4 with multilingual embeddings, hybrid memory, private web search, bilingual TTS — on your hardware, no cloud. | ~30 min, mostly model download |
| **An x86_64 + NVIDIA GPU operator** (RTX 4090, A6000, etc.) | Same wiring; swap `vllm-llm` for a model your VRAM holds (Gemma 4 12B BF16, Qwen 2.5, Llama 3.3). All non-LLM services transfer unchanged. | ~30 min + tuning |
| **A cloud-LLM user** (OpenAI, Anthropic, OpenRouter, Bedrock, remote vLLM) | Park the local LLM service, point three env vars at your hosted endpoint. You still get the local agent stack: bge-m3 embeddings, SearxNG private search, hybrid memory, dreaming, heartbeat, TTS. | ~10 min (no GPU) |
| **A contributor or curious reader** | A worked example of a deterministic, opinionated AI agent stack. Every wiring decision has a *why* in the comments; the patcher is small enough to read in one sitting. | n/a — start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |

If none of those rows describe you, this repo probably isn't your fit — it's optimized for self-hosting on real hardware (or a real cloud LLM), not for trying out a chatbot on a laptop.

## What you get

A fully local agent platform (or local-plus-cloud-LLM hybrid — your choice), with:

| Component | What it does |
|---|---|
| **Gemma 4 31B IT (NVFP4)** | 31B-parameter Google Gemma 4 dense model, quantized by NVIDIA to NVFP4 (FP4 with NVIDIA's block format). Native tool calling, 256K context, multimodal (text + image). |
| **bge-m3 embeddings** | BAAI/bge-m3 multilingual dense embeddings via vLLM. 100+ languages, 1024-dim, 8K context, EN↔HU cosine ≈ 0.88. |
| **SearxNG meta-search** | Self-hosted, privacy-respecting web search backend wired into OpenClaw's native `webSearch` provider. Strict engine whitelist (DuckDuckGo, Brave, Mojeek, Qwant, Startpage, Wikipedia family, Reddit, GitHub, arXiv) — queries never reach Google / Bing / Yandex / Yahoo / Baidu. |
| **OpenClaw gateway** | The open-source agent runtime: Chrome extension UI, CLI, persistent memory, heartbeat, multi-agent world-building. |
| **Bilingual TTS surface** | OpenAI-compatible `/v1/audio/speech` router fronting Kokoro 82M (English, Apache 2.0, ~500 MB-1 GB VRAM, ships by default) and an opt-in F5-TTS Hungarian backend (CC-BY-NC model weights — see below). Wired into OpenClaw via the sanctioned `messages.tts.providers.openai` baseUrl override. Diacritic-based autodetect re-routes Hungarian-text requests to the HU backend transparently when both are active. |
| **Whisper STT (EN + HU)** | OpenAI-compatible `/v1/audio/transcriptions` via `Systran/faster-whisper-large-v3` on a self-built CUDA 13 image (~150 LOC FastAPI wrapper around `faster-whisper` — the upstream speaches image rejects Blackwell tensor-core compute types on sm_120, so we self-build to match the `vllm-llm` / `openclaw-tts-en` wheel pattern). ~3 GB VRAM, autodetects language (FLEURS Hungarian WER 14.1%). Wired into OpenClaw's `tools.media.audio` pipeline — voice-note uploads in the Control UI chat, Discord voice channels, the VoiceCall CLI, and Talk / Voicewake nodes all transcribe through this service. MIT wrapper + MIT Whisper weights. |
| **Idempotent config patcher** | A small Node script that makes your OpenClaw config deterministic — runs on every `up`, never clobbers onboarding choices it shouldn't. Wires hybrid (BM25 + vector) retrieval with MMR re-rank on top of `memorySearch`, flips the bundled SearxNG plugin on, points the openai TTS provider at the bundled router, and upserts the STT entry into `tools.media.audio.models[]`. |

Everything lives in one Docker Compose file. No separate vLLM service definitions, no reverse-proxied DNS trickery, no `host.docker.internal` workarounds — containers reach each other by their compose service name on the default bridge network.

## Hardware targets

The reference profile (`docker compose up -d` with no edits) is designed and tested on:

- **NVIDIA DGX Spark** (GB10 Superchip, 128 GB unified LPDDR5X, 273 GB/s)
- **ASUS Ascent GB10** (same GB10 Superchip, same memory architecture)

Works unchanged on **any future workstation built around the GB10 Superchip** — the stack doesn't depend on DGX- or ASUS-specific firmware, only on the Blackwell datacenter compute capabilities (`sm_120`/`sm_121`) and the GB10's 128 GB unified memory budget.

The reference profile **won't boot as-is on non-GB10 hardware** — `vllm/vllm-openai:gemma4-cu130` and the NVFP4 model both need Blackwell FP4 kernels. Two supported alternatives, sketched briefly in [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md):

- **Other NVIDIA GPU**: switch to a stock vLLM image and a model that fits your VRAM (smaller Gemma 4 NVFP4 if you have a Blackwell desktop; Gemma 4 12B BF16 / Qwen 2.5 / Llama 3.3 elsewhere). The memory-split and concurrency constants in `.env.example` will need re-tuning for your card.
- **Cloud LLM**: park the local vLLM services behind `profiles: ["never"]` and set `OPENAI_BASE_URL` / `LLM_BASE_URL` / `EMBED_BASE_URL` in `.env` to your hosted endpoints (cloud OpenAI-compatible API, remote vLLM on another box, etc.). bge-m3 stays local by default but can also be remoted. Everything downstream — gateway, SearxNG, hybrid retrieval, dreaming, heartbeat — is unchanged.

### Performance (measured on GB10, single-shot generation)

| Scenario | Value |
|---|---|
| Decode throughput, 1 concurrent user | ~6.9 tok/s sustained (NVFP4) vs ~3.7 tok/s (BF16) — ~2× speedup |
| Stable context window, 1 concurrent user | ~220K tokens before vLLM preemption |
| Stable context window, 2 concurrent users | ~110K tokens each, served via continuous batching |
| Vision prefill per image | ~280 vision tokens for a ≈ 512×512 region, sub-second encode |
| First-boot cold start (after model download) | ~3–4 min from `up` to gateway-ready |
| KV cache | FP8 (halves cache footprint vs default BF16 KV cache) |

Numbers come from a DGX Spark with 128 GB unified LPDDR5X. Single-prompt streaming with a warm KV cache; throughput drops with longer contexts and more concurrent users. Re-tune the `LLM_GPU_MEM_UTIL` / `LLM_MAX_NUM_SEQS` constants in `.env` for other hardware.

## Quickstart

Five commands. First-boot is two-phase by design (the gateway waits for explicit OpenClaw onboarding before applying the wiring); skip the heads-up below at your peril.

```bash
git clone https://github.com/chestercs/dgx-openclaw-stack.git
cd dgx-openclaw-stack

./bootstrap.sh                              # interactive, non-destructive, idempotent
docker compose up -d                        # services start; gateway will crash-loop until step 5
# 4. Open the OpenClaw Chrome extension or run `openclaw setup` on the host,
#    pair it with `ws://<your-host>:18789` using the token printed by bootstrap.
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

That's it — the patcher applies all 14 steps and the gateway goes healthy. **Two-phase fresh-install onboarding** (gateway crash-loop → onboarding → patcher applies wiring) is the OpenClaw security model, not a bug; details in [SETUP.md](SETUP.md). If anything goes sideways, the symptoms map directly onto entries in [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

## Architecture at a glance

```
    ┌────────────────────────────────────────────────────────────────┐
    │  DGX Spark / ASUS GB10                                         │
    │                                                                 │
    │  ┌─────────────────┐  ┌─────────────────┐  ┌────────────────┐  │
    │  │ vllm-llm        │  │ vllm-embedding  │  │ searxng        │  │
    │  │ :8004 (internal)│  │ :8005 (internal)│  │ :8080 (internal)│  │
    │  │ Gemma 4 31B     │  │ bge-m3 (567M)   │  │ privacy meta-   │  │
    │  │ NVFP4, 256K ctx │  │ 1024-dim, 8K ctx│  │ search (CPU)    │  │
    │  └────────▲────────┘  └────────▲────────┘  └────────▲────────┘  │
    │           │ compose DNS        │ compose DNS        │ compose DNS│
    │  ┌────────┴────────────────────┴────────────────────┴────────┐  │
    │  │ openclaw-gateway            :18789 (exposed)              │◀── Chrome ext.
    │  │   └ openclaw-config-init    (one-shot)                    │◀── CLI
    │  │   └ openclaw-cli            (always-up)                   │   │
    │  └───────────────────────────────────────────────────────────┘  │
    └────────────────────────────────────────────────────────────────┘
```

All inter-container traffic is on the compose default bridge network; only port `18789` is published to the host. Put a reverse proxy (Nginx Proxy Manager, Caddy, Traefik, or a Cloudflared tunnel) in front for public access over `wss://`.

Deep dive: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Features

- **One compose file for everything.** LLM, embedding, web search, and agent stack in one `docker compose up -d`.
- **NVFP4-native.** Ships with the official `vllm/vllm-openai:gemma4-cu130` image; no custom build required.
- **True tool calling.** The shipped `tool_chat_template_gemma4.jinja` plus `--tool-call-parser gemma4 --enable-auto-tool-choice` produces OpenAI-format `tool_calls`. OpenClaw uses them directly.
- **Multimodal.** Gemma 4's vision tower is included. Drop an image into the chat; the model reads it at ~280 tokens per image by default.
- **Multilingual RAG built in.** bge-m3 gives you high-quality cross-lingual embeddings for `memorySearch` out of the box.
- **Hybrid retrieval + MMR.** `memorySearch` runs BM25 (SQLite FTS5) alongside vector similarity and re-ranks the candidate set with MMR for diversity — exact-keyword / ID matches stop falling through the cracks of pure cosine search.
- **Privacy-respecting web search.** Self-hosted SearxNG wired into OpenClaw's native `webSearch` tool. No commercial search API, no query leak to Google / Bing / Yandex. Strict engine whitelist (DuckDuckGo, Brave, Mojeek, Qwant, Startpage + Wikipedia / Reddit / GitHub / arXiv).
- **Bilingual self-hosted TTS.** Kokoro 82M (English, Apache 2.0) ships by default and runs alongside the LLM on the same GB10 GPU. Optional Hungarian (F5-TTS, opt-in via `--profile hu`) for fully local cross-language voice; details in [Hungarian TTS opt-in](#hungarian-tts-opt-in-cc-by-nc).
- **Bilingual self-hosted STT.** `Systran/faster-whisper-large-v3` on a self-built CUDA 13 image (~150 LOC FastAPI wrapper — Blackwell compat ate the upstream speaches-ai image), autodetecting English and Hungarian (FLEURS HU WER 14.1%). ~3 GB VRAM at float16, wired into OpenClaw's `tools.media.audio` pipeline — voice-note uploads, Discord voice, VoiceCall CLI, and Talk/Voicewake nodes all transcribe through it. Details in [`docs/reference/stt-stack.md`](docs/reference/stt-stack.md).
- **Long context, honest numbers.** 256K model max; realistic stable bands (per user count) are documented in the compose file.
- **Idempotent configuration.** The patcher re-applies a known-good state on every `up`. Safe to run repeatedly.
- **Reverse-proxy ready.** `gateway.trustedProxies` is pre-populated; add your LAN CIDR via `OPENCLAW_LAN_CIDR` if needed.
- **Non-destructive bootstrap.** `bootstrap.sh` never overwrites an existing `.env` value or host directory.

## Repository layout

```
dgx-openclaw-stack/
├─ docker-compose.yml           # the whole stack (vllm-* + searxng + openclaw-* + tts-* + stt-*)
├─ patch-config.mjs             # idempotent OpenClaw config patcher (14 steps)
├─ bootstrap.sh                 # non-destructive first-time setup
├─ .env.example                 # documented env template (every tunable lives here)
├─ templates/
│  └─ tool_chat_template_gemma4.jinja   # Gemma 4 tool-call chat template
├─ searxng/
│  └─ settings/
│     └─ settings.yml           # SearxNG override: JSON API + strict engine whitelist
├─ openclaw-tts-en/             # English TTS service (Kokoro 82M, Apache 2.0)
│  └─ server/                   #   Dockerfile + FastAPI wrapper
├─ openclaw-tts-router/         # OpenAI-compat TTS router (passthrough + ffmpeg transcode)
│  └─ server/
├─ openclaw-tts-f5hun/          # OPT-IN Hungarian TTS (CC-BY-NC model weights)
│  ├─ server/                   #   Dockerfile + F5-TTS wrapper
│  └─ voices/                   #   Bundled reference voice (Diana Majlinger, public domain)
├─ openclaw-stt-whisper/        # Self-built CUDA 13 STT image (Blackwell compat)
│  └─ server/                   #   Dockerfile + FastAPI wrapper around faster-whisper
├─ docs/
│  ├─ ARCHITECTURE.md           # service-by-service design rationale
│  ├─ CUSTOMIZATION.md          # model swaps, remote backends, hardware retuning
│  └─ TROUBLESHOOTING.md        # common failure modes and fixes
├─ README.md                    # you are here — pitch + quickstart
├─ SETUP.md                     # end-user first-boot walkthrough
├─ CHANGELOG.md                 # versioned release notes
├─ CLAUDE.md                    # contributor / coding-agent guide
├─ CONTRIBUTING.md              # how to file issues + send PRs
└─ LICENSE                      # MIT (model weights retain upstream licenses)
```

## Hungarian TTS opt-in (CC-BY-NC)

The English TTS surface (Kokoro 82M, Apache 2.0) ships in the default profile
and is safe for any usage. Hungarian TTS is **opt-in** because the only
production-grade open-weights Hungarian TTS at the time of writing — the
`sarpba/F5-TTS_V1_hun_v2` fine-tune of F5-TTS — is distributed under
**CC-BY-NC-4.0** (Creative Commons, **non-commercial only**).

The wrapper code in `openclaw-tts-f5hun/` is MIT (matches the rest of this
repo). The model weights are pulled from HuggingFace at build time — by
building the image you accept the upstream model license. This repo ships
no model weights of any kind.

**To activate Hungarian TTS, the easiest path is to re-run `bootstrap.sh`** —
it now prompts to opt in, generates `F5HUN_API_TOKEN`, sets `F5HUN_URL` to the
in-compose service, and adds `COMPOSE_PROFILES=hu` to `.env`. Then:

```bash
docker compose --profile hu up -d --build openclaw-tts-f5hun
```

Or by hand: uncomment the three lines in the "Optional: Hungarian TTS" block
in `.env.example`, fill in `F5HUN_API_TOKEN` (`openssl rand -base64 64`), and
either set `COMPOSE_PROFILES=hu` in `.env` or pass `--profile hu` on the
docker compose command line.

Once active, the router exposes `default_hu` / `hu_diana` voice ids, and the
diacritic-based autodetect silently re-routes Hungarian-text requests
(detected by `áéíóöőúüű`) to the HU backend even when OpenClaw asks for an
English default voice like `coral`. The autodetect is a no-op when the HU
profile is not active.

For commercial Hungarian deployments, override `F5_CHECKPOINT` / `F5_VOCAB`
on the `openclaw-tts-f5hun` service to point at a checkpoint with a fitting
license. Details + voice catalog in
[`openclaw-tts-f5hun/README.md`](openclaw-tts-f5hun/README.md).

## Why this stack

Running a useful local (or hybrid) agent on top of OpenClaw + vLLM is trickier than the surface picture suggests:

- The OpenClaw onboarding wizard doesn't register NVFP4 models against a self-hosted vLLM provider, leaves `memorySearch` disabled, ships an empty `gateway.trustedProxies`, and writes a placeholder API key — all of which silently break things later.
- Gemma 4 tool calling requires a specific chat template that isn't in the official vLLM image.
- The bundled OpenClaw `searxng` plugin ships **default-disabled** — `webSearch` looks wired up but doesn't actually fire until you flip it on.
- Hybrid (BM25 + vector) retrieval and MMR re-rank are native OpenClaw features but aren't on by default.
- On GB10 specifically, unified-memory GPU budgeting between two concurrent vLLM processes needs care (`LLM_GPU_MEM_UTIL` vs `EMBED_GPU_MEM_UTIL`).

This repo captures a known-good wiring for all of the above in a single deterministic `docker compose up`. The `patch-config.mjs` patcher re-applies it on every restart so the wiring survives onboarding-wizard reruns, image upgrades, and manual edits.

## License

[MIT](LICENSE). Model weights retain their upstream licenses:

- Gemma 4: **Apache 2.0**
- bge-m3: **MIT**
- Kokoro 82M (English TTS, default): **Apache 2.0**
- F5-TTS Hungarian (`sarpba/F5-TTS_V1_hun_v2`, opt-in): **CC-BY-NC-4.0** —
  non-commercial use only. The HU service block is parked behind a Docker
  Compose profile and does not start by default; building it triggers the
  weight download and constitutes acceptance of the upstream model license.
  See [`openclaw-tts-f5hun/README.md`](openclaw-tts-f5hun/README.md) for
  details on swapping the checkpoint for one with a commercial license.

## Contributing

Pull requests welcome. See [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) for the extension points that matter (model swap, quantization swap, custom agents). For issues that aren't about this stack itself, please file them upstream at [vllm-project/vllm](https://github.com/vllm-project/vllm) or [openclaw/openclaw](https://github.com/openclaw/openclaw).
