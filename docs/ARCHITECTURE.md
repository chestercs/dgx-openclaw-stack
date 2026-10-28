# Architecture

How the services in `docker-compose.yml` fit together, how `config-init` turns `.env`
into a working OpenClaw configuration, and why the main pieces are built the way they
are. For bringing the stack up, [`SETUP.md`](../SETUP.md) is the shorter path; for
changing it, [`CUSTOMIZATION.md`](CUSTOMIZATION.md); for every knob,
[`reference/configuration.md`](reference/configuration.md).

## Request flow

A Discord message, from arrival to reply:

```
Discord ──► openclaw-gateway (Discord plugin)
              │ DM?      dm-guild-gate: sender shares no guild with the bot → dropped
              │ guild?   mention gate: only @mentions and replies to the bot pass
              ▼
            binding → Discord agent (workspace-discord/)
              │ people-context: "Tagged: @Name = <@ID>" + the sender's people lines
              │ Active Memory: a short recall run over memory_search
              │ system prompt: AGENTS.md (marker blocks + skill router),
              │                people/MEMORY.md, skill list
              ▼
            vllm-llm:8004 (provider "vllm") ◄──► tool calls
              │   web_search ──► searxng:8080 (or Parallel's free Search MCP)
              │   memory_search ──► vllm-embedding:8005 (bge-m3)
              │   exec / apply_patch (gateway container, approvals as Discord DMs)
              │   browser ──► openclaw-browser (CDP)          [profile browser]
              │   python_sandbox__* ──► openclaw-python-sandbox [profile python]
              │   comfyui_image__* ──► openclaw-image-comfyui  [separate compose]
              ▼
            claim-guard: an action no tool call backs? a fact never
              │          looked up? → one more pass
              ▼
            Discord: a preview message edited as tokens arrive, then the final reply
```

The Web UI, the CLI (`openclaw agent …`) and the MeshCore radio enter at the gateway
too; they route to the `main` agent or the MeshCore agents and skip the
Discord-only hooks.

## Services

All services sit on the compose project's default bridge network and reach each other
by service name (`http://vllm-llm:8004/v1/`). Container names carry an optional
`CONTAINER_NAME_PREFIX`; DNS does not depend on it.

| Service | Role | Host port |
|---|---|---|
| `strata` | The reference host's chat model, DeepSeek-V4.1-Flash on Strata's engine (`compose/strata.yml` + `compose/strata-llm.yml`, which parks `vllm-llm`); started by `llm-ensure.sh` after the stack; `strata-router` in front of it for Claude Code | `127.0.0.1:8097`, router `8096` |
| `vllm-llm` | The quick-start chat model, Nemotron 3.5 Super 120B-A12B NVFP4 (vision, tools, reasoning), `vllm/vllm-openai:v0.32.0` | `127.0.0.1:8004` (`LLM_BIND`) |
| `vllm-embedding` | `BAAI/bge-m3` embeddings for memory search, 3 % of the pool | none |
| `searxng` | Web search backend (JSON API) | none |
| `openclaw-stt-whisper` | faster-whisper large-v3-turbo; Hungarian and English detected per request | `127.0.0.1:8093` |
| `openclaw-config-init` | Writes `openclaw.json` and the workspaces from `.env`, then exits | none |
| `openclaw-gateway` | OpenClaw: Web UI, Discord, agents, plugins | `18789` |
| `openclaw-cli` | `docker exec openclaw-cli openclaw …`; shares the gateway's network namespace | none |
| `openclaw-browser` | Chromium with named login profiles, driven over CDP (profile `browser`) | `127.0.0.1` only |
| `openclaw-python-sandbox` | Python over MCP, one kernel per session, yt-dlp, ffmpeg, a token-isolated transcription tool (profile `python`) | `127.0.0.1:8094`, preview `8095` |
| `openclaw-meshcore-bridge` | LoRa mesh access through a USB MeshCore radio; runs in the gateway's network namespace (profile `meshcore`) | none |

Compose YAML anchors keep the services short: `x-vllm` (shared vLLM environment, HF
cache, GPU, ulimits), `x-openclaw` (one image and one set of state mounts for the
three OpenClaw containers), `x-gpu`, `x-logging`.

