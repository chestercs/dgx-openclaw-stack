# CLAUDE.md

Guidance for Claude Code (and other coding agents) working on this repository.

## What this repo is

A single-file Docker Compose stack that brings up a self-hosted, OpenAI-compatible LLM (Gemma 4 31B NVFP4 on vLLM), a multilingual embedding service (bge-m3), the OpenClaw agent gateway, a privacy-first SearxNG meta-search backend, and a bilingual TTS surface (Kokoro 82M English by default + opt-in F5-TTS Hungarian, fronted by an OpenAI-compat router). Calibrated for NVIDIA GB10 (DGX Spark / ASUS Ascent), portable to other hardware via documented overrides.

The repo's value proposition is the **wiring**, not any individual component:
- Model + embedding + gateway + memory + web search are pre-integrated.
- An idempotent config patcher (`patch-config.mjs`) keeps `openclaw.json` in a deterministic, production-ready state across upgrades and re-runs of the onboarding wizard.
- A bootstrap script (`bootstrap.sh`) handles secret rotation, host paths, and prerequisite checks non-destructively.

## Repo layout

```
docker-compose.yml      # 8 services default (+1 with --profile hu); GB10 reference profile
patch-config.mjs        # 11-step idempotent openclaw.json patcher (init container)
bootstrap.sh            # First-time setup: secrets, .env, host dirs (non-destructive)
.env.example            # Tunables, well-commented
templates/              # vLLM tool-calling chat template (gemma4)
searxng/settings/       # SearxNG override settings (privacy posture)
openclaw-tts-en/        # English TTS service (Kokoro 82M, Apache 2.0) — default
openclaw-tts-router/    # OpenAI-compat /v1/audio/speech router (passthrough + ffmpeg)
openclaw-tts-f5hun/     # OPT-IN Hungarian TTS (F5-TTS, CC-BY-NC weights) — profile=hu
docs/
  ARCHITECTURE.md       # Service-by-service design rationale
  CUSTOMIZATION.md      # Swap models, retune for your hardware, remote backends
  TROUBLESHOOTING.md    # Common failure modes and fixes
SETUP.md                # End-user first-boot walkthrough
README.md               # Audience-facing pitch + quickstart
```

## Working principles

### Patcher is the source of truth, not openclaw.json

Never tell users to hand-edit `openclaw.json`. The `openclaw-config-init` container runs `patch-config.mjs` before every `up` and re-applies the desired state. If a change should persist, add or modify a step in `patch-config.mjs` (deep-merge style — read existing values, only write when they differ, log every change).

The patcher's contract:
- Skip cleanly (`exit 0`) when `openclaw.json` doesn't exist (pre-onboarding fresh install).
- Never overwrite user-managed fields (custom `agents.list[]` entries, channel credentials, etc.).
- Always log a `[patch-config]` line for each change so users can audit what shifted.

### Two-phase fresh-install onboarding

There's a known sequence on a brand-new install:

1. `docker compose up -d` → patcher skips because `openclaw.json` doesn't exist yet → gateway crash-loops with `Missing config. Run openclaw setup …`. **This is expected**, not a bug.
2. User runs onboarding (Chrome extension wizard, `openclaw onboard --non-interactive …`, or `openclaw setup`) which creates `openclaw.json`.
3. `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli` → patcher now finds the file and applies all 11 steps; gateway picks up the patched config and goes healthy.

If you find yourself wanting to "fix" the crash-loop in step 1, don't. The OpenClaw security model requires explicit onboarding; the alternative would weaken auth defaults.

### Defaults assume GB10, but env overrides keep the repo portable

The reference profile (`docker compose up -d` with no edits) targets a 128 GB GB10 box. Three documented portability paths via `.env` overrides:

- **Different NVIDIA GPU**: change `--model` / `LLM_GPU_MEM_UTIL` / `LLM_MAX_NUM_SEQS` in `.env`, swap the vLLM image if not Blackwell.
- **Remote vLLM backend** (gateway local, LLM elsewhere): set `OPENAI_BASE_URL` + `LLM_BASE_URL` + `EMBED_BASE_URL` in `.env`, add `profiles: ["never"]` to the local `vllm-llm` / `vllm-embedding` services. See `docs/CUSTOMIZATION.md` → "Run with a remote vLLM backend" — verified end-to-end on a GPU-less host pointing at a remote LAN vLLM.
- **Cloud LLM endpoint** (Bedrock proxy, OpenRouter, …): same as the remote-vLLM path, but the URLs point at a cloud service.

When adding new tunables, follow the existing pattern: `${VAR:-sensible_default}` in compose, `process.env.VAR || 'sensible_default'` in the patcher, and a `.env.example` entry with a one-paragraph comment explaining the trade-off.

### Comments earn their place

Inline comments in this repo follow a high bar — they explain *why* (a constraint, a non-obvious gotcha, a benchmark number, an OpenClaw-specific behavior), not *what*. The compose file and patcher are heavily commented for end users; the goal is that someone debugging at 2am can understand each block without leaving the file.

When you add a step to the patcher or a service to the compose file, write a comment that helps that 2am debugger. When in doubt, model your comment on the surrounding ones — they're the standard.

### Verify before declaring done

A change that touches the patcher or compose file isn't complete until:
1. `node --check patch-config.mjs` passes.
2. `docker compose config` parses cleanly with a representative `.env`.
3. (For non-trivial changes) bring the stack up on a test host, run `openclaw memory status`, `openclaw agent --agent main --message "…"`, and `curl <gateway>/healthz`.

The repo's quality bar is "real verification on a real host," not "syntax-checks only." Edge cases caught in the wild — fresh-install state, port-publishing assumptions, bridge DNS reachability from `network_mode: service:` containers — are not theoretical.

## Things to avoid

- **Don't bypass the patcher** by writing to `openclaw.json` directly from a script or from a service entrypoint. The patcher's deep-merge style is intentional — it survives OpenClaw schema migrations.
- **Don't add new services without thinking about the bridge network.** All services on the default compose bridge can reach each other by service name. New services that need LAN exposure should publish ports explicitly; new internal services should not.
- **Don't add backwards-compatibility shims for old OpenClaw versions** unless you're sure the older version is in active use. The repo tracks the latest stable OpenClaw image.
- **Don't ship interactive prompts in scripts that could be CI-driven.** `bootstrap.sh` prompts for secrets, but it also accepts pre-set `.env` values and skips. New scripts should follow the same pattern.
- **Don't generate URLs you haven't verified.** This applies especially to documentation links — broken links in CUSTOMIZATION.md are worse than no link at all.

## Useful one-liners

```bash
# Check what the patcher would do (run inside the gateway container's volume context)
docker exec <project>-openclaw-config-init node /opt/patch-config.mjs

# Inspect the live openclaw.json
cat $OPENCLAW_CONFIG_DIR/openclaw.json | jq '.models.providers.vllm, .agents.defaults.memorySearch, .plugins.entries.searxng.config.webSearch'

# Test SearxNG JSON API from inside the gateway namespace
docker exec <project>-openclaw-cli curl -sS "http://searxng:8080/search?q=test&format=json" | jq '.results | length'

# Memory hybrid search smoke test
docker exec <project>-openclaw-cli openclaw memory status --deep
docker exec <project>-openclaw-cli openclaw memory search "your query"

# Multi-tool agent run via the gateway
docker exec <project>-openclaw-cli openclaw agent --agent main \
  --message "Use web_search to find …" --thinking medium --json --timeout 240
```

## Implementation details worth knowing

These are the non-obvious bits that bit somebody once and ended up shaping the design. If you're modifying the repo, internalize these before adding anything new.

### Container name prefix is configurable

