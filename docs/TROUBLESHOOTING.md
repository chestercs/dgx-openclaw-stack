# Troubleshooting

Known failure modes and their fixes, grouped by area. When nothing here matches,
start with the logs of the service you suspect:

```bash
docker compose logs --tail=200 <service>
```

Commands below use the default container names. If you set
`CONTAINER_NAME_PREFIX`, put it in front of every name in `docker exec`. Most
config fixes end with the trio recreate, which re-runs config-init and keeps the
CLI in the gateway's network namespace:

```bash
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

- [Startup and onboarding](#startup-and-onboarding)
- [Model and vLLM](#model-and-vllm)
- [GB10 host: power-offs and freezes](#gb10-host-power-offs-and-freezes)
- [Gateway and CLI](#gateway-and-cli)
- [Discord bot](#discord-bot)
- [Web search](#web-search)
- [Memory search](#memory-search)
- [Optional components](#optional-components)
- [Host](#host)

---

## Startup and onboarding

### The gateway crash-loops with `Missing config. Run openclaw setup …`

Expected on the first `docker compose up -d` of a fresh install. There is no
`openclaw.json` yet, so config-init exits 0 without writing anything, and the
gateway has no config to start from. OpenClaw requires explicit onboarding (you
choose the gateway token and pair the UI) before it accepts connections.

Onboard as described in [`SETUP.md`](../SETUP.md), then run the trio recreate so
config-init applies the stack's wiring to the new file.

### The gateway never starts and `openclaw-config-init` exited with an error

The gateway waits for config-init to finish successfully, so a config-init error
stops the whole OpenClaw side. Read why:

```bash
docker logs openclaw-config-init
```

| Message | Cause and fix |
|---|---|
| `FOO="x" is not on/off`, `... must be one of ...`, `... is not an integer` | A knob in `.env` has a value config-init does not accept. Fix it (see [`reference/configuration.md`](reference/configuration.md)) and recreate the trio. An empty value means "use the default". |
| `openclaw.json still has the pre-2026.10 agents.list` | The config predates the 2026.10 schema. Upgrade through [`OPENCLAW_UPGRADE.md`](OPENCLAW_UPGRADE.md), or run `openclaw doctor --fix` first. |
| `template ...: no value for {{NAME}}` | A template in `config-init/templates/` uses a placeholder its step does not fill. Fix the template or the step. |
| `OPENCLAW_TTS=on needs OPENCLAW_TTS_FISH_API_KEY` | TTS was switched on without its token. |
| `EACCES` / permission denied | config-init runs as UID 1000. The host dirs in `.env` (`OPENCLAW_CONFIG_DIR`, `OPENCLAW_WORKSPACE_DIR`, `OPENCLAW_CANVAS_DIR`, ...) must be writable by that UID; `bootstrap.sh` creates them that way. |

### The gateway waits for minutes before it starts

It starts only once `vllm-llm` and `vllm-embedding` are healthy. On a first boot
that is the model download (see [the next section](#vllm-llm-stays-in-starting-for-a-long-time)).
`docker compose ps` shows which dependency it is waiting for.

### The Control UI or the Chrome extension cannot connect

- `curl http://<host>:18789/healthz` should answer `{"ok":true,...}`. If not, the
  gateway is not up or `OPENCLAW_GATEWAY_PORT` / `OPENCLAW_GATEWAY_BIND` differ.
- Behind a reverse proxy that answers `403`: the proxy has to forward the
  `Upgrade: websocket` and `Connection: upgrade` headers.
- Browsers allow plain `ws://` only on localhost. For remote access use `wss://`
  through a TLS-terminating proxy. If that proxy forwards plaintext `ws://` to the
  gateway on a private IP, set `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1`.
- Clients that reach the gateway directly (not through the proxy) show up as the
  proxy's address unless their range is in `OPENCLAW_LAN_CIDR`.

## Model and vLLM

### `vllm-llm` stays in `starting` for a long time

On a first start the default Nemotron model downloads ~80 GB and compiles kernels;
the healthcheck allows 25 minutes (`start_period: 1500s`) for that. Later starts
only load the weights from disk. Follow the progress:

```bash
docker compose logs -f vllm-llm
```

The optional model files have their own budgets: `compose/gemma4.yml` 10 minutes,
`compose/mistral.yml` 15 minutes (measured ~10 minutes to healthy from a USB SSD).

If the host has no internet when `vllm-llm` starts, the hub look-ups can hold the
start. Once the weights are cached, `HF_HUB_OFFLINE=1` in `.env` stops all hub
dial-outs.

