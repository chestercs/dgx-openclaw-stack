# Configuration reference

`.env` is read in two places. Docker Compose interpolates it into the container
settings (`${VAR:-default}` in `docker-compose.yml` and `compose/*.yml`), and
`openclaw-config-init` (`config-init/`) reads it on every `docker compose up` to
write `openclaw.json` and the agent workspace texts. Config-init switches take
`on`/`true`/`1`/`yes` or `off`/`false`/`0`/`no`; an empty value (`FOO=`) means unset,
so the default applies. An invalid switch or enum value stops config-init with an
error, and the gateway does not start until it is fixed. Compose-only knobs are
passed to the container as written, so they use whatever that service expects.
Secrets live only in `.env`: `bootstrap.sh` fills every `CHANGE_ME`, `rotate-secrets.sh`
rotates them, and config-init masks every token it copies into `openclaw.json` in its
`[patch-config]` log.

Defaults below are exactly what the code uses when the knob is unset. "(unset)"
means there is no default and the feature or key is left alone.

## Paths & compose

| Knob | Default | What it does |
|---|---|---|
| `COMPOSE_FILE` | `docker-compose.yml` | Colon-separated compose files; add `compose/gemma4.yml`, `compose/mistral.yml`, `compose/tts-fish.yml` or `compose/claw-img.yml` here. |
| `COMPOSE_PROFILES` | (unset) | Optional services: `browser`, `python`, `meshcore`. |
| `CONTAINER_NAME_PREFIX` | (empty) | Prefix for every `container_name`. Service-to-service DNS uses the service name, so it does not affect wiring. |
| `TZ` | `UTC` | Time zone of the gateway, CLI, MeshCore bridge and image bot containers. |
| `OPENCLAW_CONFIG_DIR` | (required) | Host dir for OpenClaw state (`openclaw.json`, sessions, agent workspaces). Must be writable by UID 1000. |
| `OPENCLAW_WORKSPACE_DIR` | (required) | Host dir of the `main` agent's workspace. |
| `OPENCLAW_SANDBOX_DIR` | `${OPENCLAW_WORKSPACE_DIR}/sandbox` | Folder shared by the agent and the Python sandbox, so code the agent writes is visible to both. |
| `OPENCLAW_CANVAS_DIR` | `${OPENCLAW_CONFIG_DIR}/canvas` | Generated and downloaded media the agent attaches to replies; kept apart from the config dir so it does not sit next to tokens. |
| `OPENCLAW_MEDIA_INBOUND_DIR` | `${OPENCLAW_CONFIG_DIR}/media/inbound` | Files users send in (Discord attachments). |
| `VLLM_HF_CACHE_DIR` | (required) | Host dir bound as the HuggingFace cache (~100 GB with the default model); survives `down -v`. |
| `VLLM_HF_CACHE_VOLUME_NAME` | `dgx-openclaw-hf-cache` | Docker volume name of that cache; reuse it to share weights with a sibling stack. |
| `OPENCLAW_IMAGE_REF` | `:latest` | Tag or `@sha256:` digest of `ghcr.io/openclaw/openclaw` that `openclaw-base-ext` builds on. Pin a digest for reproducible upgrades. |
| `OPENCLAW_BASE_EXT_VERSION` | `local` | Tag of the locally built `openclaw-base-ext` image the three OpenClaw containers run. |

## LLM & embeddings

