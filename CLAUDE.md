# CLAUDE.md

Guidance for Claude Code (and other coding agents) working on this repository.

## What this repo is

A single-file Docker Compose stack that brings up a self-hosted, OpenAI-compatible LLM (Gemma 4 26B-A4B MoE NVFP4 on `vllm-llm:8004` and Gemma 4 31B IT NVFP4 dense on `vllm-llm-dense:8005` running side by side, two separate OpenClaw provider entries — pick either model in the UI without restarting), a multilingual embedding service (bge-m3), the OpenClaw agent gateway, a privacy-first SearxNG meta-search backend, a bilingual TTS surface (Kokoro 82M English by default + opt-in F5-TTS Hungarian, fronted by an OpenAI-compat router), and a Whisper STT backend (`faster-whisper` large-v3 via the upstream speaches-ai image, EN + HU autodetect). Calibrated for NVIDIA GB10 (DGX Spark / ASUS Ascent), portable to other hardware via documented overrides.

The repo's value proposition is the **wiring**, not any individual component:
- Model + embedding + gateway + memory + web search are pre-integrated.
- An idempotent config patcher (`patch-config.mjs`) keeps `openclaw.json` in a deterministic, production-ready state across upgrades and re-runs of the onboarding wizard.
- A bootstrap script (`bootstrap.sh`) handles secret rotation, host paths, and prerequisite checks non-destructively.

## Repo layout

```
docker-compose.yml      # 9 services default (+1 with --profile hu, +1 with --profile browser); GB10 reference profile
patch-config.mjs        # 15-step idempotent openclaw.json patcher (init container)
bootstrap.sh            # First-time setup: secrets, .env, host dirs (non-destructive)
bootstrap-browser-login.sh  # 1x OAuth onboarding helper for openclaw-browser (noVNC)
.env.example            # Tunables, well-commented
templates/              # vLLM tool-calling chat template (gemma4)
searxng/settings/       # SearxNG override settings (privacy posture)
openclaw-tts-en/        # English TTS service (Kokoro 82M, Apache 2.0) — default
openclaw-tts-router/    # OpenAI-compat /v1/audio/speech router (passthrough + ffmpeg)
openclaw-tts-f5hun/     # OPT-IN Hungarian TTS (F5-TTS, CC-BY-NC weights) — profile=hu
openclaw-stt-whisper/   # Self-built CUDA 13 image (Blackwell compat) — FastAPI
                        # around faster-whisper large-v3, ~150 LOC wrapper
openclaw-browser/       # OPT-IN browser automation — Playwright Chromium over CDP,
                        # one warm Chromium per profile, port-per-profile routing,
                        # noVNC bridge for 1x OAuth onboarding — profile=browser
docs/
  ARCHITECTURE.md       # Service-by-service design rationale
  CUSTOMIZATION.md      # Swap models, retune for your hardware, remote backends
  TROUBLESHOOTING.md    # Common failure modes and fixes
  reference/            # Deeper reference: credential layout, patcher internals,
                        # LLM/TTS research, browser-automation design, reusable
                        # Docker patterns
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

A brand-new install always proceeds in two phases:

1. `docker compose up -d` starts every service. The patcher skips because `openclaw.json` doesn't exist yet, so the gateway crash-loops with `Missing config. Run openclaw setup …`. **This is the intended state**, not a bug.
2. The user completes onboarding (Chrome extension wizard, `openclaw onboard --non-interactive …`, or `openclaw setup`), which writes `openclaw.json` to the config volume.
3. `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli` re-runs the patcher against the now-existing config; the gateway picks up the patched file and goes healthy.

Don't try to "fix" the crash-loop in step 1 by patching defaults into a freshly created `openclaw.json` — the OpenClaw security model requires explicit onboarding so the operator chooses the gateway token and pairs the UI before the gateway accepts connections. Skipping that step would weaken the auth defaults.

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

### Documentation language: English for anything public

Every file that lands in this public repo — `README.md`, `SETUP.md`, `CLAUDE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, everything under `docs/` (including `docs/reference/`), `.github/*`, compose / patcher inline comments, commit messages, PR and issue templates, GitHub Release notes — is written in English. This is a hard rule, not a preference: the repo targets a global audience, and mixing languages splits the readership. No exceptions — if you spot any non-English prose slipping through (in existing files or a proposed change), translate it rather than committing it as-is.

If you're importing or adapting material from a non-English source (a private knowledge base, a Hungarian research note, a vendor doc), translate it before committing. Don't push a "we'll translate it later" commit; "later" becomes "never," and mixed-language docs are worse than a smaller English-only set.

## Things to avoid

