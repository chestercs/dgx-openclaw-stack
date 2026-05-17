<!--
Keywords for discovery: NVIDIA DGX Spark, ASUS Ascent GB10, Grace-Blackwell,
GB10 Superchip, NVFP4, FP4 quantization, Gemma 4 26B-A4B MoE, Gemma 4 31B,
mixture of experts, vLLM, local LLM, self-hosted agent, OpenClaw, bge-m3,
multilingual embeddings, RAG, tool calling, 128 GB unified memory, ARM64 AI
workstation, edge AI, on-device AI, docker compose, SearxNG, privacy-respecting
web search, self-hosted meta-search, hybrid retrieval, hybrid BM25 + vector
search, MMR re-ranking, x86_64 GPU server, cloud LLM backend, OpenAI compatible,
Anthropic, OpenRouter, AWS Bedrock, hosted LLM, RTX 4090.
-->

# DGX OpenClaw Stack

> **A one-command, production-grade local AI agent stack** — OpenClaw + vLLM + bge-m3 multilingual embeddings + SearxNG private web search + hybrid (BM25 + vector) memory retrieval, wired together in a single `docker compose` file.
>
> **Calibrated** for the NVIDIA GB10 "Grace-Blackwell" Superchip (NVIDIA DGX Spark, ASUS Ascent GB10) running Gemma 4 26B-A4B MoE NVFP4 and Gemma 4 31B IT NVFP4 dense **side by side on separate ports** (8004 / 8005, separate OpenClaw provider entries) — pick either model in the UI without restarting. **Portable** to other hardware — swap the LLM for whatever fits your GPU, or point OpenClaw at a cloud LLM API and keep everything else.

The default profile's tuning decisions — NVFP4 quantization, GPU memory split between LLM and embedding, FP8 KV cache, concurrency bands, context-window budgeting — are calibrated to the GB10 Superchip's specific hardware profile: **128 GB of unified LPDDR5X**, **273 GB/s bandwidth**, and **native FP4 tensor-core acceleration** (`sm_120`/`sm_121`). On a DGX Spark or ASUS Ascent GB10 you get those numbers out of the box. On other hardware everything except the LLM service is reusable as-is.

