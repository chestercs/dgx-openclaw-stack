# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases up to 0.12.x and the unreleased work before 1.0.0 are in
[docs/archive/CHANGELOG-2026-04-to-10.md](docs/archive/CHANGELOG-2026-04-to-10.md).

## [Unreleased]

## [1.2.0] - 2026-10-28

### Added

- `compose/strata.yml` (`openclaw-strata/`): DeepSeek-V4.1-Flash (284B MoE,
  ISTA GSQ-RCO IQ1_M) on Strata's `strata-dsv41` engine, built on the host
  with an aarch64 patch. Hot experts on the GPU, the rest on the CPU; one
  request at a time. On a GB10 beside the stack: code 20-33 tok/s (DSpark),
  prose 15-17, a 25K-token prompt at ~450 tok/s. It sizes its GPU tier from
  MemAvailable at start (`ds4-uma.sh`), so it has a profile and no restart
  policy; it is pinned to the Cortex-A725 cores (acpitz max 72 °C instead of
  84 °C for ~10% speed). `bench.sh`, `soak.sh`, `watch.sh` measure it.
- `compose/strata-llm.yml`: that server as OpenClaw's chat model instead of
  the Nemotron vLLM; the embeddings stay local. `LLM_VISION=off` registers a
  text-only model, and `LLM_THINKING_COMPAT=auto` now picks
  `qwen-chat-template` for DeepSeek ids.
- Claude Code on the Spark, offline: `compose/strata-router.yml` and the
  `sclaude` launchers (`openclaw-strata-router/client/`). Claude Code's
  auto-mode classifier has a fixed 60 s limit that a cold ~35K-token rule set
  does not fit on a single-stream engine; the router lets those requests
  finish after the client gives up and replays the rule set at start, so it
  stays cached (1-2 s per check, 20/20 on a 20-case bench).
- `scripts/llm-ensure.sh` also keeps a Strata server up (with
  `compose/strata.yml` in `COMPOSE_FILE`): at boot and after a memguard kill it
  starts it once the stack has loaded, at most three times per hour; a failed
  start is logged. thermguard drops a kill list from before a reboot, so a
  power-off does not restart the model in the middle of the stack's boot.
- Discord eval: `scripts/discord-eval.sh` runs regression cases as Gateway
  turns (machine checks, then a rubric-only judge that may say unknown;
  latency and tokens per run; `--rejudge` regrades an earlier report). Real
  cases from the bot's own history stay in `private/`; `evals/discord/` holds
  the schema and synthetic examples. claim-guard's log line is now the
  answer's receipt (AGENTS.md hash, model, context size, run time, sources),
  summarised by daily-status.
- people-context tools `people_note`, `people_forget` and `discord_history`
  (wrapping `/opt/people-digest.mjs`), so the agent needs no shell for them; a
  note about someone other than the sender is refused unless the owner asks.
- `harness-guard` plugin, policy outside the model: owner-only tools in DMs;
  a `Forrás:` line for every new fact in `memory/*.md` (the file is copied to
  `memory-backups/` first); a `Done when:` line in every `sessions_spawn`
  task; a member's `message` sends and `automations` deliveries only to the
  conversation they asked in (no other channel or DM, no broadcast, no mass
  mention). Owners and scheduled runs pass.
- claim-guard source check, opt-in (`OPENCLAW_CLAIM_GUARD_SOURCE_CHECK`): a
  third question rates each fact against the turn's tool results and the
  revise names the exact gap, with a second revision. Off by default: on the
  43-case eval (same grading) 72 vs 75 of 129 passed, median latency 53 → 68
  s, p90 130 → 230 s, input tokens +38 %. daily-status lists the Discord
  sessions claim-guard left unresolved.
- `people-digest.mjs server-map` writes `memory/server-map.md` (every text
  channel with id, topic and the bots posting there); the nightly `collect`
  refreshes it.
- daily-status: Discord run outcomes and failed or blocked tools from the
  audit store, and the memory notes with no source line.
- CONTRIBUTING: the incident loop (receipt, classify, repair, eval case,
  numbers).

### Changed

- Sub-agents stop after 30 minutes by default
  (`OPENCLAW_SUBAGENTS_RUN_TIMEOUT`); a day-long thread task passes
  `runTimeoutSeconds: 0` in its own call.

- Owner-only shell and browser (`OPENCLAW_DISCORD_OWNER_ONLY_TOOLS`, default
  `exec,process,browser`): guild channels through the official
  `channels.discord.guilds["*"].toolsBySender`, which also takes them out of
  everyone else's prompt; DMs through harness-guard. Scheduled runs keep them.
- The Discord agent's prompt is a map, not a manual: the marker blocks went
  from 23 KB of overlapping rules to ~8 KB (formatting, a "what done means"
  contract per request type, who is talking, delegation, the router).
  Retired blocks are removed from existing files.
- The Discord agent loses 12 tools it never used in six days of traffic
  (session admin, agent listing, transcripts, the workboard core unless
  `OPENCLAW_DISCORD_WORKBOARD_TOOLS=on`, tts without a TTS provider) and sees
  only the skills in its workspace (`OPENCLAW_DISCORD_AGENT_SKILLS_EXTRA`).
- Guild channel sessions reset after 240 idle minutes
  (`OPENCLAW_DISCORD_SESSION_IDLE_MINUTES`).
- Measured on the reference host's 43-case eval, 3 runs each, both reports
  graded by the same judge: 63 → 75 of 129 runs pass (cases passing all three:
  13 → 16; Discord look-ups 5/12 → 9/12), median latency 66 → 53 s, p90
  144 → 130 s, median input 101k → 80k tokens per turn. Regrading one report
  twice moves the pass count by up to 2. With every change below in place
  (source check off), regraded together with phase 1: 74 vs 77 of 129, median
  58 vs 53 s, 84k vs 80k tokens, within the eval's noise. The gates added
  after phase 1 act on Discord senders, which the CLI eval has none of; they
  were tested live and with simulated hook calls.

### Fixed

- claim-guard counts `people_note`, `people_forget` and `discord_history` as
  saving, reading and searching: every "elmentettem" after `people_note` had
  been sent back for a revision.
- The reminder skill computes times without `exec`, which is owner-only now.
- The eval counts the tools of every pass of a revised turn; the CLI's
  summary lists only the last pass, so a revised turn that had searched
  failed as if it had not.
- Memory keeps no raw session dumps. Config-init turns the `session-memory`
  hook off (`OPENCLAW_SESSION_MEMORY`, default `off`): it saved each
  session's last messages on reset, the bot's wrong conclusions included, and
  memory search and Active Memory served them back as facts. The agent's save
  rule now allows what the user said or a tool showed, with the source, and
  never its own guess.
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
