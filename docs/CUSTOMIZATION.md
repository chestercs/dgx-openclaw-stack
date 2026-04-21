# Customization

How to swap things out without breaking the stack.

---

## Swap the LLM

The three coupled pieces are:

1. The `--model` flag on `vllm-llm` in `docker-compose.yml`.
2. The `LLM_MODEL_ID` constant in `patch-config.mjs`.
3. The `LLM_MODEL_ENTRY` metadata in `patch-config.mjs` (context window, input modalities, reasoning flag).

Any model change requires editing all three.

### Smaller Gemma 4 (12B NVFP4)

If you want to run two users at full 256K context each, or you need to leave more headroom for other workloads, drop to the 12B variant:

```yaml
# docker-compose.yml
--model nvidia/Gemma-4-12B-IT-NVFP4
```

```js
// patch-config.mjs
const LLM_MODEL_ID = 'nvidia/Gemma-4-12B-IT-NVFP4';
```

The 12B NVFP4 weighs ~6–7 GB. Bump `LLM_GPU_MEM_UTIL` to `0.50–0.60` if you still want a big KV cache, or stay at `0.68` and get a huge effective KV budget.

### BF16 Gemma 4 (if you're on non-NVFP4 hardware)

Remove `--quantization modelopt` and swap the model id:

```yaml
--model google/gemma-4-31b-it
# (remove) --quantization modelopt
```

BF16 weights are ~62 GB — you'll need to raise `LLM_GPU_MEM_UTIL` and give up the embedding stack. Expect ~3.7 tok/s decode vs ~6.9 tok/s for NVFP4 on GB10.

### Non-Gemma models

The `--tool-call-parser gemma4` / `--reasoning-parser gemma4` / `--chat-template tool_chat_template_gemma4.jinja` trio is model-family-specific. Swapping to, say, Qwen3 or DeepSeek-R1 means:

- Change the parser names (`qwen3`, `deepseek_r1`, etc.).
- Provide the corresponding chat template under `templates/`.
- Set `LLM_MODEL_ENTRY.reasoning` correctly (some models have separate `<thinking>` channels that OpenClaw understands if you flag them).

Also update the model's `contextWindow` and `maxTokens` in `LLM_MODEL_ENTRY` — OpenClaw uses these to cap tool call prompts.

## Swap the embedding model

The embedding service accepts any XLMRoberta- or BERT-family model that vLLM's pooling runner supports. Change two things:

```yaml
# docker-compose.yml
vllm-embedding:
  command: >
    BAAI/bge-small-en-v1.5     # or intfloat/multilingual-e5-large, etc.
    ...
    --served-model-name BAAI/bge-small-en-v1.5
```

```js
// patch-config.mjs
const EMBED_MODEL = 'BAAI/bge-small-en-v1.5';
```

The OpenClaw `memorySearch` records the embedding vector dimension when you first index a document. **Changing the model after you've written memories means your existing vectors become unreadable** unless the new model uses the same dim. If you're switching, either:

- Pick a model with the same dimension as bge-m3 (1024-dim) — then the old vectors might still *load* but similarity scores will be nonsense.
- Reindex: stop the gateway, delete `$OPENCLAW_CONFIG_DIR/memory/vectors/` (or whatever the current vector-index path is — check the gateway docs for your version), and re-run. Source memory text stays intact; only the computed vectors are rebuilt.

## Tune for your actual concurrency

The shipped defaults assume ~2 concurrent users on a 128 GB GB10. If that's wrong:

- **Solo user**: `LLM_MAX_NUM_SEQS=1`, optionally raise `LLM_GPU_MEM_UTIL=0.75` if embedding stack is disabled.
- **3–4 users**: Not recommended on GB10 at 256K context. Either drop to a 12B model, or cap `LLM_MAX_MODEL_LEN=131072` (128K) and raise `LLM_MAX_NUM_SEQS=4`. Each user gets stable ~50K.
- **Batch throughput workload** (no humans, script-driven): raise `LLM_MAX_NUM_SEQS=8+`, drop `LLM_MAX_MODEL_LEN` to the shortest prompt size you'll actually hit, and accept longer per-request TTFT.

## Add your own agents

OpenClaw configures agents under `agents.list[]` in `openclaw.json`. The shipped patcher only manages `agents.defaults.*`; it leaves individual agents alone. If you want a second agent deterministically declared (not just created by the onboarding UI), add a step to `patch-config.mjs`:

```js
// (9) Ensure my-custom-agent exists
config.agents ??= {};
config.agents.list ??= [];
const existing = config.agents.list.find((a) => a?.id === 'my-custom-agent');
if (!existing) {
  config.agents.list.push({
    id: 'my-custom-agent',
    name: 'My Custom Agent',
    model: { primary: LLM_MODEL_ID },
    tools: ['search', 'memory'],
    systemPrompt: 'You are ...',
    isolatedSession: true,
  });
  changed = true;
}
```

Deep-merge the same way the existing steps do — never overwrite, always check what's there first.

## Heartbeat and dreaming schedules

Both use the timezone from `OPENCLAW_HEARTBEAT_TZ` in `.env`. To change:

- **Active hours**: edit `OPENCLAW_HEARTBEAT_ACTIVE_START` / `OPENCLAW_HEARTBEAT_ACTIVE_END`. Start > End wraps around midnight.
- **Dreaming time**: edit the `frequency: '0 3 * * *'` cron in `patch-config.mjs` step 6. The shipped default is 03:00 in your configured timezone.

Both take effect on the next `docker compose up`.

## Run without the embedding service

If you don't care about memory search:

1. Comment out the entire `vllm-embedding:` service block in `docker-compose.yml`.
2. Remove `vllm-embedding` from the gateway's `depends_on`.
3. Raise `LLM_GPU_MEM_UTIL=0.85` in `.env` to reclaim the reserved memory.
4. In `patch-config.mjs`, either disable memorySearch (`enabled: false`) or point it at a remote embedding service.

## Run without the OpenClaw UI

If you just want the vLLM endpoints for your own code and don't need OpenClaw:

1. Uncomment the `"127.0.0.1:8004:8004"` and `"127.0.0.1:8005:8005"` bindings in `docker-compose.yml`.
2. `docker compose up -d vllm-llm vllm-embedding`.

Your API is then at `http://127.0.0.1:8004/v1/` (chat) and `http://127.0.0.1:8005/v1/embeddings`, both requiring `Authorization: Bearer $VLLM_API_KEY`.

## Multi-host / scale-out

This stack is a single-host design. If you need a second GB10 as a hot standby or for throughput sharding:

- Run two separate copies of this repo on the two hosts, each with its own `$OPENCLAW_CONFIG_DIR` (agent memory doesn't replicate).
- Put a load balancer (haproxy, nginx) in front of the two `vllm-llm` endpoints. The OpenClaw gateway can then point at the LB via `OPENAI_BASE_URL`.
- Synchronize model weights by sharing `$VLLM_HF_CACHE_DIR` over a fast read-only mount, or by running a local HuggingFace mirror.

This is out of scope for the shipped compose file.