Every service uses `container_name: ${CONTAINER_NAME_PREFIX:-dgx-}<service>`. Default `CONTAINER_NAME_PREFIX=dgx-` lives in `.env.example` — gives the familiar `dgx-openclaw-gateway` / `dgx-vllm-llm` shape. Set the var to empty to drop the prefix entirely and get bare names like `openclaw-gateway`, `vllm-llm`. Multiple instances of the stack can coexist on the same host by setting different prefixes; never hard-code the prefix in scripts or examples — read it from the env or use the `${PROJ}-` placeholder in docs.

**Bridge DNS reachability is independent of `container_name`.** Services resolve each other by the compose service name plus the explicit `hostname:` directive — both unaffected by the prefix. Sibling stacks pointed at the same network can keep using `vllm-llm:8004` regardless of how the container shows up in `docker ps`.

`COMPOSE_PROJECT_NAME` (also in `.env.example`, default `dgx-openclaw`) is a separate concern — it scopes auto-generated docker resources (default bridge network, anonymous volumes) so multiple stacks don't collide on the docker daemon. It does NOT affect container names anymore.

### `openclaw-cli` shares the gateway's network namespace

`openclaw-cli` declares `network_mode: "service:openclaw-gateway"`. Two consequences:

1. The CLI reaches the gateway on `127.0.0.1:18789` and the SearxNG service on the bridge DNS name `searxng:8080` with zero extra config.
2. **If you `--force-recreate openclaw-gateway`, you must recreate `openclaw-cli` in the same command.** A recreated gateway gets a fresh network namespace; the still-running CLI ends up pointing at a dead namespace and silently loses connectivity. The pattern is always `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli`.

### Bridge DNS reachability semantics

Services on the default compose bridge can reach each other by service name (DNS resolution by `hostname:`). They can also reach LAN IPs and public hostnames outbound — Docker bridge networks NAT outbound by default. Use this when wiring remote backends: `OPENAI_BASE_URL=http://192.168.x.x:8004/v1` works from inside a container without any extra Docker network config.

What does *not* work: reaching `host.docker.internal` is platform-dependent (works on Docker Desktop, broken on raw Linux). The repo never relies on it.

### `profiles: ["never"]` is the canonical "park this service" pattern

When a service should exist in the compose file (for documentation, for users with the standard layout) but not start under the current configuration, add `profiles: ["never"]` to its top-level keys. `docker compose up` only starts services in the default profile (those with no `profiles:` key). This is how the remote-backend setup parks `vllm-llm` / `vllm-embedding` without deleting them.

Don't comment out service blocks — that loses their documentation value and makes diffs harder to review.

### SearxNG `keep_only` is a registry filter, not an enable flag

`use_default_settings.engines.keep_only:` discards every engine not in the list, but **does not flip `disabled: false` on the survivors**. Engines shipped with `disabled: true` in upstream defaults (Reddit, Wikibooks, Wikiquote, Wikisource, …) need an explicit per-engine override:

```yaml
engines:
  - name: reddit
    disabled: false
```

If you add an engine to `keep_only` and it doesn't return results, check the upstream `searx/settings.yml` for its default `disabled` flag.

### bootstrap.sh's `upsert_env` is regex-gated, not unconditional

`upsert_env KEY NEWVAL PLACEHOLDER_REGEX` only writes the new value if the current value matches the placeholder regex (e.g. `^CHANGE_ME`). This makes the script safe to re-run — real user values never get overwritten. When adding a new secret to bootstrap, follow the pattern: shipped placeholder in `.env.example` starts with `CHANGE_ME`, bootstrap regex matches that prefix.

### Heartbeat triggers from a markdown file