- **Don't bypass the patcher** by writing to `openclaw.json` directly from a script or from a service entrypoint. The patcher's deep-merge style is intentional — it survives OpenClaw schema migrations.
- **Don't add new services without thinking about the bridge network.** All services on the default compose bridge can reach each other by service name. New services that need LAN exposure should publish ports explicitly; new internal services should not.
- **Don't add backwards-compatibility shims for old OpenClaw versions** unless you're sure the older version is in active use. The repo tracks the latest stable OpenClaw image.
- **Don't ship interactive prompts in scripts that could be CI-driven.** `bootstrap.sh` prompts for secrets, but it also accepts pre-set `.env` values and skips. New scripts should follow the same pattern.
- **Don't generate URLs you haven't verified.** This applies especially to documentation links — broken links in CUSTOMIZATION.md are worse than no link at all.

## Useful one-liners

These snippets assume `${PROJ}` holds the container-name prefix (default `dgx-`, set via `CONTAINER_NAME_PREFIX` in `.env`). Source it once per shell:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)
PROJ=${PROJ:-dgx-}
```

```bash
# Re-run the patcher against the live openclaw.json
docker exec ${PROJ}openclaw-config-init node /opt/patch-config.mjs

# Inspect the live openclaw.json
cat $OPENCLAW_CONFIG_DIR/openclaw.json | jq '.models.providers.vllm, .agents.defaults.memorySearch, .plugins.entries.searxng.config.webSearch'

# Test SearxNG JSON API from inside the gateway namespace
docker exec ${PROJ}openclaw-cli curl -sS "http://searxng:8080/search?q=test&format=json" | jq '.results | length'

# Memory hybrid search smoke test
docker exec ${PROJ}openclaw-cli openclaw memory status --deep
docker exec ${PROJ}openclaw-cli openclaw memory search "your query"

# Multi-tool agent run via the gateway. `--timeout 600` is the safe floor
# for any tool-using run on Gemma 4 NVFP4 (see "Multi-step tool-call
# agent runs need a generous --timeout" in Implementation details).
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Use web_search to find …" --thinking off --json --timeout 600
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

### STT is one service, no router — Whisper autodetects

The STT surface is a single service, `openclaw-stt-whisper`, **self-built** from `./openclaw-stt-whisper/server/` on a CUDA 13 base. ~150 LOC FastAPI wrapper around `faster-whisper` running `Systran/faster-whisper-large-v3` (MIT). No router — Whisper autodetects the input language per request, so the bilingual routing that TTS needs is unnecessary. FLEURS Hungarian WER 14.1%, ~3 GB VRAM at float16, port `8093` loopback-only by default (same posture as TTS). The original plan targeted `ghcr.io/speaches-ai/speaches` upstream (zero custom code), but GB10 (sm_120) deploy logs on 2026-04-24 showed the upstream CT2 rejects every low-precision compute type and destabilizes on float32 — a CUDA 13 base + cu130 PyTorch wheels + current `faster-whisper` is the proven path (matches the `vllm-llm` / `openclaw-tts-en` wheel pattern). Swap `build:` back to `image:` when the upstream publishes a Blackwell-tensor-core variant.

