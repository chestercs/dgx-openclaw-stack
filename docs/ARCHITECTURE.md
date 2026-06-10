# Architecture

This document explains how the nine services in `docker-compose.yml` fit together (ten with the optional `hu` profile active) and **why** each design choice was made.

**Audience.** Operators who want to understand what the stack does before running it; contributors planning to modify the compose file or `patch-config.mjs`; anyone comparing this layout against their own self-hosted LLM setup. The compose file and patcher have decisions baked into them that only make sense with the rationale below — read this before tuning them.

If you only want to bring the stack up, [`SETUP.md`](../SETUP.md) is the shorter path. If you want to swap models or point at a remote backend, [`CUSTOMIZATION.md`](CUSTOMIZATION.md) is the next stop.

---

## Service map

```
                        ┌────────────────────────────────┐
                        │           host                 │
                        │  /opt/dgx-openclaw/            │
                        │   ├── hf-cache/                │── bind
                        │   ├── openclaw-config/         │── bind
                        │   └── workspace/               │── bind
                        └────────────────────────────────┘
                                          │
 ┌────────────────────────────────────────┴─────────────────────────────────┐
 │                 compose default bridge network                           │
 │                                                                           │
 │  ┌─────────────────────┐  ┌─────────────────────┐  ┌──────────────────┐  │
 │  │ vllm-llm            │  │ vllm-embedding      │  │ searxng          │  │
 │  │  8004/tcp (internal)│  │  8005/tcp (internal)│  │ 8080/tcp (internal)│ │
 │  │  vllm-openai:       │  │  vllm-openai:       │  │ searxng/searxng:  │ │
 │  │    gemma4-cu130     │  │    gemma4-cu130     │  │   latest (CPU)    │ │
 │  └──────────▲──────────┘  └──────────▲──────────┘  └──────────▲────────┘ │
 │             │                        │                        │          │
 │             │  vllm-llm:8004/v1      │ vllm-embedding:8005/v1 │searxng:8080
 │             │  (compose DNS)         │ (compose DNS)          │(compose DNS)
 │             │                        │                        │          │
 │  ┌──────────┴────────────────────────┴────────────────────────┴───────┐  │
 │  │ openclaw-gateway           :18789 (published), :18790               │  │
 │  │   depends_on healthy vllm-llm, vllm-embedding                       │  │
 │  │   depends_on success openclaw-config-init                           │  │
 │  └──────────▲──────────────────────────────────────────────────────────┘  │
 │             │ network_mode: service:openclaw-gateway                      │
 │  ┌──────────┴───────────┐         ┌─────────────────────────────┐         │
 │  │ openclaw-cli         │         │ openclaw-config-init         │         │
 │  │ (shared net namespace)│         │ one-shot, exits 0            │         │
 │  └──────────────────────┘         └─────────────────────────────┘         │
 └───────────────────────────────────────────────────────────────────────────┘
```

## Why one compose file

Earlier versions of this stack split the three concerns (LLM, embedding, OpenClaw) into separate compose projects on separate networks. That required the OpenClaw gateway to reach vLLM via `host.docker.internal:8004`, which in turn required `extra_hosts: "host.docker.internal:host-gateway"`.

That broke the day we wanted the `openclaw-cli` container to share the gateway's network namespace (so it would inherit localhost access to the gateway). Docker refuses `extra_hosts` when `network_mode: service:<other>` is used:

```
conflicting options: custom host-to-IP mapping and the network mode
```

Workaround at the time: an entrypoint that wrote the default gateway IP into `/etc/hosts` manually. That worked, but it was architectural duct tape.

**This repo eliminates the class of problem:** all five containers are in the same compose project on the same default bridge network. The gateway reaches the LLM at `http://vllm-llm:8004/v1` by compose DNS. No `host.docker.internal`, no host-gateway extra hosts, no entrypoint /etc/hosts injection. `network_mode: service:openclaw-gateway` on `openclaw-cli` now *just works* because the shared namespace resolves `vllm-llm` and `vllm-embedding` the same way as the gateway does.

## vllm-llm service design

### Image choice: `vllm/vllm-openai:gemma4-cu130`

vLLM's official day-1 Gemma 4 image. CUDA 13.0, Transformers 5.5+, native `gemma4` architecture and `modelopt` NVFP4 kernels. Community images like `scitrera/dgx-spark-vllm:0.17.0-t5` **do not work** — their Transformers version doesn't know the `gemma4` architecture and rejects the config.

### Model selection: MoE default, dense alternative

**Two backends running concurrently:**

- **`vllm-llm`** (port 8004, provider id `vllm` in OpenClaw): `nvidia/Gemma-4-26B-A4B-NVFP4` — Mixture-of-Experts, 25.2B total / 3.8B active per token, 128 fine-grained experts with top-8 routing. Decode measured at **~24.9 tok/s single-stream**, **~112 tok/s 4-parallel aggregate** (~28 tok/s per user) on GB10 with the mandatory Marlin MoE backend (SM121-specific cost — Marlin dequantizes FP4→BF16 on every expert call vs. the CUTLASS path on B200 that ai-muninn measured at 52 tok/s) and CUDA graphs ON.
- **`vllm-llm-dense`** (port 8005, provider id `vllm-dense`): `nvidia/Gemma-4-31B-IT-NVFP4` — dense 31B, ~6.9 tok/s decode on GB10. Tuned for low-RAM single-user (`LLM_MAX_NUM_SEQS_DENSE=1`, `LLM_GPU_MEM_UTIL_DENSE=0.30`, **128K context** — the dense weights occupy ~31 GB in unified memory due to heterogeneous head dim 256/512, so the 0.30 budget leaves ~7 GB for KV at 128K. Bump util to 0.40 + `LLM_MAX_MODEL_LEN_DENSE=262144` for the full 256K architecture max).

Both reachable simultaneously, no profile-mutex. The user picks the model in the OpenClaw UI dropdown; the patcher registers separate `vllm` + `vllm-dense` providers in `models.providers`. Combined RAM footprint at default settings: ~38 GB MoE + ~38 GB dense + ~10 GB embedding/everything-else ≈ 86 GB / 128 GB on the GB10, leaves ~42 GB free.

Both share the same `vllm/vllm-openai:gemma4-cu130` base image (extended with our 1-line tool-call-parser regex patch in `./vllm-llm/Dockerfile`), the same chat template (`tool_chat_template_gemma4.jinja`), the same parsers (`gemma4` for both reasoning and tool-call), and the same FP8 KV cache. The model swap is genuinely a `--model` change plus the MoE-specific `--moe-backend` flag.

