# LLM backends

> Reference material: which model runs, how to swap it, and the numbers behind
> the choice. Knob defaults are in [configuration.md](configuration.md).

The stack has one chat model service, `vllm-llm` (hostname `vllm-llm`, port 8004,
OpenClaw provider `vllm`), and one embedding service, `vllm-embedding` (bge-m3,
port 8005). `config-init/steps/models.mjs` registers exactly one `vllm/...` model:
the id in `LLM_MODEL_ID`. Every other `vllm/` entry is removed, because a stale one
shows up in the model picker and answers 404.

A swap replaces the `vllm-llm` command through a file in `compose/`, under the same
service name, hostname and port:

| Backend | How to enable | Notes |
|---|---|---|
| **DeepSeek-V4.1-Flash (284B MoE) on Strata** | `compose/strata.yml` + `compose/strata-llm.yml`, `LLM_BASE_URL=http://strata:8095/v1/` | Reference host's model since 2026-10-28; its own engine, not vLLM: [`openclaw-strata/README.md`](../../openclaw-strata/README.md) |
| Nemotron 3.5 Super 120B-A12B NVFP4 | default (`docker-compose.yml`) | Quick start: no build, vision; the reference host's model before DeepSeek |
| Gemma 4 26B-A4B NVFP4 (MoE) or 31B-IT NVFP4 (dense) | `COMPOSE_FILE=docker-compose.yml:compose/gemma4.yml` + `LLM_MODEL_ID` | Smallest footprint, best Hungarian |
| Mistral Small 4 119B-A6B NVFP4 | `COMPOSE_FILE=docker-compose.yml:compose/mistral.yml` + `LLM_MODEL_ID` | Fastest decode of the large models |

`LLM_MODEL_ID` has to be in `.env` for a swap: compose fails without it, and
config-init would otherwise register the Nemotron id. After a swap, delete
`agents/*/agent/models.json` (a stale provider cache, see
[openclaw-internals.md](openclaw-internals.md)).

## Nemotron 3.5 Super (default)

`nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4` on stock
`vllm/vllm-openai:v0.32.0`, following NVIDIA's DGX Spark recipe:

- `dflash` speculative decoding with the LiLiCorr drafter (`LLM_DRAFT_MODEL`,
  `LLM_SPEC_TOKENS=7`).
- `qwen3_coder` tool parser, `nemotron_v3` reasoning parser.
- `--default-chat-template-kwargs '{"medium_effort": true}'`: full thinking ran
  57k-token turns in tool-using agent runs; thinking off looped on tool-call JSON.
- ~70 GiB of the 128 GB pool at `LLM_GPU_MEM_UTIL=0.70`. That leaves no room for
  ComfyUI or Fish TTS beside it.
- It needs the GB10 clock cap. Under sustained load an uncapped box powers off at
  acpitz ~94 °C, well before the 104 °C trip. See
  [gb10-power-thermal.md](gb10-power-thermal.md).

Why it is the default: it was the most factual of the candidates on the Discord
agent. The operator ranks factual reliability above Hungarian fluency.

| | Qwen3.5-122B | Mistral Small 4 | Nemotron |
|---|---|---|---|
| IQ puzzles (10) | 10 | 6 | 9 |
| Web search (5) | 3, one invented detail | 5 | 5, with dates and sources |
| Honesty traps (8) | — | — | 8 |
| Refusals of edgy-but-legit requests (6) | 1 | 0 | 0 |
| Hungarian | best | odd at times | weakest |

Speed on the GB10 (vLLM v0.32.0):

| | Nemotron 3.5 Super VL | Mistral Small 4 |
|---|---|---|
| Repo eval, thinking on | 12/12 | 10–11/12 |
| Decode, Hungarian, 1 stream | 16.6 tok/s (drafter accepts ~2.3 tokens per step) | 29.6 tok/s |
| Decode, English, 1 stream | 26.2 tok/s | — |
| Decode, 4 streams | 8.8 tok/s each | 19 tok/s each |
| Prefill | ~2560 tok/s | ~1900 tok/s |
| Peak ACPI temperature, 99k-token prefill | 94 °C | 92 °C |

