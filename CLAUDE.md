# CLAUDE.md

Guidance for Claude Code (and other coding agents) working on this repository.

## What this repo is

A Docker Compose stack that runs a self-hosted OpenClaw agent on one NVIDIA GB10
(DGX Spark / ASUS Ascent GX10): a local LLM (Nemotron 3.5 Super 120B-A12B NVFP4 on
`vllm-llm:8004`), bge-m3 embeddings for memory search, SearxNG web search, Whisper
speech-to-text, and the OpenClaw gateway with a Discord bot. Other models, TTS and the
media tools are optional compose files. The defaults are calibrated for the GB10 and
can be moved to other hardware through `.env`.

The value is the **wiring**, not any single component:

- Model, embeddings, gateway, memory, web search, STT, browser and sandbox are
  pre-integrated and survive OpenClaw upgrades.
- `config-init/` brings `openclaw.json` and the agent workspaces to the desired state
  from `.env` on every `docker compose up`, and a re-run changes nothing.
- `bootstrap.sh` fills secrets, host paths and prerequisites without overwriting real
  values.

How the parts fit together: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Every
`.env` knob: [`docs/reference/configuration.md`](docs/reference/configuration.md).

## Repo layout

```
docker-compose.yml          # the stack; profiles: browser, python, meshcore
compose/                    # optional files added via COMPOSE_FILE:
                            #   gemma4.yml (+ gemma4/ chat template), mistral.yml,
                            #   tts-fish.yml, claw-img.yml
config-init/                # writes openclaw.json + workspaces (openclaw-config-init)
  patch-config.mjs          #   entry: runs the steps in order
  steps/                    #   models, memory, gateway, tools, discord, agents,
                            #   workspaces, media, plugins
  lib/                      #   env.mjs (typed knobs), config.mjs (logged writes,
                            #   secret masking), files.mjs (marker blocks, skills,
                            #   templates), secrets.mjs (env SecretRefs)
  templates/                #   prompt text: discord/AGENTS.d blocks, router
                            #   fragments, skills, agent seeds
  test/                     #   run a patcher on a copy of live state and diff
openclaw-base-ext/          # upstream OpenClaw image + ffmpeg/git/curl,
                            #   people-digest.mjs and the local plugins:
  plugins/                  #   people-context, claim-guard, dm-guild-gate,
                            #   mcp-image-saver
openclaw-stt-whisper/       # faster-whisper turbo (CUDA 13)
openclaw-browser/           # Chromium over CDP                  profile=browser
openclaw-python-sandbox/    # Python MCP sandbox                 profile=python
openclaw-meshcore-bridge/   # LoRa mesh bridge                   profile=meshcore
openclaw-tts-fish/          # Fish Audio S2 Pro TTS (compose/tts-fish.yml)
openclaw-image-comfyui/     # ComfyUI image/video/music MCP bridge (own compose)
openclaw-claw-img-bot/      # /claw-img Discord bot (compose/claw-img.yml)
openclaw-host-client/       # CLI container on the operator's PC for a remote gateway
searxng/settings/           # SearxNG engine set and privacy settings
scripts/                    # host-side ops: GB10 clock cap, thermguard, memguard,
                            #   llm-ensure, netns watcher, daily status, upgrade,
                            #   web search bench, resharding, LTX install, benches
bootstrap.sh                # first-time setup: secrets, .env, host dirs
bootstrap-browser-login.sh  # one-time login of a browser profile (noVNC)
rotate-secrets.sh           # rotate the generated secrets
.env.example                # the ~25 settings a typical install sets
docs/                       # ARCHITECTURE, CUSTOMIZATION, TROUBLESHOOTING,
                            #   OPENCLAW_UPGRADE, discord-bot-setup
  reference/                #   depth per area; index in reference/README.md
  archive/                  #   superseded material and the old changelog
SETUP.md                    # first-boot walkthrough
README.md                   # pitch + quickstart
CHANGELOG.md                # release notes
```