### Quantization: `--quantization modelopt`

Activates the NVIDIA Model Optimizer NVFP4 GEMM kernels. Both `nvidia/Gemma-4-26B-A4B-NVFP4` and `nvidia/Gemma-4-31B-IT-NVFP4` are `modelopt`-format checkpoints (the MoE one quantized via the `nvfp4_experts_only` recipe) — vLLM will error out if you pass `--quantization fp8` or leave it blank.

### MoE routing: `--moe-backend marlin` (Blackwell SM121)

GB10's `sm_121` rejects vLLM's default CUTLASS MoE backend on Gemma 4's fused 3D expert tensor format — CUTLASS produces NaN scale factors and corrupted output. Marlin decompresses FP4 weights to BF16 at inference time; slightly slower per expert but functionally correct on every Blackwell variant. Override with `LLM_MOE_BACKEND=cutlass` in `.env` only if you're on H100/B200 where the upstream CUTLASS path works.

The dense `vllm-llm-dense` service does NOT pass `--moe-backend` — the flag is MoE-specific; setting it on a dense run errors out.

### Memory budget: `--gpu-memory-utilization 0.50` (MoE) / `0.68` (dense)

The GB10 has 128 GB of unified memory. vLLM's util fraction is computed against `nvidia-smi`-reported "free memory" at process start. The MoE working set is small enough that **0.50** (~64 GB cap) is the safe default — it covers the 16.5 GB model plus 2 × 256K FP8 KV (~6.6 GB) with margin for prefix-caching and prefill buffers. The dense profile keeps the historical **0.68** default via the separate `LLM_GPU_MEM_UTIL_DENSE` env var on the `vllm-llm-dense` service.

Leaving about **64 GB free** under the MoE default lets the embedding service, ComfyUI, and the OS share the rest comfortably. Raise to `0.85` (the verified throughput-ceiling tuning point) only if the embedding service is parked and you want to maximise prefix-caching depth.

### Concurrency: `--max-num-seqs 2`

Practical stable context bands on the MoE backend at `0.50` util:

- 1 active user alone: full 256K tokens, no preemption (~3.3 GB FP8 KV).
- 2 active users simultaneously: 256K each, no preemption (~6.6 GB FP8 KV total — well inside the budget).
- The constraint at higher concurrency is throughput, not memory: at 4 simultaneously decoding users you split ~52 tok/s across them.

On the dense backend, the historical bands still apply: ~220K (1 user), ~110K each (2 users). Preemption in vLLM is **suspend/resume**, not a crash — you see higher TTFT on long prompts, not errors. So pick `max-num-seqs` based on your expected concurrency, not a worst-case margin.

### KV cache: `--kv-cache-dtype fp8`

Halves KV cache memory. Continuous batching then stretches dynamically across more concurrent sequences / longer contexts. Accuracy impact is negligible for chat use.

### Tool calling: the template matters

Without `--chat-template /templates/tool_chat_template_gemma4.jinja`, the model generates raw `call:name{args}` text in the `content` field instead of populating the OpenAI `tool_calls` array. OpenClaw would see a regular text response and never dispatch a tool.

We ship the template in `templates/` because the `gemma4-cu130` image doesn't bundle it. Source: [vLLM repo](https://github.com/vllm-project/vllm/blob/main/examples/tool_chat_template_gemma4.jinja).

### Multimodal: `--limit-mm-per-prompt` and `--mm-processor-kwargs`

Gemma 4 NVFP4 ships with the vision tower — NVIDIA quantized the whole model, not just the LM. We pass JSON (not `key=value`) because vLLM 0.11+ rejects the legacy syntax:

```yaml
--limit-mm-per-prompt '{"image":4,"audio":0}'
--mm-processor-kwargs '{"max_soft_tokens":280}'
```

`max_soft_tokens: 280` is the sweet spot for moodboard / document reading (≈ 512×512 region). Bump to 560 or 1120 for OCR, charts, or handwriting; every additional soft token eats KV cache.

### CUDA graphs (`LLM_ENFORCE_EAGER`)

CUDA graphs are ON by default — `LLM_ENFORCE_EAGER` env unset omits the `--enforce-eager` flag. Verified 2026-05-08 on GB10 + Marlin SM121 + MoE 26B-A4B: ~10% single-stream gain (22.5 → 24.9 tok/s) and ~30% 4-paralel-aggregate gain (86 → 112 tok/s) over eager mode, no hangs across a 5K-200K context sweep. The pre-2026 "hard-to-reproduce hangs during long-context decode" gotcha appears resolved in vLLM 0.19+. Set `LLM_ENFORCE_EAGER=1` in `.env` to force eager mode if a future vLLM image / model combo turns the graphs unstable on your hardware.

## vllm-embedding service design

### Model: BAAI/bge-m3

- 567M params, BF16 safetensors, ~1.1 GB on disk.
- 100+ languages, 8K context.
- Native XLMRoberta architecture (vLLM `--runner pooling` supports it directly).
- Dense output (1024-dim) + sparse + multi-vector — OpenClaw's `memorySearch` uses the dense vector.

### Why bge-m3 and not a bigger embedding model

On MIRACL / Mr.TyDi, bge-m3 lands within 1–2 points of Qwen3-Embedding-8B despite being 1/14 the size. For most RAG workloads (especially multilingual agent memory), the smaller model is a clear win:

- Saves ~5–8 GB unified memory for the LLM's KV cache.
- Runs in pure BF16; no quantization surprises.
- Cross-lingual performance is strong (EN↔HU cosine ≈ 0.88 in production validation).

### `--runner pooling`

Replaces the deprecated `--task embed`. Tells vLLM this is an encoder-only pooling model, not an autoregressive LM. Pooling runner doesn't allocate decode-style KV slots, which is why `EMBED_GPU_MEM_UTIL=0.03` is enough.

## searxng service design

### Role

Self-hosted meta-search backend for OpenClaw's native `webSearch` provider (`docs.openclaw.ai/tools/searxng-search`). The OpenClaw gateway ships a bundled `searxng` plugin that calls a SearxNG instance over HTTP JSON; pointing it at a local instance keeps search queries out of commercial search APIs (no Tavily / Brave Search API / Serper keys needed).

### Privacy posture

SearxNG only hides the *client* from upstream engines — the query text itself still reaches whichever engines you enable. The real privacy win comes from the strict engine whitelist in `searxng/settings/settings.yml`:

- **Privacy-respecting general engines**: DuckDuckGo, Brave Search, Mojeek, Qwant, Startpage.
- **Public knowledge / domain engines**: Wikipedia family, Reddit, GitHub, arXiv.
- **NOT in the whitelist**: Google, Bing, Yandex, Yahoo, Baidu — these are filtered out of the SearxNG registry entirely via `use_default_settings.engines.keep_only`, so there's no way they can run even if a per-query override requests them.

If you're OK with Google/Bing coverage in exchange for a weaker privacy story, add those engines to `keep_only` in `settings.yml` and restart SearxNG. See [`CUSTOMIZATION.md`](CUSTOMIZATION.md) for the details.

### Gotcha: `keep_only` + `disabled: true`

A subtle SearxNG quirk: `keep_only` only filters the engine *registry* — it doesn't override the per-engine `disabled: true` flags shipped in the upstream defaults. A few engines (Reddit, Wikibooks, Wikiquote, Wikisource) are shipped disabled-by-default upstream for stability / quality reasons, so the settings file also contains explicit `engines: - name: X, disabled: false` overrides for the ones we want live.

### JSON API

OpenClaw's plugin hits `/search?q=...&format=json`. JSON output is **off by default** in upstream SearxNG (`search.formats: [html]` only). The settings file adds `json` to `formats` — if you comment that line out, the gateway's `webSearch` tool will get HTML back and parsing will fail.

### No published port

Binding to `0.0.0.0:8080` inside the container is fine because no host port is published; only sibling containers on the compose bridge can reach `http://searxng:8080`. Uncomment the `"127.0.0.1:8888:8080"` binding in `docker-compose.yml` for host-side debug.

### Footprint

CPU-only (no GPU request), ~50-100 MB RAM idle, short bursts while a query fans out to its upstream engines. Negligible next to the LLM's footprint.

## openclaw-config-init (idempotent patcher)

### Why it exists

The OpenClaw onboarding wizard gets you ~80% of the way to a working config, but:

- It writes a 12-char placeholder as `vllm.apiKey` regardless of your real key.
- It registers `google/gemma-4-31B-it` (the BF16 canonical id) even when you pick the NVFP4 variant, so tool calls get routed to a model the backend doesn't serve.
- It leaves `memorySearch` disabled.
- It leaves `gateway.trustedProxies` empty.
- Its LLM idle watchdog is 120s — too tight for 31B + reasoning + vision + multi-tool chains.

Rather than tell users "also click here, there, and there after onboarding", the patcher enforces the known-good state on every `docker compose up`. Idempotent deep-merge: if the file is already correct, it exits in no-op.

### 23 steps

1. Remove legacy `models.providers.vllm.capabilities` (old schema).
2. Ensure `vllm.baseUrl`, `vllm.api`, `vllm.apiKey` from env.
3. Ensure NVFP4 model entry in the provider catalog.
4. Ensure `memorySearch` → bge-m3 via `http://vllm-embedding:8005/v1/`.
5. Ensure heartbeat (30m, reasoning, isolated session, configurable active hours).
6. Ensure/cleanup dreaming (env-gated: `OPENCLAW_ENABLE_DREAMING`).
7. Ensure `gateway.trustedProxies` (loopback + `172.16.0.0/12` + optional LAN CIDR).
8. Ensure `agents.defaults.llm.idleTimeoutSeconds = 300`.
9. Ensure `memorySearch.query.hybrid` (BM25 + vector + MMR re-rank on bge-m3).
10. Ensure `tools.web.search.provider = searxng` + `plugins.entries.searxng.enabled = true` (the bundled SearxNG plugin ships default-disabled).
11. Ensure TTS wiring (env-gated: `OPENCLAW_TTS_ROUTER_API_KEY`). Three sub-writes:
    a. Top-level `messages.tts.{enabled, auto, mode}` — without these the OpenClaw voice surfaces silently treat TTS as off even with the provider correctly wired.
    b. `messages.tts.providers.openai` (baseUrl, apiKey, model, voiceId) pointing at the bundled router.
    c. `messages.tts.voiceAliases` (`english`, `narrator`, `male`, `female`, `magyar`, `hungarian`).
