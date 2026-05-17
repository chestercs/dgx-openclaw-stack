# CLAUDE.md

Guidance for Claude Code (and other coding agents) working on this repository.

## What this repo is

A single-file Docker Compose stack that brings up a self-hosted, OpenAI-compatible LLM
(Gemma 4 26B-A4B MoE NVFP4 on `vllm-llm:8004` and Gemma 4 31B IT NVFP4 dense on
`vllm-llm-dense:8005` running side by side; pick either via the OpenClaw UI), a
multilingual embedding service (bge-m3), the OpenClaw agent gateway, a privacy-first
SearxNG meta-search backend, a multilingual TTS surface (Fish Audio S2 Pro on
`openclaw-tts-fish:8080`, served by SGLang-Omni, 80+ languages including EN+HU with
voice cloning from mounted reference clips), and a Whisper STT backend
(`faster-whisper` turbo CT2, EN + HU autodetect). Calibrated for NVIDIA GB10
(DGX Spark / ASUS Ascent), portable to other hardware via documented `.env` overrides.

The repo's value proposition is the **wiring**, not any individual component:

- Model + embedding + gateway + memory + web search + TTS/STT + browser + image-gen
  are pre-integrated and survive upgrade cycles.
- An idempotent config patcher (`patch-config.mjs`) keeps `openclaw.json` in a
  deterministic, production-ready state across re-runs of the onboarding wizard.
- A bootstrap script (`bootstrap.sh`) handles secret rotation, host paths, and
  prerequisite checks non-destructively.

## Repo layout

```
docker-compose.yml             # default + opt-in profiles (browser, python, dense)
patch-config.mjs               # idempotent openclaw.json patcher (init container)
bootstrap.sh                   # first-time setup: secrets, .env, host dirs
bootstrap-browser-login.sh     # 1x OAuth onboarding helper (noVNC)
rotate-secrets.sh              # rotate gateway / service tokens
.env.example                   # tunables, every knob commented
templates/                     # vLLM tool-calling chat template (gemma4)
searxng/settings/              # SearxNG override settings (privacy posture)
openclaw-tts-fish/             # Fish Audio S2 Pro (Research License, non-commercial)
                               #   SGLang-Omni serving fishaudio/s2-pro on CUDA 13
openclaw-stt-whisper/          # faster-whisper turbo CT2 (CUDA 13)
openclaw-browser/              # Playwright Chromium over CDP       — profile=browser
openclaw-python-sandbox/       # Python MCP exec sandbox
openclaw-image-comfyui/        # ComfyUI MCP bridge (separate compose)
openclaw-base-ext/             # local extension layer for the openclaw image
vllm-llm/                      # vllm-openai image with Gemma4 parser patch
docs/                          # ARCHITECTURE / CUSTOMIZATION / TROUBLESHOOTING
  reference/                   # deeper reference — see docs/reference/README.md
SETUP.md                       # end-user first-boot walkthrough
README.md                      # audience-facing pitch + quickstart
CHANGELOG.md                   # release notes
```

## Working principles

### Patcher is the source of truth, not openclaw.json

Never tell users to hand-edit `openclaw.json`. The `openclaw-config-init` container runs
`patch-config.mjs` before every `up` and re-applies the desired state. If a change
should persist, add or modify a step in `patch-config.mjs` (deep-merge style — read
existing values, only write when they differ, log every change).

The patcher's contract:

- Skip cleanly (`exit 0`) when `openclaw.json` doesn't exist (pre-onboarding fresh
  install).
- Never overwrite user-managed fields (custom `agents.list[]` entries, channel
  credentials, etc.).
- Always log a `[patch-config]` line for each change so users can audit what shifted.

### Two-phase fresh-install onboarding

A brand-new install always proceeds in two phases:

