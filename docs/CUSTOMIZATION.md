# Customization

How to swap things out without breaking the stack.

---

## Swap the LLM

The three coupled pieces are:

1. The `--model` flag on `vllm-llm` in `docker-compose.yml`.
2. The `LLM_MODEL_ID` constant in `patch-config.mjs`.
3. The `LLM_MODEL_ENTRY` metadata in `patch-config.mjs` (context window, input modalities, reasoning flag).

Any model change requires editing all three. **Why three places?** vLLM needs to know which weights to load (#1); the patcher needs to know which model id to register in the OpenClaw provider catalog so tool-calling routes to the right entry (#2); and OpenClaw uses the metadata in the catalog entry to cap prompt sizes, gate vision input, and drive the thinking-mode UI (#3). They aren't auto-derived from each other — consistency is your job. We hard-code these rather than templating them because a mistake here (a wrong context window, a missing `image` input type) is silent: the stack boots, chats work, and tool calls or images break in subtle ways hours later.

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

## Tune SearxNG (web search)

SearxNG has three knobs that matter for this stack. All live in `searxng/settings/settings.yml` or `patch-config.mjs` (for the plugin-side wiring).

### Which engines run

`searxng/settings/settings.yml` → `use_default_settings.engines.keep_only`. Whatever is in this list stays in the registry; every other engine in the upstream defaults is dropped. The shipped list is the strict privacy posture:

```yaml
keep_only:
  - duckduckgo
  - brave
  - mojeek
  - qwant
  - startpage
  - wikipedia
  - wikidata
  - wikibooks
  - wikiquote
  - wikisource
  - reddit
  - github
  - arxiv
```

Add `google`, `bing`, `yandex` etc. here if you want broader coverage — know that those engines will then see your query text. Any engine shipped `disabled: true` in the upstream defaults needs an explicit override in the `engines:` block:

```yaml
engines:
  - name: google
    disabled: false
```

(Reddit, Wikibooks, Wikiquote, Wikisource are already enabled this way in the shipped file.)

### Categories and language passed to SearxNG

`patch-config.mjs` step 10 writes the defaults the OpenClaw plugin sends on every call:

```js
const desiredWebSearch = {
  baseUrl: 'http://searxng:8080',
  categories: 'general,news,science',
  language: '',
};
```

Per-query overrides (different categories, a `time_range`, an explicit language) are the agent's job via the tool-call parameters. The defaults above just set the floor.

### Disabling web search entirely

If you don't want the SearxNG service at all:

1. Comment out the entire `searxng:` service block in `docker-compose.yml`.
2. Remove `SEARXNG_SECRET` from `.env.example` (optional; leftover env var is harmless).
3. In `patch-config.mjs` step 10, flip `config.plugins.entries.searxng.enabled = false` (or delete the step). Otherwise the gateway will retry failed `http://searxng:8080` calls on every `web_search` tool invocation and log errors.

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

## Run with a remote vLLM backend

Use case: you want the gateway / agent runtime / SearxNG / memory store on machine A (cheap, no GPU — your laptop, a VPS, a small home server) but the heavy LLM and/or embedding model on machine B (a remote DGX, an x86 box with a 4090, vLLM on RunPod, a cloud OpenAI-compatible endpoint, OpenRouter, an AWS Bedrock proxy, …).

The repo natively supports this via three env overrides — no docker-compose surgery beyond parking the local GPU services.

### Step-by-step

1. **Park the local vLLM services** so `docker compose up` doesn't try to start them on machine A. Edit `docker-compose.yml`:

    ```yaml
    vllm-llm:
      profiles: ["never"]    # add this one line
      image: vllm/vllm-openai:gemma4-cu130
      # …rest unchanged…

    vllm-embedding:
      profiles: ["never"]    # add this one line
      image: vllm/vllm-openai:gemma4-cu130
      # …rest unchanged…
    ```

    Also remove the two `vllm-llm` / `vllm-embedding` `service_healthy` entries from `openclaw-gateway.depends_on` (otherwise the gateway waits forever for services that never start).

2. **Point the three URL overrides at the remote endpoints** in `.env`:

    ```dotenv
    # Gateway → LLM (chat completions) — what OpenClaw sends user messages to
    OPENAI_BASE_URL=https://your-remote-vllm.example.com/v1

    # patch-config.mjs → openclaw.json provider field — must match OPENAI_BASE_URL,
    # but trailing slash is required by the OpenClaw config schema
    LLM_BASE_URL=https://your-remote-vllm.example.com/v1/

    # patch-config.mjs → openclaw.json memorySearch.remote.baseUrl — bge-m3 endpoint
    # (can be the same host or a different one; trailing slash required)
    EMBED_BASE_URL=https://your-remote-embed.example.com/v1/
    ```

    `VLLM_API_KEY` stays as your remote vLLM's auth token (the patcher writes it into both the chat provider apiKey and the embedding `remote.apiKey`).

3. **Run bootstrap.sh + first `docker compose up -d`**. The gateway will crash-loop with `Missing config. Run openclaw setup …` because there's no `openclaw.json` yet — that's expected on a fresh install.

4. **Run onboarding** to create `openclaw.json`. You can use the interactive Chrome extension wizard, or the non-interactive CLI (replace `${PROJ}` with the value of `CONTAINER_NAME_PREFIX` from `.env`, default `dgx-`):

    ```bash
    docker exec ${PROJ}openclaw-cli openclaw onboard \
      --non-interactive --accept-risk \
      --mode local --flow manual \
      --auth-choice vllm \
      --custom-base-url "$OPENAI_BASE_URL" \
      --custom-api-key "$VLLM_API_KEY" \
      --custom-model-id nvidia/Gemma-4-31B-IT-NVFP4 \
      --custom-compatibility openai \
      --gateway-bind lan --gateway-port 18789 \
      --gateway-auth token \
      --gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN \
      --skip-daemon --skip-search --skip-skills --skip-channels --skip-ui --skip-health
    ```

5. **Re-apply the patcher** so the 11 deterministic-state steps run on the freshly-created `openclaw.json` (the first `up` skipped them because the file didn't exist yet):

    ```bash
    docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
    ```

6. **Verify**: `curl http://127.0.0.1:18789/healthz` returns `{"ok":true}`, `docker exec ${PROJ}openclaw-cli openclaw memory status` shows `Embeddings: ready`, and `docker exec ${PROJ}openclaw-cli openclaw agent --agent main --message "Use web_search to find …"` produces a real reply.

### What still runs locally

- `openclaw-gateway` (TypeScript app, ~200 MB RAM)
- `openclaw-cli` (sleep container, shares gateway namespace)
- `openclaw-config-init` (init-only, exits after patching)
- `searxng` (~50–100 MB RAM, CPU-only)

Total local footprint: well under 1 GB, no GPU. Suitable for a small VPS, a Raspberry-Pi-class box, or a personal laptop that's not always-on the same network as the LLM host.

### Trade-offs

- **Latency**: every chat token round-trips to the remote endpoint. LAN is fine; cross-region cloud calls add 50–200 ms TTFT.
- **Auth**: `VLLM_API_KEY` rides on every request to the remote. Use TLS to the remote endpoint (or a private network — Tailscale / WireGuard / cloud VPC peering — over plaintext).
- **Network reachability**: the remote URL must resolve and be reachable from inside the `openclaw-gateway` container. LAN IPs work because docker bridge networks NAT outbound. Public hostnames work if DNS in the container resolves them.

## Run without the OpenClaw UI

If you just want the vLLM endpoints for your own code and don't need OpenClaw:

1. Uncomment the `"127.0.0.1:8004:8004"` and `"127.0.0.1:8005:8005"` bindings in `docker-compose.yml`.
2. `docker compose up -d vllm-llm vllm-embedding`.

Your API is then at `http://127.0.0.1:8004/v1/` (chat) and `http://127.0.0.1:8005/v1/embeddings`, both requiring `Authorization: Bearer $VLLM_API_KEY`.

## Container naming (CONTAINER_NAME_PREFIX)

Every service uses `container_name: ${CONTAINER_NAME_PREFIX:-dgx-}<service>`. Default `dgx-` keeps the familiar `dgx-openclaw-gateway`, `dgx-vllm-llm`, … shape — useful for namespacing on a host that may grow other compose stacks.

If this is the only stack on the host and you want clean names like `openclaw-gateway`, `vllm-llm`, set:

```dotenv
CONTAINER_NAME_PREFIX=
```

**Bridge DNS reachability is unaffected** — services resolve each other by the compose service name plus the explicit `hostname:` directive (`vllm-llm`, `searxng`, `openclaw-gateway`, …) regardless of how the container shows up in `docker ps`. Sibling compose stacks pointed at the same network can keep using `vllm-llm:8004` no matter what prefix you pick.

## Sharing the HF cache with sibling LLM stacks

If you run other vLLM compose stacks on the same host (an alternate quantization for A/B testing, a different embedding model, an experimental fine-tune), you can share the model cache between them. Two knobs:

```dotenv
# Same host path everywhere → bind-mount points into the same directory
VLLM_HF_CACHE_DIR=/opt/dgx-openclaw/hf-cache

# Same Docker volume label → one consistent entry in `docker volume ls`
VLLM_HF_CACHE_VOLUME_NAME=dgx-openclaw-hf-cache
```

Set both in every sibling stack's `.env`. The bind-mount makes them physically share the cache (no duplicate `~16 GB` Gemma download); the matching volume name is cosmetic but keeps `docker volume ls` clean.

## TTS port exposure

The three TTS services publish to `${TTS_*_BIND:-127.0.0.1}:${TTS_*_PORT:-…}` — loopback by default so `curl 127.0.0.1:8092/healthz` works without `docker exec` while keeping LAN clients out. To expose on the LAN:

```dotenv
TTS_ROUTER_BIND=0.0.0.0    # exposes openclaw-tts-router on LAN
TTS_EN_BIND=0.0.0.0        # exposes openclaw-tts-en (Kokoro EN backend)
TTS_F5HUN_BIND=0.0.0.0     # exposes openclaw-tts-f5hun (HU backend, profile=hu only)
```

All three are Bearer-token-protected via the existing TTS tokens (`OPENCLAW_TTS_ROUTER_API_KEY`, `TTS_API_TOKEN`, `F5HUN_API_TOKEN`), but a leaked token is still a leaked token — keep loopback unless you have a reason to expose. Sibling containers continue to use bridge DNS regardless of the binding.

## Disable TTS entirely

Two-step opt-out:

1. In `.env`, leave `OPENCLAW_TTS_ROUTER_API_KEY` empty. The patcher's step 11 detects this and skips cleanly — `messages.tts.providers.openai` stays untouched.
2. Park the two default TTS services with `profiles: ["never"]` in `docker-compose.yml` so `docker compose up -d` doesn't start them:

    ```yaml
    openclaw-tts-en:
      profiles: ["never"]    # add this one line
      # …rest unchanged…

    openclaw-tts-router:
      profiles: ["never"]    # add this one line
      # …rest unchanged…
    ```

The HU service is opt-in (`profiles: ["hu"]`) so it's already off by default — no extra step needed.

## Swap the Whisper STT model

The default `Systran/faster-whisper-large-v3` (MIT) is the accuracy-first choice — FLEURS Hungarian WER 14.1% and ~3 GB VRAM at float16. Two alternates are worth knowing:

- **Turbo (speed):** `STT_WHISPER_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2`. 8× faster inference at ~1.6 GB VRAM. ~equal English WER, but Hungarian WER is NOT independently published — run your own HU samples through both before flipping in production. Especially noticeable on short utterances where the large-v3 prefill cost dominates end-to-end latency.
- **VRAM-tight (compute_type):** `STT_WHISPER_COMPUTE_TYPE=int8_float16` on either model. Halves VRAM to ~1.5 GB at a 5-10% WER increase. Safe fallback when the LLM + TTS squeeze the GPU budget, or if a Blackwell sm_120 numerical-stability issue turns up.

Either change is a one-line `.env` edit plus `docker compose up -d --force-recreate openclaw-stt-whisper`. The patcher leaves the existing `tools.media.audio.models[]` entry intact and only rewrites `baseUrl`/`headers` when they drift.

## Disable STT entirely

Two-step opt-out, mirrors the TTS path:

1. In `.env`, leave `STT_API_TOKEN` empty. The patcher's step 14 detects this and skips cleanly — `tools.media.audio` stays untouched.
2. Park the `openclaw-stt-whisper` service with `profiles: ["never"]` in `docker-compose.yml`:

    ```yaml
    openclaw-stt-whisper:
      profiles: ["never"]    # add this one line
      # …rest unchanged…
    ```

Note: opt-out affects only the voice-note / Discord voice / VoiceCall pipelines. The Control UI realtime mic button uses the browser's Web Speech API independently and stays functional regardless of this service — that is an OpenClaw design choice, not a consequence of the wiring here.

## Remote STT backend

Same pattern as "Run with a remote vLLM backend" above. On a GPU-less host pointing at a remote or cloud Whisper endpoint:

1. Park the local service: add `profiles: ["never"]` to `openclaw-stt-whisper`.
2. Point the patcher at the remote: `OPENCLAW_STT_BASE_URL=https://your-whisper.example.com/v1/` in `.env`.
3. Set `STT_API_TOKEN` to whatever Bearer the remote accepts.

The gateway will POST voice notes and voice-channel audio to the remote endpoint. Bridge DNS isn't involved — the URL resolves outside the compose network via the host's DNS.

## Upgrading the OpenClaw gateway

All three OpenClaw containers (`openclaw-config-init`, `openclaw-gateway`, `openclaw-cli`) pull `ghcr.io/openclaw/openclaw:${OPENCLAW_IMAGE_TAG:-latest}`. The `:latest` tag moves on every OpenClaw release, so `docker compose pull` can silently bring in a new gateway with schema changes, renamed CLI subcommands, or new channel features. The runbook below makes upgrades deliberate and reversible.

### Before upgrading

1. **Record the current digest** so you can roll back:

    ```bash
    docker image inspect ghcr.io/openclaw/openclaw:latest --format '{{index .RepoDigests 0}}'
    # e.g.  ghcr.io/openclaw/openclaw@sha256:9d5f1dfbd5deedc37706c78f745b958ffbe9b4f20840cfb4d49c617a50326902
    ```

    Save the digest — it's the rollback target if the new version misbehaves.

2. **Back up the config + state**:

    ```bash
    BACKUP=~/openclaw-backup-$(date +%Y%m%d-%H%M%S).tar.gz
    tar -czf "$BACKUP" -C "$(grep ^OPENCLAW_CONFIG_DIR .env | cut -d= -f2)" .
    ls -la "$BACKUP"
    ```

    The tarball includes `openclaw.json`, the memory SQLite vector store under `workspace/`, per-agent `auth-profiles.json`, and the heartbeat journal. If anything goes wrong, `tar -xzf "$BACKUP" -C <dir>` restores it.

### The upgrade

1. **Pull the new image**:

    ```bash
    docker compose pull openclaw-config-init openclaw-gateway openclaw-cli
    ```

2. **Force-recreate the trio** — the `openclaw-config-init` runs the patcher against the new schema, and the gateway + CLI share a network namespace, so they must recreate together:

    ```bash
    docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
    ```

3. **Watch the patcher log** — every `[patch-config]` line should either land a known step or report no-op. If the patcher crashes (schema conflict), stop and roll back:

    ```bash
    docker logs openclaw-config-init 2>&1 | grep -E '\[patch-config\]|Error'
    ```

4. **Smoke test**:

    ```bash
    # Gateway healthz
    curl -sS http://127.0.0.1:18789/healthz

    # Memory status
    docker exec openclaw-cli openclaw memory status --deep

    # Agent turn
    docker exec openclaw-cli openclaw agent --agent main \
      --message "Reply with UPGRADE_OK if you can see this." --thinking off --timeout 60

    # STT round-trip (voice-note pipeline smoke)
    docker exec openclaw-cli openclaw channels list    # ensure channels still registered
    ```

    If all four return the expected output, the upgrade is good.

### Rolling back

If the patcher crashes, the agent turn fails, or any live test regresses, pin the previous digest:

```bash
echo "OPENCLAW_IMAGE_TAG=sha256:<your-recorded-digest>" >> .env
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

If the rollback itself struggles (rare, when the config-init has already mutated `openclaw.json` to a new-schema shape), restore from the pre-upgrade tarball:

```bash
docker compose stop openclaw-gateway openclaw-cli openclaw-config-init
rm -rf "$(grep ^OPENCLAW_CONFIG_DIR .env | cut -d= -f2)"/*
tar -xzf "$BACKUP" -C "$(grep ^OPENCLAW_CONFIG_DIR .env | cut -d= -f2)"
docker compose up -d openclaw-config-init openclaw-gateway openclaw-cli
```

### Cadence

OpenClaw ships a new gateway tag roughly every 1-2 weeks. A sensible rhythm: pull + test on your dev machine first (or a short-lived sibling compose project with a different `CONTAINER_NAME_PREFIX`), then on GB10 once you've verified. Don't upgrade mid-project when a significant workload is running — the cold start while the config-init re-patches can take 30-60 s.

## Rotating secrets

Use `./rotate-secrets.sh` to overwrite the auto-generated secrets in `.env` with fresh random values. Sibling of `bootstrap.sh`, same helper style. Handles the three common rotation scenarios:

- **Routine hygiene / post-suspected-leak**: rotate one or more keys, recreate the affected services, verify.
- **Fresh install without the bootstrap prompt dance**: `cp .env.example .env && ./rotate-secrets.sh --all` fills every placeholder in one shot.
- **Selective, e.g. just the TTS surface**: `./rotate-secrets.sh TTS_API_TOKEN OPENCLAW_TTS_ROUTER_API_KEY`.

```bash
./rotate-secrets.sh --help          # full flag list + default set
./rotate-secrets.sh -n --all        # dry-run: fingerprints + recreate command, no write
./rotate-secrets.sh -y --all        # non-interactive (CI); rotate the default set
./rotate-secrets.sh VLLM_API_KEY    # rotate just this key
```

The default set (`--all`): `VLLM_API_KEY`, `SEARXNG_SECRET`, `OPENCLAW_TTS_ROUTER_API_KEY`, `TTS_API_TOKEN`, `STT_API_TOKEN`, plus `F5HUN_API_TOKEN` only if it is already non-empty (empty = HU TTS opted out of the CC-BY-NC model; `--all` respects that). `OPENCLAW_GATEWAY_TOKEN` is opt-in via `--include-gateway-token` — post-onboarding the real gateway auth lives in `openclaw.json`'s `gateway.auth.token` (picked by the onboarding wizard), so rotating the env var alone is a near no-op. `HUGGING_FACE_HUB_TOKEN` is out of scope (user-owned; can't be generated).

Before every change the script writes a timestamped `.env.backup-YYYYMMDD-HHMMSS` (mode 600), does an atomic write (temp file + `mv`), and runs `docker compose config --quiet` post-write. If the config validation fails, the backup is restored automatically. The script does NOT restart services — it prints the exact `docker compose up -d --force-recreate <services>` command for the services that read each rotated key, and you pick the moment (in-flight agent requests). HU rotations auto-append `--profile hu`.

The 3-store credential layout `openclaw.json` ↔ per-agent `auth-profiles.json` ↔ `.env` is kept in sync on the next `up` by `patch-config.mjs` steps 2 / 4 / 11 / 13, so the recreate command is all you need after rotation. See `docs/reference/openclaw-internals.md` → "v0.4.x credential layout" for the full invariant.

## Multi-host / scale-out

This stack is a single-host design. If you need a second GB10 as a hot standby or for throughput sharding:

- Run two separate copies of this repo on the two hosts, each with its own `$OPENCLAW_CONFIG_DIR` (agent memory doesn't replicate).
- Put a load balancer (haproxy, nginx) in front of the two `vllm-llm` endpoints. The OpenClaw gateway can then point at the LB via `OPENAI_BASE_URL`.
- Synchronize model weights by sharing `$VLLM_HF_CACHE_DIR` over a fast read-only mount, or by running a local HuggingFace mirror.

This is out of scope for the shipped compose file.