[![Docker Compose](https://img.shields.io/badge/docker%20compose-24.0%2B-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![vLLM](https://img.shields.io/badge/vLLM-0.11%2B-7C3AED)](https://github.com/vllm-project/vllm)
[![Gemma 4](https://img.shields.io/badge/Gemma%204-26B--A4B%20MoE%20NVFP4-4285F4)](https://huggingface.co/nvidia/Gemma-4-26B-A4B-NVFP4)
[![OpenClaw](https://img.shields.io/badge/OpenClaw-2026.4.15%2B-0A7F3F)](https://openclaw.ai)
[![Hardware](https://img.shields.io/badge/hardware-DGX%20Spark%20%7C%20ASUS%20GB10-76B900?logo=nvidia&logoColor=white)](#hardware-targets)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

---

## Who this is for

| You are… | What you get | Time to working stack |
|---|---|---|
| **A GB10 owner** (DGX Spark, ASUS Ascent GB10) | The calibrated reference profile. Boot the stack and run Gemma 4 26B-A4B MoE NVFP4 (~25 tok/s decode single-stream, ~112 tok/s aggregate at 4-paralel) with multilingual embeddings, hybrid memory, private web search, bilingual TTS — on your hardware, no cloud. The dense 31B is preserved as a profile-gated alternative for parity testing. | ~30 min, mostly model download |
| **An x86_64 + NVIDIA GPU operator** (RTX 4090, A6000, etc.) | Same wiring; swap `vllm-llm` for a model your VRAM holds (Gemma 4 12B BF16, Qwen 2.5, Llama 3.3). All non-LLM services transfer unchanged. | ~30 min + tuning |
| **A cloud-LLM user** (OpenAI, Anthropic, OpenRouter, Bedrock, remote vLLM) | Park the local LLM service, point three env vars at your hosted endpoint. You still get the local agent stack: bge-m3 embeddings, SearxNG private search, hybrid memory, dreaming, heartbeat, TTS. | ~10 min (no GPU) |
| **A contributor or curious reader** | A worked example of a deterministic, opinionated AI agent stack. Every wiring decision has a *why* in the comments; the patcher is small enough to read in one sitting. | n/a — start with [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |

If none of those rows describe you, this repo probably isn't your fit — it's optimized for self-hosting on real hardware (or a real cloud LLM), not for trying out a chatbot on a laptop.

## What you get

A fully local agent platform (or local-plus-cloud-LLM hybrid — your choice), with:

| Component | What it does |
|---|---|
| **Gemma 4 26B-A4B MoE (NVFP4)** | 25.2B-total / 3.8B-active Google Gemma 4 mixture-of-experts model (128 experts, top-8 routing), quantized by NVIDIA to NVFP4 with the `nvfp4_experts_only` recipe. Native tool calling, 256K context, multimodal (text + image), ~25 tok/s decode single-stream on GB10 with Marlin SM121 backend + CUDA graphs (~4× faster than dense; ~112 tok/s aggregate at 4-parallel). |
| **Gemma 4 31B IT (NVFP4) — concurrent dense** | 31.3B dense Google Gemma 4 quantized to NVFP4. Runs side-by-side with the MoE on port 8005 (provider id `vllm-dense`), single-user / 256K context / ~6.9 tok/s decode profile. Pick either model in the OpenClaw UI without restarting; the dense backend exists for parity testing, multimodal-heavy workloads where dense quality matters, or as a fallback. |
| **bge-m3 embeddings** | BAAI/bge-m3 multilingual dense embeddings via vLLM. 100+ languages, 1024-dim, 8K context, EN↔HU cosine ≈ 0.88. |
| **SearxNG meta-search** | Self-hosted, privacy-respecting web search backend wired into OpenClaw's native `webSearch` provider. Strict engine whitelist (DuckDuckGo, Brave, Mojeek, Qwant, Startpage, Wikipedia family, Reddit, GitHub, arXiv) — queries never reach Google / Bing / Yandex / Yahoo / Baidu. |
| **OpenClaw gateway** | The open-source agent runtime: Chrome extension UI, CLI, persistent memory, heartbeat, multi-agent world-building. |
| **Multilingual TTS (Fish Audio S2 Pro)** | Single self-hosted OpenAI-compatible `/v1/audio/speech` service backed by `fishaudio/s2-pro` (5B param Qwen3-omni) served via SGLang-Omni on a custom CUDA 13 aarch64 image. 80+ languages from one checkpoint (English + Hungarian both supported), voice cloning from any 10-30 s mounted reference WAV+transcript, ~11 GB weights baked at build time. Wired into OpenClaw via `messages.tts.providers.openai`. **License: Fish Audio Research License — non-commercial only.** Wrapper code MIT. |
| **Whisper STT (EN + HU, turbo)** | OpenAI-compatible `/v1/audio/transcriptions` via `deepdml/faster-whisper-large-v3-turbo-ct2` on a self-built CUDA 13 image (~150 LOC FastAPI wrapper around `faster-whisper` — the upstream speaches image rejects Blackwell tensor-core compute types on sm_120, so we self-build to match the `vllm-llm` / `openclaw-tts-fish` wheel pattern). ~1.6 GB VRAM at float16, ~8× faster than vanilla large-v3 (pruned 4-layer decoder), autodetects language. Wired into OpenClaw's `tools.media.audio` pipeline — voice-note uploads in the Control UI chat, Discord voice channels, the VoiceCall CLI, and Talk / Voicewake nodes all transcribe through this service. MIT wrapper + MIT Whisper weights. Swap to `Trendency/whisper-large-v3-hu` via `STT_WHISPER_MODEL` for the HU-finetune (slower, more robust on noisy mic input). |
| **Browser automation (opt-in)** | OpenClaw's built-in `browser` tool attaches to a self-hosted Playwright Chromium cluster over Chrome DevTools Protocol — one warm Chromium per onboarded credential. 1x manual OAuth onboarding per service via a noVNC bridge (`./bootstrap-browser-login.sh github-user1`); afterwards the agent reaches authenticated content with no per-call re-auth until the upstream session expires (~14d GitHub, ~30d Notion, etc.). Activate via `--profile browser`. Apache 2.0. Limitation: passkey-only auth flows don't work over noVNC by W3C origin-bound spec — use password+TOTP or API tokens for those. Details in [`docs/reference/browser-automation.md`](docs/reference/browser-automation.md). |
| **Idempotent config patcher** | A small Node script that makes your OpenClaw config deterministic — runs on every `up`, never clobbers onboarding choices it shouldn't. Wires hybrid (BM25 + vector) retrieval with MMR re-rank on top of `memorySearch`, flips the bundled SearxNG plugin on, points the openai TTS provider at the bundled router, upserts the STT entry into `tools.media.audio.models[]`, and writes one `browser.profiles.<name>.cdpUrl` per registered Chromium profile. |

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

| Scenario | MoE 26B-A4B (default) | Dense 31B (alternative) |
|---|---|---|
| Decode throughput, 1 concurrent user | ~24.9 tok/s sustained (NVFP4 + Marlin MoE backend on SM121 + CUDA graphs, measured 2026-05-08) | ~6.9 tok/s sustained (NVFP4) |
| Aggregate throughput, 4 concurrent users | ~112 tok/s (~28 tok/s per user — continuous-batching + CUDA graphs amortize kernel-launch overhead) | n/a (single-stream profile) |
| Stable context window, 1 concurrent user | 256K reachable (prefill-bound past ~100K — 100K prompt + 200 gen ≈ 70s wall) | ~220K tokens before vLLM preemption |
| Stable context window, paged 4 simul users | ~4.3× concurrency at 256K (paging-runtime; full simul-256K = preempt) | ~110K tokens each, continuous batching |
| Model footprint | ~16.5 GB (NVFP4 weights, vision tower included) | ~17 GB (NVFP4 weights, vision tower included) |
| Vision prefill per image | ~280 vision tokens for a ≈ 512×512 region, sub-second encode (both) | — |
| First-boot cold start (after model download) | ~3–4 min from `up` to gateway-ready (both) | — |
| KV cache | FP8 (halves cache footprint vs default BF16 KV cache) | FP8 (same) |

Numbers come from a DGX Spark with 128 GB unified LPDDR5X. Single-prompt streaming with a warm KV cache; throughput drops with longer contexts and more concurrent users. Re-tune the `LLM_GPU_MEM_UTIL` / `LLM_MAX_NUM_SEQS` constants in `.env` for other hardware.

## Quickstart

Four shell commands plus one in-browser onboarding step — that's the minimal
path to a working default-profile install. First-boot is **two-phase by
design** (the gateway waits for explicit OpenClaw onboarding before applying
the wiring); skip the heads-up below at your peril.

```bash
git clone https://github.com/chestercs/dgx-openclaw-stack.git
cd dgx-openclaw-stack

./bootstrap.sh                              # interactive, non-destructive, idempotent
docker compose up -d                        # 10 default services; gateway will crash-loop
                                            #   with "Missing config" until you onboard

# Phase 2 — open the OpenClaw Chrome extension OR run `openclaw setup` in a
# shell on the host, pair with `ws://<your-host>:18789` using the gateway
# token printed by bootstrap. Onboarding writes openclaw.json.

docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

That's it — the patcher applies all wiring steps and the gateway goes healthy. **Two-phase fresh-install onboarding** (gateway crash-loop → onboarding → patcher applies wiring) is the OpenClaw security model, not a bug; details in [SETUP.md](SETUP.md). If anything goes sideways, the symptoms map directly onto entries in [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

This brings up the **9 default services** (LLM MoE + dense + embedding + gateway + cli + config-init + searxng + tts-fish + stt-whisper). Hungarian TTS is now built into the Fish Audio S2 Pro service — no separate `--profile hu` opt-in. Several capabilities are **opt-in profiles** that don't start with the default `up`:

- `--profile browser` — Playwright Chromium for login-gated sites; per-credential 1× OAuth via the noVNC helper.
- `--profile python` — Python code-execution sandbox (MCP).
- **Image generation** lives in a [separate compose file](openclaw-image-comfyui/) and proxies to your existing ComfyUI install (the repo ships no model weights).
- **Discord integration** is a separate operator-side flow (Developer Portal app → bot token → `openclaw channels add`). The patcher handles every Discord-related field automatically once you've created the channel; see [`docs/discord-bot-setup.md`](docs/discord-bot-setup.md) and [`docs/reference/discord-config.md`](docs/reference/discord-config.md).

A more honest "what reproduces from a fresh clone vs what's manual" breakdown is in [§ Reproducibility from a fresh clone](#reproducibility-from-a-fresh-clone) below.

## Architecture at a glance

```
    ┌──────────────────────────────────────────────────────────────────────┐
    │  DGX Spark / ASUS GB10                                               │
    │                                                                       │
    │  Default profile (10 services, all on the compose bridge network)    │
    │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐       │
    │  │ vllm-llm        │  │ vllm-llm-dense  │  │ vllm-embedding  │       │
    │  │ :8004 (MoE 26B) │  │ :8005 (dense 31)│  │ :8005 (bge-m3)  │       │
    │  └────────▲────────┘  └────────▲────────┘  └────────▲────────┘       │
    │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐       │
    │  │ searxng         │  │ openclaw-tts-   │  │ openclaw-stt-   │       │
    │  │ :8080 privacy   │  │ fish :8091      │  │ whisper :8093   │       │
    │  │ meta-search     │  │ Fish S2 Pro     │  │ faster-whisper  │       │
    │  │                 │  │ (multilingual)  │  │ turbo CT2       │       │
    │  └────────▲────────┘  └────────▲────────┘  └────────▲────────┘       │
    │           │ compose DNS (service names)                              │
    │  ┌────────┴──────────────────────────────────────────────────────┐   │
    │  │ openclaw-gateway          :18789 (only published port)        │◀── Chrome ext.
    │  │   ├ openclaw-config-init  (one-shot patcher, runs every up)   │◀── CLI
    │  │   └ openclaw-cli          (always-up, shares gateway netns)   │   │
    │  └───────────────────────────────────────────────────────────────┘   │
    │                                                                       │
    │  Opt-in profiles (parked unless explicitly enabled)                  │
    │  ─ --profile browser  → openclaw-browser   (Chromium + noVNC)        │
    │  ─ --profile python   → openclaw-python-sandbox (MCP exec)           │
    │                                                                       │
    │  Separate compose (proxies to operator-side ComfyUI on host)         │
    │  ─ openclaw-image-comfyui/docker-compose.yml --profile image-gen     │
    └──────────────────────────────────────────────────────────────────────┘
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
- **Multilingual self-hosted TTS.** Fish Audio S2 Pro (`fishaudio/s2-pro`) via SGLang-Omni in one container — 80+ languages from one checkpoint (EN + HU both supported), voice cloning from any 10-30 s mounted reference clip + transcript, ~11 GB weights baked at build time. **Fish Audio Research License — non-commercial only** (wrapper code MIT); see [Fish Audio license note](#fish-audio-license-note) for commercial path.
- **Bilingual self-hosted STT.** `deepdml/faster-whisper-large-v3-turbo-ct2` (turbo, ~8× faster than vanilla large-v3) on a self-built CUDA 13 image (~150 LOC FastAPI wrapper — Blackwell compat ate the upstream speaches-ai image), autodetecting English and Hungarian. ~1.6 GB VRAM at float16, wired into OpenClaw's `tools.media.audio` pipeline — voice-note uploads, Discord voice, VoiceCall CLI, and Talk/Voicewake nodes all transcribe through it. Swap to `Trendency/whisper-large-v3-hu` via `STT_WHISPER_MODEL` for the accuracy-first HU finetune. Details in [`docs/reference/stt-stack.md`](docs/reference/stt-stack.md).
- **Optional FLUX-Krea-dev image generation.** The `openclaw-image-comfyui` MCP bridge (opt-in via `--profile image-gen`, separate compose file) drives the operator's existing ComfyUI install through `flux-krea-2k` (SFW, 1280×720 default) and `flux-krea-2k-adult` (same pipeline + flux-uncensored-v2 LoRA) workflow templates. The bridge ships no model weights; the recommended ~35 GB download is documented in [`docs/reference/image-comfyui-bridge.md`](docs/reference/image-comfyui-bridge.md). For 4K output, render at 2K native and upscale externally with ESRGAN — diffusion-based upscalers (SUPIR, UltimateSDUpscale tile pass) produce visible tile-seam artifacts on FLUX latents and were dropped.
- **Discord-ready out of the box.** Once you create a bot in Discord's Developer Portal and run `openclaw channels add --channel discord`, the patcher writes 11 production-tested Discord overrides automatically (progressive streaming, slash-command authz for [issue #19310](https://github.com/openclaw/openclaw/issues/19310), tool-surface widening for the Discord-routed agent, cron + browser cheatsheets in the workspace). Every override is env-gated and individually disable-able — see [`docs/reference/discord-config.md`](docs/reference/discord-config.md) for the at-a-glance table.
- **Long context, honest numbers.** 256K model max; realistic stable bands (per user count) are documented in the compose file.
- **Idempotent configuration.** The patcher re-applies a known-good state on every `up`. Safe to run repeatedly.
- **Reverse-proxy ready.** `gateway.trustedProxies` is pre-populated; add your LAN CIDR via `OPENCLAW_LAN_CIDR` if needed.
- **Non-destructive bootstrap.** `bootstrap.sh` never overwrites an existing `.env` value or host directory.

## Repository layout

```
dgx-openclaw-stack/
├─ docker-compose.yml           # default + opt-in profiles (hu, browser, python)
├─ patch-config.mjs             # idempotent OpenClaw config patcher (27+ steps,
│                               #   header docblock indexes every one)
├─ bootstrap.sh                 # non-destructive interactive first-time setup
├─ bootstrap-browser-login.sh   # 1x OAuth onboarding helper (noVNC bridge)
├─ rotate-secrets.sh            # rotate gateway / service tokens in place
├─ .env.example                 # documented env template (every tunable lives here)
├─ templates/
│  ├─ tool_chat_template_gemma4.jinja        # Gemma 4 tool-call chat template
│  ├─ discord-text-agent/AGENTS.md.example   # discord-friend agent template
│  └─ userscripts/                            # web chat UI userscripts (opt-in)
├─ searxng/
│  └─ settings/settings.yml     # SearxNG override: JSON API + strict engine whitelist
├─ vllm-llm/                    # custom vLLM image (gemma4 tool-call parser patch
│                               #   for colon namespaces — see Dockerfile)
├─ openclaw-base-ext/           # local extension of the openclaw image (adds ffmpeg)
├─ openclaw-tts-fish/           # Multilingual TTS (Fish Audio S2 Pro, SGLang-Omni)
│                               #   Fish Audio Research License (non-commercial)
├─ openclaw-stt-whisper/        # Self-built CUDA 13 STT image (faster-whisper turbo)
├─ openclaw-browser/            # OPT-IN browser automation (--profile browser)
├─ openclaw-python-sandbox/     # OPT-IN Python MCP exec sandbox (--profile python)
├─ openclaw-image-comfyui/      # OPT-IN image-gen MCP bridge — SEPARATE compose file
│                               #   (proxies to operator's existing ComfyUI install)
├─ docs/
│  ├─ ARCHITECTURE.md           # service-by-service design rationale
│  ├─ CUSTOMIZATION.md          # model swaps, remote backends, hardware retuning
│  ├─ TROUBLESHOOTING.md        # common failure modes and fixes
│  ├─ discord-bot-setup.md      # zero-to-bot Discord Developer Portal walkthrough
│  └─ reference/                # deeper reference docs (15+ files — see reference/README.md)
├─ README.md                    # you are here — pitch + quickstart
├─ SETUP.md                     # end-user first-boot walkthrough
├─ CHANGELOG.md                 # versioned release notes
├─ CLAUDE.md                    # contributor / coding-agent guide
├─ CONTRIBUTING.md              # how to file issues + send PRs
└─ LICENSE                      # MIT (model weights retain upstream licenses)
```

## Reproducibility from a fresh clone

Honest scope: a `git clone` + `./bootstrap.sh` + `docker compose up -d` followed
by the onboarding handshake brings up the **10 default services and the full
agent baseline** (Gemma 4 MoE + dense, embedding, gateway, web search, EN TTS,
STT, hybrid memory, all 27+ patcher overrides). The advanced surfaces (Discord,
image-gen, Hungarian TTS, browser automation) need explicit operator steps —
each is documented but none is a one-command install. The table below is the
honest answer to *"will my clone end up where the maintainer's deploy is?"*.

| Layer | Reproduces from `compose up` alone? | What's needed beyond bootstrap |
|---|---|---|
| 9 default services (LLM MoE + dense, embedding, gateway, cli, config-init, searxng, tts-fish, stt-whisper) | ✅ after onboarding | Gateway is **expected to crash-loop** until you complete the Chrome-extension wizard or `openclaw onboard` — then re-run the patcher trio. SETUP.md §5–6b walks through this. |
| Gemma 4 NVFP4 weights | ❌ | HF account, accept the [Gemma 4 license](https://huggingface.co/nvidia/Gemma-4-26B-A4B-NVFP4), put your `hf_…` token in `.env`. `bootstrap.sh` prompts for it. |
| Fish Audio S2 Pro weights | ❌ | Pulled automatically at build time from [fishaudio/s2-pro](https://huggingface.co/fishaudio/s2-pro). Building the image constitutes acceptance of the **Fish Audio Research License (non-commercial)** — see [Fish Audio license note](#fish-audio-license-note). |
| Browser automation | ❌ | `--profile browser`, then per-credential noVNC OAuth via `./bootstrap-browser-login.sh <profile>`. Each login is 1× manual (W3C origin-bound — passkeys don't work). |
| Python sandbox | ❌ | `--profile python`, secrets generated by `bootstrap.sh`. |
| Discord integration | ❌ | Discord Developer Portal (create app + bot token), `openclaw channels add --channel discord`, copy `templates/discord-text-agent/AGENTS.md.example` to `workspace-discord/AGENTS.md`. Walkthrough: [`docs/discord-bot-setup.md`](docs/discord-bot-setup.md). The 11 patcher overrides (steps 20-30) auto-apply once the channel exists; the operator-tunable env knobs are catalogued in [`docs/reference/discord-config.md`](docs/reference/discord-config.md). |
| Image generation | ❌ | Separate compose at `openclaw-image-comfyui/docker-compose.yml` (`--profile image-gen`), **plus** your own ComfyUI install on `host.docker.internal:13036` (or LAN IP), **plus** model weights of your choice (FLUX Krea / SDXL fine-tunes). The repo ships no weights. See [`docs/reference/image-comfyui-bridge.md`](docs/reference/image-comfyui-bridge.md). |
| Memory contents | ❌ (by design) | User's accumulated notes under `workspace/memory/*.md` are operator data, not code. Back up with `tar czf openclaw-$(date +%F).tar.gz -C $OPENCLAW_CONFIG_DIR .` |

What the repo **does** guarantee: bit-stable wiring of every service it ships,
deterministic patcher state on every `up`, pinned `OPENCLAW_IMAGE_REF` digest in
`.env.example`, and idempotent secret generation in `bootstrap.sh`. The
externals (HF model licences, Discord, browser OAuth, image-gen weights) are
externalised precisely because they're decisions the operator must make — not
oversights.

## Fish Audio license note

The TTS surface uses **Fish Audio S2 Pro** (`fishaudio/s2-pro`, 5B param
Qwen3-omni architecture). The model is distributed under the **Fish Audio
Research License — non-commercial use only**. Building the
`openclaw-tts-fish` image pulls the ~11 GB checkpoint from HuggingFace and
constitutes acceptance of the upstream license.

The wrapper code in `openclaw-tts-fish/` is MIT (matches the rest of this
repo). This repo ships no model weights of any kind — they download at build
time.

**For commercial deployments**, either contact Fish Audio (`business@fish.audio`)
for a commercial license, or swap the `FISH_REPO` build arg in
`openclaw-tts-fish/server/Dockerfile` to point at a checkpoint you have a
commercial license to. Wrapper architecture is model-agnostic — the shim
expects any SGLang-Omni-compatible TTS checkpoint with the same `references[]`
voice-cloning schema.

### Adding a custom voice (any language)

Voice cloning happens at request time from mounted reference files — no
fine-tune, no re-build:

```bash
# 1. Record 10-30 s of clean mono speech (16/24 kHz preferred), write the
#    verbatim transcript to a sibling .txt:
#       myvoice.wav   (16-bit PCM mono, no music/noise, no echo)
#       myvoice.txt   (UTF-8 text, exactly what was said in the wav)
#
# 2. Drop both into the openclaw-tts-fish container's voice mount:
docker cp myvoice.wav ${CONTAINER_NAME_PREFIX:-}openclaw-tts-fish:/app/voices/
docker cp myvoice.txt ${CONTAINER_NAME_PREFIX:-}openclaw-tts-fish:/app/voices/

# 3. Request it (no restart needed):
curl -H "Authorization: Bearer $TTS_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"input":"Hello world.","voice":"myvoice"}' \
     http://127.0.0.1:8091/v1/audio/speech --output out.wav
```

Default voices shipped: `default_en` (LibriSpeech / LibriVox PD) and
`default_hu` (Diana Majlinger / "Egri csillagok", LibriVox PD). Both are
seeded from the image's `/app/voices_seed/` on first start without
overwriting user voices. Details + endpoint reference in
[`openclaw-tts-fish/README.md`](openclaw-tts-fish/README.md).

## Why this stack

Running a useful local (or hybrid) agent on top of OpenClaw + vLLM is trickier than the surface picture suggests:

- The OpenClaw onboarding wizard doesn't register NVFP4 models against a self-hosted vLLM provider, leaves `memorySearch` disabled, ships an empty `gateway.trustedProxies`, and writes a placeholder API key — all of which silently break things later.
- Gemma 4 tool calling requires a specific chat template that isn't in the official vLLM image, **plus** a one-line fix to the upstream `gemma4` tool-call parser so colon namespaces like `discord:add_reaction` aren't rejected by the regex — both ship as part of the local `vllm-llm/` image build.
- The bundled OpenClaw `searxng` plugin ships **default-disabled** — `webSearch` looks wired up but doesn't actually fire until you flip it on.
- Hybrid (BM25 + vector) retrieval and MMR re-rank are native OpenClaw features but aren't on by default.
- Discord slash commands [silently fail in guilds](https://github.com/openclaw/openclaw/issues/19310) because of an upstream dual-permission check; the auto-ack reaction has [a known cycle bug](https://github.com/openclaw/openclaw/issues/46024); the `coding` tool profile (default for non-main agents) excludes `browser`, `tts`, and `canvas` so a Discord-routed agent can't reach for half the tools the main agent uses. The patcher fixes all three by default — and every override is env-gated so you can disable any of them.
- On GB10 specifically, unified-memory GPU budgeting between two concurrent vLLM processes needs care (`LLM_GPU_MEM_UTIL` vs `EMBED_GPU_MEM_UTIL`).

This repo captures a known-good wiring for all of the above in a single deterministic `docker compose up`. The `patch-config.mjs` patcher re-applies its 27+ steps on every restart so the wiring survives onboarding-wizard reruns, image upgrades, and manual edits — every step is logged with a `[patch-config]` line and gated by user-managed protection (your hand-edits to `openclaw.json` are preserved).

## License

[MIT](LICENSE). Model weights retain their upstream licenses:

- Gemma 4: **Apache 2.0**
- bge-m3: **MIT**
- Whisper turbo (`deepdml/faster-whisper-large-v3-turbo-ct2`): **MIT**
- Fish Audio S2 Pro (`fishaudio/s2-pro`): **Fish Audio Research License** —
  non-commercial use only. Building `openclaw-tts-fish` triggers the
  ~11 GB weight download from HuggingFace and constitutes acceptance of the
  upstream license. See [Fish Audio license note](#fish-audio-license-note)
  for the commercial path and the `FISH_REPO` build-arg override.

## Contributing

Pull requests welcome. See [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) for the extension points that matter (model swap, quantization swap, custom agents). For issues that aren't about this stack itself, please file them upstream at [vllm-project/vllm](https://github.com/vllm-project/vllm) or [openclaw/openclaw](https://github.com/openclaw/openclaw).
