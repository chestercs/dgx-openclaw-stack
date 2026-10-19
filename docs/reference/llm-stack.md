# LLM stack — Gemma 4 + bge-m3 embedding

> Reference material: model choices, variants, and the rationale behind them.

## Active LLM (unified OpenClaw stack, 2026-05 onward)

Primary: `llm/dgx-openclaw-stack/docker-compose.yml` — TWO concurrent in-stack services running side by side, no profile-mutex:
- `vllm-llm` (MoE) on hostname `vllm-llm`, port 8004, OpenClaw provider id `vllm`
- `vllm-llm-dense` on hostname `vllm-llm-dense`, port 8005, OpenClaw provider id `vllm-dense`

Both reachable simultaneously; the UI picks the active model.

**MoE default** (`vllm-llm` service):
- Image: `vllm/vllm-openai:gemma4-cu130` + 1-line tool-call-parser regex patch (`./vllm-llm/Dockerfile`)
- Model: `nvidia/Gemma-4-26B-A4B-NVFP4` — 25.2B total / 3.8B active per token, 128 experts top-8
- Quantization: NVFP4 via NVIDIA Model Optimizer `nvfp4_experts_only` recipe (~16.5 GB weights)
- MoE backend: Marlin (mandatory on Blackwell SM121 — CUTLASS NaNs on the fused 3D expert format)
- Port: 8004
- Decode: ~52 tok/s on GB10 (verified ai-muninn 2026-04, ~7.5× faster than the dense 31B it replaces)
- Vision tower included; same `gemma4` parser + `tool_chat_template_gemma4.jinja` as dense

**Dense concurrent** (`vllm-llm-dense`, runs alongside MoE, port 8005, no profile):
- Same image, same chat template, same parsers
- Model: `nvidia/Gemma-4-31B-IT-NVFP4` — 31.3B dense in NVFP4 (~17 GB weights)
- Decode: ~6.9 tok/s on GB10
- Tuned for low-RAM single-user 256K (`LLM_GPU_MEM_UTIL_DENSE=0.30`, `LLM_MAX_NUM_SEQS_DENSE=1`)
- Hostname `vllm-llm-dense`, separate OpenClaw provider id `vllm-dense` (`baseUrl: http://vllm-llm-dense:8005/v1/`)
- Together both backends ≈ 76 GB host RAM use, leaving 50+ GB free

Both are registered in the OpenClaw catalog by `patch-config.mjs` (`LLM_MODEL_ENTRIES[]` array). The active model is picked via the OpenClaw UI's model dropdown — the schema doesn't expose a writable `agents.defaults.llm.model` field, so the patcher only ensures the entries exist; it doesn't pin a default.

## Mistral Small 4 backend (opt-in override, 2026-10)