| Knob | Default | What it does |
|---|---|---|
| `LLM_MODEL_ID` | `nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4` | The id vLLM serves and config-init registers as the only `vllm/...` model; every other `vllm/` entry is removed because a stale one answers 404. |
| `VLLM_API_KEY` | (empty) | API key of both vLLM servers, also used by memory search. Without it config-init leaves `memory.search` as onboarding wrote it. |
| `OPENCLAW_SECRET_REFS` | `on` | `on`: `openclaw.json` holds `{source: "env", id: "VLLM_API_KEY"}`-style references for the vLLM and TTS keys and the gateway resolves them from its environment, so the values stay out of the config file and `openclaw secrets audit` is clean for them. `off`: the plain values are written. |
| `HUGGING_FACE_HUB_TOKEN` | (empty) | HF token for model downloads (vLLM, Whisper, Fish build). Accept the model licence on its HF page first. |
| `HF_HUB_OFFLINE` | `0` | `1` stops all hub dial-outs once weights are cached: faster boots, no hang when the uplink is down. |
| `VLLM_LOGGING_LEVEL` | `INFO` | vLLM log verbosity (`ERROR`, `WARNING`, `INFO`, `DEBUG`). |
| `LLM_IMAGE` | `vllm/vllm-openai:v0.32.0` | vLLM image of `vllm-llm`. |
| `LLM_BIND` | `127.0.0.1` | Host interface of the published port 8004. `0.0.0.0` exposes the API to the LAN with `VLLM_API_KEY` as the only barrier. |
| `LLM_GPU_MEM_UTIL` | `0.70` | Share of the unified pool vLLM takes; Nemotron needs ~70 GiB at 0.70, which leaves no room for ComfyUI or Fish TTS beside it. |
| `LLM_MAX_MODEL_LEN` | `131072` | vLLM's maximum context length. |
| `LLM_MAX_NUM_SEQS` | `4` | Concurrent sequences vLLM schedules. |
| `LLM_DRAFT_MODEL` | `nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr` | Draft model for dflash speculative decoding. |
| `LLM_SPEC_TOKENS` | `7` | Speculative tokens per step. |
| `LLM_LIMIT_MM_IMAGES` | `4` | Images per prompt; past it vLLM answers 400 and the turn dies, which is why the Discord prompt carries an image-history rule. |
| `LLM_BASE_URL` | `http://vllm-llm:8004/v1/` | OpenAI-compatible base URL OpenClaw calls; point it at a remote vLLM or a cloud endpoint. Keep the trailing slash. |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `1800` | Provider request timeout. A long prefill on the GB10 outruns the ~120 s upstream watchdog and the turn dies as "timed out". |
| `LLM_CONTEXT_WINDOW` | `131072` | Context OpenClaw budgets per prompt (`contextWindow` and `contextTokens`). A full 128K prefill already takes ~2 minutes on the GB10. |
| `LLM_MAX_TOKENS` | `32768` | Output budget shared by thinking and answer; at 8192 a long think plus a large tool-call JSON ended turns empty. |
| `LLM_THINKING_COMPAT` | `auto` | `auto` picks the thinking control from the model id (Gemma 4 and Nemotron: `qwen-chat-template`; Mistral: `reasoning_effort` none/high); `qwen-chat-template` forces it; `off` writes none. Without it OpenClaw sends no thinking control and the model never thinks. |
| `LLM_TEMPERATURE` | `auto` | `auto` = 0.6 for Nemotron, upstream default otherwise; a number sets it; `off` removes it. At Nemotron's 1.0, Hungarian replies filled with other scripts mid-word (38 stray characters in six answers, 2 at 0.6). |
| `EMBED_IMAGE` | `vllm/vllm-openai:gemma4-cu130` | vLLM image of the bge-m3 embedding server. |
| `EMBED_GPU_MEM_UTIL` | `0.03` | Pool share of the embedding server. |
| `EMBED_MAX_MODEL_LEN` | `8192` | Maximum input length for embeddings. |
| `EMBED_MAX_NUM_SEQS` | `2` | Concurrent embedding requests. |
| `EMBED_BASE_URL` | `http://vllm-embedding:8005/v1/` | Embedding endpoint memory search calls. |

## Web search

| Knob | Default | What it does |
|---|---|---|
| `SEARXNG_SECRET` | (empty) | SearxNG's secret key (generated by `bootstrap.sh`). |
| `SEARXNG_PULL_POLICY` | `weekly` | How often the unpinned SearxNG image is re-pulled. Its engines scrape sites that change, and a months-old image silently degrades `web_search`; `missing` pulls once. |

The search wiring itself (provider `searxng`, categories `general,news`) has no knob;
engines and privacy settings are in `searxng/settings/settings.yml`.

## Speech (STT/TTS)

| Knob | Default | What it does |
|---|---|---|
| `STT_API_TOKEN` | (empty) | Bearer token of the Whisper service. Config-init wires Whisper as the first audio model only when it is set; the Python sandbox's transcription tool uses it too. |
| `STT_WHISPER_BIND` | `127.0.0.1` | Host interface of the published Whisper port. |
| `STT_WHISPER_PORT` | `8093` | Host port of the Whisper service. |
| `STT_WHISPER_MODEL` | `deepdml/faster-whisper-large-v3-turbo-ct2` | Model the Whisper container loads (~8x faster than large-v3, ~1.6 GB). `Trendency/whisper-large-v3-hu` is slower but more accurate on noisy Hungarian. |
| `STT_WHISPER_COMPUTE_TYPE` | `float16` | CTranslate2 compute type. |
| `WHISPER_LANGUAGE` | (empty) | Server-side default language; empty = detect per request. |
| `OPENCLAW_STT_BASE_URL` | `http://openclaw-stt-whisper:8080/v1` | Whisper URL in `tools.media.models[]`; a trailing slash is added. |
| `OPENCLAW_STT_MODEL` | `deepdml/faster-whisper-large-v3-turbo-ct2` | Model name OpenClaw sends. The server only ever runs `STT_WHISPER_MODEL`, so keep them the same for readable logs. |
| `OPENCLAW_STT_LANGUAGE` | (unset) | Language OpenClaw requests; unset = autodetect. |
| `OPENCLAW_TTS_FISH_API_KEY` | (empty) | Token of the Fish TTS shim (`compose/tts-fish.yml`). When set, config-init writes the top-level `tts` block. |
| `OPENCLAW_TTS` | on if the key is set | `off` removes the whole `tts` key; `on` without the key is an error. Unset with no key leaves `tts` alone. |
| `OPENCLAW_TTS_AUTO` | `always` | When replies get audio: `off`, `always`, `inbound`, `tagged`. Use `tagged` on Discord text channels: with `always` every reply gets an attachment and a failed transcode drops the text too. |
| `OPENCLAW_TTS_TIMEOUT_MS` | `120000` | TTS request timeout, capped at the schema's 120000. The 30 s gateway default times out on Fish's ~2000-char chunks (60-110 s each on the GB10). |
| `OPENCLAW_TTS_FISH_URL` | `http://openclaw-tts-fish:8080/v1` | Shim URL. |
| `OPENCLAW_TTS_DEFAULT_VOICE` | `default_en` | Voice OpenClaw asks for; friendly aliases (`magyar`, `female`, `british`, ...) are written too. |

