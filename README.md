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

## What you get

A fully local agent platform (or local-plus-cloud-LLM hybrid — your choice), with:

| Component | What it does |
|---|---|
| **Gemma 4 31B IT (NVFP4)** | 31B-parameter Google Gemma 4 dense model, quantized by NVIDIA to NVFP4 (FP4 with NVIDIA's block format). Native tool calling, 256K context, multimodal (text + image). |
| **bge-m3 embeddings** | BAAI/bge-m3 multilingual dense embeddings via vLLM. 100+ languages, 1024-dim, 8K context, EN↔HU cosine ≈ 0.88. |
| **SearxNG meta-search** | Self-hosted, privacy-respecting web search backend wired into OpenClaw's native `webSearch` provider. Strict engine whitelist (DuckDuckGo, Brave, Mojeek, Qwant, Startpage, Wikipedia family, Reddit, GitHub, arXiv) — queries never reach Google / Bing / Yandex / Yahoo / Baidu. |
| **OpenClaw gateway** | The open-source agent runtime: Chrome extension UI, CLI, persistent memory, heartbeat, multi-agent world-building. |
| **Idempotent config patcher** | A small Node script that makes your OpenClaw config deterministic — runs on every `up`, never clobbers onboarding choices it shouldn't. Wires hybrid (BM25 + vector) retrieval with MMR re-rank on top of `memorySearch`, and flips the bundled SearxNG plugin on. |

Everything lives in one Docker Compose file. No separate vLLM service definitions, no reverse-proxied DNS trickery, no `host.docker.internal` workarounds — containers reach each other by their compose service name on the default bridge network.

## Who is this for

- **GB10 owners (DGX Spark, ASUS Ascent GB10)** — the calibrated reference profile. Boot it, get ~6.9 tok/s decode on Gemma 4 31B NVFP4 with 256K context, multimodal, tool calling, hybrid memory, private web search.
- **x86_64 + consumer NVIDIA GPU (RTX 4090 etc.)** — the LLM service is the only piece tied to GB10 hardware. Swap `vllm-llm` for any model your VRAM holds (smaller Gemma 4, Qwen 2.5, Llama 3.3…) using a stock vLLM image; the rest of the stack and the patcher's known-good wiring transfer unchanged. Pointers in [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md).
- **Cloud LLM users (OpenAI, Anthropic, OpenRouter, AWS Bedrock…)** — comment out the `vllm-llm` service entirely and point OpenClaw's vllm provider at your hosted endpoint. You still get bge-m3 (cheap local multilingual embeddings, no per-token cost), SearxNG (private web search, no Tavily/Serper key), hybrid + MMR retrieval, idempotent config, dreaming, heartbeat — most of what makes this repo useful is independent of where the LLM runs.

## Hardware targets

The reference profile (`docker compose up -d` with no edits) is designed and tested on:

- **NVIDIA DGX Spark** (GB10 Superchip, 128 GB unified LPDDR5X, 273 GB/s)
- **ASUS Ascent GB10** (same GB10 Superchip, same memory architecture)

Works unchanged on **any future workstation built around the GB10 Superchip** — the stack doesn't depend on DGX- or ASUS-specific firmware, only on the Blackwell datacenter compute capabilities (`sm_120`/`sm_121`) and the GB10's 128 GB unified memory budget.

The reference profile **won't boot as-is on non-GB10 hardware** — `vllm/vllm-openai:gemma4-cu130` and the NVFP4 model both need Blackwell FP4 kernels. Two supported alternatives, sketched briefly in [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md):

- **Other NVIDIA GPU**: switch to a stock vLLM image and a model that fits your VRAM (smaller Gemma 4 NVFP4 if you have a Blackwell desktop; Gemma 4 12B BF16 / Qwen 2.5 / Llama 3.3 elsewhere). The memory-split and concurrency constants in `.env.example` will need re-tuning for your card.
- **Cloud LLM**: drop the `vllm-llm` service and let OpenClaw talk OpenAI/Anthropic/etc. directly via its vllm provider config. The bge-m3 embedding service still runs locally (or also goes cloud), everything else is unchanged.

### Performance (measured)

| Scenario | Value |
|---|---|
| Decode throughput, 1 user | ~6.9 tok/s (NVFP4) vs ~3.7 tok/s (BF16) — ~2× speedup |
| Stable context, 1 user | ~220K tokens (before preemption) |
| Stable context, 2 users | ~110K tokens each, continuous batching |
| Vision prefill per image | ~280 vision tokens (≈ 512×512 region), sub-second encode |
| First-boot cold start | ~3–4 min once weights are cached |
| KV cache type | FP8 (halves cache footprint) |

## Quickstart

```bash
git clone https://github.com/chestercs/dgx-openclaw-stack.git
cd dgx-openclaw-stack

./bootstrap.sh          # interactive, non-destructive, idempotent
docker compose up -d
docker compose logs -f  # watch services come up (~3-4 min for vllm-llm)
```

Then open the OpenClaw Chrome extension and pair it with `ws://<your-host>:18789` using the gateway token printed at the end of `bootstrap.sh`.

Full walkthrough: [SETUP.md](SETUP.md).

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
- **Long context, honest numbers.** 256K model max; realistic stable bands (per user count) are documented in the compose file.
- **Idempotent configuration.** The patcher re-applies a known-good state on every `up`. Safe to run repeatedly.
- **Reverse-proxy ready.** `gateway.trustedProxies` is pre-populated; add your LAN CIDR via `OPENCLAW_LAN_CIDR` if needed.
- **Non-destructive bootstrap.** `bootstrap.sh` never overwrites an existing `.env` value or host directory.

## Repository layout

```
dgx-openclaw-stack/
├─ docker-compose.yml           # the whole stack (vllm-llm + vllm-embedding + searxng + openclaw-*)
├─ patch-config.mjs             # idempotent OpenClaw config patcher (10 steps)
├─ bootstrap.sh                 # non-destructive first-time setup
├─ .env.example                 # documented env template
├─ templates/
│  └─ tool_chat_template_gemma4.jinja   # Gemma 4 tool-call chat template
├─ searxng/
│  └─ settings/
│     └─ settings.yml           # SearxNG override: JSON API + strict engine whitelist
├─ docs/
│  ├─ ARCHITECTURE.md
│  ├─ TROUBLESHOOTING.md
│  └─ CUSTOMIZATION.md
├─ LICENSE                      # MIT
├─ README.md                    # you are here
└─ SETUP.md                     # detailed step-by-step guide
```

## Why this stack

Running a useful local (or hybrid) agent on top of OpenClaw + vLLM is trickier than the surface picture suggests:

- The OpenClaw onboarding wizard doesn't register NVFP4 models against a self-hosted vLLM provider, leaves `memorySearch` disabled, ships an empty `gateway.trustedProxies`, and writes a placeholder API key — all of which silently break things later.
- Gemma 4 tool calling requires a specific chat template that isn't in the official vLLM image.
- The bundled OpenClaw `searxng` plugin ships **default-disabled** — `webSearch` looks wired up but doesn't actually fire until you flip it on.
- Hybrid (BM25 + vector) retrieval and MMR re-rank are native OpenClaw features but aren't on by default.
- On GB10 specifically, unified-memory GPU budgeting between two concurrent vLLM processes needs care (`LLM_GPU_MEM_UTIL` vs `EMBED_GPU_MEM_UTIL`).

This repo captures a known-good wiring for all of the above in a single deterministic `docker compose up`. The `patch-config.mjs` patcher re-applies it on every restart so the wiring survives onboarding-wizard reruns, image upgrades, and manual edits.

## License

[MIT](LICENSE). Model weights retain their upstream licenses (Gemma 4: Apache 2.0, bge-m3: MIT).

## Contributing

Pull requests welcome. See [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) for the extension points that matter (model swap, quantization swap, custom agents). For issues that aren't about this stack itself, please file them upstream at [vllm-project/vllm](https://github.com/vllm-project/vllm) or [openclaw/openclaw](https://github.com/openclaw/openclaw).