## Working principles

### config-init is the source of truth, not openclaw.json

Never tell users to hand-edit `openclaw.json`. The `openclaw-config-init` container
runs `config-init/patch-config.mjs` before every `up` and re-applies the desired state.
A change that should persist goes into the matching `config-init/steps/<area>.mjs`,
through the `Config` helpers in `lib/config.mjs` (compare first, write only on a
difference, one `[patch-config]` log line per change, secrets masked). Prompt text goes
into `config-init/templates/`, not into JavaScript strings.

The contract:

- No `openclaw.json` yet (before onboarding): exit 0.
- Write the current OpenClaw schema (2026.10: keyed `agents.entries`, `memory.search`,
  `tts`, `tools.media.models`, `exec.mode`). A config that still has `agents.list`
  makes config-init stop and ask for `openclaw doctor --fix`.
- Never touch what the operator owns: their own agents, channel credentials, text
  outside the `<!-- patch-config:<id>:start/end -->` marker blocks in `AGENTS.md`.
  Skill files (`skills/<name>/SKILL.md`) are wholly config-init-owned; agent seeds
  are written once and then belong to the operator.
- Names and IDs in prompts come from `.env` (`OPENCLAW_DISCORD_MEMBER_NAMES`, ...),
  so the public templates carry none.
- Credentials go in through `secret()` (`lib/secrets.mjs`) where the field takes a
  SecretRef: `openclaw.json` then holds `{ source: "env", id: NAME }` and the gateway
  resolves it from its own environment. A new key handled this way must also be passed
  in the gateway's `environment:` in `docker-compose.yml`. The OpenClaw secret store is
  not an option: only a process in the gateway's PID namespace may write it, and
  config-init runs beside the old gateway during a recreate.

### Two-phase fresh-install onboarding

1. `docker compose up -d` starts every service. config-init exits 0 because
   `openclaw.json` does not exist yet, so the gateway crash-loops with
   `Missing config. Run openclaw setup …`. **This is the intended state.**
2. The operator onboards (`openclaw onboard --non-interactive …` or `openclaw setup`),
   which writes `openclaw.json`.
3. `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli`
   applies the desired state and the gateway goes healthy.

Do not "fix" step 1 by writing a default `openclaw.json`: onboarding is where the
operator chooses the gateway token and pairs the UI.

### Defaults assume GB10, `.env` keeps it portable

New tunables follow one pattern: `${VAR:-default}` in compose, an `env.*` reader with
a default in config-init (`lib/env.mjs`: `on/off`, empty = default), and a row in
`docs/reference/configuration.md`. `.env.example` lists only what a typical install
sets; do not grow it with every knob. Another model is a file in `compose/` plus
`LLM_MODEL_ID`; a remote or cloud backend is `LLM_BASE_URL` / `EMBED_BASE_URL` (see
`docs/CUSTOMIZATION.md`).

### Comments earn their place

Comments explain *why*: a constraint, a measured number, an OpenClaw behaviour that
is not obvious. Not *what*, and not history. Someone debugging at 2am should
understand a block without leaving the file. Model new comments on the surrounding
ones.

### Verify before declaring done

A change to config-init or compose is not done until:

1. `node --check` passes on every config-init file.
2. `docker compose config` parses with a representative `.env` (and the profiles and
   `compose/` files the change touches).
3. The test harness (`config-init/test/`) runs the new config-init on a copy of the
   live state and the diff against the current one shows only the intended changes.
4. For non-trivial changes: the stack comes up on a test host, `curl <gateway>/healthz`
   is ok, and a real agent turn (`openclaw agent …`, or a Discord turn for
   Discord-only hooks) behaves.

The bar is "real verification on a real host", not "syntax checks only".

### English for anything public

Every tracked file (docs, compose and code comments, templates' instructions, commit
messages, PR and issue templates, release notes) is in English. Hungarian appears only
where it is data: example user phrases the bot must recognise, and the language the bot
answers in. Translate imported material before committing.