The drafter predicts Hungarian poorly (English runs report ~6.5 accepted tokens per
step), and Hungarian is not among the model card's listed languages. A Discord turn
takes 2–2.5× as long as on Mistral.

### Sampling temperature

OpenClaw sends no temperature unless the model entry in `agents.defaults.models` has
`params.temperature`, so vLLM falls back to the model's `generation_config.json`. For
Nemotron that is 1.0 / top_p 0.95, and at 1.0 Hungarian replies pick up stray tokens
from other scripts (Korean, Cyrillic, Arabic, often mid-word). In a direct A/B, six
Hungarian ~250-word answers per setting, there were 38 foreign-script characters at
1.0 and 2 at 0.6. `LLM_TEMPERATURE=auto` (`config-init/steps/models.mjs`) writes 0.6
on Nemotron entries. The entry hot-reloads; vLLM does not need a restart.

Tried on top of 0.6 (12 Hungarian answers per setting, thinking off, misspelled or
non-Hungarian words counted by a judge pass):

| Setting | Foreign-script chars | Flagged words / 100 |
|---|---|---|
| top_p 0.95 (model default, kept) | 2 | 2.3 |
| top_p 0.9 | 5 | 2.95 |
| min_p 0.05 | — | — |

`min_p` (and `logit_bias`) are rejected with HTTP 400 while speculative decoding is
on, so they would cost the drafter.

### Thinking depth

Nemotron's chat template has three settings: `enable_thinking` false (off),
`medium_effort` true (the server default here) and `medium_effort` false (full).
OpenClaw exposes only an on/off `/think` for vLLM Nemotron, so `thinkingDefault:
"high"` and `"medium"` send the same request. A deeper level is reachable through a
per-agent `params.chat_template_kwargs`, which does reach vLLM.

Two sets of 10 reasoning questions with checkable answers, two runs each, directly on
vLLM:

| Set | Medium | Full |
|---|---|---|
| easier | 19/20, median 34 s, 410 tokens | 20/20, 47 s, 873 tokens |
| harder | 20/20, median 21 s, 499 tokens | 20/20, 48 s, 1081 tokens |

Full thinking bought nothing measurable at 2–2.5× the time, so the full-thinking
`deep` sub-agent (`OPENCLAW_DISCORD_DEEP_AGENT`) ships off.

## Thinking switch (catalog `compat`)

A custom `vllm` provider sends no thinking control unless the catalog entry says how.
Without it the model never thinks, whatever `thinkingDefault` says.
`config-init/steps/models.mjs` writes `compat` per model family
(`LLM_THINKING_COMPAT=auto`):

- Gemma 4 and Nemotron: `thinkingFormat: "qwen-chat-template"`. OpenClaw then sends
  `chat_template_kwargs.enable_thinking`. The Gemma 4 template defaults to `false`.
- DeepSeek-V4.1 on Strata: `thinkingFormat: "deepseek"` with efforts `low`, `medium`, `high`,
  `xhigh` and `max` (`minimal` → low, `xhigh` → max). OpenClaw sends DeepSeek's
  `thinking: {type}` and `reasoning_effort`, so `/think low|medium|high|max` on Discord and the
  thinking picker in the Web UI change how long DeepSeek thinks. With the qwen switch every level
  thought at the template's default (high): OpenClaw sends no effort in that format. `/think off`
  arrives as `thinking.type: "disabled"` alone, which Strata reads with the repo's patch.
- Mistral / Magistral: `supportsReasoningEffort: true`, efforts `none` / `high`, with
  every OpenClaw level mapped onto those two (vLLM rejects other values).

Check what a turn really did in the agent sqlite: a `model.completed` event carries
`usage.reasoningTokens`.

## Gemma 4 (compose/gemma4.yml)