`docker-compose.mistral.yml` swaps the `vllm-llm` service to
`mistralai/Mistral-Small-4-119B-2603-NVFP4` under the same service name,
hostname and port. Enable it with both `.env` lines:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.mistral.yml
LLM_MODEL_ID=mistralai/Mistral-Small-4-119B-2603-NVFP4
```

The patcher then replaces the Gemma catalog entry and moves the default,
image and utility model references (and `modelPolicy.allow`) to the Mistral id.
After a swap, delete `agents/*/agent/models.json` (stale provider cache, see
openclaw-internals).

- Model: 119B total / ~6.5B active per token (128 experts, 4 active), MLA
  attention, 256K context, text + image input, Apache 2.0.
- Image: upstream `vllm/vllm-openai:v0.31.0` (multi-arch, CUDA 13.0, FlashInfer
  kernels for sm121a). No patches.
- Mistral-format checkpoint (no HF `config.json`):
  `--tokenizer-mode/--config-format/--load-format mistral`.
- `--attention-backend TRITON_MLA`: the FlashAttention / FlashInfer MLA decode
  kernels are sm100 builds and crash on GB10.
- Thinking is per request via `reasoning_effort` `none` | `high`. OpenClaw only
  sends it when the catalog entry has `compat.supportsReasoningEffort`. The
  patcher writes that for Mistral ids (see "Thinking switch" below).
- No speculative decoding. The official EAGLE draft does not load on vLLM
  v0.31.0 / v0.32.0, and the published GB10 EAGLE run reached the same
  ~29.5 tok/s.

Measured on GB10 (vLLM v0.31.0, 2026-10-18):

| | Mistral Small 4 NVFP4 | Gemma 4 26B-A4B NVFP4 |
|---|---|---|
| Weights | 66 GiB | ~16.5 GiB |
| vLLM process at the stack default | ~78 GiB (util 0.65) | ~36 GiB (util 0.30) |
| Decode, 1 stream | 29.6 tok/s | ~25–52 tok/s (see above) |
| Decode, 4 streams | 75 tok/s total, 19 each | — |
| Prefill | ~1900 tok/s (99k tokens in 52 s) | — |
| Cold start to healthy | ~9.5 min (450 s weight load from USB SSD) | ~3 min |
| Repo eval, thinking on | 11/12 | 9.5/12 |
| Repo eval, thinking off | 8/12 | 7/12 |

The eval is 12 Discord-agent turns through `openclaw agent` (Hungarian chat,
arithmetic, a logic puzzle, Python exec, code, strict formatting, web search,
honesty about unknown history, memory write and read, casual chat,
translation). Mistral wins on reasoning and tool use. Gemma writes the more
natural Hungarian: Mistral now and then invents words.

### Nemotron 3.5 Super VL 120B-A12B NVFP4 (tried 2026-10-18, default on the reference host since 2026-10-19)

**Update 2026-10-19.** The reference host now runs Nemotron as its Discord
model through [`docker-compose.nemotron.yml`](../../docker-compose.nemotron.yml)
(util 0.70, medium-effort thinking, GPU clock cap). The operator ranks factual
reliability above Hungarian fluency. A three-way run on the Discord agent:

| | Qwen3.5-122B | Mistral Small 4 | Nemotron |
|---|---|---|---|
| IQ puzzles (10) | 10 | 6 | 9 |
| Web search (5) | 3, one invented detail | 5 | 5, with dates and sources |
| Honesty traps (8) | — | — | 8 |
| Refusals of edgy-but-legit requests (6) | 1 | 0 | 0 |
| Hungarian | best | odd at times | weakest |

The two hard power-offs blamed on Nemotron below turned out to be the GB10
cutting out at acpitz ~94 °C, well before the 104 °C trip. The clock cap in
[`gb10-power-thermal.md`](./gb10-power-thermal.md) fixes that.

NVIDIA's DGX Spark recipe (vLLM v0.32.0, LiLiCorr `dflash` drafter with 7
draft tokens, `--max-num-seqs 1`, `qwen3_coder` tool parser, `nemotron_v3`
reasoning parser, thinking via `chat_template_kwargs.enable_thinking`) runs on
the GX10. It is not the default:

| | Nemotron 3.5 Super VL | Mistral Small 4 |
|---|---|---|
| Repo eval, thinking on | 12/12 | 10–11/12 |
| Decode, Hungarian, 1 stream | 16.6 tok/s (drafter accepts ~2.3 tokens per step) | 29.6 tok/s |
| Decode, English, 1 stream | 26.2 tok/s | — |
| Decode, 4 streams | 8.8 tok/s each | 19 tok/s each |
| Prefill | ~2560 tok/s | ~1900 tok/s |
| vLLM process at util 0.75 / 0.65 | ~91 GiB | ~78 GiB |
| Peak ACPI temperature, 99k-token prefill | 94 °C | 92 °C |

The LiLiCorr drafter predicts Hungarian poorly: the forum's English runs
report ~6.5 accepted tokens per step. Hungarian is not among the model card's
listed languages either.

Tuning at `--gpu-memory-utilization 0.68` with ComfyUI image generation
running beside it (both fit, ~8-16 GB left). Chars/s compares the two
tokenizers fairly. The 12-case eval below uses the English agent prompt.

| | Mistral Small 4 | Nemotron, 7 draft tokens | Nemotron, 3 draft tokens | Nemotron, no drafter |
|---|---|---|---|---|
| Hungarian prose, 1 stream | 98 chars/s | ~55 chars/s | 69 chars/s | 64 chars/s |
| Hungarian prose, 4 streams, each | 61 chars/s | ~29 chars/s | 36 chars/s | 41 chars/s |
| Thinking on (English reasoning), 1 stream | — | — | 87 chars/s (38.8 tok/s, acceptance ~3) | 47 chars/s |
| Repo eval | 11/12, 218 s total | — | 11/12, 430 s total | 12/12, 532 s total |
| KV cache | 441k tokens (util 0.65) | — | 633k tokens | 1.7M tokens |

Nemotron is the stronger reasoner, but a Discord turn takes 2-2.5× as long.
Its Hungarian is also worse than Mistral's: Portuguese and Chinese tokens
showed up in the replies ("ajudarni", "只有"), along with misspellings, so
Mistral Small 4 stayed the default. The GX10's ACPI trip point is 104 °C. One forum report has a DGX
Spark powering off during 32K+ contexts with this model. Run long-prefill
experiments with a temperature watchdog that stops the vLLM container first.

## Thinking switch (catalog `compat`)

A custom `vllm` provider sends no thinking control unless the catalog entry
says how. Before 2026-10-18 every Gemma Discord turn ran without thinking,
despite `thinkingDefault: "high"` (0 of 225 turns had reasoning tokens).
`patch-config.mjs` now writes `compat` per model family
(`LLM_THINKING_COMPAT=auto`, default):

- Gemma 4 → `thinkingFormat: "qwen-chat-template"`. OpenClaw then sends
  `chat_template_kwargs.enable_thinking`, which the Gemma 4 template reads.
  The template's default is `false`.
- Mistral / Magistral / Devstral → `supportsReasoningEffort: true`, efforts
  `none` / `high`, and every OpenClaw level mapped onto those two (vLLM rejects
  other values).

Check what a turn really did in the agent sqlite: a `model.completed` event
carries `usage.reasoningTokens`.

## Standalone variants (port 8004 mutex — only one at a time)

Under `llm/` there's an intentional pattern: several Gemma 4 variants use the same port 8004 — only one can run at a time. Swap = `docker compose down` in one directory, `up -d` in another.

- `gemma_4_31b_bf16/` — BF16 standalone (`google/gemma-4-31B-it`), 8K × 8 users
- `litellm/` — BF16 + LiteLLM proxy (port 4000) + Postgres, 64K × 2 users
- `gemma_4_31b_nvfp4/` — NVFP4 standalone twin of the unified `vllm-llm-dense` (legacy / fallback)
- `qwen3-5_27b_opus/` — alternative model

Before swapping: `docker compose stop vllm-llm` (and `vllm-embedding` if the sibling embedding stack also swaps).

## Embedding stack (port 8005)

**Current (2026-04-22 onward):** in-stack `vllm-embedding` service

- Model: `BAAI/bge-m3` BF16 (native, model ~1.1 GB)
- Encoder: XLMRoberta
- Flags: `--runner pooling --gpu-memory-utilization 0.03 --max-model-len 8192 --max-num-seqs 2 --enforce-eager`
- 1024-dim dense output
- VRAM: ~3.6 GB
- Cross-lingual EN↔HU cosine similarity 0.88 (validated)

**Previous (retired):** `qwen3_embedding_8b_nvfp4/` — `alexliap/Qwen3-Embedding-8B-NVFP4`, 4096-dim, ~5 GB. Overkill for RAG. The standalone directory remains as a swap target.

## Sibling compose env trick

The `qwen3_embedding_8b_nvfp4/` standalone uses `env_file: ../dgx-openclaw-stack/.env` to inherit `VLLM_API_KEY`. vLLM reads it automatically as an env variable when no `--api-key` flag is passed. Without `--api-key ${VLLM_API_KEY:-}`, an empty argument would trigger an `expected at least one argument` error.

## Embedding swap → reindex mandatory

4096-dim Qwen3 vectors and 1024-dim BGE-M3 vectors are incompatible; the sqlite-vec index becomes invalid. Run `docker exec openclaw-cli openclaw memory index --force`.

## Shared HF cache volume

`${VLLM_HF_CACHE_VOLUME_NAME:-dgx-openclaw-hf-cache}`. The unified stack and every sibling alt-LLM compose mounts the same volume — download a model in any of them and all the others reuse the cached weights.

## Why NVFP4

~4× smaller weights → more KV budget in the 128 GB unified memory. For embedding, LLM KV budget doesn't matter; what matters is that bge-m3 fits alongside Gemma on the same GPU.

## Non-existent option: official NVIDIA NVFP4 embedding

There is no `nvidia/*` NVFP4 embedding release (as of 2026-04). Only community ports: alexliap (4.93k downloads), Forturne (6.08k), MidnightPhreaker, mdavidson83. The alexliap release is the best-documented, with a vLLM deploy guide (compressed-tensors, `task=embed`).
