# Setup

This guide takes a fresh GB10 box (DGX Spark / ASUS Ascent GX10) to a running
agent. Most of the time goes into the first model download (~80 GB for the
default model).

For "how do I change X", see [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md).
For every `.env` knob, see
[`docs/reference/configuration.md`](docs/reference/configuration.md). When
something breaks, see [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

The commands below use the default container names (`CONTAINER_NAME_PREFIX`
empty). If you set a prefix, put it in front of every name in `docker exec`.
Run all commands from the repository folder on the host.

## 1. Prerequisites

- **OS:** Ubuntu 24.04 on ARM64 (the DGX OS the box ships with).
- **Docker Engine** with the Compose v2 plugin.
- **NVIDIA Container Toolkit**, registered as a Docker runtime.
- **Disk:** about 150 GB free on the drive that holds `VLLM_HF_CACHE_DIR` and
  Docker's images. The Nemotron weights and their drafter are ~80 GB. The
  vLLM, OpenClaw, Whisper and browser images, bge-m3 and the Whisper model
  take the rest. An extra model file (Gemma, Mistral) or the Fish TTS needs
  room of its own.

Check:

```bash
docker compose version
docker info | grep -i 'runtimes.*nvidia'   # must list nvidia
nvidia-smi                                 # must show the GB10
```

If `docker info` does not list the nvidia runtime:

```bash
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Host setup outside Docker (GB10)

Under sustained LLM load, an uncapped GB10 can power itself off at about
94 °C, with nothing in the journal. Locking the GPU clock at 2200 MHz stops
this, and decode speed stays the same. That lock, the memory and thermal
guards, the job that brings the chat model back after a reboot, and a few
kernel settings that keep the box reachable when the unified memory fills up
are all installed from the repo by one script:

```bash
sudo bash scripts/gb10-host-setup.sh system       # GPU clock cap (enabled), page-cache unit + polkit rule
sudo bash scripts/gb10-host-setup.sh resilience   # SysRq, 1 GiB kernel reserve, swappiness 10, watchdog, sshd OOM shield
bash scripts/gb10-host-setup.sh cron              # the stack's cron jobs, as the operator (after .env exists)
bash scripts/gb10-host-setup.sh check             # what is installed, what differs from the repo
```

What each piece does, the state it keeps and how to undo it:
[`docs/reference/host-setup.md`](docs/reference/host-setup.md). If the box
still shuts down, lower the cap in `scripts/gb10-clock-cap.service` to 2100,
then 2000, and run `system` again. Background:
[`docs/reference/gb10-power-thermal.md`](docs/reference/gb10-power-thermal.md).

## 2. HuggingFace access

The model weights download from HuggingFace on first start. Create a token
with read access at <https://huggingface.co/settings/tokens>. Then open each
model page you will use (`https://huggingface.co/<model id>`) and accept its
licence if the page asks you to:

| Backend | Model ids |
|---|---|
| Nemotron (default) | `nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4` and its drafter `nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr` |
| Gemma 4 (`compose/gemma4.yml`) | `nvidia/Gemma-4-26B-A4B-NVFP4` (MoE) or `nvidia/Gemma-4-31B-IT-NVFP4` (dense) |
| Mistral Small 4 (`compose/mistral.yml`) | `mistralai/Mistral-Small-4-119B-2603-NVFP4` |
| Fish TTS (`compose/tts-fish.yml`) | `fishaudio/s2-pro`, downloaded while the image builds. Research licence, non-commercial only |

bge-m3 and the Whisper model need no licence step. The same token is used for
them.

## 3. Clone and bootstrap

```bash
git clone https://github.com/chestercs/dgx-openclaw-stack.git
cd dgx-openclaw-stack
./bootstrap.sh
```

`bootstrap.sh`:

1. checks for docker, the compose plugin, openssl and the NVIDIA runtime;
2. copies `.env.example` to `.env` (mode 600) if there is no `.env` yet;
3. fills every `CHANGE_ME` secret with a random value;
4. asks for `HUGGING_FACE_HUB_TOKEN`;
5. creates the host folders named in `.env` (`/srv/openclaw/...` by default).

It is safe to run again. A value is only replaced while it still holds the
`CHANGE_ME` placeholder. Without a terminal (CI) it asks nothing, so set
`HUGGING_FACE_HUB_TOKEN` in `.env` first. The OpenClaw containers run as UID
1000, so the host folders must be writable by that user. If bootstrap cannot
create a folder, it prints the `sudo mkdir` / `chown` command to run.

Then open `.env` and look at least at these:

- `OPENCLAW_HEARTBEAT_TZ` and `TZ`: your time zone, for heartbeat hours and
  nightly jobs.
- `OPENCLAW_LAN_CIDR`: your LAN range, if clients reach the gateway directly
  rather than through a reverse proxy.
- `COMPOSE_PROFILES`: `browser,python` by default. Remove what you do not
  want. Add `meshcore` only with the radio plugged in.
- The paths: `VLLM_HF_CACHE_DIR` needs the disk space from step 1.

To rotate secrets later, use `./rotate-secrets.sh` (`--help` lists what it
covers).

## 4. Choose the backend

Nemotron 3.5 Super is the default, and it needs no `.env` change. To use
another model, set both lines in `.env`:

```bash
# Gemma 4 MoE (or nvidia/Gemma-4-31B-IT-NVFP4 for the dense model)
COMPOSE_FILE=docker-compose.yml:compose/gemma4.yml
LLM_MODEL_ID=nvidia/Gemma-4-26B-A4B-NVFP4

# or Mistral Small 4
COMPOSE_FILE=docker-compose.yml:compose/mistral.yml
LLM_MODEL_ID=mistralai/Mistral-Small-4-119B-2603-NVFP4
```

The compose file changes the `vllm-llm` command, and `LLM_MODEL_ID` tells
config-init which id to register in OpenClaw. Leave one of them out and you
get a compose error (no id) or a model that answers 404 (wrong id). The
measured numbers and the trade-offs are in the
[README](README.md#hardware-and-measured-numbers) and
[`docs/reference/llm-stack.md`](docs/reference/llm-stack.md).

If you switch models on an install that is already running, run
`docker compose up -d`, then clear OpenClaw's per-agent model cache. The image
tool reads the cache, not the live config:

```bash
docker exec openclaw-gateway sh -c 'rm -f /home/node/.openclaw/agents/*/agent/models.json'
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

## 5. Onboarding

### Phase 1: first start

```bash
docker compose up -d
docker compose logs -f vllm-llm
```

What to expect:

- `vllm-llm` downloads the weights and compiles kernels. Its health check
  allows 25 minutes for that. The gateway starts only after both vLLM servers
  report healthy, so `docker compose up -d` can wait a long time. If the
  download takes longer than the health check allows, compose reports
  `vllm-llm` as unhealthy. Let the download finish (watch the log), then run
  `docker compose up -d` again.
- `vllm-embedding`, `searxng` and `openclaw-stt-whisper` come up within a few
  minutes.
- `openclaw-config-init` logs that `openclaw.json` does not exist yet, then
  exits 0.
- `openclaw-gateway` keeps restarting with `Missing config. Run openclaw
  setup …`. **This is expected.** OpenClaw does not accept connections until
  someone has onboarded it on purpose and chosen the gateway token.
  Config-init does not create `openclaw.json` on its own, because that would
  skip this step.

### Phase 2: create openclaw.json

Run the OpenClaw onboarding in the CLI container. The interactive wizard:

```bash
docker exec -it openclaw-cli openclaw onboard
```

Or headless. The CLI container already has `OPENCLAW_GATEWAY_TOKEN` from `.env`
in its environment, and onboarding stores only a reference to it:

```bash
docker exec openclaw-cli openclaw onboard \
  --non-interactive --accept-risk \
  --mode local --flow manual \
  --auth-choice skip \
  --gateway-bind lan --gateway-port 18789 \
  --gateway-auth token \
  --gateway-token-ref-env OPENCLAW_GATEWAY_TOKEN \
  --skip-daemon --skip-search --skip-skills --skip-channels --skip-ui --skip-health
```

`--auth-choice skip` leaves the model to config-init, which writes the vLLM
provider with an env reference to `VLLM_API_KEY`, the model entry, memory search
and web search in the next step. Choosing the vLLM provider here instead also
works, but it adds an auth profile that `openclaw secrets audit` then reports as
shadowing the config's reference. In the interactive wizard, what matters is the
gateway token: use `OPENCLAW_GATEWAY_TOKEN` from `.env`.

This exact sequence (phases 2 and 3 on an empty state, then a web search turn,
memory status and `openclaw secrets audit`) was checked on OpenClaw 2026.10.4.

### Phase 3: let config-init wire it

```bash
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
docker compose logs openclaw-config-init
```

Config-init now finds `openclaw.json` and writes the stack's settings. It logs
one `[patch-config]` line for each change, with tokens masked. It runs again
on every `docker compose up`, so for a lasting change, edit `.env` rather than
`openclaw.json` (see
[`docs/reference/configuration.md`](docs/reference/configuration.md)).

Always recreate those three services together. `openclaw-cli` shares the
gateway's network namespace. If the gateway is recreated on its own, the CLI
is left in a dead namespace with no network.

### Open the Web UI

Open `http://<host>:18789` and sign in with the gateway token. A new browser
or client may first show "pairing required". Approve it from the CLI:

```bash
docker exec openclaw-cli openclaw devices list --timeout 60000
docker exec openclaw-cli openclaw devices approve <request-id> --timeout 60000
```

For access from outside your LAN, put a reverse proxy with TLS in front of
port 18789. If the proxy forwards plain `ws://` to the gateway on a private
address, set `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1`. If clients reach the
gateway directly, set `OPENCLAW_LAN_CIDR`. Recreate the three services after
either change.

## 6. Sanity checks

```bash
# Gateway
curl -sS http://127.0.0.1:18789/healthz          # {"ok":true,"status":"live"}

# Memory search: embeddings ready, 1024 dimensions (bge-m3)
docker exec openclaw-cli openclaw memory status --deep

# Web search through OpenClaw, as the agent sees it
docker exec openclaw-cli openclaw infer web search \
  --provider searxng --query "docker compose" --limit 3 --json

# One agent turn with a tool call. Tool runs need --timeout 600; the
# default 60 s is too short on a cold cache.
docker exec openclaw-cli openclaw agent --agent main \
  --message "Use web_search to find the title of docker.com. Reply TITLE: <title>" \
  --thinking off --json --timeout 600

# Speech-to-text backend
curl -sS http://127.0.0.1:8093/health
```

`scripts/websearch-bench.py searxng` measures search quality with 20 fact
questions (Hungarian and English). It is useful after you change the engines
in `searxng/settings/settings.yml`.

## 7. Optional services

- **Discord bot.** Create the bot and invite it as described in
  [`docs/discord-bot-setup.md`](docs/discord-bot-setup.md). Put the token in
  `DISCORD_BOT_TOKEN` and your user ID in `OPENCLAW_DISCORD_COMMAND_OWNERS`,
  recreate the three services, then run
  `docker exec openclaw-cli openclaw channels add --channel discord`. Recreate
  the three services once more, so that config-init writes the Discord
  settings. The bot answers only when it is tagged or replied to, and accepts
  DMs only from people who share a server with it. Every override and the
  knob that changes it:
  [`docs/reference/discord-config.md`](docs/reference/discord-config.md).
- **Browser logins** (profile `browser`). Log a profile in once with
  `./bootstrap-browser-login.sh <profile>`. It prints a noVNC URL. Sign in with
  a password plus TOTP, SMS or a magic link. Passkeys do not work over noVNC.
  Extra profiles go in `BROWSER_PROFILE_NAMES`. Details:
  [`docs/reference/browser-automation.md`](docs/reference/browser-automation.md).
- **Python sandbox** (profile `python`). Works once its token is set
  (bootstrap does that). For web apps the agent builds, set
  `OPENCLAW_SANDBOX_PUBLIC_URL`. For the `git_push` tool, set `GITHUB_TOKEN`.
  Details: [`docs/reference/python-sandbox.md`](docs/reference/python-sandbox.md).
- **MeshCore radio** (profile `meshcore`). Plug in the companion radio, then
  set `MESHCORE_ENABLED=on`, `MESHCORE_SERIAL_DEVICE` (a `/dev/serial/by-id/`
  path) and `MESHCORE_ALLOWED_PUBKEYS`. Details:
  [`docs/reference/meshcore-bridge.md`](docs/reference/meshcore-bridge.md).
- **Text-to-speech** (`compose/tts-fish.yml`). Add the file to `COMPOSE_FILE`
  and set `OPENCLAW_TTS_FISH_API_KEY`. The first build and boot take a long
  time, because kernels are compiled for the GB10. It does not fit next to
  Nemotron, so pair it with a Gemma model. The Fish Audio Research License
  allows non-commercial use only. Details:
  [`openclaw-tts-fish/README.md`](openclaw-tts-fish/README.md).
- **Image, video and music** (ComfyUI bridge). See
  [`openclaw-image-comfyui/README.md`](openclaw-image-comfyui/README.md),
  then set `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` to the groups that stay off
  (`none` turns all of them on). `compose/claw-img.yml` adds the separate
  slash-command bot.

## 8. Daily operation

Whenever the gateway restarts, restart the three OpenClaw services together:

```bash
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
docker compose logs -f openclaw-gateway
```

`docker compose down` stops everything and keeps all state.

### Host cron jobs

`bash scripts/gb10-host-setup.sh cron` writes them as one marked block in the
operator's crontab, from `.env`: the thermal and memory guards (with `strata`
in their lists when `compose/strata.yml` is enabled), `llm-ensure.sh`, the
netns watcher and the daily status. Re-run it after changing `COMPOSE_FILE`;
`check` shows the difference. The jobs and their logs:
[`docs/reference/host-setup.md`](docs/reference/host-setup.md).

### Backups

Everything OpenClaw keeps (config, sessions, agent memory, workspaces) is
under `OPENCLAW_CONFIG_DIR`. Back it up regularly, for example:

```bash
dir=$(grep '^OPENCLAW_CONFIG_DIR=' .env | cut -d= -f2-)
sudo tar czf "openclaw-$(date +%F).tar.gz" -C "$dir" .
```

### Upgrades

Upgrade OpenClaw with `scripts/upgrade-openclaw.sh` (`--prepare <version>`,
then `--apply <version>`), not with `openclaw update` inside the container. It
builds the new image next to the running one, migrates a copy of the state,
and keeps the old one for rollback. Details:
[`docs/OPENCLAW_UPGRADE.md`](docs/OPENCLAW_UPGRADE.md).

SearxNG's image is pulled again weekly (`SEARXNG_PULL_POLICY`), because
scraped engines break as search sites change. The vLLM images are fixed tags
in the compose files; change them there or through `LLM_IMAGE` / `EMBED_IMAGE`.

## 9. Uninstall

```bash
docker compose down -v --rmi all     # containers, named volumes, images
```

This keeps the host folders from `.env`: `OPENCLAW_CONFIG_DIR`, the canvas,
inbound media and sandbox folders, and `VLLM_HF_CACHE_DIR` (the model
weights). Delete them by hand only when you are sure. Deleting
`OPENCLAW_CONFIG_DIR` cannot be undone, and deleting the weights means the
next install downloads them again. If you installed the clock cap, the
resilience setup or the cron jobs, remove those too:
`sudo systemctl disable --now gb10-clock-cap`, the undo line at the end of
`scripts/gb10-resilience-setup.sh`, and `crontab -e`.
