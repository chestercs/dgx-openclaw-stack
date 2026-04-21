<!--
Keywords for discovery: NVIDIA DGX Spark, ASUS Ascent GB10, Grace-Blackwell,
GB10 Superchip, NVFP4, FP4 quantization, Gemma 4 31B, vLLM, local LLM,
self-hosted agent, OpenClaw, bge-m3, multilingual embeddings, RAG, tool calling,
128 GB unified memory, ARM64 AI workstation, edge AI, on-device AI, docker compose.
-->

# DGX OpenClaw Stack

> A one-command, production-grade local AI agent stack for **NVIDIA DGX Spark** and **ASUS Ascent GB10**.
> Gemma 4 31B (NVFP4) + bge-m3 embeddings + the OpenClaw agent gateway,
> wired together in a single `docker compose` file.

[![Docker Compose](https://img.shields.io/badge/docker%20compose-24.0%2B-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![vLLM](https://img.shields.io/badge/vLLM-0.11%2B-7C3AED)](https://github.com/vllm-project/vllm)
[![Gemma 4](https://img.shields.io/badge/Gemma%204-31B%20NVFP4-4285F4)](https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4)
[![OpenClaw](https://img.shields.io/badge/OpenClaw-2026.4.15%2B-0A7F3F)](https://openclaw.ai)
[![Hardware](https://img.shields.io/badge/hardware-DGX%20Spark%20%7C%20ASUS%20GB10-76B900?logo=nvidia&logoColor=white)](#hardware-targets)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

---

## What you get

A fully local agent platform, running on a single GB10-class box, with:

| Component | What it does |
|---|---|
| **Gemma 4 31B IT (NVFP4)** | 31B-parameter Google Gemma 4 dense model, quantized by NVIDIA to NVFP4 (FP4 with NVIDIA's block format). Native tool calling, 256K context, multimodal (text + image). |
| **bge-m3 embeddings** | BAAI/bge-m3 multilingual dense embeddings via vLLM. 100+ languages, 1024-dim, 8K context, EN↔HU cosine ≈ 0.88. |
| **OpenClaw gateway** | The open-source agent runtime: Chrome extension UI, CLI, persistent memory, heartbeat, multi-agent world-building. |
| **Idempotent config patcher** | A small Node script that makes your OpenClaw config deterministic — runs on every `up`, never clobbers onboarding choices it shouldn't. |

Everything lives in one Docker Compose file. No separate vLLM service definitions, no reverse-proxied DNS trickery, no `host.docker.internal` workarounds — containers reach each other by their compose service name on the default bridge network.

## Hardware targets

Designed and tested on:

- **NVIDIA DGX Spark** (GB10 Superchip, 128 GB unified LPDDR5X, 273 GB/s)
- **ASUS Ascent GB10** (same GB10 Superchip, same memory architecture)

It will **not** run on non-GB10 hardware out of the box because of NVFP4 kernel requirements (`sm_120`/`sm_121`). For RTX 50-series Blackwell desktops see `docs/CUSTOMIZATION.md` (smaller models, different kernels).

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
    │  ┌─────────────────┐    ┌─────────────────┐                    │
    │  │ vllm-llm        │    │ vllm-embedding  │                    │
    │  │ :8004 (internal)│    │ :8005 (internal)│                    │
    │  │ Gemma 4 31B     │    │ bge-m3 (567M)   │                    │
    │  │ NVFP4, 256K ctx │    │ 1024-dim, 8K ctx│                    │
    │  └────────▲────────┘    └────────▲────────┘                    │
    │           │ compose DNS          │ compose DNS                  │
    │  ┌────────┴──────────────────────┴────────┐                    │
    │  │ openclaw-gateway      :18789 (exposed) │◀── Chrome ext.     │
    │  │   └ openclaw-config-init (one-shot)    │◀── CLI             │
    │  │   └ openclaw-cli         (always-up)   │                    │
    │  └────────────────────────────────────────┘                    │
    └────────────────────────────────────────────────────────────────┘
```

All inter-container traffic is on the compose default bridge network; only port `18789` is published to the host. Put a reverse proxy (Nginx Proxy Manager, Caddy, Traefik, or a Cloudflared tunnel) in front for public access over `wss://`.

Deep dive: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Features

- **One compose file for everything.** LLM, embedding, and agent stack in one `docker compose up -d`.
- **NVFP4-native.** Ships with the official `vllm/vllm-openai:gemma4-cu130` image; no custom build required.
- **True tool calling.** The shipped `tool_chat_template_gemma4.jinja` plus `--tool-call-parser gemma4 --enable-auto-tool-choice` produces OpenAI-format `tool_calls`. OpenClaw uses them directly.
- **Multimodal.** Gemma 4's vision tower is included. Drop an image into the chat; the model reads it at ~280 tokens per image by default.
- **Multilingual RAG built in.** bge-m3 gives you high-quality cross-lingual embeddings for `memorySearch` out of the box.
- **Long context, honest numbers.** 256K model max; realistic stable bands (per user count) are documented in the compose file.
- **Idempotent configuration.** The patcher re-applies a known-good state on every `up`. Safe to run repeatedly.
- **Reverse-proxy ready.** `gateway.trustedProxies` is pre-populated; add your LAN CIDR via `OPENCLAW_LAN_CIDR` if needed.
- **Non-destructive bootstrap.** `bootstrap.sh` never overwrites an existing `.env` value or host directory.

## Repository layout

```
dgx-openclaw-stack/
├─ docker-compose.yml           # the whole stack (vllm-llm + vllm-embedding + openclaw-*)
├─ patch-config.mjs             # idempotent OpenClaw config patcher
├─ bootstrap.sh                 # non-destructive first-time setup
├─ .env.example                 # documented env template
├─ templates/
│  └─ tool_chat_template_gemma4.jinja   # Gemma 4 tool-call chat template
├─ docs/
│  ├─ ARCHITECTURE.md
│  ├─ TROUBLESHOOTING.md
│  └─ CUSTOMIZATION.md
├─ LICENSE                      # MIT
├─ README.md                    # you are here
└─ SETUP.md                     # detailed step-by-step guide
```

## Why this stack

Running a useful local agent on a DGX Spark / ASUS GB10 is trickier than the hardware specs suggest:

- The onboarding wizards of most agent frameworks don't register NVFP4 models correctly against a self-hosted vLLM provider.
- Gemma 4 tool calling requires a specific chat template that isn't in the official image.
- Embedding providers, memory search, and reverse-proxy trust need to be wired up manually.
- Unified-memory GPU budgeting with two concurrent vLLM processes needs care (`LLM_GPU_MEM_UTIL` vs `EMBED_GPU_MEM_UTIL`).

This repo captures a known-good configuration for all of the above in one deterministic bring-up.

## License

[MIT](LICENSE). Model weights retain their upstream licenses (Gemma 4: Apache 2.0, bge-m3: MIT).

## Contributing

Pull requests welcome. See [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) for the extension points that matter (model swap, quantization swap, custom agents). For issues that aren't about this stack itself, please file them upstream at [vllm-project/vllm](https://github.com/vllm-project/vllm) or [openclaw/openclaw](https://github.com/openclaw/openclaw).