## Things to avoid

- **Writing `openclaw.json` from anywhere but config-init** (a script, an entrypoint).
- **Patching OpenClaw's bundled files.** The image adds tools and plugins only; plugins
  use the plugin SDK and load through `plugins.load.paths`. Find a hook or a
  documented config key instead.
- **Registering a `reply_payload_sending` or `message_sending` hook.** Any such hook
  turns off Discord preview streaming for every Discord turn.
- **Unbounded or guessed keys under `agents.defaults.*`.** An unknown key or an
  out-of-range value fails validation and the gateway crash-loops. Gate new keys on a
  knob (empty = skip, `off` = remove) and check the schema first (set it in the Web UI,
  save, read the persisted JSON).
- **New services without thinking about the bridge network.** Every service on the
  compose bridge reaches the others by name. Publish ports only for LAN-facing services,
  and bind them to `127.0.0.1` by default.
- **Commenting out services.** Use a profile or a `compose/` file.
- **Backwards-compatibility shims for old OpenClaw versions.** The repo tracks the
  release pinned in `OPENCLAW_IMAGE_REF`.
- **Interactive prompts in scripts that could run in CI.** Accept pre-set `.env`
  values and skip, like `bootstrap.sh`.
- **URLs you have not verified.**

## Useful one-liners

Container names have an optional prefix (`CONTAINER_NAME_PREFIX`, empty by default):

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)

# Apply the desired state to the live config (config-init exits after the run).
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli

# Inspect parts of the live config (SecretRef'd keys show as env references).
jq '.models.providers.vllm.models, .memory.search.model, .plugins.entries.searxng.config.webSearch' \
  "$(grep '^OPENCLAW_CONFIG_DIR=' .env | cut -d= -f2)/openclaw.json"

# SearxNG from inside the gateway namespace.
docker exec ${PROJ}openclaw-cli curl -sS "http://searxng:8080/search?q=test&format=json" \
  | jq '.results | length, .unresponsive_engines'

# Memory search.
docker exec ${PROJ}openclaw-cli openclaw memory status --deep
docker exec ${PROJ}openclaw-cli openclaw memory search "your query"