**Start order.** The gateway waits for config-init to finish successfully and for both
vLLM servers to be healthy. `openclaw-cli` and the MeshCore bridge join the gateway's
network namespace, so a recreated gateway leaves them in a dead namespace: recreate
them together, and run `scripts/netns-dependents-up.sh --watch` from cron so a reboot
heals itself ([`reference/patterns.md`](reference/patterns.md)).

**The unified memory pool.** The GB10 has one 128 GB pool for CPU and GPU. Each vLLM
server takes a fixed share (`--gpu-memory-utilization`, Nemotron 0.70), so what runs
beside the LLM is a budget question: Nemotron leaves no room for Fish TTS or ComfyUI
renders, which is why both are off on the reference host. A model load that drains
the pool livelocks the host instead of triggering the OOM killer; the guards are in
[`reference/gb10-power-thermal.md`](reference/gb10-power-thermal.md).

**State on the host.** The OpenClaw containers share `OPENCLAW_CONFIG_DIR` (config,
sessions, agent workspaces), `OPENCLAW_WORKSPACE_DIR` (the main workspace),
`OPENCLAW_SANDBOX_DIR` (shared with the python sandbox), `OPENCLAW_CANVAS_DIR`
(generated and downloaded media the agent attaches) and `OPENCLAW_MEDIA_INBOUND_DIR`
(files users send). Model weights live in `VLLM_HF_CACHE_DIR`, bound so they survive
`down -v`. Everything must be writable by UID 1000.

## The model

`vllm-llm` serves one model id, `LLM_MODEL_ID`. Other models are compose files that
replace the service's command on the same port: `compose/gemma4.yml` (Gemma 4 MoE or
dense, with the repo's tool chat template) and `compose/mistral.yml` (Mistral Small 4).
config-init registers exactly that id as the one entry of the `vllm` provider and
removes every other `vllm/...` entry, which would otherwise show in the model picker
and answer 404.

Nemotron settings that matter:

- **Thinking depth.** `--default-chat-template-kwargs '{"medium_effort": true}'`:
  full thinking ran 57k-token turns; thinking off looped on tool-call JSON.
- **Thinking switch.** OpenClaw sends no thinking control to a custom provider unless
  the catalog entry has a `compat`; config-init picks it from the model id
  (`LLM_THINKING_COMPAT`).
- **Temperature 0.6.** At the model's default 1.0, Hungarian replies picked up
  characters from other scripts mid-word (`LLM_TEMPERATURE`).
- **Speculative decoding** with the DFlash draft model, and long timeouts
  (`LLM_REQUEST_TIMEOUT_SECONDS`, 1800 s): a cold 128K prefill alone takes about two
  minutes on the GB10.
- **The GB10 clock cap** (`scripts/gb10-clock-cap.service`): under sustained load an
  uncapped box powers itself off.

Measurements and the other models: [`reference/llm-stack.md`](reference/llm-stack.md).

## config-init

OpenClaw keeps its whole configuration in `openclaw.json`, and the onboarding wizard,
Doctor and the Web UI all write to it. Hand edits drift and are lost on the next
wizard run. config-init makes the stack's part of that file a function of `.env`: it
runs on every `docker compose up` before the gateway starts, brings the file to the
desired state, and a second run changes nothing.

**How it applies the state.** `config-init/patch-config.mjs` loads `openclaw.json`
into a `Config` object (`lib/config.mjs`). Every write goes through its helpers
(`set`, `merge`, `setDefault`, `addTo`, `removeFrom`, `remove`), which compare first,
write only on a difference and log one `[patch-config] <path> = <value> (<why>)`
line with secrets masked. The file is written once, at the end, and only if something
changed. Rules the steps follow:

- Knobs are read through `lib/env.mjs`: one on/off vocabulary, and an empty value is
  the default. config-init gets the whole `.env` through `env_file`.
- Lists the operator may extend (`tools.deny`, `alsoAllow`, plugin load paths) are
  merged, not replaced.