1. `docker compose up -d` starts every service. The patcher skips because
   `openclaw.json` doesn't exist yet, so the gateway crash-loops with `Missing config.
   Run openclaw setup …`. **This is the intended state**, not a bug.
2. The user completes onboarding (Chrome extension wizard,
   `openclaw onboard --non-interactive …`, or `openclaw setup`), which writes
   `openclaw.json` to the config volume.
3. `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli`
   re-runs the patcher against the now-existing config; the gateway picks up the
   patched file and goes healthy.

Don't try to "fix" the crash-loop in step 1 by patching defaults into a freshly created
`openclaw.json` — the OpenClaw security model requires explicit onboarding so the
operator chooses the gateway token and pairs the UI before the gateway accepts
connections. Skipping that step would weaken the auth defaults.

### Defaults assume GB10, but env overrides keep the repo portable

The reference profile targets a 128 GB GB10 box. Three documented portability paths via
`.env` overrides:

- **Different NVIDIA GPU**: change `--model` / `LLM_GPU_MEM_UTIL` / `LLM_MAX_NUM_SEQS`
  in `.env`, swap the vLLM image if not Blackwell.
- **Remote vLLM backend** (gateway local, LLM elsewhere): set `OPENAI_BASE_URL` +
  `LLM_BASE_URL` + `EMBED_BASE_URL` in `.env`, add `profiles: ["never"]` to the local
  `vllm-llm` / `vllm-embedding` services. See `docs/CUSTOMIZATION.md` → "Run with a
  remote vLLM backend".
- **Cloud LLM endpoint** (Bedrock proxy, OpenRouter, …): same as the remote-vLLM path,
  but the URLs point at a cloud service.

When adding new tunables, follow the existing pattern: `${VAR:-sensible_default}` in
compose, `process.env.VAR || 'sensible_default'` in the patcher, and a `.env.example`
entry with a one-paragraph comment explaining the trade-off.

### Comments earn their place

Inline comments in this repo follow a high bar — they explain *why* (a constraint, a
non-obvious gotcha, a benchmark number, an OpenClaw-specific behavior), not *what*.
The compose file and patcher are heavily commented for end users; the goal is that
someone debugging at 2am can understand each block without leaving the file.

When you add a step to the patcher or a service to the compose file, write a comment
that helps that 2am debugger. When in doubt, model your comment on the surrounding
ones — they're the standard.

### Verify before declaring done

A change that touches the patcher or compose file isn't complete until:

1. `node --check patch-config.mjs` passes.
2. `docker compose config` parses cleanly with a representative `.env`.
3. (For non-trivial changes) bring the stack up on a test host, run
   `openclaw memory status`, `openclaw agent --agent main --message "…"`, and
   `curl <gateway>/healthz`.

The repo's quality bar is "real verification on a real host," not "syntax-checks
only."

### Documentation language: English for anything public

Every file that lands in this public repo — `README.md`, `SETUP.md`, `CLAUDE.md`,
`CHANGELOG.md`, `CONTRIBUTING.md`, everything under `docs/`, `.github/*`, compose /
patcher inline comments, commit messages, PR and issue templates, GitHub Release notes
— is written in English. This is a hard rule, not a preference: the repo targets a
global audience, and mixing languages splits the readership.

If you're importing or adapting material from a non-English source (a private
knowledge base, a Hungarian research note, a vendor doc), translate it before
committing.

## Things to avoid

- **Don't bypass the patcher** by writing to `openclaw.json` directly from a script or
  service entrypoint. The patcher's deep-merge style survives OpenClaw schema
  migrations.
- **Don't add new services without thinking about the bridge network.** All services
  on the default compose bridge can reach each other by service name. New services
  that need LAN exposure should publish ports explicitly; new internal services should
  not.
- **Don't add backwards-compatibility shims for old OpenClaw versions** unless you're
  sure the older version is in active use. The repo tracks the latest stable OpenClaw
  image.
- **Don't ship interactive prompts in scripts that could be CI-driven.** `bootstrap.sh`
  prompts for secrets but also accepts pre-set `.env` values and skips. New scripts
  should follow the same pattern.
- **Don't generate URLs you haven't verified.** Broken links in `docs/CUSTOMIZATION.md`
  are worse than no link at all.

## Useful one-liners

These snippets assume `${PROJ}` holds the container-name prefix (default `dgx-`, set
via `CONTAINER_NAME_PREFIX` in `.env`). Source it once per shell:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)
PROJ=${PROJ:-dgx-}
```

```bash
# Re-run the patcher against the live openclaw.json
docker exec ${PROJ}openclaw-config-init node /opt/patch-config.mjs

# Inspect the live openclaw.json
cat $OPENCLAW_CONFIG_DIR/openclaw.json | \
  jq '.models.providers.vllm, .agents.defaults.memorySearch, .plugins.entries.searxng.config.webSearch'

# Test SearxNG JSON API from inside the gateway namespace
docker exec ${PROJ}openclaw-cli curl -sS "http://searxng:8080/search?q=test&format=json" | jq '.results | length'

# Memory hybrid search smoke test
docker exec ${PROJ}openclaw-cli openclaw memory status --deep
docker exec ${PROJ}openclaw-cli openclaw memory search "your query"

# Multi-tool agent run via the gateway. Always use --timeout 600 for tool-using
# examples; the default 60s trips on cold cache. See docs/reference/llm-stack.md.
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Use web_search to find …" --thinking off --json --timeout 600
```

## Implementation gotchas — quick index

Every non-obvious bit that bit somebody once and ended up shaping the design has been
moved to a focused reference doc. The bullets below are the index — read the linked
file before changing the area.

**Network / Docker patterns** → [`docs/reference/patterns.md`](docs/reference/patterns.md)

- Container name prefix is configurable (`CONTAINER_NAME_PREFIX`, default `dgx-`);
  bridge DNS resolution is independent of `container_name`.
- `openclaw-cli` shares the gateway's network namespace via `network_mode:
  "service:openclaw-gateway"` — recreate the CLI in the same `docker compose up
  --force-recreate` as the gateway, or the CLI ends up in a dead namespace.
- `profiles: ["never"]` parks a service without removing the documentation. Don't
  comment out service blocks.
- Volume bind mounts run as UID/GID `1000:1000` (OpenClaw image's `node` user); the
  host dirs must be writable by that UID.
- SearxNG `use_default_settings.engines.keep_only` is a registry filter, not an
  enable flag — engines shipped `disabled: true` need an explicit per-engine override.
- `bootstrap.sh`'s `upsert_env` is regex-gated (`^CHANGE_ME`), so re-runs never
  overwrite real values.
- HF cache volume label is configurable (`VLLM_HF_CACHE_VOLUME_NAME`) for sibling-
  stack sharing.

**OpenClaw internals** → [`docs/reference/openclaw-internals.md`](docs/reference/openclaw-internals.md)

- 3-store credential layout: keys live in the per-agent auth-profile store, not in
  `openclaw.json`'s plaintext `apiKey` field. Patcher step 13 syncs them.
- `OPENCLAW_LAN_CIDR` controls `gateway.trustedProxies` — add your LAN range when
  clients hit the gateway directly (not via reverse proxy).
- `restart: unless-stopped` × `OPENCLAW_NO_RESPAWN=1` — keep both in sync.
- `NODE_COMPILE_CACHE` lives inside the config volume to survive `down`.
- Heartbeat fires from `~/.openclaw/workspace/HEARTBEAT.md`; empty file → skipped.
- `memorySearch.sources` defaults to `workspace/memory/*.md`; embedding dimension is
  fixed when the first chunk is indexed.
- The patcher writes baseUrls with trailing slashes (`v1/`); `OPENAI_BASE_URL` does
  NOT take a trailing slash.
- `openclaw-base-ext` is the local image extension layer (adds `ffmpeg`); rebuild
  with `docker compose build --no-cache openclaw-config-init` when bumping
  `OPENCLAW_IMAGE_REF`.
- Per-agent `~/.openclaw/agents/<id>/agent/models.json` is a stale-prone cache of
  `models.providers.*` — the image / vision tool reads from it, not from live
  config. When you change a provider's `baseUrl` (or a service like the historical
  `vllm-llm-proxy` is removed), delete `agents/*/agent/models.json` so the next
  agent run regenerates it from current config. Text generation is unaffected
  (it reads `models.providers` directly).

**LLM stack (vLLM + Gemma 4 NVFP4)** → [`docs/reference/llm-stack.md`](docs/reference/llm-stack.md)

- Two backends side-by-side: MoE 26B-A4B on port 8004 (default), dense 31B-IT on port
  8005 (opt-in via `profiles: ["dense"]`).
- vLLM tool-call template ships in `templates/`, not in the image; bind-mounted at
  `/templates:ro` with `--chat-template /templates/tool_chat_template_gemma4.jinja`.
- HuggingFace token is exposed under both `HUGGING_FACE_HUB_TOKEN` and `HF_TOKEN`.
- vLLM ports are NOT published by default (sibling containers reach via bridge DNS);
  enable host debug access by uncommenting `127.0.0.1:8004:8004`.
- Multi-step tool-call runs need `--timeout 600` — default 60s trips on cold cache.
  Always document tool-using examples with `--timeout 600`.

**TTS surface (Fish Audio S2 Pro + SGLang-Omni)** → [`openclaw-tts-fish/README.md`](openclaw-tts-fish/README.md)

- ONE container with two processes: a FastAPI shim on `:8080` (auth +
  voice→references mapping + onset silence pad) wrapping the SGLang-Omni
  native HTTP server on loopback `:9090`. Replaces the legacy 3-service
  Kokoro EN + F5-TTS HU + router pipeline (whose reference doc is now
  SUPERSEDED — see [`docs/reference/tts-stack.md`](docs/reference/tts-stack.md)).
- Loader is `sgl-project/sglang-omni`, NOT the legacy `fishaudio/fish-speech`
  `tools/api_server.py` (which targets the 1.x LLaMA2 arch and does NOT load
  the s2-pro Qwen3-omni checkpoint).
- Upstream `frankleeeee/sglang-omni:dev` is amd64-only — we build a custom
  image on `nvidia/cuda:13.0.0-cudnn-devel-ubuntu24.04` so sgl-kernel can
  compile from source on aarch64 + sm_120.
- Voice cloning is via mounted file paths, **NOT inline base64** (SGLang-Omni
  upstream schema accepts `references[].audio_path`). The shim resolves the
  OpenAI-style `voice` field to `/app/voices/<voice>.{wav,txt}` at request
  time. `docker cp <name>.{wav,txt}` adds a voice without restart.
- License: **Fish Audio Research License — non-commercial**. The image bake
  downloads `fishaudio/s2-pro` weights (~11 GB) and constitutes acceptance.
  Wrapper code in `openclaw-tts-fish/server/` is MIT.
- Patcher step 11 writes three things: top-level
  `messages.tts.{enabled,auto,mode}`, `providers.openai`, and `voiceAliases`.
  Without the top-level switches, voice surfaces silently treat TTS as off.
- Web chat UI uses the browser's native `speechSynthesis` and does NOT call
  the shim (upstream OpenClaw limitation).
- `TTS_FISH_LEADING_SILENCE_MS=300` defends against the Whisper STT onset
  clip ("Szia" → "Zia"). Done in-process via soundfile + numpy WAV splice,
  no ffmpeg shell-out in the hot path.

**STT (Whisper turbo)** → [`docs/reference/stt-stack.md`](docs/reference/stt-stack.md)

- One service (`openclaw-stt-whisper`), self-built CUDA 13 image, ~150 LOC
  FastAPI wrapper around `faster-whisper`. Default model is
  `deepdml/faster-whisper-large-v3-turbo-ct2` (~8× faster than vanilla
  large-v3, pruned 4-layer decoder, ~1.6 GB VRAM). Swap to
  `Trendency/whisper-large-v3-hu` via `STT_WHISPER_MODEL` for the HU finetune
  (slower, higher accuracy on noisy HU mic input).
- Wired via `tools.media.audio.models[]` (NOT `messages.stt` — that schema
  name doesn't exist in OpenClaw). Auth lives in per-entry
  `headers.Authorization`.
- Whisper autodetects language per request — no router needed.

**Browser automation** → [`docs/reference/browser-automation.md`](docs/reference/browser-automation.md)

- CDP-attach via `browser.profiles.<name>.cdpUrl`, port-per-profile, query-string
  token (`?token=…` — OpenClaw's cdpUrl field accepts that or HTTP Basic, NOT
  Authorization headers).
- WebAuthn / passkeys do NOT work over the noVNC login helper (origin-bound).
  Document this on any onboarding flow.

**Image-gen bridge** → [`docs/reference/image-comfyui-bridge.md`](docs/reference/image-comfyui-bridge.md)

- Separate compose file, joined to the main stack's bridge via `external: true`.
- Host-gateway hop to the operator's existing ComfyUI (no shared external network
  needed).
- Model-agnostic — the repo ships NO model weights; workflows use
  `"REPLACE_ME.safetensors"` placeholders.
- Chat-side inline image render is blocked at the browser security layer (markdown
  sanitizer + cross-origin Basic auth). Recommended UX: user opens the
  `display_markdown` URL in a new tab. Same-origin canvas path is the workaround.

**Discord integration** → [`docs/reference/discord-config.md`](docs/reference/discord-config.md)
(operator config / patcher overrides) and [`docs/reference/discord-text-agent.md`](docs/reference/discord-text-agent.md)
(agent design) / [`docs/reference/discord-voice-agent.md`](docs/reference/discord-voice-agent.md) (voice)

- The patcher writes 11 Discord-related fields (steps 20-30). All env-gated. The
  "At a glance" table in `discord-config.md` lists every override, the default value,
  and the env knob that disables it / restores vanilla upstream behaviour.
- Override categories: bug workarounds (20, 21), UX improvements for slow LLMs (24),
  capability widening for the Discord-routed agent (22, 25), doc-side cheatsheets
  (26, 27), homelab policy choices (28, 29, 30).
- Two layers that are easy to conflate: `channels.discord.guilds["*"].requireMention`
  is the **gate** (config); `/activation mention|always` is the **LLM behaviour hint**
  inside the gate. See `discord-config.md` → "Discord mention gate vs `/activation`
  slash" before debugging *"slash doesn't behave like I thought"*.

## Verification recipes

These cover cases that have actually broken in practice. Run the relevant ones before
declaring a non-trivial change done.

```bash
# 1. Syntax checks
node --check patch-config.mjs
docker compose --env-file .env config --services
docker compose --env-file .env --profile hu config --services
docker compose --env-file .env --profile browser config --services

