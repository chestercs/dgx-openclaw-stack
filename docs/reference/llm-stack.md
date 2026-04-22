# LLM stack — Gemma 4 + bge-m3 embedding

> **Public knowledge** — research / architecture, megosztható.

## Aktív LLM (unified OpenClaw stack, 2026-04-22-től)

Primary: `llm/dgx-openclaw-stack/docker-compose.yml` in-stack `vllm-llm` service:
- Image: `vllm/vllm-openai:gemma4-cu130`
- Modell: `nvidia/Gemma-4-31B-IT-NVFP4`
- Port: 8004
- Decode: ~6.9 tok/s GB10-en

## Standalone variánsok (port 8004 mutex, csak egy futhat)

A `llm/` alatt szándékos minta: több Gemma 4 31B variáns ugyanazt a 8004 portot használja — egyszerre csak egyik futhat, swap = `docker compose down` + másik mappa `up -d`.

- `gemma_4_31b_bf16/` — BF16 standalone (`google/gemma-4-31B-it`), 8K × 8 user
- `litellm/` — BF16 + LiteLLM proxy (port 4000) + Postgres, 64K × 2 user
- `gemma_4_31b_nvfp4/` — NVFP4 standalone twin a unified vllm-llm-nek (legacy/fallback)
- `qwen3-5_27b_opus/` — alternative model

Swap előtt: `docker compose stop vllm-llm` (és sibling embedding stack swap-jénél `vllm-embedding`).

## Embedding stack (port 8005)

**Aktuális (2026-04-22-től)**: in-stack `vllm-embedding` service
- Modell: `BAAI/bge-m3` BF16 (natív, modell ~1.1 GB)
- Encoder: XLMRoberta
- Flags: `--runner pooling --gpu-memory-utilization 0.03 --max-model-len 8192 --max-num-seqs 2 --enforce-eager`
- 1024 dim dense kimenet
- VRAM: ~3.6 GB
- Cross-lingual EN↔HU cosine similarity 0.88 (validált)

**Előző (leállítva)**: `qwen3_embedding_8b_nvfp4/` — `alexliap/Qwen3-Embedding-8B-NVFP4`, 4096 dim, ~5 GB. Overkill volt RAG-hez. Standalone dir megmaradt swap-célnak.

## Sibling compose env-trükk

A `qwen3_embedding_8b_nvfp4/` standalone `env_file: ../dgx-openclaw-stack/.env` beülteti a `VLLM_API_KEY`-t. A vLLM automatikusan olvassa env-változóként ha `--api-key` flag nincs megadva. `--api-key ${VLLM_API_KEY:-}` nélkül üres argumentumra dobódna `expected at least one argument` hiba.

## Embedding swap → reindex kötelező

4096-dim Qwen3 vs 1024-dim BGE-M3 vektorok inkompatibilisek, sqlite-vec index invalid lesz. `docker exec openclaw-cli openclaw memory index --force`.

## Közös HF cache volume

`${VLLM_HF_CACHE_VOLUME_NAME:-openclaw-hf-cache}` (operator `.env`-ben `openclaw-hf-cache`, public default `dgx-openclaw-hf-cache`). A unified stack és minden sibling alt-LLM compose ugyanazt mountolja → modell letöltés bármelyikben → mind újrafelhasználja.

## Why NVFP4

~4× kisebb weights → több KV budget a 128 GB unified memoryban. Embedding-nél nem LLM KV budget számít, hanem hogy a Gemma mellé befér.

## Nem létező opció: NVFP4 embedding NVIDIA hivatalos

Nincs `nvidia/*` NVFP4 embedding kiadás (2026-04 állapot). Csak community: alexliap (4.93k dl), Forturne (6.08k dl), MidnightPhreaker, mdavidson83. Az alexliap release dokumentált vLLM deploy útmutatóval (compressed-tensors, `task=embed`).