### `HfHubHTTPError: 401 Client Error: Unauthorized`

`HUGGING_FACE_HUB_TOKEN` is missing or wrong, or your HuggingFace account has not
accepted the model's licence. Accept it on the model's HuggingFace page, create a
read-scope token, put it in `.env`, then `docker compose up -d --force-recreate vllm-llm`.

### `torch.OutOfMemoryError` or the model does not fit

The GB10 has one 128 GB memory pool for the GPU and the OS. Nemotron at
`LLM_GPU_MEM_UTIL=0.70` takes ~70 GiB, which leaves no room for ComfyUI or Fish TTS
beside it. vLLM sizes itself from the memory that is free when it starts, so two
large servers that start at the same time both over-commit. Check `free -g` and
`docker stats`, stop the other large workload, then restart `vllm-llm`. The Fish
TTS file waits for a healthy `vllm-llm` for this reason.

### `The quantization method modelopt is not supported for the current GPU`

The NVFP4 checkpoints need a Blackwell GPU. On other hardware use a model and
quantization that fits it; see [`CUSTOMIZATION.md`](CUSTOMIZATION.md#a-model-this-repo-does-not-ship).

### Gemma prints `call:name{...}` text instead of making tool calls

The Gemma tool chat template is not mounted. It ships in `compose/gemma4/`, and
`compose/gemma4.yml` mounts it at `/templates`:

```bash
docker exec vllm-llm ls /templates/    # tool_chat_template_gemma4.jinja
```

If it is missing, check that `COMPOSE_FILE` includes `compose/gemma4.yml`, then
recreate `vllm-llm`.

### Turns die as "timed out" while vLLM is still working

A long prefill (a coding session's context, a big tool result) can take minutes on
the GB10. Config-init raises the provider timeout (`LLM_REQUEST_TIMEOUT_SECONDS`)
and the per-run timeout (`OPENCLAW_AGENT_TIMEOUT_SECONDS`) to 1800 s for this; check
that neither was lowered in `.env`.

For tests through the CLI, `openclaw agent` has its own `--timeout`, and its default
is too short for tool-using runs. Use `--timeout 600`:

```bash
docker exec openclaw-cli openclaw agent --agent main \
  --message "Use web_search to find the title of docker.com." \
  --thinking off --json --timeout 600
```

### The model never thinks, or turns end with an empty answer

- **Never thinks:** the catalog entry needs the right `compat` for the model family,
  or OpenClaw sends no thinking control at all. `LLM_THINKING_COMPAT=auto` picks it
  from `LLM_MODEL_ID`; a model id config-init does not recognize gets none. See
  [`reference/llm-stack.md`](reference/llm-stack.md#thinking-switch-catalog-compat).
- **Empty answers after a long think:** thinking and answer share `LLM_MAX_TOKENS`
  (default 32768). At 8192 a long think plus a large tool-call JSON ran out.

### Hungarian replies contain characters from other scripts

Nemotron samples at temperature 1.0 from its own generation config, and at 1.0
Hungarian replies picked up foreign-script characters mid-word. `LLM_TEMPERATURE=auto`
sets 0.6 for Nemotron; check that it was not overridden.

### vLLM answers 400 and the turn dies when a conversation has many images

vLLM accepts at most `LLM_LIMIT_MM_IMAGES` images per prompt (default 4). The Discord
prompt carries a rule about earlier images for this reason; raise the limit only if
the memory budget allows it.

## GB10 host: power-offs and freezes

Details and measurements: [`reference/gb10-power-thermal.md`](reference/gb10-power-thermal.md).

### The box switches off under load, with nothing in the journal

Thermal or power protection. Under sustained LLM load a GB10 cuts out at about
94 °C on the hottest `acpitz` zone, far below the 104.8 °C ACPI trip, and
`nvidia-smi` reads 8-15 °C lower than that zone. The fix is a GPU clock cap,
installed as a systemd unit because it does not survive a reboot:

```bash
sudo cp scripts/gb10-clock-cap.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gb10-clock-cap
```

It locks the graphics clock at 2200 MHz (`nvidia-smi -pl` is not available on
GB10). If the box still shuts down, lower the cap to 2100, then 2000.
`scripts/gb10-thermguard.sh` (from cron) is the backstop: it kills the LLM
containers at 93 °C and starts them again after the box has cooled.

### After a power-off the GPU is stuck at 611 MHz and everything is slow

The USB-C power-delivery controller is wedged. `nvidia-smi -rgc` does not help;
only a cold drain does: power off, unplug the PSU from the wall and from the box,
hold the power button for 30 s, wait a minute, power on.

### The host freezes during a model load (no SSH, `docker ps` hangs)

The unified pool ran out. The kernel then thrashes in reclaim instead of
OOM-killing, and swap makes it slower, not safer. Recover from the local console
with `sudo pkill -9 -f '[vV][lL][lL][mM]'`. To keep it from happening:

- `scripts/gb10-memguard.sh` (from cron) kills the listed model containers while
  `MemAvailable` is still above 6 GiB.
- `sudo bash scripts/gb10-resilience-setup.sh` once: kernel memory reserve, lower
  swappiness, hardware watchdog, sshd kept out of the OOM killer.
- Checkpoints with huge shards: `scripts/reshard-safetensors.py` splits them, since
  vLLM keeps a whole shard mapped while it loads.

### `vllm-llm` stays stopped after a guard killed it

A killed container counts as stopped by hand, so `restart: unless-stopped` does not
bring it back. `scripts/llm-ensure.sh` (from cron) starts it again, unless
thermguard still holds it for heat or `~/.llm-ensure-off` exists.

## Gateway and CLI

### CLI commands fail with network errors after a gateway restart or a reboot

`openclaw-cli` (and `openclaw-meshcore-bridge`) run in the gateway's network
namespace. A sharer that was not recreated together with the gateway keeps running
in the old, dead namespace: no network at all, while `docker ps` still shows it Up.
Typical output: `Gateway agent failed; falling back to embedded`, then
`LLM request failed: network connection error`. After a reboot the daemon may also
start a sharer before the gateway, which fails once and is not retried;
`openclaw-cli` has no restart policy at all.

Fix it now with the trio recreate (add `--profile meshcore` and
`openclaw-meshcore-bridge` if you run it). Fix it for good with the watcher, which
starts, restarts or recreates the sharers after every gateway start:

```bash
S=$PWD/scripts/netns-dependents-up.sh
( crontab -l 2>/dev/null | grep -v netns-dependents-up.sh
  echo "@reboot $S --watch >> $HOME/netns-dependents.log 2>&1"
  echo "*/5 * * * * $S --watch >> $HOME/netns-dependents.log 2>&1" ) | crontab -
```

Details: [`reference/patterns.md`](reference/patterns.md#openclaw-cli-network-namespace-dependency).

### A tool the bot has on Discord is missing in `openclaw agent` tests

Some tools are owner-gated and only appear for a real owner sender, which a CLI run
does not have. Test those on Discord, or read the agent's actual catalog with
`openclaw gateway call tools.effective` instead of judging from a CLI run.

### The image or vision tool still calls an old model URL

`agents/<id>/agent/models.json` is a per-agent cache of the providers, regenerated
only when it is missing; the image tool reads it, text generation does not. After a
provider URL change, delete it and let the next run rebuild it:

```bash
docker exec openclaw-gateway sh -c 'rm -f /home/node/.openclaw/agents/*/agent/models.json'
```

### Every CLI command takes a few seconds before output

Node.js cold start: each `openclaw` invocation is a fresh process. Not fixable from
this stack; call the gateway's HTTP API for high-frequency scripted use.

## Discord bot

Configuration behind these entries: [`reference/discord-config.md`](reference/discord-config.md).

### The bot does not answer in a channel

By design it answers only when @mentioned or when someone replies to one of its
messages (`OPENCLAW_DISCORD_REQUIRE_MENTION=on`). Check:

- The mention is a real mention pill (pick the bot from Discord's autocomplete), not
  the typed text "@Name". See [`reference/discord-text-agent.md`](reference/discord-text-agent.md#mentions).
- `/activation always` does not open the gate; it is only a hint to the model. To
  have the bot read every message in specific channels, list them in
  `OPENCLAW_DISCORD_ALWAYS_CHANNELS`.
- The plugin is connected:
  `docker exec openclaw-cli openclaw channels status --deep | grep -i discord`.

### The bot ignores a DM

DMs are answered only for people who share a server with the bot (the
`dm-guild-gate` plugin). The decision is logged, structure only:

```bash
docker exec openclaw-gateway tail -n 20 /home/node/.openclaw/logs/dm-guild-gate.jsonl
```

Outcomes: `allow`, `block` (not a member of any of the bot's servers), `error_allow`
(Discord gave no clear answer, so the DM went through). Members are cached for 6 h,
non-members for 10 minutes. To let a specific person in, add their ID to `allow` in
`plugins.entries.dm-guild-gate.config`; `OPENCLAW_DISCORD_DM_GUILD_GATE=off` removes
the gate.

### The bot says it did something it did not, or states a fact it did not check

The `claim-guard` plugin checks each answer: claimed actions against the run's
successful tool calls, and specific, uncommon facts against whether any search ran.
On a mismatch it asks for one more model pass. Its log holds structure only:

```bash
docker exec openclaw-gateway tail -n 20 /home/node/.openclaw/logs/claim-guard.jsonl
```

| Outcome | Meaning |
|---|---|
| `none` | nothing to fix |
| `revise` | a second pass was requested |
| `resolved` | the second pass fixed it |
| `unresolved` | still wrong after the second pass |
| `judge_failed` | judge timeout or unreadable JSON; the answer went out unchanged |

Many `judge_failed` lines mean the model is overloaded or slow. It does not catch a
tool result the bot then misreads: the tool ran, so the claim counts as backed.
After changing the plugin, rebuild `openclaw-base-ext` and recreate the gateway;
`openclaw plugins reload` cannot replace it while a run holds the old instance.

### The bot does not remember people or past conversations in channels

OpenClaw never loads the root `MEMORY.md` into channel or group sessions (a privacy
filter). Guild long-term memory therefore lives in
`workspace-discord/people/MEMORY.md`, which is loaded into every Discord session, and
in the community summaries under `workspace-discord/memory/community/`, which memory
search finds. Check, in order:

- The nightly people-memory job and the community-summary job exist. They are
  created once per install with `openclaw cron add`; the commands are in
  [`reference/discord-config.md`](reference/discord-config.md#long-term-memory-in-guild-channels).
- `scripts/daily-status.sh` prints the people file's size and section count, the
  number of community summaries and the rejected proposals of the nightly job.
- What a session really loads: `systemPromptReport.injectedWorkspaceFiles` in the
  output of `openclaw agent --json`.

The bot writes the people file only through `/opt/people-digest.mjs note|forget`,
which heals the file from its last good copy if a section went missing.

### The heartbeat never sends a DM

- An empty `HEARTBEAT.md` means the poll is skipped.
- The DM goes to `OPENCLAW_HEARTBEAT_TO`, or the first ID in
  `OPENCLAW_DISCORD_COMMAND_OWNERS`. Without either, the upstream `owner` target
  finds no DM and every poll logs `reason=no-route`.
- Outside `OPENCLAW_HEARTBEAT_ACTIVE_START` / `_END` (in `OPENCLAW_HEARTBEAT_TZ`)
  nothing runs.

### Other Discord symptoms

| Symptom | Cause |
|---|---|
| The bot says image, video or music generation is off | The media tools are off until the ComfyUI bridge runs and `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` allows them; see [`openclaw-image-comfyui/README.md`](../openclaw-image-comfyui/README.md). |
| Links and images do not unfurl | The plugin suppresses embeds unless `suppressEmbeds` is explicitly false; config-init writes that (`OPENCLAW_DISCORD_SUPPRESS_EMBEDS=off`). |
| The bot cycles emoji reactions on a message | A stale auto-ack queue replaying; `OPENCLAW_DISCORD_ACK_REACTION_SCOPE=off` (the default) turns auto-acks off. |
| A second person's request got folded into the first one's answer | A guild channel is one session. `OPENCLAW_DISCORD_QUEUE_MODE=followup` (the default) queues it instead of steering the running turn. |
| A code answer arrives as many short messages | `OPENCLAW_DISCORD_MAX_LINES` (default 60) sets the lines per message. |

## Web search

### Searches return few, off-topic or no results

A blocked SearxNG engine fails silently: no error, just fewer results, and the
model answers from whatever is left. Check which engines answer:

```bash
docker exec openclaw-cli curl -sS "http://searxng:8080/search?q=test&format=json" \
  | jq '{n: (.results | length), down: .unresponsive_engines}'
```

`scripts/daily-status.sh` runs one Hungarian and one English query each day and
prints a `search` line with the engines that answered and the ones that failed. The
SearxNG image is re-pulled weekly (`SEARXNG_PULL_POLICY`) because scraped engines
break as sites change. To compare engines on your own IP, use
`scripts/websearch-bench.py`; how to change the engine list is in
[`CUSTOMIZATION.md`](CUSTOMIZATION.md#tune-web-search-searxng).

### The agent has no `web_search` tool

The bundled searxng plugin ships disabled; config-init enables it and sets the
provider. Check that config-init ran after onboarding (`docker logs
openclaw-config-init | grep searxng`), and recreate the trio if it did not.

## Memory search

### Memory search finds nothing, although the notes are there

Check what the index reports:

```bash
docker exec openclaw-cli openclaw memory status --deep
```

Look for `Embeddings: ready` and a vector dimension of 1024 (bge-m3). An upgrade or
rollback can lose the index metadata, and the search then reports paused or empty
while the Markdown files are intact. Rebuild the index:

```bash
docker exec openclaw-cli openclaw memory index --force
```

Each agent has its own index (`main` and the Discord agent separately); rebuild the
one that is affected.

### The index is incomplete after `vllm-embedding` restarted

Under memory pressure the embedding server can die with
`CUDA error: operation not permitted`. Docker restarts it within seconds, but the
chunks being indexed at that moment are skipped, and OpenClaw does not mark their
files dirty again, so `memory status` still says `Dirty: no`. If the indexed file
count is below the real one and
`docker inspect vllm-embedding --format '{{.RestartCount}}'` is not zero, run
`openclaw memory index --force` as above. Tracked upstream:
[openclaw/openclaw#70567](https://github.com/openclaw/openclaw/issues/70567).

### Embedding calls return 401

The embedding server uses `VLLM_API_KEY`, the same key as the LLM. If you changed
the key, recreate both vLLM services and the trio.

### `memory status --deep` shows 0 chunks

Nothing has been indexed yet. Write a note under the workspace's `memory/` folder,
then run `openclaw memory index --force`.

## Optional components

Each of these has its own document; the entries here are the first things to check.

- **Browser** ([`reference/browser-automation.md`](reference/browser-automation.md)):
  the service runs only with `browser` in `COMPOSE_PROFILES`, and config-init wires
  it only with `BROWSER_API_TOKEN` set. A login that expired: re-run
  `./bootstrap-browser-login.sh <profile>`. Passkeys and WebAuthn do not work over
  the noVNC login helper (they are bound to the origin); use a password plus TOTP.
  Profiles past the 20th in `BROWSER_PROFILE_NAMES` are skipped with a warning,
  because 20 CDP ports are published.
- **Python sandbox** ([`reference/python-sandbox.md`](reference/python-sandbox.md)):
  needs `python` in `COMPOSE_PROFILES` and `PYTHON_SANDBOX_API_TOKEN`. MCP tools are
  named `<server>__<tool>` in the catalog (`python_sandbox__python_exec`); use that
  name in test prompts. Long computations hit `PYTHON_SANDBOX_KERNEL_TIMEOUT_S`
  (the kernel is interrupted, its state survives); large data hits
  `PYTHON_SANDBOX_MEMORY_MB` (the kernel is killed and restarts empty).
- **Fish TTS** ([`openclaw-tts-fish/README.md`](../openclaw-tts-fish/README.md#troubleshooting)):
  `compose/tts-fish.yml` in `COMPOSE_FILE` plus `OPENCLAW_TTS_FISH_API_KEY`. The
  first start compiles kernels for a long time.
- **ComfyUI bridge** ([`openclaw-image-comfyui/README.md`](../openclaw-image-comfyui/README.md)): runs
  from its own compose file; config-init wires it only with `IMAGE_GEN_API_TOKEN`
  set. ComfyUI shares the GPU pool and does not fit next to Nemotron.
- **MeshCore** ([`reference/meshcore-bridge.md`](reference/meshcore-bridge.md)):
  `No response from meshcore node` is the radio, not the gateway; the runbook there
  covers the serial device and companion recovery.

## Host

### `Failed to initialize NVML` inside a container

The NVIDIA container toolkit is missing, or Docker was not restarted after it was
configured:

```bash
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Containers exit with code 128 after a power-off

The state is on a USB drive that was not plugged in at boot. With
`nofail,x-systemd.automount` in fstab the host boots without it, and every
container with a bind mount on it exits. Plug the drive in before powering on, or
`docker start` the containers once it is back.

### The disk fills up

Each service keeps at most 3 × 50 MB of logs (`x-logging` in `docker-compose.yml`).
The usual growth is model weights in `VLLM_HF_CACHE_DIR` (~100 GB with the default
model, more per extra model) and old images:

```bash
docker system df          # images, volumes, build cache
docker image prune -a     # removes every image no container uses
```