The Fish container's own knobs are under [Optional model files](#optional-model-files).

## Browser

Profile `browser`. Details: [browser-automation.md](browser-automation.md).

| Knob | Default | What it does |
|---|---|---|
| `BROWSER_API_TOKEN` | (empty) | CDP token. When set, config-init enables the `browser` tool with one CDP port per profile. |
| `BROWSER_PROFILE_NAMES` | (empty) | Extra named login profiles after `self-hosted`, comma-separated. Order is the port mapping (base+1, base+2, ...): append, never reorder. 20 ports are published. |
| `BROWSER_VNC_PASSWORD` | (empty) | Password of the noVNC login helper; the browser refuses to start without it. |
| `BROWSER_DEFAULT_TIMEOUT_MS` | `60000` | Navigation timeout; 30000 tripped on heavy pages and leaked tabs on every timeout. |
| `BROWSER_BIND` | `127.0.0.1` | Host interface of the API port and the CDP ports 9222-9241. |
| `BROWSER_API_PORT` | `9220` | Host port of the browser's control API. |
| `BROWSER_VNC_BIND` | `127.0.0.1` | Host interface of the VNC port. |
| `BROWSER_VNC_PORT` | `5901` | Host port of VNC. |
| `BROWSER_CDP_HOST` | `http://openclaw-browser` | CDP host in each profile's `cdpUrl`; only this hostname is exempted from the gateway's SSRF guard. |
| `BROWSER_PORT_BASE` | `9222` | First CDP port. Must match the compose port range. |
| `OPENCLAW_BROWSER_DEFAULT_PROFILE` | `self-hosted` | Default profile, written only if it exists. Without one the tool falls through to sandbox/host targets this stack does not run. |
| `OPENCLAW_SANDBOX_BROWSER` | `off` | OpenClaw's sandbox browser. No such container runs here; on, it only adds a noVNC hint, the model then targets `sandbox` and fails. |

## Python sandbox

Profile `python`. Details: [python-sandbox.md](python-sandbox.md).