12. Mirror `gateway.auth.token` into `gateway.remote.token` so the loopback CLI WS-connect doesn't hit a token mismatch and silently fall back to the embedded runner (a side-car path, not the production agent route).
13. Sync the per-agent `auth-profiles.json` `vllm:default.key` with `VLLM_API_KEY`. The agent runner reads the credential from this per-agent store, not from `models.providers.vllm.apiKey` — drift after a `.env` rotation produces HTTP 401 from vLLM even when the config-file apiKey is correct.
14. Ensure `tools.media.audio` wires the Whisper STT backend (env-gated: `STT_API_TOKEN`). Upserts an entry into `tools.media.audio.models[]` with `provider: "openai"`, the configured model id, `baseUrl: http://openclaw-stt-whisper:8000/v1/`, and `headers.Authorization: Bearer $STT_API_TOKEN` so the Whisper Bearer stays isolated from any global `models.providers.openai.apiKey` (upsert-by-baseUrl preserves user-added entries like a Deepgram fallback). Feeds voice-note upload, Discord voice-channel transcription, VoiceCall CLI, Talk / Voicewake nodes. The Control UI realtime mic button is a separate path — it uses the browser's Web Speech API (`speech.ts`) and does NOT go through this pipeline.
15. Ensure `browser.enabled = true` and write one `browser.profiles.<name>.cdpUrl` per registered Chromium profile in `openclaw-browser` (env-gated: `BROWSER_API_TOKEN`). Default profile gets port `BROWSER_PORT_BASE` (9222); each name in `BROWSER_PROFILE_NAMES` (comma-separated, populated by `./bootstrap-browser-login.sh`) gets the next port in sequence — port-per-profile because OpenClaw does not forward `?profile=<name>` query params on cdpUrl attaches (issues #4841 / #9723 / #11926). Auth is `?token=<BROWSER_API_TOKEN>` in the URL — OpenClaw's cdpUrl field accepts query tokens or Basic URL auth only, not Authorization headers. Loopback host bind plus weekly rotation are the mitigations against query-string token leakage.
16. Idempotently inject a soft browser-policy block into the workspace `AGENTS.md` (HTML-comment-marked region, deep-merge-style append-once). Tells the agent: default profile for throwaway browsing, opt in to credentialed profiles only when the task requires that identity, never persist via the default profile. Soft layer — prompt-injection can override; the hard layer would be a separate `bot-ops` agent.
17. Idempotently inject a `browser.act` cheatsheet block into the same `AGENTS.md`. Smaller open models (Gemma 4 in particular) routinely emit the flat `{element, text}` shape on `kind="fill"` actions that need the nested `{fields: [{ref, type, value}]}` shape; the cheatsheet shows the right shape next to a labelled wrong shape plus a one-line recovery hint.
18. Wire `mcp.servers.python_sandbox` at the `openclaw-python-sandbox` service (env-gated: `PYTHON_SANDBOX_API_TOKEN`). Streamable-HTTP transport, 10-second connect timeout, Bearer auth via `headers.Authorization`. When the token is unset, the entry is *removed* from `openclaw.json` (and empty parent objects cleaned up) so the gateway doesn't try to dial a parked service. Schema verified against `docs.openclaw.ai/cli/mcp` on 2026-04-26.
19. Wire `mcp.servers.comfyui_image` at the `openclaw-image-comfyui` bridge (env-gated: `IMAGE_GEN_API_TOKEN`). Same shape as step 18 (Streamable-HTTP, 10-second connect timeout, Bearer auth via `headers.Authorization`). The bridge runs in a *separate compose file* (`openclaw-image-comfyui/docker-compose.yml`, opt-in via `--profile image-gen`) and joins this stack's bridge via an `external: true` network reference, so bridge DNS resolves `openclaw-image-comfyui:9095` once both composes are up. When the token is unset the entry is *removed* (with parent cleanup) so the gateway doesn't try to dial a parked bridge.
20. Discord `channels.discord.ackReactionScope = "off"` defends against openclaw issue #46024 (stale reaction-event queue replays emoji ack-reactions on session resume — bot rapidly cycles 👀🤔👍🔥 without agent awareness). Only writes when `channels.discord` is configured AND the user hasn't set the field themselves. Env override: `OPENCLAW_DISCORD_ACK_REACTION_SCOPE` (default `"off"`).
21. Discord `channels.discord.actions.reactions = true` enables `discord:add_reaction` for agents. Default `true` because the bundled vllm-llm image carries a 1-line patch to the gemma4 parser regex (`vllm-llm/Dockerfile` + `patch_parser.py`) so colon namespaces like `discord:add_reaction` are accepted. Env override: `OPENCLAW_DISCORD_ACTIONS_REACTIONS=false` to disable.
22. Discord-routed agent `tools.alsoAllow += ["group:messaging"]`. Walks `agents.routes[]` for entries where `match.channel === "discord"`, finds the corresponding agent in `agents.list[]`, and ensures `tools.alsoAllow` contains the configured groups. Without this, the Discord-routed agent inherits the default `tools.profile: "coding"` which excludes `group:messaging` (the `message` tool used for reactions, replies, etc.). Env override: `OPENCLAW_DISCORD_AGENT_ALSO_ALLOW` (comma-separated, default `"group:messaging"`); empty disables the step.
23. Ensure `${OPENCLAW_CONFIG_DIR}/canvas` exists for image-gen Path A inline rendering. The bridge mirrors generated PNGs there and emits `[embed url="/__openclaw__/canvas/<file>"]` shortcodes the gateway serves under `/__openclaw__/canvas/`. Idempotent mkdir, not env-gated (an empty dir is harmless when Path A is off). Doesn't flip the `changed` flag — it's a sibling filesystem prep, not an `openclaw.json` mutation.
24. Discord progressive streaming — `channels.discord.streaming` (default `"partial"`) plus optional `draftChunk` sub-knobs (`minChars`, `maxChars`, `breakPreference`) and a `streaming.preview.toolProgress` opt-out. With Gemma 4 NVFP4 at ~6 tok/s, the upstream default `"off"` produces ~80s of channel silence on long replies; `"partial"` edit-in-place keeps the cadence at ~5.5s/edit, comfortably under the Discord 5-edits/5s rate limit on a single bot account. Env knobs: `OPENCLAW_DISCORD_STREAMING`, `OPENCLAW_DISCORD_DRAFTCHUNK_MIN_CHARS` / `_MAX_CHARS` / `_BREAK_PREFERENCE`, `OPENCLAW_DISCORD_STREAMING_PREVIEW_TOOL_PROGRESS`. Same user-managed protection as steps 20-22.
25. Discord-routed agent `tools.profile = "full"`. Walks the same top-level `bindings[]` as step 22, writes `tools.profile` only when undefined (operator-set values preserved). Without this the agent inherits the global `coding` default, which excludes `browser` / `tts` / `canvas`. Env override: `OPENCLAW_DISCORD_AGENT_TOOLS_PROFILE` (enum `minimal | coding | messaging | full`, default `full`); empty disables the step.
26. Workspace-discord `AGENTS.md` patcher-managed blocks. Appends two idempotent blocks to `/home/node/.openclaw/workspace-discord/AGENTS.md` — `<!-- patch-config:cron-tools:* -->` (canonical one-shot + recurring `cron` tool shapes) and `<!-- patch-config:browser-tools:* -->` (mirror of step 17's body for the discord-friend's separate workspace). Skips cleanly if the file doesn't exist (pre-onboarding state).

See inline comments in `patch-config.mjs` for the detail on each step.

## openclaw-gateway

Standard OpenClaw gateway. The two things worth noting:

- `OPENAI_BASE_URL: http://vllm-llm:8004/v1` — the gateway's built-in OpenAI client uses this for tool synthesis and supplementary calls; the actual agent routing is via the vllm provider entry in `openclaw.json`.
- `depends_on` uses `condition: service_healthy` for both vLLM services. The gateway won't start until both have passed their healthchecks — avoids a race where the gateway times out its first LLM call because vllm-llm is still loading weights.

## openclaw-cli

`network_mode: service:openclaw-gateway` shares the gateway's network namespace. The CLI reaches the gateway on `127.0.0.1:18789` and the two vLLM services on their compose DNS names. No `/etc/hosts` hack required — the shared namespace resolves the same DNS entries the gateway sees.

The entrypoint is just `sleep infinity`. The container exists to keep a stable Node.js environment hot so `docker exec openclaw-cli openclaw <cmd>` starts in ~5s cold (Node module loading baseline), instead of spinning up a fresh container every time.

## TTS subsystem (1 service)

### Layout

```
                           ┌─────────────────────────────────────┐
   gateway.openai TTS ────▶│ openclaw-tts-fish                    │
   provider.baseUrl        │  127.0.0.1:8091 (loopback publish)  │
   = http://openclaw-      │                                      │
     tts-fish:8080/v1      │  ┌────────────────────────────────┐ │
                           │  │ FastAPI shim (:8080)            │ │
                           │  │  - Bearer auth (TTS_API_TOKEN)  │ │
                           │  │  - voice → references mapping   │ │
                           │  │  - onset silence pad (in-proc)  │ │
                           │  └──────────────┬─────────────────┘ │
                           │                 │ loopback :9090     │
                           │  ┌──────────────▼─────────────────┐ │
                           │  │ SGLang-Omni native HTTP server  │ │
                           │  │  python -m sglang_omni.cli.cli  │ │
                           │  │  serve fishaudio/s2-pro          │ │
                           │  └──────────────┬─────────────────┘ │
                           │                 │                    │
                           │   /app/voices/<name>.{wav,txt}      │
                           │   (mounted volume tts-fish-voices)  │
                           └─────────────────────────────────────┘
```

One container, two processes. The FastAPI shim does three jobs: Bearer auth
(SGLang-Omni ships no auth), voice→references mapping (SGLang-Omni accepts
`references[].audio_path`, not the OpenAI `voice` string — so the shim
resolves `voice: "default_en"` to `references: [{audio_path: "/app/voices/
default_en.wav", text: <transcript>}]`), and optional onset silence pad
(prepended in-process via soundfile + numpy — defends against the Whisper
STT first-phoneme clip observed in the F5-TTS-era benchmark).

### Why one service replaces the legacy 3-service stack

Previously, the TTS surface was three services (Kokoro EN + F5-TTS HU + an
OpenAI-compat router with diacritic autoroute and ffmpeg transcoding). Fish
Audio S2 Pro is multilingual (80+ languages from one checkpoint, EN + HU
both supported) and supports reference-audio voice cloning, so one service
covers what previously required three. The legacy reference doc at
[`docs/reference/tts-stack.md`](reference/tts-stack.md) is preserved with a
SUPERSEDED banner for historical context.

### Why SGLang-Omni instead of fish-speech `tools/api_server.py`

`fishaudio/s2-pro` is a Qwen3-omni architecture (5B params, ~11 GB weights)
that requires the `sgl-project/sglang-omni` inference engine to load. The
legacy `fishaudio/fish-speech` repo's `tools/api_server.py` targets the
older 1.x LLaMA2-based architecture and does NOT load the s2-pro checkpoint.
The upstream reference Docker image (`frankleeeee/sglang-omni:dev`) is
amd64-only, so we build a custom image on `nvidia/cuda:13.0.0-cudnn-devel-
ubuntu24.04` and let SGLang-Omni compile `sgl-kernel` from source against
the cu130 torch wheels. First build is long (~15-30 min including model
download); subsequent builds hit the layer cache.

### Voice cloning workflow

Reference audio lives at `/app/voices/<name>.{wav,txt}` (mounted volume
`tts-fish-voices`). The shim resolves the OpenAI `voice` field to the file
pair at request time — no restart required to add a voice:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}
docker cp myclone.wav ${PROJ}openclaw-tts-fish:/app/voices/
docker cp myclone.txt ${PROJ}openclaw-tts-fish:/app/voices/
```

Default voice library (7 voices, bundled in `openclaw-tts-fish/server/voices/`
and baked into `/app/voices_seed/`; the shim copies them into `/app/voices/`
on first start without overwriting user voices): `default_en`, `bella`,
`nicole`, `michael`, `fenrir`, `emma` — Kokoro 82M syntheses (Apache-2.0
generated audio) covering US/UK male/female timbres — plus `default_hu`
(Diana Majlinger / "Egri csillagok", LibriVox public domain). Patcher step 11
maps friendly aliases (`english`/`female`/`male`/`british`/`deep`/`soft`/
`magyar`/`hungarian`) onto the raw ids. Provenance table in
[`openclaw-tts-fish/README.md`](../openclaw-tts-fish/README.md).

### License

Fish Audio S2 Pro weights are distributed under the **Fish Audio Research
License — non-commercial use only**. Building the `openclaw-tts-fish` image
pulls the ~11 GB checkpoint from `fishaudio/s2-pro` on HuggingFace and
constitutes acceptance of the upstream license. Wrapper code in
`openclaw-tts-fish/server/` is MIT. For commercial deployments, contact
`business@fish.audio` or swap `FISH_REPO` in the Dockerfile to a
checkpoint with a commercial license.

### Port publishing posture

`${TTS_FISH_BIND:-127.0.0.1}:${TTS_FISH_PORT:-8091}:8080` — loopback by
default, consistent with the STT service. `curl 127.0.0.1:8091/healthz`
works without `docker exec` gymnastics. Set `TTS_FISH_BIND=0.0.0.0` in
`.env` to expose on the LAN (Bearer-protected via `TTS_API_TOKEN`).

### Web chat UI limitation

The OpenClaw web chat UI is hard-wired to the browser's native
`speechSynthesis` API — it does NOT call the configured
`messages.tts.providers.openai`. Voice surfaces that go through the
gateway's TTS pipeline (Discord channel, agent `tts` skill) DO use this
service. This is an upstream OpenClaw limitation.

## STT subsystem (1 service)

### Layout

```
      ┌────────────────────────────────────────────────┐
      │ OpenClaw gateway                                │
      │   tools.media.audio.models[0]                   │
      │     provider: "openai"                          │
      │     baseUrl:  http://openclaw-stt-whisper:8080/v1/ │
      │     model:    deepdml/faster-whisper-large-v3-turbo-ct2 │
      │     headers:  Authorization: Bearer $STT_API_TOKEN │
      └─────────────────────┬──────────────────────────┘
                            │ (bridge DNS, POST multipart)
                            ▼
         ┌─────────────────────────────────────┐
         │ openclaw-stt-whisper                 │
         │  127.0.0.1:8093 (loopback publish)  │
         │  self-built: CUDA 13 + faster-whisper│
         │  Whisper turbo CT2 (MIT) @ float16  │
         │  ~1.6 GB VRAM, autodetect EN + HU   │
         └─────────────────────────────────────┘