# 2. End-to-end on a test host (after `docker compose up -d` and onboarding)
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}

curl -sS http://127.0.0.1:18789/healthz                # → {"ok":true,"status":"live"}

docker exec ${PROJ}openclaw-cli openclaw memory status --deep \
  | grep -E "Provider|Vector dims|Embeddings"          # all "ready", dims = 1024 for bge-m3

docker exec ${PROJ}openclaw-cli sh -c \
  'curl -sS "http://searxng:8080/search?q=docker&format=json"' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('results:', len(d['results']))"

docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Use web_search to find the title of docker.com. Reply TITLE: <title>" \
  --thinking off --json --timeout 600 \
  | jq '.toolSummary, .finalAssistantVisibleText'

# 3. Hybrid memory smoke
docker exec ${PROJ}openclaw-cli sh -c \
  'mkdir -p ~/.openclaw/workspace/memory && \
   echo "Gemma 4 26B-A4B MoE NVFP4 runs at ~52 tok/s on GB10." > ~/.openclaw/workspace/memory/test.md'
docker exec ${PROJ}openclaw-cli openclaw memory index --force
docker exec ${PROJ}openclaw-cli openclaw memory search "How fast is Gemma on GB10?"
```

## When in doubt

- For runtime / config questions, read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
  first.
- For "how do I change X" questions, read
  [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md).
- For deeper reference material (3-store credential layout, patcher step-by-step,
  Discord override table, LLM/TTS research, reusable Docker patterns), read
  [`docs/reference/`](docs/reference/) — start with
  [`docs/reference/README.md`](docs/reference/README.md) for the index.
- For OpenClaw-specific behaviour (CLI flags, plugin schema, gateway protocol), check
  the upstream docs at <https://docs.openclaw.ai/>.
- For vLLM-specific questions (chat template, tool parser, NVFP4 kernels), check the
  upstream vLLM repo and the gemma4 image notes.
- For hardware tuning, the GB10 numbers in `README.md` and `.env.example` are
  measured, not theoretical — they're a useful baseline when adapting to other GPUs.