- `nvidia/Gemma-4-26B-A4B-NVFP4`: 25.2B total / 3.8B active per token, 128 experts
  top-8, ~16.5 GB of weights. Measured ~52 tok/s decode on the GB10.
- `nvidia/Gemma-4-31B-IT-NVFP4`: 31.3B dense, ~17 GB of weights, ~6.9 tok/s.
- `gemma4` tool and reasoning parsers, plus the repo's tool chat template
  (`compose/gemma4/tool_chat_template_gemma4.jinja`, mounted at `/templates`), tuned
  for OpenClaw's tool calls.
- The numbers were measured on vLLM 0.19–0.31 with a patched image; they have not
  been re-measured on stock v0.32.

Gemma writes the most natural Hungarian of the three, but scored lowest on the
reasoning and tool-use eval below.

## Mistral Small 4 (compose/mistral.yml)

`mistralai/Mistral-Small-4-119B-2603-NVFP4` on `vllm/vllm-openai:v0.31.0`:

- 119B total / ~6.5B active per token (128 experts, 4 active), MLA attention, 256K
  context, text + image input, Apache 2.0.
- Mistral-format checkpoint (no HF `config.json`): `--tokenizer-mode`,
  `--config-format` and `--load-format mistral`.
- `--attention-backend TRITON_MLA`: the FlashAttention / FlashInfer MLA decode
  kernels crash on sm121.
- Thinking per request via `reasoning_effort` `none` | `high`.
- No speculative decoding: the EAGLE draft does not load on vLLM v0.31 / v0.32, and
  a published GB10 EAGLE run reached the same ~29.5 tok/s.

Measured on the GB10 (vLLM v0.31.0):

| | Mistral Small 4 NVFP4 | Gemma 4 26B-A4B NVFP4 |
|---|---|---|
| Weights | 66 GiB | ~16.5 GiB |
| vLLM process | ~78 GiB (util 0.65) | ~36 GiB (util 0.30) |
| Decode, 1 stream | 29.6 tok/s | ~25–52 tok/s |
| Decode, 4 streams | 75 tok/s total, 19 each | — |
| Prefill | ~1900 tok/s (99k tokens in 52 s) | — |
| Cold start to healthy | ~9.5 min (450 s weight load from USB SSD) | ~3 min |
| Repo eval, thinking on | 11/12 | 9.5/12 |
| Repo eval, thinking off | 8/12 | 7/12 |

The repo eval is 12 Discord-agent turns through `openclaw agent` (Hungarian chat,
arithmetic, a logic puzzle, Python exec, code, strict formatting, web search, honesty
about unknown history, memory write and read, casual chat, translation). Mistral
sometimes invents Hungarian words.

## Embeddings (vllm-embedding)

- `BAAI/bge-m3` BF16 (~1.1 GB), XLMRoberta encoder, 1024-dim dense output.
- `--runner pooling --gpu-memory-utilization 0.03 --max-model-len 8192
  --max-num-seqs 2 --enforce-eager`; ~3.6 GB of the pool.
- Cross-lingual EN↔HU cosine similarity 0.88.

The embedding dimension is fixed when the first chunk is indexed. After a change of
embedding model, rebuild the index for each agent:
`docker exec openclaw-cli openclaw memory index --force`.

## Shared HF cache volume

`${VLLM_HF_CACHE_VOLUME_NAME:-dgx-openclaw-hf-cache}` is bound to
`VLLM_HF_CACHE_DIR`. A sibling compose project that mounts the same volume reuses the
cached weights instead of downloading them again.

## Remote or cloud backend

Point `LLM_BASE_URL` and `EMBED_BASE_URL` at the other endpoint (keep the trailing
slash) and park the local `vllm-llm` / `vllm-embedding` services. See
[CUSTOMIZATION.md](../CUSTOMIZATION.md).

## Why NVFP4

About 4× smaller weights than BF16, which leaves more KV-cache budget in the 128 GB
unified pool.