```

Single service. Built from `./openclaw-stt-whisper/server/` on a CUDA 13 base with a ~150 LOC FastAPI wrapper around `faster-whisper`. Exposes OpenAI-compatible `/v1/audio/transcriptions`, `/v1/audio/translations`, `/v1/models`, `/health` endpoints. Whisper autodetects the input language per request, so no bilingual router is needed (contrast with TTS, which needs one backend per language).

The original 2026-04-23 plan pointed at `ghcr.io/speaches-ai/speaches` upstream (zero custom code), but its latest published CUDA tag (12.6.3) rejects every low-precision CT2 compute type on Blackwell sm_120 and destabilizes on `float32`. The CUDA 13 + cu130 PyTorch wheel pattern that `vllm-llm` and `openclaw-tts-fish` already use on GB10 is the proven path. The wrapper retires trivially when speaches upstream publishes a Blackwell-tensor-core image (swap `build:` back to `image:` in `docker-compose.yml`).

### Three voice surfaces, one backend

The STT service backs two of OpenClaw's three voice-input paths:

1. **Control UI realtime mic button** (chat composer mic icon) — browser-native Web Speech API (`speech.ts`). Does NOT use this service. Language support depends on the browser + OS.
2. **Voice-note attachment** — drop a wav/mp3/m4a/opus into the chat composer. OpenClaw's `tools.media.audio` pipeline picks the first matching `models[]` entry and POSTs the file. Transcript replaces the message body (wrapped in `[Audio]`); slash commands inside the transcript still fire.
3. **Voicewake / Talk / VoiceCall nodes + Discord voice-channel** — node pipelines (`docs.openclaw.ai/nodes/{talk,voicewake}`, `cli/voicecall`) use the same `tools.media.audio` configuration.

Paths 2 and 3 converge on the single `tools.media.audio.models[]` entry written by patcher step 14.

### Why Whisper turbo CT2 as default

`deepdml/faster-whisper-large-v3-turbo-ct2` is a pre-converted CT2 build of
the turbo Whisper variant (4-layer pruned decoder, ~8× faster than vanilla
large-v3 at near-equal WER on EN). MIT weights, ~1.6 GB VRAM at float16,
multilingual including EN + HU autodetect. Picked as the default because
voice-chat latency (Discord voice channel: Fish Audio S2 Pro → LLM → STT
roundtrip) matters more than the last percentage point of HU WER. For
accuracy-first Hungarian workloads on noisy mic input, swap to
`Trendency/whisper-large-v3-hu` via `STT_WHISPER_MODEL` (slower, full
32-layer decoder, ~3 GB VRAM, ~3pp lower HU WER on phone-grade audio).
See `docs/reference/stt-stack.md` for the full comparison matrix.

### Auth isolation via per-entry `headers`

The OpenClaw audio schema resolves provider auth through the standard chain — `models.providers.openai.apiKey` or env vars or auth profiles. If we wrote the Whisper Bearer into `models.providers.openai.apiKey`, it would collide with any cloud OpenAI account the user also configures. Per-entry `headers.Authorization: Bearer <token>` is explicitly supported by `tools.media.audio.models[]` (`docs.openclaw.ai/nodes/audio`) and keeps the STT token orthogonal to the global openai apiKey.

### Port publishing posture

`${STT_WHISPER_BIND:-127.0.0.1}:${STT_WHISPER_PORT:-8093}:8080` — loopback by default, consistent with the TTS services. `curl 127.0.0.1:8093/health` works without `docker exec` gymnastics. Set `STT_WHISPER_BIND=0.0.0.0` in `.env` to expose on the LAN (Bearer-protected via `STT_API_TOKEN`).

## Volumes

- `hf-cache` (named volume, bound to `$VLLM_HF_CACHE_DIR`) is shared by both vLLM services so the bge-m3 weights live next to the Gemma 4 weights in the same HF cache structure. Both services get `volumes: - hf-cache:/root/.cache/huggingface`. The Docker volume label is `${VLLM_HF_CACHE_VOLUME_NAME:-dgx-openclaw-hf-cache}` — change this if a sibling LLM stack on the same host bind-mounts the same `VLLM_HF_CACHE_DIR` and you want one consistent label in `docker volume ls`.
- `$OPENCLAW_CONFIG_DIR` is bind-mounted into the config-init, gateway, and cli services — they all read/write the same `openclaw.json`, memory/, heartbeat journal.
- `$OPENCLAW_WORKSPACE_DIR` is bind-mounted into the gateway and cli services — the agent's writable working directory.
- `tts-fish-hf-cache`, `tts-fish-voices` — Docker named volumes (no host bind). `tts-fish-hf-cache` holds runtime HF downloads from SGLang-Omni; `tts-fish-voices` is the user-overridable `/app/voices/` reference-voice directory for Fish Audio S2 Pro voice cloning.
- `stt-whisper-hf-cache` — Docker named volume for the faster-whisper CT2 weights (~1.6 GB turbo CT2 by default). Survives `docker compose down` so the next boot doesn't re-download. No host bind.
- `browser-storage` — Docker named volume for `openclaw-browser`'s per-profile Chromium user-data-dirs. Cookies + localStorage + IndexedDB persist across container restarts so a 1x manual login holds for the upstream session lifetime (~14d GitHub, ~30d Notion, etc.). Treat backups as secret-equivalent — the contents include live session tokens.
- `browser-diagnostics` — Docker named volume for failure screenshots + HAR captures from `openclaw-browser`. Diagnostic-only; safe to delete.

## Browser automation subsystem (1 service, opt-in)

```
   ┌─────────────────────────────────────────────────────┐
   │ openclaw-gateway                                    │
   │   browser tool (built-in)                           │
   │     reads browser.profiles.<name>.cdpUrl from       │
   │     openclaw.json (written by patch-config step 15) │
   └────────────────────┬────────────────────────────────┘
                        │ Playwright connectOverCDP, port-per-profile
                        ▼
   ┌─────────────────────────────────────────────────────┐
   │ openclaw-browser  (profiles: ["browser"])           │
   │                                                     │
   │   FastAPI mgmt  (:9220) ─ session lifecycle, login  │
   │                              helper, /v1/extract    │
   │                                                     │
   │   Chromium cluster (port-per-profile)               │
   │     :9222 default      (anonymous throwaway)        │
   │     :9223 github-user1 (--user-data-dir=/storage/…) │
   │     :9224 notion-personal                           │
   │       …  up to 9241 (20 profiles default)           │
   │                                                     │
   │   noVNC bridge (:5901, transient during onboarding) │
   │     Xvfb :99 + x11vnc + websockify + headful        │
   │     Chromium for the operator's 1x OAuth flow       │
   └─────────────────────────────────────────────────────┘
