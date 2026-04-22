# Architecture

This document explains how the eight services in `docker-compose.yml` fit together (nine with the optional `hu` profile active) and **why** each design choice was made. Read this if you plan to modify the stack — the compose file and `patch-config.mjs` have decisions baked into them that only make sense with the rationale below.

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

### 11 steps

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

## Volumes

- `hf-cache` (named volume, bound to `$VLLM_HF_CACHE_DIR`) is shared by both vLLM services so the bge-m3 weights live next to the Gemma 4 weights in the same HF cache structure. Both services get `volumes: - hf-cache:/root/.cache/huggingface`. The Docker volume label is `${VLLM_HF_CACHE_VOLUME_NAME:-dgx-openclaw-hf-cache}` — change this if a sibling LLM stack on the same host bind-mounts the same `VLLM_HF_CACHE_DIR` and you want one consistent label in `docker volume ls`.
- `$OPENCLAW_CONFIG_DIR` is bind-mounted into the config-init, gateway, and cli services — they all read/write the same `openclaw.json`, memory/, heartbeat journal.
- `$OPENCLAW_WORKSPACE_DIR` is bind-mounted into the gateway and cli services — the agent's writable working directory.
- `tts-en-hf-cache`, `tts-f5hun-hf-cache`, `tts-f5hun-voices` — Docker named volumes (no host bind). Hold runtime HF downloads + user-supplied reference voices for the F5-TTS HU service.

## Networking, trust, and exposure

- **Gateway**: `18789` (and `18790` control) are published to the host. Put a TLS-terminating reverse proxy in front for public access over `wss://`.
- **vLLM ports stay compose-internal.** For host-side debug, uncomment the `"127.0.0.1:8004:8004"` / `"127.0.0.1:8005:8005"` bindings in `docker-compose.yml`. Binding to `127.0.0.1` keeps them off your LAN.
- **TTS ports publish to loopback by default** (`127.0.0.1:8090–8092`) — see the TTS subsystem section above. Set `TTS_*_BIND=0.0.0.0` in `.env` to expose on the LAN.
- `gateway.trustedProxies` includes `172.16.0.0/12` so any reverse proxy on the same docker bridge (or on host-network with bridge-adjacent IPs) is trusted. If you access the gateway **directly** from your LAN (bypassing the proxy), add your LAN CIDR via `OPENCLAW_LAN_CIDR`.
- The Chrome extension expects `wss://` (secure) for public use. Put a TLS-terminating reverse proxy in front and set `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1` so the gateway accepts the plain `ws://` hop from the proxy on your private network.