| Knob | Default | What it does |
|---|---|---|
| `PYTHON_SANDBOX_API_TOKEN` | (empty) | MCP token. When set, config-init registers the `python_sandbox` MCP server; unset removes it so the gateway does not dial a parked service. |
| `PYTHON_SANDBOX_URL` | `http://openclaw-python-sandbox:8094/mcp` | MCP endpoint. Calls get 120 s (the MCP SDK's own 60 s was too short for downloads and installs). |
| `PYTHON_SANDBOX_BIND` | `127.0.0.1` | Host interface of the MCP port. |
| `PYTHON_SANDBOX_PORT` | `8094` | Host port of the MCP server. |
| `SANDBOX_PREVIEW_BIND` | `0.0.0.0` | Host interface of the static preview server for web apps the agent builds. |
| `SANDBOX_PREVIEW_PORT` | `8095` | Host port of the preview server. |
| `OPENCLAW_SANDBOX_PUBLIC_URL` | `http://localhost:8095` | URL the coding skill tells users their web app is at; usually the reverse-proxy name of the preview port. |
| `PYTHON_SANDBOX_KERNEL_TIMEOUT_S` | `30` | Per-execution limit; the kernel is interrupted, not killed, so session state survives. |
| `PYTHON_SANDBOX_MEMORY_MB` | `8192` | Container memory limit. |
| `PYTHON_SANDBOX_CPUS` | `4` | Container CPU limit. |
| `GITHUB_TOKEN` | (empty) | Token for the `git_push` tool, kept away from the agent's code. A classic token with `repo` scope also lets it create a missing repo. |
| `GITHUB_REPO` | (empty) | Default `owner/repo` when a `git_push` call names none. |
| `GIT_AUTHOR_NAME` | `openclaw-bot` | Commit author name. |
| `GIT_AUTHOR_EMAIL` | `openclaw-bot@localhost` | Commit author email. |

## Gateway & sessions

| Knob | Default | What it does |
|---|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | (empty) | Gateway auth token passed to the gateway container (generated by `bootstrap.sh`). |
| `OPENCLAW_GATEWAY_PORT` | `18789` | Host port of the gateway (Web UI, WebSocket). |
| `OPENCLAW_GATEWAY_BIND` | `lan` | `lan` listens on all interfaces; `local` only on loopback. |
| `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS` | (empty) | `1` when a TLS-terminating reverse proxy forwards plaintext `ws://` to a private-IP gateway. |
| `OPENCLAW_LAN_CIDR` | (unset) | Extra range for `gateway.trustedProxies` (loopback and `172.16.0.0/12` are always there). Add your LAN when clients hit the gateway directly, or they show up as the proxy's address. |
| `OPENCLAW_DEVICE_PAIR_PUBLIC_URL` | (unset) | `wss://` URL `openclaw qr` advertises; otherwise it shows the bridge IP and a plaintext URL downgrades setup codes to "Access: limited". `off` removes it. |
| `OPENCLAW_GATEWAY_NODE_COMMANDS_ALLOW` | (unset) | Exact node command names to admit beyond the per-platform default (Windows leaves out `canvas.*`, `tts.*`, `stt.*`). `off` removes the list. |
| `OPENCLAW_HEARTBEAT_ACTIVE_START` | `09:00` | Start of the heartbeat window (every 30 min, isolated session). |
| `OPENCLAW_HEARTBEAT_ACTIVE_END` | `02:00` | End of the heartbeat window. |
| `OPENCLAW_HEARTBEAT_TZ` | `UTC` | Time zone of the heartbeat window and of nightly dreaming (03:00). |
| `OPENCLAW_HEARTBEAT_TO` | first `OPENCLAW_DISCORD_COMMAND_OWNERS` ID | Discord user who gets heartbeat DMs. The upstream `owner` target finds no DM (`reason=no-route`); `off` leaves delivery to OpenClaw. |
| `OPENCLAW_BOOTSTRAP_MAX_CHARS` | `60000` | Per-file cap on injected workspace files. Past it the file is cut silently; the Discord AGENTS.md is ~29 KB and config-init warns when it outgrows the cap. |
| `OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS` | `80000` | Total cap on injected workspace files. |
| `OPENCLAW_AGENT_TIMEOUT_SECONDS` | `1800` | Per-run timeout, aligned with `LLM_REQUEST_TIMEOUT_SECONDS`; the upstream 600 s aborted runs vLLM was still serving. `off` removes it. |
| `OPENCLAW_AGENT_CONTEXT_PRUNING_MODE` | `cache-ttl` | Trims old tool results from the in-memory context before each model call, keeping prefill bounded on long coding runs. `off` removes it. |
| `OPENCLAW_AGENT_CONTEXT_PRUNING_TTL` | `5m` | Age after which tool results are trimmed. |
| `OPENCLAW_SUBAGENTS_BOUNDS` | `on` | Sub-agent limits: depth 2, 5 children, 8 concurrent, no run timeout (for day-long work). Out-of-range values crash-loop the gateway, so they are fixed. |
| `OPENCLAW_SESSION_THREAD_SPAWN` | `on` | `sessions_spawn {thread:true}` may create a thread-bound session that starts from a clean transcript. |
| `OPENCLAW_SYSTEM_AGENT` | `main` | Agent that owns system consults (node approvals); the Discord persona has no gateway tools. Written only if that agent exists; `off` skips. |
| `OPENCLAW_STATUS_REACTIONS` | `on` | Run-state emoji on the user's message; with a slow local model it is the first sign the bot is working. |
| `OPENCLAW_MESSAGES_QUEUE_MODE` | `steer` | What a message sent mid-run does: `steer` folds it into the running turn, `followup`, `collect`, `interrupt`. |
| `OPENCLAW_DISCORD_QUEUE_MODE` | `followup` | Same for Discord. A guild channel is one shared session, and `steer` merged a second user's request into the first one's run. |
| `OPENCLAW_ENABLE_DREAMING` | `on` | Nightly memory consolidation into MEMORY.md (separate storage, so session metadata does not rank in memory search). `off` removes the plugin entry. |
| `OPENCLAW_DREAMING_MAX_PROMOTED_SNIPPET_TOKENS` | `80` | Tokens per promoted snippet; MEMORY.md is in every bootstrap and grew to ~12 KB unbounded. |
| `OPENCLAW_DREAMS_ROTATE_BYTES` | `32768` | DREAMS.md over this size moves to `memory/dreams-archive/` (still searchable). `0` disables rotation. |
| `OPENCLAW_ACTIVE_MEMORY` | `on` | Recall before every reply of the `discord-friend` agent (mode `always`, 30 s, thinking off). The upstream `escalate` mode never fired on Hungarian. `off` removes it. |

## Tools & exec

Details: [agentic-coding.md](agentic-coding.md).

| Knob | Default | What it does |
|---|---|---|
| `OPENCLAW_TOOLS_EXEC` | `on` | The exec shell. `off` removes `tools.exec` (upstream default-deny) and the Discord approval routing. |
| `OPENCLAW_EXEC_SECURITY` | `allowlist` | `allowlist` = commands in `exec-approvals.json` run, others become an approval prompt; `full` = no gate, so anyone the bot answers can run commands in a container that mounts the secrets. |
| `OPENCLAW_EXEC_TIMEOUT_SEC` | `1800` | Exec timeout; npm installs and cmake builds on the GB10 outrun the upstream default. |
| `OPENCLAW_EXEC_SAFE_BINS` | `cat,grep,sed,head,tail,cut,wc` | Stdin-only filters that run without approval. Keep interpreters out. |
| `OPENCLAW_TOOLS_AGENT_TO_AGENT` | `on` | `sessions_send` to another agent id (Discord persona to `main`). |
| `OPENCLAW_TOOLS_AGENT_TO_AGENT_ALLOW` | `*` | Agents that may be messaged. |
| `OPENCLAW_TOOLS_PLAN_TOOL` | `on` | The `update_plan` tool for multi-step work. |
| `OPENCLAW_TOOLS_LOOP_DETECTION` | `on` | Circuit breaker for repeated tool calls. |
| `OPENCLAW_TOOLS_LOOP_DETECTION_HISTORY_SIZE` | (upstream 30) | Calls the breaker remembers. A build-fix-build loop looks like a loop to it, so raise these for long coding runs. |
| `OPENCLAW_TOOLS_LOOP_DETECTION_WARNING_THRESHOLD` | (upstream 10) | Repeats before a warning. |
| `OPENCLAW_TOOLS_LOOP_DETECTION_CRITICAL_THRESHOLD` | (upstream 20) | Repeats before the run is stopped. |
| `OPENCLAW_TOOL_SEARCH` | `off` | `on` / `directory` hide tools behind a search meta-tool. Off, because MCP results then come back as JSON text: ~13.5k tokens of base64 per image, and the image never reached vision input. |
| `OPENCLAW_DENY_OPENAI_VIDEO_TOOL` | `on` | Denies the built-in `video_generate` (OpenAI Sora, 401 without a key; one turn called it 16 times). Video goes through ComfyUI. |
| `OPENCLAW_WORKBOARD` | (unset) | `on` enables the workboard plugin and its skill, `off` removes both; unset leaves the plugin entry alone and removes the skill. |

## Discord channel

Nothing here applies until onboarding has created an enabled `channels.discord`
block. Details: [discord-config.md](discord-config.md).

| Knob | Default | What it does |
|---|---|---|
| `DISCORD_BOT_TOKEN` | (empty) | Read once by `openclaw channels add --channel discord` in the CLI container. |
| `OPENCLAW_DISCORD_COMMAND_OWNERS` | `*` | Owner IDs (`commands.ownerAllowFrom`, written as `discord:<id>`). Also the fallback for exec approvers and the heartbeat DM. |
| `OPENCLAW_DISCORD_COMMANDS_ALLOW` | `*` | Who may run guild slash commands; unset upstream, every guild slash command is "not authorized". |
| `OPENCLAW_TOOLS_ELEVATED_DISCORD_ALLOW` | `*` | Who may use elevated tools from Discord. |
| `OPENCLAW_DISCORD_AUTHZ` | `open` | `open` (everyone, DMs open), `pairing` (DM strangers need owner approval, group DMs off), `owner-only` (needs `OPENCLAW_DISCORD_OWNER_IDS`), `allowlist` (upstream defaults). |
| `OPENCLAW_DISCORD_OWNER_IDS` | (unset) | Snowflakes for `owner-only`; without valid ones authz is left as is rather than locking everyone out. |
| `OPENCLAW_DISCORD_REQUIRE_MENTION` | `on` | The mention gate for every guild: on = only @mentions and replies to the bot get through. Written every run; `/activation` is only a prompt hint and cannot open a closed gate. |
| `OPENCLAW_DISCORD_ALWAYS_CHANNELS` | (unset) | Channel IDs the bot reads in full despite a closed gate; a `*` fallback mirroring the gate is written with them, since unlisted channels would otherwise go silent. `off` removes the map. |
| `OPENCLAW_DISCORD_STREAMING` | `partial` | `off`, `partial`, `block`, `progress`. A placeholder edited as tokens arrive; posted at once, a long answer is a minute of silence. |
| `OPENCLAW_DISCORD_STREAMING_PREVIEW_TOOL_PROGRESS` | (unset) | The "Working... tool: x" lines in the preview. |
| `OPENCLAW_DISCORD_MAX_LINES` | `60` | Lines per message before a split; at the upstream 17 a code answer came out as six messages. `off` leaves the key alone. |
| `OPENCLAW_DISCORD_SUPPRESS_EMBEDS` | `off` | Without an explicit false the plugin suppresses every embed, so no image, video or link unfurls. |
| `OPENCLAW_DISCORD_ACK_REACTION_SCOPE` | `off` | Auto-ack reactions, written only if unset. A stale reaction queue replays them on session resume and the bot cycles emojis. |
| `OPENCLAW_DISCORD_ACTIONS_REACTIONS` | `on` | The agent's own reaction action. |
| `OPENCLAW_DISCORD_REPLY_TO_MODE` | `first` | `off`, `first`, `all`, `batched`. `first` makes a run's first message a native reply, so in a busy channel it is clear which question it answers. |
| `OPENCLAW_DISCORD_VOICE` | `stt-tts` | `/vc` voice with this stack's Whisper and Fish (sets `voice.enabled` if unset). `off` leaves the key alone. |
| `OPENCLAW_DISCORD_THREAD_BINDINGS` | `on` | `/focus`, `/unfocus`: a thread stays on one session (sets `enabled` if unset). |
| `OPENCLAW_DISCORD_THREAD_IDLE_HOURS` | (upstream 24) | Idle hours before a thread is unfocused; coding threads are revisited the next day. |
| `OPENCLAW_DISCORD_THREAD_MAX_AGE_HOURS` | (upstream: never) | Maximum age of a thread binding. |
| `OPENCLAW_DISCORD_EXEC_APPROVALS` | `on` | Exec approval prompts as Discord DMs with buttons; without it a prompt waits on a UI nobody watches and times out to deny. |
| `OPENCLAW_EXEC_APPROVERS` | owner IDs | Approver snowflakes; falls back to `OPENCLAW_DISCORD_OWNER_IDS`, then `OPENCLAW_DISCORD_COMMAND_OWNERS`. Never `*`: anyone could approve the bot's own escalation. |
| `OPENCLAW_DISCORD_AUTO_PRESENCE` | `on` | Presence mirrors runtime health, so a degraded stack shows in the member list. |
| `OPENCLAW_DISCORD_AUTO_PRESENCE_{HEALTHY,DEGRADED,EXHAUSTED}_TEXT` | (unset) | Custom presence texts (three knobs). |
| `OPENCLAW_DISCORD_MENTION_ALIASES` | (unset) | `Handle=snowflake,...` so outbound `@handle` becomes a real mention; the fallback directory is empty right after a restart. Additive; `off` removes the map. |
| `OPENCLAW_DISCORD_ALLOW_BOTS` | (unset) | `mentions` lets other bots in when they tag this one (loop protection caps it), `true`, `false`; `off` removes the key. |
| `OPENCLAW_DISCORD_DEFAULT_TO` | (unset) | `channel:<id>` or `user:<id>` for sends that name no target; without it cron and heartbeat turns invented targets. `off` removes it. |

## Discord agent & prompt text

These apply to the agent(s) a catch-all Discord binding routes to, and to the
marker blocks and skills in `workspace-discord/`.

| Knob | Default | What it does |
|---|---|---|
| `OPENCLAW_DISCORD_AGENT_TOOLS_PROFILE` | `full` | Tool profile; unset, it inherits `coding`, which has no browser, tts or canvas. `off` skips. |
| `OPENCLAW_DISCORD_AGENT_ALSO_ALLOW` | `group:messaging,browser,tts,canvas,cron` | Added on top of the profile; on guild channels `cron` drops out even under `full`. Additive; `off` skips. |
| `OPENCLAW_DISCORD_AGENT_THINKING` | `high` | Default thinking level (`off` leaves it unset); at `minimal` the model skipped tool calls and claimed it could not search. |
| `OPENCLAW_DISCORD_AGENT_TOOLS_DENY` | built-in list (66 tools) | Operator and desktop tools the persona never needs; `full` plus the bundled plugins gave it 113 tools, and a MoE picks badly from a catalog that size. Additive; `off` skips. |
| `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` | `image,video,music` | Media groups ComfyUI does not run: denied on the agent, skills removed, an "off" line in the router so the bot says so instead of calling a dead tool. `none` = all groups on (needs the ComfyUI bridge). |
| `OPENCLAW_DISCORD_MEMBER_NAMES` | (unset) | Names used in the prompt's examples, comma-separated; missing ones become neutral placeholders, so the repo carries no member names. |
| `OPENCLAW_DISCORD_ADMIN_NAME` | (unset) | The admin's display name in the prompt (placeholder `Admin`). |
| `OPENCLAW_DISCORD_ADMIN_ID` | (unset) | The admin's ID, shown next to the name. |
| `OPENCLAW_DISCORD_PEOPLE_MEMORY` | `on` | `people/MEMORY.md` (guild long-term memory) loaded into every Discord session by `bootstrap-extra-files`, the `people-context` plugin and the nightly people-memory skill. The root MEMORY.md never reaches channel sessions. |
| `OPENCLAW_PEOPLE_DIGEST_NSFW` | `off` | Include NSFW channels in the digests. Off because the file reaches every channel's prompt. |
| `OPENCLAW_PEOPLE_DIGEST_EXCLUDE` | (unset) | Channel names or IDs kept out of the digests (e.g. a patch-notes channel). |
| `OPENCLAW_COMMUNITY_SUMMARY` | `on` | Skill for the multi-day community summary into `memory/community/`. |
| `OPENCLAW_DISCORD_CLAIM_GUARD` | `on` | `claim-guard` plugin: checks claimed actions against the run's successful tool calls and asks for one more pass on a mismatch. Audit: `logs/claim-guard.jsonl`. |
| `OPENCLAW_DISCORD_DM_GUILD_GATE` | `on` | `dm-guild-gate` plugin: answers DMs only from people who share a server with the bot (`dmPolicy` has no such mode). Audit: `logs/dm-guild-gate.jsonl`. |
| `OPENCLAW_DISCORD_DEEP_AGENT` | `off` | A full-thinking sub-agent the Discord agent can spawn; it mirrors that agent's tools and workspace. Off: 40/40 against medium's 39/40 on checkable reasoning, at 2-2.5x the time. |
| `OPENCLAW_DISCORD_DEEP_AGENT_ID` | `deep` | Its agent id. |

## MeshCore

Profile `meshcore` plus `MESHCORE_ENABLED=on`. The bridge also reads `.env` directly;
its remaining knobs (chunking, pacing, retries, protocol) are in
[meshcore-bridge.md](meshcore-bridge.md).

| Knob | Default | What it does |
|---|---|---|
| `MESHCORE_ENABLED` | `off` | Config-init registers the MeshCore agent(s) with workspace and AGENTS.md seed. |
| `MESHCORE_AGENT_ID` | `meshcore` | Agent of the private DM rail (read by config-init and the bridge). |
| `MESHCORE_CHANNEL_AGENT_ID` | (unset) | Separate agent for the shared channel, so DM topics stay out of a room several people read; unset = the channel uses the DM agent. |
| `MESHCORE_AGENT_TOOLS_PROFILE` | `minimal` | Tool profile. The mesh is a public radio and the allowlist is its only gate. |
| `MESHCORE_AGENT_ALSO_ALLOW` | `web_search` | Tools on top of the profile. |
| `MESHCORE_AGENT_THINKING` | `off` | Thinking level; off keeps latency low for users waiting on ~130-byte packets. |
| `MESHCORE_SERIAL_DEVICE` | `/dev/ttyACM0` | Host device of the companion radio; a `/dev/serial/by-id/` path survives re-enumeration. |
| `MESHCORE_GATEWAY_WS_URL` | `ws://127.0.0.1:18789` | Gateway WebSocket (the bridge shares the gateway's network namespace). |
| `MESHCORE_BRIDGE_VERSION` | `0.1.1` | Image tag of the bridge. |
| `MESHCORE_GATEWAY_TOKEN` | (empty) | Gateway token; empty = read from `openclaw.json`. |
| `MESHCORE_ALLOWED_PUBKEYS` | (empty) | Pubkey prefixes allowed to DM the agent; the only gate on the radio side. |
| `MESHCORE_ALLOW_ALL` | `off` | Answers everyone. For bench tests only. |
| `MESHCORE_AGENT_TIMEOUT_S` | `180` | Seconds before the bridge gives up on a turn. |
| `MESHCORE_DM_SESSION_GROUPS` | (empty) | `label:prefix,prefix;label2:prefix`: several radios of one person share one conversation. |
| `MESHCORE_DM_EXTRA_PROMPT` | (empty) | Extra system prompt for DM turns. |
| `MESHCORE_CHANNEL_EXTRA_PROMPT` | sociable-room text if one agent | Extra system prompt for channel turns; empty default when a channel agent exists. |
| `MESHCORE_CHANNEL_IDX` (+ `_NAME`, `_SECRET`, `_PASSWORD`) | (unset) | Enables a group channel. Channel messages carry no sender key, so the allowlist cannot gate them; the channel secret is the only gate. |
| `MESHCORE_CHANNEL_TRIGGER` | `?` | Prefix a channel message needs to reach the agent. |

## Valheim

DevBridge MCP on the gaming PC, reached over the LAN; its ~58 `valheim__*` tools go to
a builder agent bound to the listed channels and are denied on every other agent.

| Knob | Default | What it does |
|---|---|---|
| `VALHEIM_MCP_URL` | (unset) | DevBridge MCP URL. With the token set, config-init wires the server, the builder agent and its bindings. |
| `VALHEIM_MCP_TOKEN` | (unset) | DevBridge bearer token. |
| `VALHEIM_MCP` | (unset) | `off` removes the server, the bindings and the deny glob; the agent and its notes stay. |
| `VALHEIM_MCP_REQUEST_TIMEOUT_MS` | `660000` | Per-call timeout; one call can wait 10 min for the owner to confirm an in-game write. |
| `VALHEIM_AGENT_ID` | `valheim` | Builder agent id. |
| `VALHEIM_AGENT_CHANNELS` | (unset) | Discord channel IDs routed to the builder (peer bindings win over the catch-all). |
| `VALHEIM_AGENT_TOOLS` | (unset) | Tools the builder gets (with or without `valheim__`), plus the sub-agent tools. Unset = all; with all schemas the prompt was ~81k tokens and the model narrated calls instead of making them. |
| `VALHEIM_AGENT_TOOLS_DENY` | `valheim__csharp_run` | Extra denies on the builder; `none` = none. |
| `VALHEIM_AGENT_THINKING` | (unset) | Thinking level (created at `high`); full thinking spent ~57k tokens and 40 minutes on one dry run. |

## Media (ComfyUI image/video/music, LTX)

The bridge runs from its own compose file (`openclaw-image-comfyui/`) with its own
knobs; see [openclaw-image-comfyui/README.md](../../openclaw-image-comfyui/README.md). These are what
config-init reads. `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` (above) turns groups off.

| Knob | Default | What it does |
|---|---|---|
| `IMAGE_GEN_API_TOKEN` | (empty) | Bridge token. When set, config-init registers the `comfyui_image` MCP server; unset removes it. |
| `IMAGE_GEN_URL` | `http://openclaw-image-comfyui:9095/mcp` | Bridge MCP endpoint. |
| `IMAGE_GEN_MCP_REQUEST_TIMEOUT_MS` | `960000` | Per-call timeout; above the bridge's own 900 s render budget so the bridge answers first with a clean error. |
| `IMAGE_GEN_DEFAULT_WORKFLOW` | (unset) | Default image workflow; the image-generation skill is written only once it is set. |
| `LTX_VIDEO_ENABLED` | `off` | Writes the video skill; turn on once the ~71 GB LTX download has finished. |
| `LTX_VIDEO_DEFAULT_LENGTH_FRAMES` | `145` | Default clip length in the video skill. |
| `LTX_VIDEO_DEFAULT_FPS` | `24` | Default frame rate in the video skill. |
| `LTX_VIDEO_MAX_DURATION_S` | `10` | Maximum duration the skill states; the bridge enforces its own limit. |

## Image bot (compose/claw-img.yml)

A separate Discord bot with real slash commands that calls the bridge directly, no LLM.
Details: `openclaw-claw-img-bot/README.md`.

| Knob | Default | What it does |
|---|---|---|
| `CLAW_IMG_DISCORD_TOKEN` | (empty) | The bot's own Discord token (a second application). |
| `CLAW_IMG_GUILD_ID` | (empty) | Server ID for instant command registration; global registration takes about an hour. |
| `CLAW_IMG_BOT_VERSION` | `0.5.1` | Image tag. |
| `IMAGE_GEN_URL`, `IMAGE_GEN_API_TOKEN` | as above | Shared with config-init. |
| `CLAW_IMG_DEFAULT_WORKFLOW` | `flux-krea-2k` | Workflow of `/claw-img`; set an adult workflow to make it NSFW by default. |
| `CLAW_IMG_SFW_WORKFLOW` | `flux-krea-2k` | Workflow the `safe` option forces. |
| `CLAW_IMG2IMG_DEFAULT_WORKFLOW` | `flux-krea-2k-i2i` | Image-to-image workflow. |
| `CLAW_IMG2IMG_SFW_WORKFLOW` | `flux-krea-2k-i2i` | Image-to-image workflow for `safe`. |
| `CLAW_IMG_MAX_BYTES` | `9437184` | Above this the bot posts a link instead of an attachment (Discord's 10 MiB cap). |
| `CLAW_IMG_TIMEOUT_S` | `600` | Image render timeout. |
| `CLAW_VIDEO_TIMEOUT_S` | `900` | Video render timeout (LTX cold cache takes 3-10 min). |
| `CLAW_VIDEO_MAX_SECONDS` | `10` | Longest video a command accepts. |
| `CLAW_MUSIC_TIMEOUT_S` | `600` | Music render timeout. |
| `CLAW_MUSIC_MAX_SECONDS` | `120` | Longest track a command accepts. |

## Optional model files

`compose/gemma4.yml` and `compose/mistral.yml` replace the `vllm-llm` command and need
`LLM_MODEL_ID` in `.env` (compose fails without it; config-init would otherwise
register the Nemotron id). The same knobs as the default model, with these defaults:

| Knob | Default | What it does |
|---|---|---|
| `LLM_IMAGE` | `v0.32.0` (gemma4), `v0.31.0` (mistral) | vLLM image (`vllm/vllm-openai:`). |
| `LLM_GPU_MEM_UTIL` | `0.50` (gemma4), `0.65` (mistral) | Pool share. Mistral Small 4 has ~66 GiB of weights. |
| `LLM_MAX_MODEL_LEN` | `262144` (gemma4), `131072` (mistral) | vLLM context length; OpenClaw still budgets `LLM_CONTEXT_WINDOW`. |
| `LLM_MAX_NUM_SEQS` | `2` (gemma4), `4` (mistral) | Concurrent sequences. |
| `LLM_LIMIT_MM_IMAGES` | `4` | Images per prompt. |

`compose/tts-fish.yml` (Fish Audio S2 Pro, research licence, non-commercial). It does
not fit next to Nemotron on the 128 GB pool. Details: `openclaw-tts-fish/README.md`.

| Knob | Default | What it does |
|---|---|---|
| `OPENCLAW_TTS_FISH_API_KEY` | (empty) | Shim token (`TTS_API_TOKEN` inside); also what config-init wires OpenClaw with. |
| `TTS_FISH_BIND` | `127.0.0.1` | Host interface of the shim port. |
| `TTS_FISH_PORT` | `8091` | Host port of the shim. |
| `TTS_FISH_MEM_LIMIT` | `26g` | Memory and swap limit of the container. |
| `TTS_FISH_CPUS` | `12` | CPU limit. |
| `TTS_FISH_OMP_THREADS` | `4` | OpenMP threads. |
| `TTS_FISH_DEFAULT_VOICE` | `default_en` | Voice when a request names none. |
| `TTS_FISH_VOICE_ALIASES` | (empty) | Extra `name:target,...` aliases on top of the built-in map (OpenAI voice names, friendly handles). |
| `TTS_FISH_HU_AUTOROUTE_VOICE` | `default_hu` | Voice Hungarian-looking text is rerouted to when the requested voice is not Hungarian; an English reference voice gives an English accent. |
| `TTS_FISH_LEADING_SILENCE_MS` | `300` | Silence before speech; Whisper clipped the first syllable without it. |
| `FISH_S2PRO_CONFIG` | `/opt/configs/s2pro_tts_gb10.yaml` | SGLang-Omni pipeline config. The upstream one assumes a dedicated GPU and wedged the shared pool. |
| `SGLANG_OMNI_STARTUP_TIMEOUT` | `3000` | Engine-ready deadline in seconds; the first boot JIT-compiles kernels for sm_121 (cached afterwards). |