```

Self-hosted Playwright Chromium that OpenClaw's built-in `browser` tool
attaches to over Chrome DevTools Protocol. We do not implement any of the
browser-control surface ourselves — navigate, click, fill, type, evaluate,
snapshot, screenshot, cookies, storage all live in the gateway.

### Why CDP-attach (not MCP, not a bespoke HTTP tool)

At v0.7.0 design time (2026-04-25) OpenClaw had no MCP slot in its config
schema, so MCP would have meant a custom plugin maintained against every
gateway release. A bespoke HTTP tool adapter would re-implement the full
Playwright control surface that OpenClaw already gives us. CDP-attach via
`browser.profiles.<name>.cdpUrl` was documented and supported; the only
custom code we own is the supervisor, the login helper, and a small
markdown extractor. Native MCP client support landed shortly after via
`mcp.servers.<name>` (transports: stdio / SSE-HTTP / Streamable-HTTP), so
net new tools default to MCP — but the browser stack stays on CDP-attach
because port-per-profile + query-string token routing already works.
Full rationale in `docs/reference/browser-automation.md`.

### Why port-per-profile

OpenClaw does NOT pass `?profile=<name>` query params from cdpUrl through
to Playwright's `connectOverCDP` call (issues #4841 / #9723 / #11926).
Each profile must resolve to a distinct cdpUrl. Solution: each Chromium
binds its own port. Patcher step 15 enumerates ports deterministically:
default = `BROWSER_PORT_BASE` (9222), then named profiles in
`BROWSER_PROFILE_NAMES` order get the next ports.

### Auth posture

`?token=<BROWSER_API_TOKEN>` in the URL — that's the only auth surface
OpenClaw's cdpUrl field accepts. Mitigations against query-string leakage:
- `BROWSER_BIND` defaults to 127.0.0.1 (loopback host bind).
- Sibling containers reach Chromium via the docker bridge (LAN cannot pivot).
- `rotate-secrets.sh` covers `BROWSER_API_TOKEN` in `--all`.

If you need LAN exposure of CDP, do not relax the bind without a
reverse-proxy layer that strips the query token and adds proper Bearer
headers. An unauthenticated remote-debugging-port has been the root
cause of multiple Chromium credential-theft CVEs.

### 1x OAuth onboarding flow

`./bootstrap-browser-login.sh <profile-name>` POSTs to the FastAPI app's
`/v1/sessions/<n>/login-helper`, which spins up Xvfb + x11vnc +
websockify + a headful Chromium for the named profile and returns a
noVNC URL. The operator opens it in their laptop browser, drives the
auth flow (password + TOTP — passkeys don't work over noVNC by W3C
origin-bound spec), hits Enter. The service flushes Chromium (cookies
persist), tears down the VNC chain, and re-launches Chromium headless
on the same `--user-data-dir`. The script then appends the profile name
to `BROWSER_PROFILE_NAMES` and runs `openclaw-config-init` so patcher
step 15 writes the new `browser.profiles.<n>.cdpUrl` entry.

## Python sandbox subsystem (1 service, opt-in)

`openclaw-python-sandbox` is the v0.8.0+ opt-in `--profile python`
service that gives the agent a `python_exec` tool over MCP — load a
CSV, run pandas, return a chart, persist state across calls.

### Why MCP, not the native `code_execution` tool

OpenClaw exposes a native `code_execution` tool, but it routes to xAI's
Responses API (cloud, paid, requires `XAI_API_KEY`). This stack is
self-hosted by design. We could also lean on `agents.defaults.sandbox`
+ the generic `exec` tool, but that knob is gateway-wide and one-shot
per call (no session persistence, every other tool's behavior changes
too). MCP gives us a Python-specific, scoped, opt-in surface.

OpenClaw added native MCP client support shortly after v0.7.0 — config
path `mcp.servers.<name>`, transports stdio / SSE-HTTP /
Streamable-HTTP. We use Streamable-HTTP at
`http://openclaw-python-sandbox:8094/mcp` with a Bearer header.

