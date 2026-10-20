# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Releases up to 0.12.x and the unreleased work before 1.0.0 are in
[docs/archive/CHANGELOG-2026-04-to-10.md](docs/archive/CHANGELOG-2026-04-to-10.md).

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
