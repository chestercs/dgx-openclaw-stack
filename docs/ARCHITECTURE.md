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

### Quantization: `--quantization modelopt`

Activates the NVIDIA Model Optimizer NVFP4 GEMM kernels. `nvidia/Gemma-4-31B-IT-NVFP4` is a `modelopt`-format checkpoint — vLLM will error out if you pass `--quantization fp8` or leave it blank.

### Memory budget: `--gpu-memory-utilization 0.68`

The GB10 has 128 GB of unified memory. vLLM's util fraction is computed against `nvidia-smi`-reported "free memory" at process start, so the final footprint is:

```
vllm_footprint ≈ 0.68 * gpu_free_at_start + ~14 GB fixed overhead
              ≈ 96 GB on an otherwise idle GB10
```

Leaving about **9 GB for the embedding service** (`~8 GB vLLM runtime + ~1.1 GB bge-m3 weights`) and **~15 GB of true host headroom** for the kernel, Docker, logging, and any other workload you might run on the box. `0.85` is the "embedding-free" setting and gives you ~86 GB KV cache — but the embedding service's `docker compose up` will OOM-fail in that state.

### Concurrency: `--max-num-seqs 2`

Practical stable context bands at `0.68` util:

- 1 active user alone: up to ~220K tokens before preemption (256K theoretical max reachable occasionally).
- 2 active users simultaneously: up to ~110K tokens each before preemption.

Preemption in vLLM is **suspend/resume**, not a crash. You see higher TTFT on long prompts, not errors. So pick `max-num-seqs` based on your expected concurrency, not a worst-case margin.

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

### `--enforce-eager`

CUDA graphs disabled. On GB10's unified memory, CUDA graphs have caused hard-to-reproduce hangs during long-context decode. Eager mode costs a few percent of throughput but is stable. Feel free to flip this off on your own hardware if you don't see issues.

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

## TTS subsystem (3 services)

### Layout

```
                           ┌──────────────────────────────┐
   gateway.openai TTS ────▶│ openclaw-tts-router          │
   provider.baseUrl        │  127.0.0.1:8092 (loopback)   │
   = http://openclaw-      │  ~150 LOC FastAPI + ffmpeg   │
     tts-router:8080/v1    └──┬───────────────────────┬───┘
                              │                       │
            (always wired) ◀──┘                       └──▶ (only when HU profile + token)
              │                                               │
   ┌──────────▼──────────┐                       ┌────────────▼────────────┐
   │ openclaw-tts-en     │                       │ openclaw-tts-f5hun       │
   │  127.0.0.1:8091     │                       │  127.0.0.1:8090          │
   │  Kokoro 82M (EN)    │                       │  F5-TTS HU (CC-BY-NC)   │
   │  Apache 2.0         │                       │  profiles: ["hu"]        │
   └─────────────────────┘                       └─────────────────────────┘
```