### Layout

One container, one uvicorn process:

- **FastAPI `/mcp` endpoint** — JSON-RPC 2.0 over POST,
  Streamable-HTTP transport, Bearer auth. Hand-rolled (~250 LOC) so
  the protocol shape isn't pinned to the churning `mcp` Python SDK.
- **`KernelPool`** — `jupyter_client.MultiKernelManager` wrapping
  one ipykernel child per `session_id`. Lazy spawn, idle reaper
  (default 30 min TTL on a 5 min sweep), per-session async lock so
  concurrent calls on the same kernel serialize.
- **`/workspace` bind** — `${OPENCLAW_WORKSPACE_DIR}/sandbox/` on
  the host, mounted rw. The agent's canonical place to read/write
  files; survives container restarts.

The kernel pool runs in-process via `jupyter_client` rather than
fronting a separate Jupyter Kernel Gateway subprocess — saves a HTTP
hop per call and removes second-process lifecycle coordination.

### Patcher wiring (step 18)

When `PYTHON_SANDBOX_API_TOKEN` is set, `patch-config.mjs` step 18
writes:

```json
"mcp": {
  "servers": {
    "python_sandbox": {
      "transport": "streamable-http",
      "url": "http://openclaw-python-sandbox:8094/mcp",
      "connectionTimeoutMs": 10000,
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

When the token is unset, the entry is *removed* (and empty parent
objects cleaned up) so the gateway doesn't try to dial a parked
service. The bootstrap-prompt + Compose profile + patcher step
together form the standard three-lever opt-in triad used elsewhere
in the repo.

### Threat model

Trusted-prompt only. Container namespaces + non-root user (UID 1000)
+ `cap_drop: [ALL]` + `no-new-privileges:true` protect the host
filesystem; Python introspection or a kernel-exploit chain could in
principle escape. We don't ship gVisor / firecracker — that's the
upgrade path for multi-tenant deployments. The default
`PYTHON_SANDBOX_NETWORK=none` env var is a documented placeholder
only; today egress is implicitly limited but not enforced. See
`docs/reference/python-sandbox.md` for the full threat model and
`docs/CUSTOMIZATION.md` for the hard-egress hardening recipe.

### Port publishing posture

`8094` publishes to `127.0.0.1` only by default
(`PYTHON_SANDBOX_BIND`); the gateway uses bridge DNS so the host
publish is operator ergonomics for `curl` smoke tests. Resource caps:
`PYTHON_SANDBOX_MEMORY_MB=8192`, `PYTHON_SANDBOX_CPUS=4` — both are
docker-engine-enforced hard limits, OOM-killed kernels return on the
next call as a fresh kernel.

## Image-gen bridge subsystem (1 service, opt-in, separate compose)

`openclaw-image-comfyui` is the v0.9.0+ opt-in `--profile image-gen`
bridge that exposes `comfyui_image__*` tools to the agent and proxies
to the operator's existing ComfyUI install. Lives in **its own compose
file** (`openclaw-image-comfyui/docker-compose.yml`) — every other
service in this stack is in the main `docker-compose.yml`, and this is
the first deliberate exception. Rationale: image generation is a
satellite feature, the operator likely already runs ComfyUI for
unrelated reasons, and the bridge ships no model weights or GPU
workload of its own.

### Layout

```
   ┌──────────────────────────────────────────────┐
   │ openclaw-gateway                             │
   │   mcp.servers.comfyui_image (step 19)        │
   │     transport: streamable-http               │
   │     url: http://openclaw-image-comfyui:9095/mcp │
   │     headers.Authorization: Bearer …          │
   └────────────────────┬─────────────────────────┘
                        │ bridge DNS over the main stack network
                        ▼
   ┌──────────────────────────────────────────────┐
   │ openclaw-image-comfyui (separate compose)    │
   │   FastAPI MCP wire (~250 LOC)                │
   │   workflow loader (class_type + targets)     │
   │   asyncio.Lock (single-flight default)       │
   │   no GPU, no torch, no model weights         │
   └────────────────────┬─────────────────────────┘
                        │ HTTP via host-gateway
                        │ host.docker.internal:13036
                        ▼
   ┌──────────────────────────────────────────────┐
   │ YOUR existing ComfyUI install                │
   │   (separate compose project, e.g. petyus-gpt) │
   │   /prompt /history /view /queue /interrupt   │
   └──────────────────────────────────────────────┘
