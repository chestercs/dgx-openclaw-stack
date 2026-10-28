# DGX OpenClaw Stack

A self-hosted [OpenClaw](https://openclaw.ai) agent on one NVIDIA GB10 box (DGX
Spark / ASUS Ascent GX10), brought up with `docker compose`.

**The model: DeepSeek-V4.1-Flash, 284B parameters, on one desktop box.** It
runs on [Strata](https://github.com/Niko1221/Strata)'s engine in a ~1.75-bit
quantisation: the experts the model routes to most sit on the GPU, the rest is
computed on the CPU straight from the files. On the reference GX10 it serves
the Discord bot, the MeshCore mesh, and Claude Code on the LAN, offline. It
comes back by itself after a reboot, stays below 80 °C under sustained load,
and its limits allow agent runs of up to a week. It is not fast (code 20–33 tok/s, prose 15–17, one request at a time),
but it is the largest model this box runs, and it runs reliably
([`openclaw-strata/README.md`](openclaw-strata/README.md)).

Around it: bge-m3 embeddings for memory, SearxNG web search, faster-whisper for
voice notes, and OpenClaw's Web UI, agents and Discord bot. The quick start
needs no engine build: Nemotron 3.5 Super on vLLM, one `docker compose up`;
Gemma 4 and Mistral Small 4 are other vLLM options. The value is the wiring:
`config-init` writes `openclaw.json` and the agent workspaces from `.env` on
every `docker compose up`, so the setup stays the same across onboarding
re-runs, restarts and OpenClaw upgrades.

[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

## What runs

| Service | What it does | Host port | Starts with |
|---|---|---|---|
| `strata` | **Chat model: DeepSeek-V4.1-Flash** (ISTA GSQ-RCO IQ1_M) on Strata's `strata-dsv41` engine, built on the host; tools, thinking, DSpark drafting, text only | `127.0.0.1:8097` | `compose/strata.yml`, started by `llm-ensure.sh` after the stack |
| `strata-router` | Claude Code's endpoint for it: keeps the auto-mode classifier inside its 60 s limit | `127.0.0.1:8096` | `compose/strata-router.yml` |
| `vllm-llm` | Quick-start chat model: Nemotron 3.5 Super 120B-A12B VL NVFP4 on `vllm/vllm-openai:v0.32.0`, with the LiLiCorr `dflash` drafter (vision, tools, medium-effort thinking); parked by `compose/strata-llm.yml` | `127.0.0.1:8004` | default |
| `vllm-embedding` | `BAAI/bge-m3` embeddings for memory search | none (8005 inside) | default |
| `searxng` | Web search backend for the `web_search` tool | none (8080 inside) | default |
| `openclaw-stt-whisper` | Speech-to-text, `faster-whisper-large-v3-turbo`, Hungarian and English detected per request | `127.0.0.1:8093` | default |
| `openclaw-config-init` | Writes `openclaw.json` and the workspaces from `.env`, then exits | none | default |
| `openclaw-gateway` | OpenClaw: Web UI, agents, Discord | `18789` | default |
| `openclaw-cli` | Runs `openclaw ...` commands; shares the gateway's network namespace | none | default |
| `openclaw-browser` | Chromium with named login profiles, driven over CDP; noVNC for logging in | `127.0.0.1:9220`, `9222-9241`, `5901` | profile `browser` |
| `openclaw-python-sandbox` | Python over MCP, one kernel per session, plus yt-dlp, ffmpeg and a static preview server | `127.0.0.1:8094`, `8095` | profile `python` |
| `openclaw-meshcore-bridge` | LoRa mesh access through a USB MeshCore companion radio | none | profile `meshcore` |

`.env.example` turns on `COMPOSE_PROFILES=browser,python`. Services reach each
other by service name on the compose network.

The gateway image is `openclaw-base-ext`: the upstream OpenClaw image plus
ffmpeg, git, curl and four local plugins (`people-context`, `claim-guard`,
`dm-guild-gate`, `mcp-image-saver`). The upstream entrypoint is kept, and the
upstream code is not patched.

## Optional extras

Extra compose files go into `COMPOSE_FILE` in `.env`, for example
`COMPOSE_FILE=docker-compose.yml:compose/gemma4.yml`. Each file's header says
what else it needs.

| File | What it adds |
|---|---|
| `compose/gemma4.yml` | Gemma 4 instead of Nemotron, on the same service and port (26B-A4B MoE or 31B dense) |
| `compose/mistral.yml` | Mistral Small 4 119B-A6B NVFP4 instead of Nemotron |
| `compose/tts-fish.yml` | Text-to-speech with Fish Audio S2 Pro (80+ languages, voice cloning). Off by default |
| `compose/claw-img.yml` | A separate Discord bot with `/claw-img`, `/claw-video` and `/claw-music` slash commands that call the ComfyUI bridge directly. Off by default |
| `compose/strata.yml` | DeepSeek-V4.1-Flash (284B MoE, ~1.75 bit) on [Strata](https://github.com/Niko1221/Strata)'s engine: hot experts on the GPU, the rest on the CPU. One request at a time |
| `compose/strata-llm.yml` | That DeepSeek as OpenClaw's chat model instead of Nemotron |
| `compose/strata-router.yml` | Claude Code against that DeepSeek, auto mode included (below) |

A model file needs `LLM_MODEL_ID` set to the id it serves, because config-init
registers that id in OpenClaw.

**Claude Code on the Spark, offline.** With `compose/strata.yml` and
`compose/strata-router.yml`, Claude Code on any machine in the LAN runs
against the DeepSeek on the GB10: no request leaves the network. Code comes at
20–33 tok/s, prose at 15–17, and a resumed turn reads only its new tokens. The
router keeps Claude Code's auto-mode safety classifier inside its fixed 60 s
limit, so long unattended runs work. Setup and the `sclaude` launchers:
[`openclaw-strata-router/README.md`](openclaw-strata-router/README.md); the
server: [`openclaw-strata/README.md`](openclaw-strata/README.md).

The ComfyUI image, video and music bridge has its own compose file in
[`openclaw-image-comfyui/`](openclaw-image-comfyui/README.md). It talks to a
ComfyUI install you run yourself, and the repo ships no model weights. The
agent's media tools stay off (`OPENCLAW_DISCORD_MEDIA_TOOLS_OFF`) until you
turn them on.

## Hardware and measured numbers

The defaults are sized for a GB10: one 128 GB memory pool shared by the CPU
and the GPU, so each vLLM server gets its share through
`--gpu-memory-utilization`. Two things matter on this box:

- **Install the GPU clock cap.** Under sustained load an uncapped GB10 can
  power itself off with nothing in the journal, at about 94 °C, well below the
  ACPI trip point. With the clock locked at 2200 MHz, a 25-minute prefill
  stress test stayed at 87 °C or below, and decode speed did not drop. Details:
  [`docs/reference/gb10-power-thermal.md`](docs/reference/gb10-power-thermal.md).
- **Do not overfill the pool.** When it runs dry, the host stalls instead of
  killing a process. Nemotron takes about 70 GiB at the default share, so the
  Fish TTS and a ComfyUI install do not fit next to it.

Both, and everything else the stack needs outside Docker (the thermal and
memory guards, the job that brings the chat model back after a reboot, kernel
settings that keep the box reachable), come from one script:
`sudo bash scripts/gb10-host-setup.sh system`, `... resilience`, then
`bash scripts/gb10-host-setup.sh cron`. `check` compares the host with the
repo and changes nothing
([`docs/reference/host-setup.md`](docs/reference/host-setup.md)).

Measured on the reference GX10. The setup for each number is in
[`docs/reference/llm-stack.md`](docs/reference/llm-stack.md) and the compose
file headers.

| | **DeepSeek-V4.1-Flash on Strata** | Nemotron 3.5 Super (quick start) | Mistral Small 4 | Gemma 4 26B-A4B MoE | Gemma 4 31B dense |
|---|---|---|---|---|---|
| Parameters | 284B MoE | 120B-A12B | 119B-A6B | 26B-A4B | 31B |
| Weights | 121 GB IQ1_M + 111 GB Engram table, on disk | ~80 GB download | ~66 GiB | ~16.5 GB | ~17 GB |
| Memory | ~80–88 GB of experts on the GPU (~11k of 15.7k), the rest read from the files by the CPU | 0.70 share (~70 GiB) | 0.65 (~78 GiB process) | 0.50 | 0.50 |
| Decode, 1 stream | code 20–33 tok/s (DSpark), prose 15–17 tok/s | 26.2 tok/s English, 16.6 tok/s Hungarian (the drafter predicts Hungarian poorly); 19.8 tok/s with the clock cap | ~30 tok/s | ~25–52 tok/s (depends on the vLLM build) | ~6.9 tok/s |
| Decode, 4 streams | one request at a time; the rest queue | 8.8 tok/s each | 19 tok/s each | n/a | n/a |
| Prefill | ~450 tok/s; a resumed conversation reads only its new tokens | ~2560 tok/s | ~1900 tok/s | n/a | n/a |
| Engine | Strata 0.2.7 (`strata-dsv41`), built on the host | vLLM v0.32.0 | vLLM v0.31.0 | vLLM v0.32.0 | vLLM v0.32.0 |

DeepSeek is the reference host's model since 2026-10-28: the bot, the mesh and
Claude Code all run on it. Its numbers are measured beside the rest of the
stack, with the engine pinned to the A725 cores (acpitz below 80 °C). Strata
measures this quantisation at +9–10% perplexity on code and agent work against
the full model. The Gemma numbers were measured on older vLLM releases with a
patched image and have not been measured again on stock v0.32. Of the vLLM
models, Nemotron gave the most reliable facts and tool use in the repo's
Discord agent evals, which is why it is the quick-start default. Its Hungarian is weaker than Mistral's or Gemma's; config-init
sets temperature 0.6 for it, which keeps other scripts out of Hungarian
replies.

On other hardware, everything except `vllm-llm` carries over. To use a
different model, a remote vLLM or a cloud endpoint, see
[`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) and `LLM_BASE_URL` /
`EMBED_BASE_URL` in [`docs/reference/configuration.md`](docs/reference/configuration.md).

## Quickstart

```bash
git clone https://github.com/chestercs/dgx-openclaw-stack.git
cd dgx-openclaw-stack

./bootstrap.sh            # creates .env, fills the secrets, asks for the HF token
docker compose up -d      # first start downloads ~80 GB of weights
```

The first start takes two phases, on purpose. Before onboarding there is no
`openclaw.json`, so config-init has nothing to change and the gateway
restarts with `Missing config`. OpenClaw wants you to onboard explicitly, so
that you choose the gateway token. Onboard as described in
[SETUP.md](SETUP.md#5-onboarding), then let config-init wire the new config:

```bash
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
curl -sS http://127.0.0.1:18789/healthz
```

Always recreate those three together. `openclaw-cli` lives in the gateway's
network namespace and loses its network when the gateway is recreated without
it.

The full walkthrough, with sanity checks and day-to-day operation, is in
[SETUP.md](SETUP.md).

**Then move to DeepSeek.** The quick start runs on Nemotron. For DeepSeek,
build Strata's engine on the host, download the model (~232 GB) and build its
pack, then add `compose/strata.yml` and `compose/strata-llm.yml` to
`COMPOSE_FILE`: [`openclaw-strata/README.md`](openclaw-strata/README.md) and
[SETUP.md](SETUP.md#4-choose-the-backend).

## Licences

The repo's code is [MIT](LICENSE). Model weights keep their upstream licences.
They are downloaded on first start and are not part of the repo:

- Nemotron 3.5 Super and its drafter: NVIDIA's licence on the model card.
- bge-m3: MIT. Whisper turbo (`deepdml/faster-whisper-large-v3-turbo-ct2`): MIT.
- Gemma 4 (`compose/gemma4.yml`): Apache 2.0. Mistral Small 4
  (`compose/mistral.yml`): Apache 2.0.
- DeepSeek-V4.1-Flash and its quantisations (`compose/strata.yml`): the
  licences on the model cards; you download them yourself. Strata, which the
  container mounts from your own build: MIT.
- Fish Audio S2 Pro, used only by the optional `compose/tts-fish.yml`: the
  **Fish Audio Research License, non-commercial use only**. Building that
  image downloads the weights and counts as accepting the licence. The default
  stack does not include it. The wrapper code in `openclaw-tts-fish/` is MIT.
  For the commercial route, see [`openclaw-tts-fish/README.md`](openclaw-tts-fish/README.md).

## Documentation

- [SETUP.md](SETUP.md): first boot, onboarding, checks, daily operation.
- [docs/reference/configuration.md](docs/reference/configuration.md): every `.env` knob, with its default and effect.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the pieces fit together.
- [docs/CUSTOMIZATION.md](docs/CUSTOMIZATION.md): other models, remote backends, other hardware.
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md): known failure modes.
- [docs/discord-bot-setup.md](docs/discord-bot-setup.md): creating the Discord bot.
- [docs/OPENCLAW_UPGRADE.md](docs/OPENCLAW_UPGRADE.md): upgrading the OpenClaw image.
- [docs/reference/host-setup.md](docs/reference/host-setup.md): what runs on the host outside Docker, and the script that installs and checks it.
- [openclaw-strata/README.md](openclaw-strata/README.md) and [openclaw-strata-router/README.md](openclaw-strata-router/README.md): DeepSeek-V4.1-Flash on Strata, and Claude Code against it, offline.
- [docs/reference/README.md](docs/reference/README.md): deeper reference (LLM stack, GB10 thermals, Discord agent, browser, sandbox, MeshCore).
- [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

Issues that are not about this stack belong upstream:
[vllm-project/vllm](https://github.com/vllm-project/vllm) or
[openclaw/openclaw](https://github.com/openclaw/openclaw).