- A feature switched `off` is removed, including the empty parent objects it leaves.
- Credentials go in as env SecretRefs (`lib/secrets.mjs`): `openclaw.json` names the
  variable and the gateway resolves it from its own environment, so the value stays
  out of the file and its backups.
- No `openclaw.json` (before onboarding): exit 0. A pre-2026.10 config (`agents.list`):
  stop and ask for `openclaw doctor --fix`.

**Modules.** `steps/*.mjs` run in this order; later steps read what earlier ones wrote.

| Step | What it owns |
|---|---|
| `models` | The `vllm` provider and its one catalog entry (context window, max tokens, thinking `compat`), the default model and its temperature, stale model references |
| `memory` | `memory.search` on bge-m3, dreaming (`memory-core`), Active Memory, rotation of large `DREAMS.md` files |
| `gateway` | Trusted proxies, the CLI's remote token, node commands, heartbeat (DM to the owner), bootstrap size caps, run timeouts, context pruning, sub-agent bounds, message queue modes |
| `tools` | SearxNG web search, Whisper as the audio model, the CDP browser profiles, the python sandbox MCP server, exec mode and approvals, plan tool, loop detection, tool search off |
| `discord` | Who may talk to the bot and run commands, the mention gate and always-on channels, streaming and chunking, exec approvals by DM, presence, reply threading, outbound mention aliases |
| `agents` | The Discord agent's tool profile, deny list and thinking; the MeshCore agents; the Valheim builder; the optional full-thinking `deep` sub-agent |
| `workspaces` | Marker blocks in the Discord `AGENTS.md`, the skill files, the people-memory seed and its job recipes, the main agent's browser policy |
| `media` | Fish TTS wiring, the ComfyUI MCP server, the media skills and router lines, the media tool deny list |
| `plugins` | The local plugins, the `bootstrap-extra-files` hook, workboard |

**Templates.** Prompt text lives in `config-init/templates/` and is rendered with
`{{NAME}}` placeholders (a missing value fails the run rather than writing half a
prompt):

- `discord/AGENTS.d/*.md`: the blocks of the Discord `AGENTS.md` (format rules,
  honesty, sender identity, tool policy, delegation, thread tasks, the skill router).
  They sit between `<!-- patch-config:<id>:start/end -->` markers; text outside the
  markers is the operator's.
- `discord/router/*.md`: the router lines for image, video and music, in an "on" and
  an "off" form. An off group gets a line telling the bot to say so instead of
  reaching for another tool.
- `skills/*.md`: recipes the agent reads on demand (`skills/<name>/SKILL.md`), wholly
  owned by config-init. `AGENTS.md` is injected into every session, so it holds only
  what must be seen at decision time; moving recipes into skills kept the Discord
  bootstrap at ~29 KB instead of ~53 KB.
- `agents/*.md`: seeds for the agents config-init creates, written once.
- `main/AGENTS.d/`, `discord/people-MEMORY.seed.md`: the main agent's browser policy
  and the first version of `people/MEMORY.md`.

Member names and IDs in the prompt examples come from `.env`
(`OPENCLAW_DISCORD_MEMBER_NAMES`, `OPENCLAW_DISCORD_ADMIN_NAME`, ...), so the public
templates carry none.