```

### Cross-compose join

The bridge attaches to the main stack's bridge network via
`external: true`. The named-network reference defaults to
`${COMPOSE_PROJECT_NAME:-dgx-openclaw}_default`. Implication: the
main stack must be `up` at least once before this bridge can start
(otherwise the network doesn't exist yet). Documented in the bridge's
README and in the bootstrap-output line that appears after a
successful prompt 3e.

### Why not run ComfyUI in this compose

Two reasons:

1. The operator already runs one. Duplicating ComfyUI inside the main
   stack would put two ComfyUI processes on the same GB10 GPU,
   competing for VRAM and pre-empting each other.
2. License isolation. The bridge ships no model weights — operators
   pick checkpoints (FLUX Dev / Schnell, SDXL fine-tunes, Pony XL,
   Illustrious, RealVisXL, …) under whichever upstream license they
   accept; this stack stays content-agnostic. Same posture as Fish
   Audio S2 Pro's Research-License opt-in.

### Concurrency: single-flight by default

`IMAGE_GEN_MAX_CONCURRENCY=1` (`asyncio.Semaphore(1)`). ComfyUI runs
on the same GB10 GPU as vLLM; concurrent generation pauses LLM token
generation. Set to `0` for pass-through only if your ComfyUI lives on
a different GPU. Higher integers (`2`, `3`, …) increase bridge
parallelism but the GPU still bottlenecks at one render at a time.

### Workflow templates

Two reference templates ship under `server/workflows/`:
`flux-schnell.json` (4-step distilled) and `sdxl-base.json` (25-step
generic). Each declares `_metadata.targets` mapping bridge parameter
names (`prompt`, `negative`, `checkpoint`, `width`, `height`, …) to
node ids and `inputs.*` keys. Both ship with the
`"REPLACE_ME.safetensors"` placeholder; the bridge refuses to
generate with the placeholder, forcing the operator to either pass
`checkpoint=` per call or edit the JSON once. See
`docs/reference/image-comfyui-bridge.md` for the design rationale and
`openclaw-image-comfyui/server/workflows/README.md` for the authoring
guide.

### Threat model

The bridge is content- and model-agnostic. Bearer auth on `POST /mcp`
(`IMAGE_GEN_API_TOKEN`); rotation requires a cross-compose
force-recreate (`./rotate-secrets.sh IMAGE_GEN_API_TOKEN` prints both
commands). The bridge → ComfyUI hop is unauth'd by ComfyUI default —
mitigation is keeping ComfyUI's port loopback-only on the host. The
bridge container is `cap_drop: [ALL]`, `no-new-privileges`,
`mem_limit: 1024m`, non-root `1000:1000`. See
`docs/reference/image-comfyui-bridge.md` for the full threat model.

### Port publishing posture

`9095` publishes to `127.0.0.1` only by default (`IMAGE_GEN_BIND`);
the gateway reaches it via bridge DNS so the host publish is operator
ergonomics for `curl` smoke tests. Set `IMAGE_GEN_BIND=0.0.0.0` only
behind a header-auth reverse proxy.

## Networking, trust, and exposure

- **Gateway**: `18789` (and `18790` control) are published to the host. Put a TLS-terminating reverse proxy in front for public access over `wss://`.
- **vLLM ports stay compose-internal.** For host-side debug, uncomment the `"127.0.0.1:8004:8004"` / `"127.0.0.1:8005:8005"` bindings in `docker-compose.yml`. Binding to `127.0.0.1` keeps them off your LAN.
- **TTS ports publish to loopback by default** (`127.0.0.1:8090–8092`) — see the TTS subsystem section above. Set `TTS_*_BIND=0.0.0.0` in `.env` to expose on the LAN.
- **STT port (`openclaw-stt-whisper`) publishes to `127.0.0.1:8093`** by default. Set `STT_WHISPER_BIND=0.0.0.0` to expose on the LAN (Bearer-protected via `STT_API_TOKEN`).
- **Browser ports (`openclaw-browser`) publish to `127.0.0.1:9220` (management API) + `127.0.0.1:9222-9241` (Chromium debug ports) + `127.0.0.1:5901` (noVNC during onboarding only)** by default — opt-in via `--profile browser`. Set `BROWSER_BIND=0.0.0.0` only with a header-auth reverse proxy in front; raw CDP ports on the LAN are a credential-theft vector. See `docs/reference/browser-automation.md`.
- **Python sandbox port (`openclaw-python-sandbox`) publishes to `127.0.0.1:8094`** by default — opt-in via `--profile python`. Bearer-protected via `PYTHON_SANDBOX_API_TOKEN`. Set `PYTHON_SANDBOX_BIND=0.0.0.0` only behind a header-auth reverse proxy. See `docs/reference/python-sandbox.md`.
- **Image-gen bridge port (`openclaw-image-comfyui`) publishes to `127.0.0.1:9095`** by default — opt-in via `--profile image-gen`, lives in a separate compose file. Bearer-protected via `IMAGE_GEN_API_TOKEN`. The bridge → ComfyUI hop is unauth'd (ComfyUI default); keep ComfyUI's port loopback-only on the host. See `docs/reference/image-comfyui-bridge.md`.
- `gateway.trustedProxies` includes `172.16.0.0/12` so any reverse proxy on the same docker bridge (or on host-network with bridge-adjacent IPs) is trusted. If you access the gateway **directly** from your LAN (bypassing the proxy), add your LAN CIDR via `OPENCLAW_LAN_CIDR`.
- The Chrome extension expects `wss://` (secure) for public use. Put a TLS-terminating reverse proxy in front and set `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1` so the gateway accepts the plain `ws://` hop from the proxy on your private network.