# A tool-using agent turn. Always --timeout 600: the default 60 s trips on a cold cache.
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Use web_search to find …" --thinking off --json --timeout 600
```

Do not `jq` whole `tools.*` or `mcp.*` subtrees into a log or a chat: per-entry
`headers.Authorization` values sit there.

## Gotchas by area

Read the linked doc before changing the area.

- **Docker and networking** → [`docs/reference/patterns.md`](docs/reference/patterns.md).
  `openclaw-cli` and `openclaw-meshcore-bridge` share the gateway's network namespace;
  a recreated gateway leaves them in a dead namespace, so recreate them together, and
  run `scripts/netns-dependents-up.sh --watch` from cron for reboots. Bind mounts must
  be writable by UID 1000. The gateway uses public DNS (`dns:`) because npm hit
  `EAI_AGAIN` on Docker's resolver.
- **OpenClaw schema and internals** → [`docs/reference/openclaw-internals.md`](docs/reference/openclaw-internals.md).
  Agents are keyed under `agents.entries.<id>`; `agents.defaults.model` may be a bare
  string. The per-agent `agents/<id>/agent/models.json` caches providers and the image
  tool reads it: after a `baseUrl` change, delete it so it regenerates. The `cron` tool
  is now `automations` (`cron` stays an alias). Unset `tools.toolSearch` means "on";
  config-init turns it off. Measure an agent's real tool catalog with
  `gateway call tools.effective` before adding tools. `OPENCLAW_LAN_CIDR` feeds
  `gateway.trustedProxies`. `restart: unless-stopped` and `OPENCLAW_NO_RESPAWN=1` go
  together.
- **Plugins** → the header comment of each `openclaw-base-ext/plugins/*/index.js` and
  [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Non-bundled plugins need
  `hooks.allowConversationAccess` for conversation hooks. A load path missing from the
  image fails the gateway's plugin scan (config-init removes it). `openclaw plugins
  reload` cannot replace a plugin while a run holds it: rebuild the image and recreate.
- **Memory** → [`docs/reference/discord-config.md`](docs/reference/discord-config.md).
  The root `MEMORY.md` is never injected into group/channel sessions, so guild
  long-term memory is `workspace-discord/people/MEMORY.md`, loaded by the
  `bootstrap-extra-files` hook. The bot writes it only through
  `/opt/people-digest.mjs note|forget` (people-context blocks direct edits; its own
  `write` once replaced the whole file). Check what a session really loads with
  `systemPromptReport.injectedWorkspaceFiles` in `openclaw agent --json`. `openclaw
  agent` has no sender, so test people-context on a real Discord turn. The embedding
  dimension is fixed when the first chunk is indexed; an upgrade or rollback can lose
  the index metadata (`openclaw memory index --force`).
- **claim-guard** (`before_agent_finalize`): the host model judges which actions the
  answer claims and checks them against the run's successful tool calls; a second,
  separate question flags uncommon facts stated without a look-up. Keep the two
  questions separate (merged, the fact rules leaked into the action verdicts) and do
  not go back to claim-word regexes (they missed "jegyeztem"). Active Memory recall
  runs are skipped. Audit `logs/claim-guard.jsonl` (`none` / `revise` / `resolved` /
  `unresolved` / `judge_failed`).
- **dm-guild-gate** (`before_dispatch`): a DM is answered only when the sender shares a
  guild with the bot (Discord REST, cached); `dmPolicy` has no such mode. Audit
  `logs/dm-guild-gate.jsonl`.
- **LLM stack** → [`docs/reference/llm-stack.md`](docs/reference/llm-stack.md).
  vLLM serves one model id; `LLM_MODEL_ID` must match the compose file in use, and
  config-init drops every other `vllm/...` entry. A local model only thinks when its
  catalog entry has the right `compat` (`LLM_THINKING_COMPAT`, auto by model id).
  Nemotron needs `--default-chat-template-kwargs '{"medium_effort": true}'` (full
  thinking ran 57k-token turns, off looped on tool JSON) and temperature 0.6 for
  Hungarian. Mistral Small 4 needs `TRITON_MLA` on GB10 and its EAGLE draft does not
  load on vLLM 0.31/0.32.
- **GB10 power, thermals, memory** → [`docs/reference/gb10-power-thermal.md`](docs/reference/gb10-power-thermal.md).
  Under sustained load the box powers off with no journal entry at acpitz ~94 °C;
  the fix is the clock cap (`scripts/gb10-clock-cap.service`, `-pl` is N/A on GB10)
  with `scripts/gb10-thermguard.sh` as backstop. A 611 MHz / ~10 W stuck GPU after a
  crash needs a cold drain. A model load can livelock the unified pool (no OOM kill,
  no SSH): `scripts/gb10-memguard.sh`, `scripts/gb10-resilience-setup.sh`,
  `scripts/reshard-safetensors.py`; `scripts/llm-ensure.sh` restarts `vllm-llm` after
  a guard killed it. Do not start two big GPU services sizing memory at the same time.
- **Discord** → [`docs/reference/discord-config.md`](docs/reference/discord-config.md),
  [`discord-text-agent.md`](docs/reference/discord-text-agent.md).
  `guilds["*"].requireMention` is the gate; `/activation` is only a prompt hint inside
  it. Once a guild entry has a `channels` map, an unlisted channel is silent, so a
  `"*"` fallback is written with it. Snowflakes stay strings; owners are written
  `discord:<id>` and the heartbeat target `user:<id>`, or the DM finds no route.
- **Agentic coding** → [`docs/reference/agentic-coding.md`](docs/reference/agentic-coding.md).
  `OPENCLAW_EXEC_SECURITY=allowlist` (exec mode `ask`, approvals as Discord DMs) or
  `full` (no gate at all). Approvers are never `"*"`. AGENTS.md holds policy and the
  skill router only; recipes are skills. The bootstrap caps (`bootstrapMaxChars`)
  cut a file silently past the limit.
- **Web search** → [`searxng/settings/settings.yml`](searxng/settings/settings.yml).
  `keep_only` is a registry filter, not an enable flag. Scraped engines rot silently
  (fewer results, no error); re-measure with `scripts/websearch-bench.py` before
  changing the engine list.
- **STT / TTS** → [`docs/reference/stt-stack.md`](docs/reference/stt-stack.md),
  [`openclaw-tts-fish/README.md`](openclaw-tts-fish/README.md). Whisper is wired via
  `tools.media.models[]` with the Bearer in the entry's own headers. Fish TTS is off by
  default: it does not fit beside Nemotron, and its licence is non-commercial.
- **Browser** → [`docs/reference/browser-automation.md`](docs/reference/browser-automation.md).
  One CDP port per profile, token in the query string; passkeys do not work over
  noVNC; `sandbox.browser` stays off or the model targets a sandbox that does not run.
- **Image / video / music** → [`openclaw-image-comfyui/README.md`](openclaw-image-comfyui/README.md).
  Separate compose joined to this stack's network; the repo ships no weights. The MCP
  `requestTimeoutMs` must outlast the bridge's render budget.
  `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` takes groups off the Discord agent when ComfyUI
  has no room next to the LLM.

## Verification recipes

```bash
# 1. Syntax and compose
for f in config-init/patch-config.mjs config-init/lib/*.mjs config-init/steps/*.mjs; do
  node --check "$f" || echo "FAIL $f"
done
docker compose --env-file .env config --services
docker compose --env-file .env --profile browser --profile python --profile meshcore config --services
# compose/ files need LLM_MODEL_ID set (they refuse to start without it)
docker compose --env-file .env -f docker-compose.yml -f compose/gemma4.yml config --services

# 2. config-init on a copy of the live state (on the Docker host). run.sh copies
# what config-init reads into $HOME/pc-harness/<name>/state, redirects every mount
# there, runs config-init and logs to log.txt. STACK = the checkout to use.
STACK=/path/to/current-checkout bash config-init/test/run.sh before
STACK=/path/to/changed-checkout bash config-init/test/run.sh after
cp config-init/test/diff.mjs "$HOME/pc-harness/"   # diff.sh runs it from there
bash config-init/test/diff.sh before after         # config paths + files that differ
# A second run on "after" must log "already in the desired state".

# 3. End to end on a test host (after `up -d` and onboarding)
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)
curl -sS http://127.0.0.1:18789/healthz                       # {"ok":true,...}
docker exec ${PROJ}openclaw-cli openclaw memory status --deep  # provider ready, dims 1024
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Use web_search to find the title of docker.com. Reply TITLE: <title>" \
  --thinking off --json --timeout 600 | jq '.toolSummary, .finalAssistantVisibleText'
# Discord agent: skills and AGENTS.md size
docker exec ${PROJ}openclaw-gateway sh -lc \
  'ls ~/.openclaw/workspace-discord/skills/ && wc -c ~/.openclaw/workspace-discord/AGENTS.md'
```

## When in doubt

- How it fits together: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
- How to change something: [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md).
- What a knob does: [`docs/reference/configuration.md`](docs/reference/configuration.md).
- Depth per area: [`docs/reference/README.md`](docs/reference/README.md).
- Upgrading OpenClaw: [`docs/OPENCLAW_UPGRADE.md`](docs/OPENCLAW_UPGRADE.md).
- OpenClaw behaviour (CLI flags, plugin SDK, schema): <https://docs.openclaw.ai/>.
- The GB10 numbers in the compose comments and the reference docs are measured; use
  them as the baseline when adapting to other hardware.