Wired via `tools.media.audio.models[]` in `openclaw.json` (NOT `messages.stt` — that schema name doesn't exist in OpenClaw). See `docs.openclaw.ai/nodes/audio`. Path used by voice-note upload in the Control UI composer, Discord voice channels, VoiceCall CLI, Talk / Voicewake nodes. The Control UI realtime mic button is a separate path — it uses the browser's native Web Speech API (`speech.ts`) and does NOT go through this service; that is an OpenClaw design choice, not a wiring limitation.

Auth isolation: the Bearer lives in the per-entry `headers.Authorization` rather than `apiKey`. The schema defaults `apiKey` to the standard `models.providers.openai` chain (env vars → auth profiles → global provider apiKey), which would collide with any cloud OpenAI account the user also has. Per-entry `headers` overrides are explicitly supported by the schema (`docs.openclaw.ai/nodes/audio`) and keep the Whisper token orthogonal to the global openai apiKey. Patcher step 14 writes the header, and re-runs deep-merge (user-added extra headers survive).

### HF cache volume label is configurable for sibling-stack sharing

The shared HF cache volume name is `${VLLM_HF_CACHE_VOLUME_NAME:-dgx-openclaw-hf-cache}` (default in `.env.example`). The actual cache lives at the bind-mounted host path `${VLLM_HF_CACHE_DIR}` — the volume name is just a Docker label. If you run sibling LLM stacks on the same host that bind-mount the same `VLLM_HF_CACHE_DIR`, set the same `VLLM_HF_CACHE_VOLUME_NAME` in each so they show up under one consistent label in `docker volume ls`. Bridge DNS reachability is unaffected — only the volume label.

### The patcher writes baseUrls with trailing slashes; OPENAI_BASE_URL drops it

The OpenClaw config schema requires trailing slashes on `models.providers.vllm.baseUrl` and `agents.defaults.memorySearch.remote.baseUrl`. The vLLM OpenAI client (used by the gateway) wants `OPENAI_BASE_URL` *without* a trailing slash. The repo handles this asymmetry: the patcher always appends `/v1/` to `LLM_BASE_URL` / `EMBED_BASE_URL`, while compose passes `OPENAI_BASE_URL` straight through. If a user reports "404 Not Found from gateway → vLLM," check whether they accidentally added a trailing slash to `OPENAI_BASE_URL`.

### Browser automation: CDP-attach, port-per-profile, query-string token

`openclaw-browser` is the opt-in `--profile browser` service that OpenClaw's built-in `browser` tool attaches to over Chrome DevTools Protocol. Three implementation details worth internalizing:

1. **CDP-attach beat MCP and bespoke HTTP adapters at v0.7.0 design time (2026-04-25)** — OpenClaw didn't expose an MCP slot then, so MCP would have meant either patching the gateway or shoving everything through a custom HTTP tool that duplicated the gateway's built-in surface. CDP-attach via `browser.profiles.<name>.cdpUrl` let us own only the supervisor + login helper + markdown extractor (~600 lines). Native MCP client support landed shortly after (config: `mcp.servers.<name>`, transports: stdio / SSE-HTTP / Streamable-HTTP), so net new tools (e.g. the Python sandbox in v0.8.0+) default to MCP — but the browser stack stays on CDP-attach because port-per-profile + query-string token routing already works and there's no structural reason to migrate. Full rationale in `docs/reference/browser-automation.md`.

2. **Port-per-profile, NOT `?profile=<name>` routing** — OpenClaw issues #4841 / #9723 / #11926 confirm the gateway does NOT pass cdpUrl query params through to Playwright's `connectOverCDP`. So we run one Chromium per profile on a dedicated port (default = `BROWSER_PORT_BASE` 9222, named profiles 9223-9241 in `BROWSER_PROFILE_NAMES` order). Patcher step 15 enumerates the mapping. Don't add cleverness about query-param routing later — it's a known dead end.

3. **Query-string token in cdpUrl is the only auth surface** — OpenClaw's cdpUrl config field accepts `?token=<...>` or HTTP Basic only, not Authorization headers. Mitigations: loopback host bind (`BROWSER_BIND` default `127.0.0.1`), `rotate-secrets.sh --all` covers `BROWSER_API_TOKEN`. The FastAPI management API on port 9220 *does* use Bearer headers; the limitation is purely the CDP attach path. Document any LAN exposure of CDP ports loudly — an unauth'd remote-debugging-port has been the root of multiple Chromium credential-theft CVEs.

### WebAuthn / passkeys do NOT work over the noVNC login helper

The W3C WebAuthn spec is origin-bound. In a noVNC session, the operator's browser is on `http://127.0.0.1:5901` (or via SSH tunnel) but the remote Chromium runs at its own origin. Platform authenticators (Apple Keychain, Windows Hello, Google Password Manager) are bound to the operator's device + origin and cannot reach the remote Chromium's origin. USB hardware passkeys (YubiKey) are not pass-through to the container either. **Document clearly in any onboarding-related guidance: password + TOTP / SMS OTP / magic links work; passkeys don't.** Services that are passkey-only (some Google Workspace SSO) cannot be onboarded via the browser path — use the service's API token / PAT / service account instead.

### Image-gen bridge: separate compose, host-gateway hop, model-agnostic

`openclaw-image-comfyui` is the v0.9.0+ opt-in `--profile image-gen` bridge that exposes `comfyui_image__*` MCP tools to the agent and proxies generation to the operator's existing ComfyUI install. Four implementation details worth internalizing:

1. **First service in this stack to live in its own compose file.** Every other service is in the main `docker-compose.yml`; this one lives in `openclaw-image-comfyui/docker-compose.yml`. Rationale: the operator likely already runs ComfyUI for unrelated reasons; duplicating it inside the main stack would put two ComfyUI processes on the same GB10 GPU. Trade-off: the bridge compose attaches to the main stack's bridge via `external: true`, so the main stack must be `up` at least once before the bridge can start. **`./rotate-secrets.sh IMAGE_GEN_API_TOKEN` therefore prints two `up -d --force-recreate` commands** (one per compose) — this is by design, not a bug to "simplify" away.

2. **Host-gateway, not shared external network.** The bridge reaches the user's ComfyUI (separate compose project, e.g. `petyus-gpt`) via `extra_hosts: host.docker.internal:host-gateway` + `COMFYUI_URL=http://host.docker.internal:13036`. We deliberately do NOT require modifying the user's existing ComfyUI compose. LAN-resident ComfyUI works too: set `COMFYUI_URL=http://192.168.x.x:<port>`. See `docs/reference/image-comfyui-bridge.md` for the table comparing all three options.

3. **Tool-prefix gotcha re-applies.** OpenClaw flattens MCP tool namespaces with `<server>__<tool>` — the agent must call `comfyui_image__generate`, NOT bare `generate`. Same finding as the python sandbox; document any agent-facing prompt with the prefixed name.

4. **Model-agnostic by design.** The repo ships NO model weights. Workflow templates under `server/workflows/` use `"REPLACE_ME.safetensors"` as a placeholder; the bridge refuses to generate without either an explicit `checkpoint=` arg or an operator-edited workflow. Operator picks the upstream models (FLUX Dev / Schnell, SDXL fine-tunes — Pony XL, Illustrious XL, RealVisXL — adult fine-tunes, …) under whichever license they accept. Same posture as F5-TTS HU's CC-BY-NC isolation.

5. **Chat-side inline image render is BLOCKED at the browser security layer** as of openclaw 2026.4.22 + Chrome/FF 2026.04, verified end-to-end on 2026-04-27 with `vision.petyuspolisz.com`. Two independent layers conspire: (a) the chat's markdown sanitizer drops `![alt](url)` entirely (only `alt` survives as a `<p>`) AND drops `[text](https://...)` external-origin links — only `mailto:` links pass through. (b) Even if you bypass the sanitizer with a userscript, browsers refuse to send cached HTTP Basic auth credentials to cross-origin `<img>` fetches — the cached creds aren't exposed to image-tag fetches across origins by design. **The recommended UX**: the user copies the `display_markdown` URL from the tool-output JSON bubble and opens it in a new tab — direct navigation does send Basic auth. Don't burn cycles on markdown-syntax tricks; only same-origin (e.g. `/__openclaw__/canvas/` proxy or workspace-bind + `read` tool) or upstream openclaw native-image-content support will fix it. Document the limit loudly in any image-gen-adjacent docs you touch — the next person will hit it within five minutes.

6. **Token-auth proxy with `auth_request` (v0.9.8–v0.9.10)** removes the secret from the NPM admin config — the bridge holds `COMFYUI_VIEW_TOKEN` in its `.env`, the proxy delegates token validation to the bridge's `GET /auth-validate` endpoint via NGINX's `auth_request` sub-request. Three implementation gotchas worth knowing: (a) **`auth_request /auth-validate;` is a sub-request with a STATIC URI** — the parent request's `?token=...` does NOT propagate to the sub-request's `$args`. Recovery: the proxy sets `proxy_set_header X-Original-URI $request_uri;` (NPM default for the custom auth-validate location), and the bridge `/auth-validate` parses the token out of that header as a fallback. Don't try to fix it with `proxy_pass http://.../auth-validate?token=$arg_token;` — NPM auto-emits its own `proxy_pass` from the Forward Hostname/Port mezők, and a duplicate `proxy_pass` directive is a config-test error. (b) **`auth_basic off;` in a custom location's Advanced is a trap** — NPM auto-emits `auth_basic "Authorization required";` for every location when the host has an Access List with Basic auth, and a duplicate `auth_basic` directive is `[emerg]`. Solution: drop the `auth_basic off;` from the Advanced and use **`Satisfy Any` on the Access List Details tab** so the `auth_request` 200 result alone satisfies the request (Basic creds aren't required when token-auth passes). (c) **`Satisfy Any` + `Allow all` IP rule = wide-open** — `Allow all` means every IP passes the IP-check, and `Satisfy Any` lets that single satisfaction stand in for auth entirely. Drop the `Allow all` rule from the Access List Rules tab; only the auto-fallback `deny all` should remain. With those three settled, the per-location split works: `/` and `/api/view` keep Basic auth (browser UI), `/view` accepts the URL-param token via auth_request (chat-image fetch).

### Multi-step tool-call agent runs need a generous `--timeout`

The dense Gemma 4 31B NVFP4 (the historical default, now opt-in via `profiles: ["dense"]`) generates at ~6 tok/s decode on GB10. The MoE 26B-A4B NVFP4 default measured on this stack with `--moe-backend marlin` (mandatory on Blackwell SM121), `LLM_GPU_MEM_UTIL=0.30`, `LLM_MAX_NUM_SEQS=4`, CUDA graphs ON (`LLM_ENFORCE_EAGER` unset):

- **Decode**: ~24.9 tok/s single-stream (200-token generation, tiny prompt, warm)
- **Prefill**: ~1400–1600 tok/s, chunked, near-flat across 10K-100K context
- **Concurrent 4-parallel** (4 simul tiny-prompt 200-gen): **112 tok/s aggregate**, ~28 tok/s per user — continuous-batching is ~100% efficient on this profile, AND CUDA graphs amortize kernel-launch overhead across iterations giving an extra ~30% multi-user boost
- **Long-context**: 100K prompt + 200 gen ≈ 68s wall; 200K + 200 gen ≈ 4 min (prefill-bound past ~50K context)
- **Verified 2026-05-08 on a single-tool web_search agent run**: cold-cache first run ~120s (system-prompt prefill + tool round-trip + model JIT), warm-cache subsequent runs ~11s. Multi-tool runs scale roughly linearly with call count from there.

The Blackwell-SM121 Marlin path runs at roughly half the throughput of the CUTLASS path on B200 (the 52 tok/s ai-muninn published number) — that's the SM121-specific cost of the Marlin fp4→bf16 dequantize on every expert call. Still ~3.7× faster than the dense 31B baseline, and the 4-parallel aggregate keeps multi-user throughput linear. The pre-2026 "GB10 unified-mem CUDA-graphs hangs" gotcha appears resolved in vLLM 0.19+ — eager mode kept as a rollback path via `LLM_ENFORCE_EAGER=1`.

Even so: a multi-step tool-call agent run does several LLM calls in sequence — system-prompt prefill → tool-call args → tool-result digestion → final reply. With the current MCP catalog (`python_sandbox__*`, `comfyui_image__*`, plus the always-on memory/file/exec/browser surface) the system-prompt prefill alone burns the first 1-5s of every call (faster on MoE, slower on dense).

The default `openclaw agent --timeout` of 60s was repeatedly tripping during v0.9.0 smoke tests on the dense backend with both `python_sandbox__python_exec` and `comfyui_image__list_workflows` — the gateway logs `embedded run timeout, rawErrorPreview: "Request was aborted." failoverReason: "timeout"` and the agent reply field is empty. Bumping to `--timeout 600` resolves it cleanly; `--timeout 300` is the floor for single-tool runs on dense. On MoE, `--timeout 300` is comfortably safe even for multi-tool runs, and `--timeout 600` keeps the same prompt portable to any operator who flips back to dense for parity testing. **Always document `--timeout 600` for tool-using examples** so they survive both backends — the next person to copy-paste shouldn't have to know which is active. The vLLM idle watchdog (`agents.defaults.llm.idleTimeoutSeconds=600`, patcher step 8) is independent — it only catches a stuck connection, not a slow-but-progressing run. Reasoning multipliers (`--thinking medium`, `--thinking high`) make this 2-3× worse; reserve them for genuinely hard prompts.

When a tool-using prompt is documented anywhere in this repo (CLAUDE.md, CUSTOMIZATION.md, README.md, docs/reference/, …) it must use `--timeout 600`. Don't drop it to 60-180s for "looks cleaner" — the next person to copy-paste it will hit the timeout and waste an hour debugging.

### Discord progressive streaming via `channels.discord.streaming`

The decode-rate above has a second consequence on Discord: the upstream OpenClaw default `channels.discord.streaming = "off"` posts replies atomically, so on dense at 6 tok/s a 500-token answer produces ~80 seconds of channel silence before anything appears. Users read this as "the bot is frozen." Patcher step 24 defaults the field to `"partial"` — a single preview message edit-in-place as tokens arrive. Discord enforces 5 message edits / 5 s per channel; on dense at 6 tok/s with the docs-default `draftChunk.minChars=200` (~33 tokens), edits land roughly every 5.5 s, comfortably under the limit on a dedicated single-bot account.

**MoE re-tuning note (post-2026-05 backend swap):** at 52 tok/s the MoE backend hits the same `minChars=200` boundary every ~0.6s, which would burn through Discord's 5-edits/5s rate limit in the first second. With the default `OPENCLAW_DISCORD_DRAFTCHUNK_MIN_CHARS=200` left in `.env` the cadence still works but feels closer to atomic delivery (each edit lands far below the rate-limit floor). For genuinely typewriter-feel streaming on MoE, consider `OPENCLAW_DISCORD_DRAFTCHUNK_MIN_CHARS=1000`+ and `OPENCLAW_DISCORD_DRAFTCHUNK_BREAK_PREFERENCE=paragraph` — that's ~1s of MoE decode per edit, well under the rate-limit budget. The right knob value depends on the actual reply length distribution; defer the tuning until smoke-tested traffic data is available.

Trade-offs and gotchas worth remembering before you tweak this:

- **`"partial"` is right for this stack specifically** because the Discord application token is dedicated (not shared across multiple gateway processes) and the LLM is slow enough that the edit cadence is naturally throttled. A faster backend (e.g. operator points `OPENAI_BASE_URL` at a cloud Sonnet/Haiku endpoint generating at 80+ tok/s) would burn through the rate-limit budget — drop to `"block"` or `"off"` in that case.
- **Media, error, and explicit-reply finals cancel the pending preview edit** per `docs.openclaw.ai/channels/discord.md`. The final then arrives atomically. This is correct behaviour, not a regression — image-gen replies and tool errors should reach the user as standalone events, not as overwrites of an in-progress preview.
- **Streaming is text-only.** Image attachments (the `comfyui_image__generate` Path A `[embed]` shortcode) and file uploads still flow through the atomic delivery path; they don't get partial frames. Voice-channel TTS is independent — the streaming flag doesn't touch it.
- **`draftChunk` is env-knobbed** via `OPENCLAW_DISCORD_DRAFTCHUNK_MIN_CHARS`, `_MAX_CHARS`, and `_BREAK_PREFERENCE`. Each knob is independently optional — unset means the patcher leaves the field undefined and OpenClaw uses its docs defaults (200 / 800 / `"paragraph"`). Default UX with paragraph-grain edits feels chunky for short interactive replies; setting `MIN_CHARS=100` + `BREAK_PREFERENCE=newline` shifts to ~2-3s line-grain edits — much closer to typing UX. The `breakPreference` enum is `{paragraph, newline, sentence}` (confirmed from the openclaw 2026.4.22 runtime validator on 2026-04-29 — the upstream docs only list `"paragraph"`). The most common wrong guess is `"line"`; the patcher refuses any value outside the validated enum to avoid putting the gateway into a config-invalid restart-loop. The Discord 5-edits/5s rate limit puts a soft floor at `MIN_CHARS≈80` (~13 tokens at 6 tok/s ≈ 2s/edit cadence) on a single dedicated bot account.
- **`streaming.preview.toolProgress`** is env-knobbed via `OPENCLAW_DISCORD_STREAMING_PREVIEW_TOOL_PROGRESS=true|false`. Default unset → upstream default `true` applies. Useful to see what the agent is doing mid-stream (`web_search`, `comfyui_image__generate`, `browser.act`, …). **Known cosmetic bug on Discord 2026.4.22**: tool names with double-underscore separators get mangled by Discord's italic markdown parser — `_image_` in `comfyui_image__generate` becomes italic mid-name and the line looks broken. There is no documented upstream config flag to escape the tool name or wrap it in a code block; only this on/off knob exists. Set to `false` if the rendering bothers you more than the loss of mid-stream visibility. Tracked as a feature request in `docs/upstream-feedback/discord-toolprogress-rendering.md` for the upstream `openclaw/openclaw` repo.
- When the operator opts in to setting `streaming.preview.toolProgress`, the patcher coerces the `streaming` field from the scalar string form (`"partial"`) to the nested object form (`{mode: "partial", preview: {toolProgress: false}}`) — both shapes are documented as supported by OpenClaw, but the nested form is the only way to reach `preview.*` sub-keys.
- **Override via `OPENCLAW_DISCORD_STREAMING=off|partial|block|progress` in `.env`**, or set to empty string to skip the step entirely. Step 24 also follows the same user-managed protection as steps 20-22: if the operator already wrote `channels.discord.streaming` (any value) into `openclaw.json`, the patcher leaves it alone.

### Discord-routed agent tools.profile defaults to `full`, not `coding`

OpenClaw's non-main agent default is `tools.profile: "coding"` — that profile catalogs `cron`, `image_generate`, `web_search`, memory/fs/runtime/web/sessions groups, but **excludes `browser`, `tts`, and `canvas`**. For an agent on a Discord route this manifests as three observable failures:

- *"screenshot startlap.hu"* → bot replies *"Sorry, I can't navigate the browser and take a screenshot"*. The `browser` tool isn't in its catalog (verified 2026-04-29).
- *"speak this on voice"* → no audio attaches. The `[[tts:speak]]` directive is parsed but the underlying tool isn't reachable, so the gateway silently strips the directive on its way to Discord.
- canvas-embed shortcodes from `comfyui_image__generate` don't render inline — the agent can't mint same-origin URLs without the `canvas` tool.

The `cron` tool IS in the `coding` profile (and the patcher already adds the catalog entry via step 22), but smaller open models like Gemma 4 NVFP4 don't reliably surface a tool from the catalog alone — they need a worked example in `AGENTS.md` to reach for it. Verified 2026-04-30: the bot replied *"I can't wake up on a timer"* to *"remind me in 1 minute"*, even though the tool was technically callable. **Step 26 patcher writes a cron-tools cheatsheet block into the discord-friend's `workspace-discord/AGENTS.md`** with the canonical `{tool: "cron", action: "add", at: "+1m", agent: "discord-friend", message: "...", channel: "discord", to: "user:<id>", deleteAfterRun: true}` shape — the doc-side fix that makes the tool actually get used.

Three patcher steps work together for Discord-routed agents (step 22, 25, 26). The defaults are wired with the assumption that operators want a Discord bot that can reach for everything the main agent can:

- **Step 22** (existing, default widened in v0.11.1): `tools.alsoAllow += ["group:messaging", "browser", "tts", "canvas"]`. Belt-and-braces — these stay effective even if step 25 is disabled or the operator picks a stricter profile.
- **Step 25** (new in v0.11.1): `tools.profile = "full"`. Same effective surface as the main agent. Operator-set values in `openclaw.json` are preserved.
- **Step 26** (new in v0.11.1): `<!-- patch-config:cron-tools:* -->` and `<!-- patch-config:browser-tools:* -->` blocks appended to `workspace-discord/AGENTS.md` so the agent reads them on session startup.

Env knobs: `OPENCLAW_DISCORD_AGENT_TOOLS_PROFILE` (enum `minimal | coding | messaging | full`, default `full`, empty string disables step 25), `OPENCLAW_DISCORD_AGENT_ALSO_ALLOW` (comma-separated, default `group:messaging,browser,tts,canvas`, empty string disables step 22).

### Discord slash-command authorization (issue #19310 dual perm check)

OpenClaw's Discord channel runs slash commands through a **dual perm check** that is hostile to the default config: (1) global `channels.discord.allowFrom` allowlist, AND (2) per-guild `channels.discord.guilds.<gid>.users` array. Both must match. The default `dmPolicy: "pairing"` implicitly satisfies (1) for DM contexts after the user pairs once, but guild contexts have no equivalent fallback — the `groupPolicy: "allowlist"` default + empty `users` array silently blocks every slash invocation. Discord renders the gateway's rejection as an ephemeral "You are not authorized to use this command" only the invoker can see, so the operator never gets a server-side log line they can grep for.

Symptom: `/discord input: hello`, `/talkvoice input: hello`, `/activation mode: always` work in DM, fail in guild. Confirmed in upstream issue #19310 ("[Bug] Discord Slash Commands Require Owner Configuration in Channels Despite Pairing"); upstream's stance is "operator must hand-edit allowFrom + per-channel users", no CLI shortcut.

The native slash UX is materially better than @mention text on this stack — Discord renders an immediate ack-dot "thinking…" indicator the moment the interaction is received, so the user never sees the dead-air gap that text-mention paths suffer from while the agent prefills (~1-5s) + generates (6-50 tok/s depending on backend). Operators want slash on every channel, not only DM.

Patcher step 28 fixes this by writing the open-guild defaults: `allowFrom = ["*"]`, `dmPolicy = "open"`, `groupPolicy = "open"`. Each field is user-managed-protected (only written when undefined), so hand-set operator values survive. Env knob `OPENCLAW_DISCORD_AUTHZ` accepts `open` (default), `allowlist` (skip the step entirely, preserve upstream defaults — for shared / multi-tenant / public guild deploys), or `owner-only` (lock to `OPENCLAW_DISCORD_OWNER_IDS` snowflakes, writes `allowFrom = [<ids>]` + both policies = `"allowlist"`).

Why open-guild as the default: this stack ships as a single-operator, self-hosted homelab deploy where the bot lives in the operator's own guild(s). The guild member list IS the trusted population — narrower allowlists add config burden without adding security. The `bootstrap.sh` 3f section asks once and saves the choice; re-runs preserve it via key-presence guard.

When debugging a "slash works in DM, fails in guild" report: check `cat $OPENCLAW_CONFIG_DIR/openclaw.json | jq '.channels.discord | {allowFrom, dmPolicy, groupPolicy}'`. If any of those is undefined, step 28 either didn't run (operator set `OPENCLAW_DISCORD_AUTHZ=allowlist`) or pre-existed in openclaw.json (user-managed protection respected an explicit value).

### Discord slash-command matrix and the two feature gates (`voice` + `threadBindings`)

OpenClaw splits its Discord slash surface across three feature buckets, and a slash command's "Discord availability" depends on which bucket it falls in:

1. **Always-on (gated by `commands.native`/`commands.text` globally — both default to enabled on this stack)**: `/help`, `/commands`, `/status`, `/whoami` (alias `/id`), `/tools`, `/tasks`, `/context`, `/usage`, `/model`, `/models`, `/think`, `/fast`, `/reasoning`, `/verbose`, `/queue`, `/steer`, `/skill <name>`, `/new`, `/reset`, `/stop`, `/compact`, `/export-session`, `/btw`, `/trace`, `/dock-discord`, `/dock-slack`, `/dock-telegram`, `/dock-mattermost`, `/subagents`, `/acp`, `/kill`, `/send`, `/approve`, `/activation mention|always`, `/tts`, `/voice`, `/talkvoice`, `/dreaming`, `/pair`, `/restart`. Plus the bundled-plugin natives `/discord input:` and `/codex …`.

2. **Owner-only (gated by `commands.<feature>` flags and `commands.ownerAllowFrom` snowflake list)**: `/config show|get|set|unset` (needs `commands.config: true`), `/mcp ...` (`commands.mcp: true`), `/plugins inspect|enable|disable` (`commands.plugins: true`), `/debug show|set|unset` (`commands.debug: true`), `/bash <cmd>` + `!cmd` shorthand (`commands.bash: true`), `/diagnostics`, `/export-trajectory`. On this stack `commands.bash: true` is on for the owner snowflake (the `!~/.openclaw/bin/img` bypass for image-gen — see `project_image_gen_v0_11_state.md`), the others stay off until you explicitly enable them.

3. **Discord-feature-gated (gated by `channels.discord.<feature>.enabled`)**: `/vc join|leave|status` (needs `channels.discord.voice.enabled: true`); `/focus`, `/unfocus`, `/agents`, `/session idle <duration|off>`, `/session max-age <duration|off>` (need `channels.discord.threadBindings.enabled: true`). Patcher step 29 enables both by default — env knobs `OPENCLAW_DISCORD_VOICE` (default `stt-tts`, alternatives `agent-proxy` / `bidi` / `off`) and `OPENCLAW_DISCORD_THREAD_BINDINGS` (default `on`, alternative `off`). Without step 29 these slash commands don't even register in Discord's autocomplete.

**Voice modes** — `stt-tts` is the only mode that works on this stack out of the box because the realtime alternatives (`agent-proxy`, `bidi`) need an OpenAI Realtime / equivalent provider that the bundle doesn't ship. The `stt-tts` mode chains the self-hosted faster-whisper (port 8093) for STT and the openclaw-tts-router (port 8090) for TTS — both already configured by patcher steps 11 + 14. When a user runs `/vc join` in a Discord voice channel, the bot connects with Connect + Speak permissions and runs the loop: hear → Whisper → agent → Kokoro/F5-TTS → speak. Higher latency than realtime but fully offline.

**Thread bindings** — they're opt-in per-thread, not automatic per agent. After step 29 enables them, the operator creates a thread in a guild channel, types `/focus <agent-or-target>`, and from that point follow-up messages in the thread route to the bound session. `/unfocus` releases the binding; `/session idle 30m` auto-releases after inactivity; `/session max-age 4h` hard-expires regardless. `/agents` shows current bindings. Useful when you want one Discord channel to host multiple parallel agent conversations (e.g. a research session in thread A, a coding session in thread B).

The patcher steps 20, 21, 22, 24, 24c, 25, 25c, 28, 29 collectively wire the Discord side from "out-of-the-box minimal" to "full-feature homelab assistant" — each step has a one-line console log when it writes, and `docker logs openclaw-config-init | grep '[patch-config]'` is the canonical way to confirm which features landed on a given install.

### `openclaw-base-ext` is the local image extension layer

Three openclaw services (`openclaw-config-init`, `openclaw-gateway`, `openclaw-cli`) all reference `openclaw-base-ext:${OPENCLAW_BASE_EXT_VERSION:-0.11.0}`, NOT the upstream `ghcr.io/openclaw/openclaw:${OPENCLAW_IMAGE_REF}` image directly. The local image is built by the `build:` block on `openclaw-config-init`, with `./openclaw-base-ext/` as context — a tiny Dockerfile that wraps the upstream tag and adds whatever the stack needs but the upstream image lacks. As of v0.11.0 that's just `apt-get install ffmpeg`, but the layer exists so future patches (custom node deps, system libs, locale fixes) have a clean home that doesn't fork the upstream image.

When you bump `OPENCLAW_IMAGE_REF` in `.env` (typically pin a new sha256 digest after upstream releases), run `docker compose build --no-cache openclaw-config-init` to rebuild the local extension on top of the new base. The other two services pick up the rebuilt tag on the next `up -d --force-recreate`. Don't put `build:` blocks on `openclaw-gateway` or `openclaw-cli` — that triggers redundant builds.

The reason this layer exists rather than upstream-style "use the official image": the gateway's Discord text-channel TTS-attachment path shells out to `ffmpeg` to transcode wav into Opus/mp3 for the Discord upload API. The upstream image doesn't ship ffmpeg — historically operators had to set `OPENCLAW_TTS_AUTO=tagged` to avoid the silent crash. With the local extension, `OPENCLAW_TTS_AUTO=always` works on every surface (default since the patcher's step-11 default).

## Verification recipes (copy-paste ready)

These cover the cases that have actually broken in practice. When making non-trivial changes, run the relevant ones before declaring done.

```bash
# 1. Compose + patcher syntax
node --check patch-config.mjs
docker compose --env-file .env config --services                # lists 9 default services
docker compose --env-file .env --profile hu config --services   # 10 with HU opt-in active
docker compose --env-file .env --profile browser config --services  # 10 with browser opt-in
docker compose --env-file .env --profile hu --profile browser config --services  # 11 (both)

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
  --thinking off --json --timeout 600 \
  | jq '.toolSummary, .finalAssistantVisibleText'
                                                        # → tools: ["web_search"], failures: 0
                                                        # → "TITLE: Docker: …"

# 3. Hybrid memory smoke
docker exec ${PROJ}openclaw-cli sh -c \
  'mkdir -p ~/.openclaw/workspace/memory && \
   echo "Gemma 4 26B-A4B MoE NVFP4 runs at ~52 tok/s on GB10." > ~/.openclaw/workspace/memory/test.md'
docker exec ${PROJ}openclaw-cli openclaw memory index --force
docker exec ${PROJ}openclaw-cli openclaw memory search "How fast is Gemma on GB10?"
                                                        # → score >0.4, returns test.md
```

## When in doubt

- For runtime/config questions, read `docs/ARCHITECTURE.md` first.
- For "how do I change X" questions, read `docs/CUSTOMIZATION.md`.
- For deeper reference material (OpenClaw 3-store credential layout, patcher step-by-step, LLM / TTS research, reusable Docker patterns), read `docs/reference/` — see `docs/reference/README.md` for the index.
- For OpenClaw-specific behavior (CLI flags, plugin schema, gateway protocol), check the upstream docs at https://docs.openclaw.ai/.
- For vLLM-specific questions (chat template, tool parser, NVFP4 kernels), check the upstream vLLM repo and the gemma4 image notes.
- For hardware tuning, the GB10 numbers in `README.md` and `.env.example` are measured, not theoretical — they're a useful baseline when adapting to other GPUs.
