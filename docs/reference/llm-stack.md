# LLM stack — Gemma 4 + bge-m3 embedding

> Reference material: model choices, variants, and the rationale behind them.

## Active LLM (unified OpenClaw stack, 2026-04-22 onward)

Primary: `llm/dgx-openclaw-stack/docker-compose.yml` — in-stack `vllm-llm` service:

- Image: `vllm/vllm-openai:gemma4-cu130`
- Model: `nvidia/Gemma-4-31B-IT-NVFP4`
- Port: 8004
- Decode: ~6.9 tok/s on GB10

## Standalone variants (port 8004 mutex — only one at a time)

Under `llm/` there's an intentional pattern: several Gemma 4 31B variants use the same port 8004 — only one can run at a time. Swap = `docker compose down` in one directory, `up -d` in another.

- `gemma_4_31b_bf16/` — BF16 standalone (`google/gemma-4-31B-it`), 8K × 8 users
- `litellm/` — BF16 + LiteLLM proxy (port 4000) + Postgres, 64K × 2 users
- `gemma_4_31b_nvfp4/` — NVFP4 standalone twin of the unified `vllm-llm` (legacy / fallback)
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