Three loosely-coupled FastAPI services. `openclaw-tts-router` is the OpenAI-compatible seam that the OpenClaw gateway hits via the `messages.tts.providers.openai.baseUrl` override (sanctioned per closed upstream issues #13907 / #29224). The router fronts the EN backend (mandatory) and optionally the HU backend (opt-in via `--profile hu` + `F5HUN_API_TOKEN` + `F5HUN_URL`).

### Why a router instead of direct provider wiring

The OpenClaw `openai` TTS provider accepts exactly **one** `baseUrl`. To support multiple language backends behind one logical provider, we need a fronting service. The router is ~150 lines of FastAPI + httpx, no GPU, and its second job is transcoding the backend's wav into mp3/opus/aac on the fly via bundled ffmpeg — necessary because the OpenClaw openai TTS provider asks for mp3 by default and content-type sniffing on the voice surfaces is finicky.

### Hungarian autodetect

When the HU backend is wired AND the gateway sends one of the OpenAI default voices (`alloy`, `coral`, `shimmer`, …) AND the input contains Hungarian diacritics (`áéíóöőúüű`), the router silently re-routes the request to the HU backend so the agent doesn't need to know HU voice ids to get correct pronunciation. No-op when the HU profile is not active.

### Port publishing posture

All three TTS services publish to `${TTS_*_BIND:-127.0.0.1}:${TTS_*_PORT:-…}` — loopback by default. This differs from the vLLM services (which don't publish at all) for one reason: TTS services are commonly debugged with `curl <port>/healthz` from the host, and a loopback bind covers that without exposing them on the LAN. To bind on the LAN, set `TTS_*_BIND=0.0.0.0` in `.env` (Bearer-token-protected via the existing `TTS_API_TOKEN` / `F5HUN_API_TOKEN` / `OPENCLAW_TTS_ROUTER_API_KEY`).

### Web chat UI limitation

The OpenClaw web chat UI is hard-wired to the browser's native `speechSynthesis` API — it does NOT call the configured `messages.tts.providers.openai`. Voice surfaces that go through the gateway's TTS pipeline (Discord channel, agent `tts` skill) DO use this router. This is an upstream OpenClaw limitation.

## STT subsystem (1 service)

### Layout

```
      ┌────────────────────────────────────────────────┐
      │ OpenClaw gateway                                │
      │   tools.media.audio.models[0]                   │
      │     provider: "openai"                          │
      │     baseUrl:  http://openclaw-stt-whisper:8080/v1/ │
      │     model:    Systran/faster-whisper-large-v3   │
      │     headers:  Authorization: Bearer $STT_API_TOKEN │
      └─────────────────────┬──────────────────────────┘
                            │ (bridge DNS, POST multipart)
                            ▼
         ┌─────────────────────────────────────┐
         │ openclaw-stt-whisper                 │
         │  127.0.0.1:8093 (loopback publish)  │
         │  self-built: CUDA 13 + faster-whisper│
         │  Whisper large-v3 (MIT) @ float16   │
         │  ~3 GB VRAM, autodetect EN + HU     │
         └─────────────────────────────────────┘
```

Single service. Built from `./openclaw-stt-whisper/server/` on a CUDA 13 base with a ~150 LOC FastAPI wrapper around `faster-whisper`. Exposes OpenAI-compatible `/v1/audio/transcriptions`, `/v1/audio/translations`, `/v1/models`, `/health` endpoints. Whisper autodetects the input language per request, so no bilingual router is needed (contrast with TTS, which needs one backend per language).

The original 2026-04-23 plan pointed at `ghcr.io/speaches-ai/speaches` upstream (zero custom code), but its latest published CUDA tag (12.6.3) rejects every low-precision CT2 compute type on Blackwell sm_120 and destabilizes on `float32`. The CUDA 13 + cu130 PyTorch wheel pattern that `vllm-llm` and `openclaw-tts-en` already use on GB10 is the proven path. The wrapper retires trivially when speaches upstream publishes a Blackwell-tensor-core image (swap `build:` back to `image:` in `docker-compose.yml`).

### Three voice surfaces, one backend

The STT service backs two of OpenClaw's three voice-input paths:

1. **Control UI realtime mic button** (chat composer mic icon) — browser-native Web Speech API (`speech.ts`). Does NOT use this service. Language support depends on the browser + OS.
2. **Voice-note attachment** — drop a wav/mp3/m4a/opus into the chat composer. OpenClaw's `tools.media.audio` pipeline picks the first matching `models[]` entry and POSTs the file. Transcript replaces the message body (wrapped in `[Audio]`); slash commands inside the transcript still fire.
3. **Voicewake / Talk / VoiceCall nodes + Discord voice-channel** — node pipelines (`docs.openclaw.ai/nodes/{talk,voicewake}`, `cli/voicecall`) use the same `tools.media.audio` configuration.

Paths 2 and 3 converge on the single `tools.media.audio.models[]` entry written by patcher step 14.

### Why Whisper large-v3 as default

FLEURS Hungarian WER 14.1% (best validated number among the OpenAI-compatible candidates as of 2026-04). MIT weights + MIT upstream server → no opt-in profile gate needed, ships in the default profile. The alternatives evaluated: NVIDIA Parakeet/Canary (no OpenAI-compat server → requires wrapper code we'd have to maintain), Microsoft Phi-4 Multimodal (Hungarian audio explicitly unsupported), Distil-Whisper (English-only). See `docs/reference/stt-stack.md` for the full comparison matrix.

### Auth isolation via per-entry `headers`

The OpenClaw audio schema resolves provider auth through the standard chain — `models.providers.openai.apiKey` or env vars or auth profiles. If we wrote the Whisper Bearer into `models.providers.openai.apiKey`, it would collide with any cloud OpenAI account the user also configures. Per-entry `headers.Authorization: Bearer <token>` is explicitly supported by `tools.media.audio.models[]` (`docs.openclaw.ai/nodes/audio`) and keeps the STT token orthogonal to the global openai apiKey.

### Port publishing posture

`${STT_WHISPER_BIND:-127.0.0.1}:${STT_WHISPER_PORT:-8093}:8080` — loopback by default, consistent with the TTS services. `curl 127.0.0.1:8093/health` works without `docker exec` gymnastics. Set `STT_WHISPER_BIND=0.0.0.0` in `.env` to expose on the LAN (Bearer-protected via `STT_API_TOKEN`).

## Volumes

- `hf-cache` (named volume, bound to `$VLLM_HF_CACHE_DIR`) is shared by both vLLM services so the bge-m3 weights live next to the Gemma 4 weights in the same HF cache structure. Both services get `volumes: - hf-cache:/root/.cache/huggingface`. The Docker volume label is `${VLLM_HF_CACHE_VOLUME_NAME:-dgx-openclaw-hf-cache}` — change this if a sibling LLM stack on the same host bind-mounts the same `VLLM_HF_CACHE_DIR` and you want one consistent label in `docker volume ls`.
- `$OPENCLAW_CONFIG_DIR` is bind-mounted into the config-init, gateway, and cli services — they all read/write the same `openclaw.json`, memory/, heartbeat journal.
- `$OPENCLAW_WORKSPACE_DIR` is bind-mounted into the gateway and cli services — the agent's writable working directory.
- `tts-en-hf-cache`, `tts-f5hun-hf-cache`, `tts-f5hun-voices` — Docker named volumes (no host bind). Hold runtime HF downloads + user-supplied reference voices for the F5-TTS HU service.
- `stt-whisper-hf-cache` — Docker named volume for the faster-whisper CT2 weights (~3 GB large-v3 by default). Survives `docker compose down` so the next boot doesn't re-download. No host bind.
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
   accept; this stack stays content-agnostic. Same posture as F5-TTS
   HU's CC-BY-NC opt-in.

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