`agents.defaults.heartbeat` schedules wake-ups, but each wake-up reads `~/.openclaw/workspace/HEARTBEAT.md` (relative to the agent's workspace) for what to actually do. An empty / missing file → `status: skipped, reason: empty-heartbeat-file`. To smoke-test heartbeat end-to-end, seed a `HEARTBEAT.md` and run `openclaw system event --text "…" --mode now --expect-final`.

### Memory sources default to `workspace/memory/*.md`

`agents.defaults.memorySearch.sources` defaults to a single source named `memory` rooted at `~/.openclaw/workspace/memory/`. Reindex with `openclaw memory index --force`. The vector dimension is fixed when the first chunk is indexed — switching embedding models without reindexing leaves stale vectors with the wrong dimension. If a user changes `EMBED_BASE_URL` to a different model family, they must drop the SQLite vec store and reindex.

### `restart: unless-stopped` × `OPENCLAW_NO_RESPAWN=1`

The OpenClaw process knows how to respawn itself on certain failures. We disable that with `OPENCLAW_NO_RESPAWN=1` so Docker is the single source of restart truth, and Docker's `restart: unless-stopped` handles the actual lifecycle. Keep both in sync — if you remove one, remove the other.

### `OPENCLAW_LAN_CIDR` must include any client that hits the gateway directly

`gateway.trustedProxies` controls X-Forwarded-For trust. Defaults: `127.0.0.1`, `::1`, `172.16.0.0/12` (the docker bridge range). Add `OPENCLAW_LAN_CIDR=192.168.x.0/24` if any LAN client hits the gateway *without* going through a reverse proxy — otherwise the gateway logs spurious "untrusted proxy" warnings.

### vLLM tool-call template ships in `templates/`, not in the image

`vllm/vllm-openai:gemma4-cu130` doesn't include the gemma4 tool-call chat template. Without it, Gemma emits raw `call:tool{args}` text in the content field instead of a populated `tool_calls` JSON array — and OpenClaw silently ignores tool calls. The compose file bind-mounts `./templates:/templates:ro` and passes `--chat-template /templates/tool_chat_template_gemma4.jinja`. Don't move or rename the template without updating both.

### HuggingFace token is exposed under both names

vLLM accepts `HUGGING_FACE_HUB_TOKEN`; some downstream tools want `HF_TOKEN`. The compose file sets both from the same `.env` value. If you add a new vLLM service, mirror that pattern.

### Volume mounts run as user `1000:1000`

`openclaw-config-init` and the gateway run as UID/GID `1000:1000` (matches the OpenClaw image's `node` user). The host directories that get bind-mounted (`OPENCLAW_CONFIG_DIR`, `OPENCLAW_WORKSPACE_DIR`, `VLLM_HF_CACHE_DIR`) must be writable by that UID. `bootstrap.sh` does not chown — it lets the user do it. If you add a new bind mount, surface this in the bootstrap script's path-prompt step.

### `NODE_COMPILE_CACHE` lives inside the config volume

`NODE_COMPILE_CACHE: /home/node/.openclaw/.node-compile-cache` lets the OpenClaw Node.js process cache compiled bytecode across restarts. Because it's inside the OPENCLAW_CONFIG_DIR mount, it survives `docker compose down` and accelerates subsequent boots by ~3–5 seconds. Don't relocate it without thinking about persistence.

### TTS surface: three services, one OpenAI-compat seam

The TTS surface is three services wired together:

- `openclaw-tts-en` — Kokoro 82M (Apache 2.0), English. Ships in the default profile, ~500 MB-1 GB VRAM, coexists with the LLM on the same GB10 GPU. Voices baked into the image at build time (only the A/A-/B-grade entries from Kokoro VOICES; full pack is 54, we ship the production-ready subset).
- `openclaw-tts-router` — ~150 LOC FastAPI passthrough. Mandatory if any TTS is wired. Bundles ffmpeg so it can transcode the backend's wav into mp3/opus/aac on the fly (OpenClaw's openai TTS provider asks for mp3 by default, content-type sniffing is finicky). Activates the HU voice ids and the diacritic-based autodetect when `F5HUN_URL` + `F5HUN_API_TOKEN` are both non-empty in the router env.
- `openclaw-tts-f5hun` — F5-TTS Hungarian, **OPT-IN** via `profiles: ["hu"]`. Pulls `sarpba/F5-TTS_V1_hun_v2` (CC-BY-NC) at build time. Does not start without the profile.

OpenClaw uses this via `messages.tts.providers.openai.baseUrl` (sanctioned per closed OpenClaw issues #13907 / #29224). Patcher step 11 writes **three things** when `OPENCLAW_TTS_ROUTER_API_KEY` is set:

1. **Top-level `messages.tts.{enabled,auto,mode}`** — without these the OpenClaw voice surfaces silently treat TTS as off even when the provider is correctly wired. The original step-11 implementation only wrote points 2-3 below; voice playback was 100% silent until those top-level switches were added in v0.4.0.
2. **`messages.tts.providers.openai`** — `baseUrl`, `apiKey`, `model`, `voiceId` pointing at the bundled router.
3. **`messages.tts.voiceAliases`** — friendly aliases like `english`, `narrator`, `male`, `female`, `magyar`, `hungarian` mapped to concrete Kokoro / F5-TTS voice ids.

Unset → step 11 skips cleanly so users can opt out of TTS by leaving the var empty (and parking the two TTS services with `profiles: ["never"]`).

The web chat UI is hard-wired to the browser's native `speechSynthesis` and does not call this router (known OpenClaw limitation). Voice surfaces that go through the gateway's TTS pipeline (Discord, agent `tts` skill) do.

### Hungarian TTS opt-in mechanism (CC-BY-NC license isolation)

Three independent levers gate the HU service so users can't accidentally pull CC-BY-NC weights:

1. **Compose profile** — `profiles: ["hu"]` on `openclaw-tts-f5hun`. Without `--profile hu` (or `COMPOSE_PROFILES=hu`), the service block is parked. `docker compose up -d` plain does not start it; `docker compose build` does not build it.
2. **Token gate** — router activation requires non-empty `F5HUN_API_TOKEN` *and* `F5HUN_URL`. Without both, `F5HUN_ENABLED = False` in the router app, HU voice ids return 404, autodetect is a no-op.
3. **Bootstrap prompt** — `bootstrap.sh` asks once on first run; declining leaves all three opt-in vars empty. Re-runs preserve the user's choice (token-presence guard).

The wrapper code is MIT (matches the rest of the repo); only the model weights are CC-BY-NC. Building the image is what triggers the HF download — and that's what constitutes acceptance of the upstream license. The repo ships no model weights of any kind. This is the same pattern as Gemma 4 NVFP4 (gated, license-acceptance via HF).

When adding a similar opt-in service in the future, follow this triad: profile guard + env-token guard + bootstrap-prompt opt-in. Don't ship CC-BY-NC content in the default code path even by accident.

### vLLM ports are NOT published by default

The default compose layout intentionally does not publish 8004 / 8005 on the host — sibling containers reach them via bridge DNS. If a user wants debug access from the host, they uncomment the `127.0.0.1:8004:8004` line on the service. Don't change the default to publish — many users run this on machines reachable from the LAN, and an unauthenticated 0.0.0.0 vLLM port is an accidental open door.

For the remote-backend use case, the vLLM endpoints **are** expected to be reachable on the network — that's a separate deployment, and the user controls its exposure (TLS, auth, private network, etc.).

### TTS ports are published, but loopback-only by default

Unlike the vLLM services, the three TTS services *do* publish their port on the host so `curl 127.0.0.1:809{0,1,2}/healthz` works without `docker exec` gymnastics. The defaults (`TTS_EN_BIND=127.0.0.1`, `TTS_F5HUN_BIND=127.0.0.1`, `TTS_ROUTER_BIND=127.0.0.1` in `.env.example`) bind to loopback so LAN clients can't reach them — same security posture as the unpublished vLLM ports, but ergonomic for local debugging. To expose any of them on the LAN, set `TTS_*_BIND=0.0.0.0`. All three services are Bearer-token-protected via the existing TTS tokens, but a leaked token is still a leaked token — keep the loopback default unless you have a reason.

### HF cache volume label is configurable for sibling-stack sharing

The shared HF cache volume name is `${VLLM_HF_CACHE_VOLUME_NAME:-dgx-openclaw-hf-cache}` (default in `.env.example`). The actual cache lives at the bind-mounted host path `${VLLM_HF_CACHE_DIR}` — the volume name is just a Docker label. If you run sibling LLM stacks on the same host that bind-mount the same `VLLM_HF_CACHE_DIR`, set the same `VLLM_HF_CACHE_VOLUME_NAME` in each so they show up under one consistent label in `docker volume ls`. Bridge DNS reachability is unaffected — only the volume label.

### The patcher writes baseUrls with trailing slashes; OPENAI_BASE_URL drops it

The OpenClaw config schema requires trailing slashes on `models.providers.vllm.baseUrl` and `agents.defaults.memorySearch.remote.baseUrl`. The vLLM OpenAI client (used by the gateway) wants `OPENAI_BASE_URL` *without* a trailing slash. The repo handles this asymmetry: the patcher always appends `/v1/` to `LLM_BASE_URL` / `EMBED_BASE_URL`, while compose passes `OPENAI_BASE_URL` straight through. If a user reports "404 Not Found from gateway → vLLM," check whether they accidentally added a trailing slash to `OPENAI_BASE_URL`.

## Verification recipes (copy-paste ready)

These cover the cases that have actually broken in practice. When making non-trivial changes, run the relevant ones before declaring done.

```bash
# 1. Compose + patcher syntax
node --check patch-config.mjs
docker compose --env-file .env config --services                # lists 8 default services
docker compose --env-file .env --profile hu config --services   # 9 with HU opt-in active

# 2. End-to-end on a test host (after `docker compose up -d` and onboarding)
# CONTAINER_NAME_PREFIX (default `dgx-`) already includes the trailing dash —
# don't add another one in the docker exec target.
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)
PROJ=${PROJ:-dgx-}

curl -sS http://127.0.0.1:18789/healthz                # → {"ok":true,"status":"live"}

docker exec ${PROJ}openclaw-cli openclaw memory status --deep \
  | grep -E "Provider|Vector dims|Embeddings"           # all "ready", dims = 1024 for bge-m3

docker exec ${PROJ}openclaw-cli sh -c \
  'curl -sS "http://searxng:8080/search?q=docker&format=json"' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('results:', len(d['results']))"
                                                        # → results: ~30

docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Use web_search to find the title of docker.com. Reply with TITLE: <title>" \
  --thinking off --json --timeout 180 \
  | jq '.toolSummary, .finalAssistantVisibleText'
                                                        # → tools: ["web_search"], failures: 0
                                                        # → "TITLE: Docker: …"

# 3. Hybrid memory smoke
docker exec ${PROJ}openclaw-cli sh -c \
  'mkdir -p ~/.openclaw/workspace/memory && \
   echo "Gemma 4 31B NVFP4 runs at ~6.9 tok/s on GB10." > ~/.openclaw/workspace/memory/test.md'
docker exec ${PROJ}openclaw-cli openclaw memory index --force
docker exec ${PROJ}openclaw-cli openclaw memory search "How fast is Gemma on GB10?"
                                                        # → score >0.4, returns test.md
```

## When in doubt

- For runtime/config questions, read `docs/ARCHITECTURE.md` first.
- For "how do I change X" questions, read `docs/CUSTOMIZATION.md`.
- For OpenClaw-specific behavior (CLI flags, plugin schema, gateway protocol), check the upstream docs at https://docs.openclaw.ai/.
- For vLLM-specific questions (chat template, tool parser, NVFP4 kernels), check the upstream vLLM repo and the gemma4 image notes.
- For hardware tuning, the GB10 numbers in `README.md` and `.env.example` are measured, not theoretical — they're a useful baseline when adapting to other GPUs.
