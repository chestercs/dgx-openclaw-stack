# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases up to 0.12.x and the unreleased work before 1.0.0 are in
[docs/archive/CHANGELOG-2026-04-to-10.md](docs/archive/CHANGELOG-2026-04-to-10.md).

## [Unreleased]

### Fixed

- Discord channel history: `people-digest.mjs history --grep` also matches
  the author's name, and the tool policy says to search the channel where a
  thing happened. Asked who won the last /ruin round, the bot searched the
  channel it was asked in for the word "ruinbot", which the embed-only win
  post in the game's own channel never contains.
- claim-guard's fact question sees the user's message and counts who a named
  person is (job, role, office) as a specific fact. "jó magyar előadó" about a
  central banker had passed as an opinion; on a 12-case A/B the check went
  from 9/12 to 11/12 with no new false flags.

## [1.1.0] - 2026-10-21

### Added

- `OPENCLAW_WEB_SEARCH_PROVIDER=parallel-free`: `web_search` through the
  official Parallel plugin and its free Search MCP (no key; the queries leave
  the host). 56/60 against SearxNG's 52/60 on `scripts/websearch-bench.py`.
  SearxNG stays the default and keeps running as the way back
  ([CUSTOMIZATION.md](docs/CUSTOMIZATION.md#parallel-free-instead-of-searxng)).

### Changed

- The gateway token (`gateway.auth.token`, `gateway.remote.token`) and the
  Discord bot token (`channels.discord.token`) are env SecretRefs to
  `OPENCLAW_GATEWAY_TOKEN` / `DISCORD_BOT_TOKEN`. Config-init swaps a plain
  value only when `.env` holds the same one (`adopt` in
  `config-init/lib/secrets.mjs`). The gateway and the CLI get every referenced
  variable from one `x-openclaw-secrets` block; `people-digest.mjs`,
  dm-guild-gate and the MeshCore bridge fall back to the environment.
  `openclaw secrets audit` is clean on the reference host.
- people-context gives the model the sender's `<@ID>` too, not only the
  tagged people's: asked to tag an untagged sender back, it made one up.
- The Discord agent's `alsoAllow` default is `browser,canvas,cron` (+`tts`
  while TTS is on); `group:messaging` and `tts` without TTS are removed, they
  matched no tool and were logged on every turn.
- claim-guard skips heartbeat runs: a revise pass turned a quiet "nothing to
  do" heartbeat into a DM to the owner. The heartbeat docs describe the 2026.10
  model (the `heartbeat:<agent>` automation's scratch and `heartbeat_respond`)
  instead of `HEARTBEAT.md`. `agents.defaults.heartbeat.directPolicy: "allow"`
  is written next to the owner DM target, which silences a Doctor warning on
  every start.
- No agent id is hard-coded any more. Active Memory's `agents`, the deep
  agent's parent and the new `config.agents` of people-context and
  claim-guard come from the agents the catch-all Discord binding routes to
  (`discordAgentIds`). `PEOPLE_CONTEXT_AGENT` and `CLAIM_GUARD_AGENTS` are
  gone.
- SETUP.md's headless onboarding uses `--auth-choice skip` and the gateway
  token reference only; config-init wires the model. Checked end to end on an
  empty state (onboarding, config-init, a web search turn, memory status,
  `openclaw secrets audit` clean). The old command stored the vLLM key in an
  auth profile that shadowed the config's reference.
- Without the ComfyUI MCP server wired (every media group off or no
  `IMAGE_GEN_API_TOKEN`), its tools are neither denied, routed nor given
  skills.

## [1.0.0] - 2026-10-21

Current-state baseline. The repo now describes and deploys what runs on the
reference host (GB10, OpenClaw 2026.10.4) and nothing else.

### The stack

- `docker-compose.yml`: `vllm-llm` (Nemotron 3.5 Super 120B-A12B NVFP4,
  `vllm/vllm-openai:v0.32.0`), `vllm-embedding` (bge-m3), `searxng`,
  `openclaw-stt-whisper`, `openclaw-config-init`, `openclaw-gateway`,
  `openclaw-cli`. Profiles `browser`, `python`, `meshcore`.
- `compose/`: optional files added through `COMPOSE_FILE`: `gemma4.yml`,
  `mistral.yml` (LLM swaps), `tts-fish.yml` (Fish Audio S2 Pro),
  `claw-img.yml` (the `/claw-img` bot). The ComfyUI bridge keeps its own
  compose file in `openclaw-image-comfyui/`.
- Every knob with its default: [docs/reference/configuration.md](docs/reference/configuration.md).

### Changed

- **config-init replaces `patch-config.mjs`.** The 6500-line patcher is now
  `config-init/`: one entry, nine area modules (`config-init/steps/`), shared
  helpers (`config-init/lib/`) and the prompt text as templates
  (`config-init/templates/`). It writes the OpenClaw 2026.10 schema directly,
  masks secrets in its log, and a second run changes nothing. A test harness
  (`config-init/test/`) runs it on a copy of live state and diffs the result.
- **Compose layout:** one core file with shared YAML anchors; Nemotron is the
  default model; optional pieces moved to `compose/`.
- **`openclaw-base-ext`** keeps the upstream entrypoint and patches nothing in
  the OpenClaw dist. It adds ffmpeg, git, curl, `people-digest.mjs` and the
  local plugins. MCP timeouts are set per server (`requestTimeoutMs`).
- **Discord mention IDs** come from the `people-context` plugin, which asks
  the Discord API, instead of a dist patch.
- **Active Memory** runs in mode `always` for the Discord agent. The upstream
  `escalate` mode never fired on Hungarian messages.
- **Web search:** SearxNG engines chosen by a 40-question bench
  (`scripts/websearch-bench.py`): 22/40 before, 34/40 after (Hungarian 7/20
  to 17/20). Categories are `general,news`.
- **`.env.example`** lists only what an install sets (~25 settings).
  `bootstrap.sh` fills every placeholder secret, asks only for the
  HuggingFace token, and creates the host folders.
- **Safe defaults:** the mention gate is on (the bot answers @mentions and
  replies to it) and the image/video/music tools are off until ComfyUI runs.

### Added

- **`dm-guild-gate` plugin:** Discord DMs are answered only for people who
  share a server with the bot.
- **`claim-guard` plugin:** checks claimed actions against the run's tool
  calls and flags uncommon facts stated without a look-up; asks for one more
  pass on a mismatch. It skips Active Memory's own recall run.

### Fixed

- **Heartbeats on Discord:** they go to the owner's DM (`user:<id>`). The
  upstream `owner` target found no route and every poll was skipped.

### Removed

- The old `patch-config.mjs`, `docker-compose.nemotron.yml`,
  `docker-compose.mistral.yml`, `templates/` (the Gemma chat template is now in
  `compose/gemma4/`).
- The `vllm-llm-dense` service and the patched Gemma vLLM image (`vllm-llm/`).
- The `!img` bash shortcut and `commands.bash`; the guild-level video deny.
- The dist patches (MCP timeout, mention rewrite, Active Memory), Homebrew,
  the legacy AGENTS.md docs mode, one-off scripts and dated smoke logs.