**Testing.** `config-init/test/run.sh` copies what config-init reads from the live
state, redirects every mount to the copy and runs a given config-init there;
`diff.sh` compares two such runs (config paths and workspace files, secrets masked).
Recipe in [`CLAUDE.md`](../CLAUDE.md#verification-recipes).

**First start.** Onboarding writes the first `openclaw.json`; config-init then runs
again. The two phases are described in [`SETUP.md`](../SETUP.md).

## Plugins

The `openclaw-base-ext` image is the upstream OpenClaw image plus ffmpeg, git, curl,
`/opt/people-digest.mjs` and five local plugins in `/opt/openclaw-plugins`. Nothing
patches OpenClaw itself: the plugins use the plugin SDK and are loaded through
`plugins.load.paths`; the upstream entrypoint (state migration, then the gateway) is
kept.

| Plugin | Hooks | What it does |
|---|---|---|
| `people-context` | `message_received`, `before_prompt_build`, `before_tool_call`; tools `people_note`, `people_forget`, `discord_history` | The Discord plugin turns `<@ID>` into `@Name` before any hook sees it, so the model could not tag people back. The plugin reads the message's mention list from the Discord API and adds `Tagged: @Name = <@ID>` and the sender's `<@ID>`, then prepends the sender's (and tagged people's) lines from `people/MEMORY.md`. It refuses direct edits of that file. Its tools wrap `/opt/people-digest.mjs` (the guarded writer and the channel reader), so the agent needs no shell for them; a note about someone other than the sender is refused unless the owner asks. |
| `claim-guard` | `after_tool_call`, `before_agent_finalize` | Asks the host model which actions the answer claims and checks them against the run's successful tool calls; a second question flags specific, uncommon facts stated without a look-up (optionally a third checks them against the turn's tool results, `OPENCLAW_CLAIM_GUARD_SOURCE_CHECK`). A gap gets one more pass: do it or look it up, or say it was not done. If the judge fails, the answer goes out as is. Log: `logs/claim-guard.jsonl`, structure only. |
| `dm-guild-gate` | `before_dispatch` | Answers a DM only when the sender shares a guild with the bot (Discord REST, cached). `dmPolicy` has no such mode, and `open` is needed so members can DM without pairing. If Discord cannot be asked, the message goes through. Log: `logs/dm-guild-gate.jsonl`. |
| `harness-guard` | `before_tool_call` | Policy outside the model. Owner-only tools (`OPENCLAW_DISCORD_OWNER_ONLY_TOOLS`) in DMs, where Discord has no per-sender tool policy; guild channels get the same rule from `channels.discord.guilds["*"].toolsBySender`. Runs without a sender (scheduled jobs) pass. A new fact in `memory/*.md` needs a `Forrás:` line among the added lines (the file is copied to `memory-backups/` first); `memory/server-map.md` is read-only. A `sessions_spawn` task needs a `Done when:` line. A member's `message` sends and `automations` deliveries go only to the conversation they asked in (no other channel or DM, no broadcast, no mass mention); the sender comes from `before_prompt_build`. |
| `mcp-image-saver` | tool-result middleware | Saves images returned by MCP tools (the Valheim builder's screenshots) into the canvas and adds a `[saved: <path>]` line, so the reply can attach them as `MEDIA:`. |

The Discord plugins are wired only while the Discord channel is configured, and each
can be switched off from `.env`. None registers a `reply_payload_sending` or `message_sending`
hook: any such hook turns off Discord preview streaming for every Discord turn.

## Memory

Several layers, each for a different question:

| Layer | Where | Written by | Read by |
|---|---|---|---|
| Memory search | `workspace*/memory/*.md`, indexed per agent with bge-m3 | The agent (notes it was asked to keep), dreaming, the community-summary job | `memory_search`; Active Memory |
| Root `MEMORY.md` | each workspace | Dreaming promotions, the agent | Injected into DM and main sessions only |
| People memory | `workspace-discord/people/MEMORY.md` | the `people_note` / `people_forget` tools (bot) and `people-digest.mjs apply` (nightly job) | Every Discord session via `bootstrap-extra-files`; people-context per turn |
| Community summaries | `workspace-discord/memory/community/<from>_<to>.md` | The community-summary job, every 3 days | `memory_search` |
| Server map | `workspace-discord/memory/server-map.md` | `people-digest.mjs server-map`, at the end of the nightly `collect` | `memory_search`; the prompt points to it for "where did it happen" |
| Active Memory | no storage | — | A recall run before each Discord reply |

- **Memory search** embeds with the local bge-m3 server (1024 dimensions, fixed once
  the first chunk is indexed). Each agent has its own workspace and index, which is
  what keeps a separate agent's memory separate.
- **Only deliberate writes become memory.** The `session-memory` hook is off
  (`OPENCLAW_SESSION_MEMORY`): its raw session dumps kept the bot's own wrong
  conclusions, and recall served them back as facts a day later. The prompt lets
  the agent save what the user said or a tool showed, with the source, never its
  own guess, and harness-guard refuses a new fact in `memory/*.md` without a
  `Forrás:` line. Every change is preceded by a copy in `memory-backups/<agent>/`
  (last 20 per file), outside the workspace so search does not index it.
- **Dreaming** (`memory-core`) consolidates nightly at 03:00 in `OPENCLAW_HEARTBEAT_TZ`.
  Storage is `separate`: inline mode wrote session metadata into the daily notes,
  where it ranked next to real notes. Promotions into `MEMORY.md` are capped at 80
  tokens each because that file goes into every session's bootstrap. A `DREAMS.md`
  over 32 KB moves to `memory/dreams-archive/`, which search still indexes.
- **People memory.** OpenClaw drops the root `MEMORY.md` from group and channel
  sessions, so in a guild the bot had no long-term memory. `people/MEMORY.md` holds
  short lasting facts per member (`## Name (<@ID>)` sections) and is injected by the
  `bootstrap-extra-files` hook, which runs after that filter. The bot changes it only
  through `people-digest.mjs`, which finds or creates the person's section and changes
  one line; the nightly job proposes a new file, and `apply` swaps it in only if no
  section vanished, no permanent rule was lost and the file did not shrink below 70 %.
  NSFW channels stay out of the digest by default because the file reaches every
  channel.
- **Community summaries** record what was talked about, not who people are: a few days
  of guild traffic into one searchable file.
- **Active Memory** runs a short recall before each reply of the Discord agent, so the
  bot remembers other channels and DMs without deciding to search. Mode `always`: the
  default `escalate` runs only on recall intent, and its intent detector has no
  Hungarian patterns, so a Hungarian guild never escalated. Recall runs with thinking off and a 30 s timeout.

The two jobs (`people-memory-nightly`, `community-summary`) are `openclaw cron`
agent turns the operator creates once; their recipes are the `people-memory` and
`community-summary` skills, which config-init writes. `scripts/daily-status.sh`
collects the outcomes of these quiet jobs and of claim-guard into one daily line.
Details: [`reference/discord-config.md`](reference/discord-config.md).

## Web search

`web_search` goes to the bundled `searxng` plugin (it ships disabled; config-init
enables it), which calls `http://searxng:8080/search?format=json` with the categories
`general,news`. SearxNG keeps only a measured engine set: yahoo, duckduckgo web,
privacywall, marginalia, yep and wikipedia for general queries, plus bing, duckduckgo
and yahoo news. The set was chosen on a 40-question Hungarian/English fact bench
(`scripts/websearch-bench.py`); the per-engine numbers and the privacy settings are in
[`searxng/settings/settings.yml`](../searxng/settings/settings.yml).

`OPENCLAW_WEB_SEARCH_PROVIDER=parallel-free` switches `web_search` to the official
Parallel plugin and its free Search MCP (no key; the queries leave the host). It
scored 59/60 against SearxNG's 53/60 on the same bench. SearxNG keeps running as
the way back ([CUSTOMIZATION.md](CUSTOMIZATION.md#parallel-free-instead-of-searxng)).

Scraped engines rot without an error: a blocked engine just returns nothing, and the
agent gets fewer, worse results. Check `unresponsive_engines` in a JSON response and
re-run the bench before changing the list. The image is re-pulled weekly instead of
pinned because engine fixes ship upstream.

## Agents

- **`main`**: the operator agent behind the Web UI and the CLI, with the gateway and
  node tools the Discord agent lacks.
- **Discord agent** (`discord-friend` on the reference host): whichever agent the
  catch-all Discord binding routes to; created by the channel onboarding. config-init
  gives it the `full` tool profile minus a deny list of operator and desktop tools (a
  model picks badly from a 113-tool catalog), thinking `high`, and its prompt blocks
  and skills. Discord messages are queued `followup`: a guild channel is one shared
  session, and `steer` merged a second user's request into the first one's run. Exec runs in mode `ask` by default, with approvals as Discord DMs
  to the owners ([`reference/agentic-coding.md`](reference/agentic-coding.md)).
- **`deep`** (optional, `OPENCLAW_DISCORD_DEEP_AGENT`): the same tools with Nemotron's
  full thinking, for `sessions_spawn`. Off by default: 40/40 against medium's 39/40 on
  checkable reasoning questions, at 2–2.5× the time.
- **MeshCore agents** (`MESHCORE_ENABLED`): a private DM agent and an optional channel
  agent with their own memory, narrow tools and short replies for ~130-byte packets
  ([`reference/meshcore-bridge.md`](reference/meshcore-bridge.md)).
- **Valheim builder** (`VALHEIM_MCP_URL`): a separate agent bound to listed Discord
  channels that drives a game client over MCP; its tools are denied on every other
  agent.

The heartbeat runs on the agent defaults and DMs the first command owner. Discord
behaviour in detail: [`reference/discord-config.md`](reference/discord-config.md),
[`reference/discord-text-agent.md`](reference/discord-text-agent.md).

## Optional extras

| Extra | How to enable | Notes |
|---|---|---|
| Gemma 4 / Mistral Small 4 | `COMPOSE_FILE=docker-compose.yml:compose/gemma4.yml` (or `mistral.yml`) + `LLM_MODEL_ID` | [`reference/llm-stack.md`](reference/llm-stack.md) |
| Fish Audio S2 Pro TTS | `compose/tts-fish.yml` + `OPENCLAW_TTS_FISH_API_KEY` | Does not fit beside Nemotron; non-commercial licence. [`openclaw-tts-fish/README.md`](../openclaw-tts-fish/README.md) |
| Image / video / music tools | `openclaw-image-comfyui/docker-compose.yml` + `IMAGE_GEN_API_TOKEN`; `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF=none` | Bridge to an existing ComfyUI, joined to this stack's network. [`../openclaw-image-comfyui/README.md`](../openclaw-image-comfyui/README.md), [`../openclaw-image-comfyui/README.md`](../openclaw-image-comfyui/README.md) |
| `/claw-img` Discord bot | `compose/claw-img.yml` | Slash commands straight to the bridge, no LLM. [`openclaw-claw-img-bot/README.md`](../openclaw-claw-img-bot/README.md) |
| Browser | profile `browser` | One CDP port per login profile. [`reference/browser-automation.md`](reference/browser-automation.md) |
| Python sandbox | profile `python` | [`reference/python-sandbox.md`](reference/python-sandbox.md) |
| MeshCore radio | profile `meshcore` + `MESHCORE_ENABLED` | [`reference/meshcore-bridge.md`](reference/meshcore-bridge.md) |
| CLI on another machine | `openclaw-host-client/` | [`openclaw-host-client/README.md`](../openclaw-host-client/README.md) |
| Workboard | `OPENCLAW_WORKBOARD=on` | Card board for multi-hour tasks. [`reference/agentic-coding.md`](reference/agentic-coding.md) |

## Host scripts

`scripts/` holds what runs on the host rather than in a container, installed and checked by
`gb10-host-setup.sh` ([`reference/host-setup.md`](reference/host-setup.md)): the GB10 clock cap
unit, `gb10-thermguard.sh` and `gb10-memguard.sh` (stop load before a power-off or a
livelock), `llm-ensure.sh` (brings the chat model, `vllm-llm` or `strata`, back after a guard
killed it or a reboot),
`gb10-resilience-setup.sh`, `netns-dependents-up.sh`, `daily-status.sh`,
`upgrade-openclaw.sh` ([`OPENCLAW_UPGRADE.md`](OPENCLAW_UPGRADE.md)),
`websearch-bench.py`, `reshard-safetensors.py`, and the LTX-Video installer and
benchmarks.

## Exposure

Only the gateway (`18789`) and the sandbox preview (`8095`) listen on all interfaces
by default; every other published port binds to `127.0.0.1`. Put the gateway behind
a TLS reverse proxy for anything beyond the LAN. Clients that reach it directly need
their range in `OPENCLAW_LAN_CIDR` (`gateway.trustedProxies`). Discord reaches the bot
through the gateway's outbound connection, so no inbound port is needed for it.
