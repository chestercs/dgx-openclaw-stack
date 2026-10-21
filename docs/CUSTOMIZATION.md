# Customization

How to change the stack without breaking it. Every knob, with its default and
why it has that default, is in [`reference/configuration.md`](reference/configuration.md);
this page covers the changes that touch more than one knob.

Two rules apply everywhere:

- **Change `.env`, not `openclaw.json`.** `openclaw-config-init` rewrites
  `openclaw.json` and the workspace texts from `.env` on every `docker compose up`,
  so a hand edit to a field it owns is undone on the next start. If a change should
  last, it belongs in `.env` or in `config-init/`.
- **Apply config changes with the trio recreate.** The CLI shares the gateway's
  network namespace and has to be recreated with it:

  ```bash
  docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
  ```

  `docker compose run --rm openclaw-config-init` runs config-init alone against the
  live config and prints its `[patch-config]` lines, without touching the gateway.

Commands below use the default container names. If you set
`CONTAINER_NAME_PREFIX`, put it in front of every name in `docker exec`.

---

## Swap the model

`vllm-llm` serves exactly one model on port 8004, and config-init registers exactly
that id in OpenClaw (`LLM_MODEL_ID`). Other `vllm/...` catalog entries are removed,
because a stale one shows up in the model picker and answers 404. A model swap is
therefore always two settings in `.env`: an optional compose file that replaces the
`vllm-llm` command, and the matching `LLM_MODEL_ID`.

| Model | `.env` |
|---|---|
| Nemotron 3.5 Super 120B-A12B NVFP4 (default) | nothing; `COMPOSE_FILE` unset or `docker-compose.yml` |
| Gemma 4 26B-A4B NVFP4 (MoE) | `COMPOSE_FILE=docker-compose.yml:compose/gemma4.yml`<br>`LLM_MODEL_ID=nvidia/Gemma-4-26B-A4B-NVFP4` |
| Gemma 4 31B IT NVFP4 (dense, slower) | `COMPOSE_FILE=docker-compose.yml:compose/gemma4.yml`<br>`LLM_MODEL_ID=nvidia/Gemma-4-31B-IT-NVFP4` |
| Mistral Small 4 119B-A6B NVFP4 | `COMPOSE_FILE=docker-compose.yml:compose/mistral.yml`<br>`LLM_MODEL_ID=mistralai/Mistral-Small-4-119B-2603-NVFP4` |

Then recreate the model and the trio:

```bash
docker compose up -d vllm-llm
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

Why `LLM_MODEL_ID` has to be in `.env` even though the compose file names the
model: the optional files fail without it (`${LLM_MODEL_ID:?...}`), and config-init
would otherwise register the Nemotron id, which the new server does not serve.

Each compose file states what it needs in its header comment (vLLM image, memory
share, parsers, measured speed). Two things config-init does from the model id:

- **Thinking control** (`LLM_THINKING_COMPAT=auto`). Gemma 4 and Nemotron get
  `chat_template_kwargs.enable_thinking`, Mistral gets `reasoning_effort` none/high.
  Without the right `compat` on the catalog entry OpenClaw sends no thinking control
  and the model never thinks.
- **Temperature** (`LLM_TEMPERATURE=auto`). 0.6 for Nemotron, the upstream default
  for the others.

Memory: Nemotron at the default `LLM_GPU_MEM_UTIL=0.70` takes ~70 GiB of the 128 GB
pool, which leaves no room for ComfyUI or Fish TTS next to it. Long-run notes and
measurements per model are in [`reference/llm-stack.md`](reference/llm-stack.md).

### A model this repo does not ship

Write your own `compose/<name>.yml` on the pattern of `compose/mistral.yml`:

- Keep the service name `vllm-llm` and port 8004, so the gateway and the
  healthcheck need no change.
- Serve the model under the id you put in `LLM_MODEL_ID`
  (`--served-model-name ${LLM_MODEL_ID}` if it differs from `--model`).
- Pick the vLLM tool-call and reasoning parsers for the model family, and keep
  `--enable-auto-tool-choice`; OpenClaw relies on native tool calls.
- If config-init does not recognize the family, set `LLM_THINKING_COMPAT`
  (`qwen-chat-template` or `off`), and check `LLM_CONTEXT_WINDOW` / `LLM_MAX_TOKENS`
  against what the model and `--max-model-len` allow.

Then follow the same two-step recreate.

## Use a remote or cloud LLM backend

Use case: the gateway, SearxNG and the agent state on a small machine, the model
somewhere else (another GPU box, a hosted vLLM, an OpenAI-compatible cloud endpoint).
Config-init reads two URLs:

```dotenv
# OpenAI-compatible chat endpoint. Keep the trailing slash.
LLM_BASE_URL=https://llm.example.com/v1/
# Embedding endpoint for memory search. Keep the trailing slash.
EMBED_BASE_URL=https://embed.example.com/v1/
# The id the remote serves.
LLM_MODEL_ID=<model id on the remote>
# Sent as the API key to BOTH endpoints.
VLLM_API_KEY=<remote key>
```

Limits of this path, from the code:

- One key for both endpoints. Config-init writes `VLLM_API_KEY` as the chat
  provider's key and as the memory-search key.
- The embedding model name is fixed to `BAAI/bge-m3` (`config-init/steps/memory.mjs`).
  The remote embedding endpoint has to serve that name, and switching to another
  embedding model means a reindex (vector dimensions are fixed once the first
  chunk is indexed).
- The provider speaks `openai-completions`. For an endpoint that rejects
  `chat_template_kwargs`, set `LLM_THINKING_COMPAT=off`.

Then stop the local vLLM services from starting. Do not comment them out; park them
with a small override file, for example `remote-backend.yml` next to
`docker-compose.yml`:

```yaml
# remote-backend.yml: the model and embeddings run elsewhere.
services:
  vllm-llm:
    profiles: ["remote-off"]
  vllm-embedding:
    profiles: ["remote-off"]
  # The gateway waits for both vLLM services by default. `!override` replaces
  # the whole depends_on map; a profile alone makes compose refuse the project
  # ("depends on undefined service").
  openclaw-gateway:
    depends_on: !override
      openclaw-config-init:
        condition: service_completed_successfully
```

```dotenv
COMPOSE_FILE=docker-compose.yml:remote-backend.yml
```

`!override` needs Docker Compose 2.24.4 or newer. Check the result with
`docker compose config --services`: neither vLLM service should be listed.

Onboard as in [`SETUP.md`](../SETUP.md). Config-init then points the `vllm`
provider at `LLM_BASE_URL` on the trio recreate, whatever onboarding wrote.

Trade-offs: every token travels to the remote, so a LAN backend is fine and a
cross-region one adds latency to every turn. Use TLS or a private network
(WireGuard, Tailscale, a VPC peering); the key goes with every request. The URL has
to resolve and be reachable from inside the gateway container.

### Use the model API without OpenClaw

`vllm-llm` publishes port 8004 on `LLM_BIND` (default `127.0.0.1`), so other code
on the host can call `http://127.0.0.1:8004/v1/` with
`Authorization: Bearer $VLLM_API_KEY`. `LLM_BIND=0.0.0.0` opens it to the LAN, with
the key as the only barrier. The embedding server is not published; it is reachable
only on the compose network.

## Tune web search

### Parallel-free instead of SearxNG

`OPENCLAW_WEB_SEARCH_PROVIDER=parallel-free` sends `web_search` to Parallel's free
Search MCP (`https://search.parallel.ai/mcp`). It needs no key, account or payment,
but every query goes to Parallel instead of staying on your host, and the free
tier has lower rate limits than the paid API. On the reference host it answered
59 of the 60 bench questions against SearxNG's 53, with fuller excerpts. Install
the official plugin once, at the version of the pinned OpenClaw image (a newer
plugin may require a newer OpenClaw):

```bash
docker exec openclaw-cli openclaw plugins install @openclaw/parallel-plugin@2026.10.4
# .env: OPENCLAW_WEB_SEARCH_PROVIDER=parallel-free
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

SearxNG keeps running, so switching back is the knob alone. Compare both on your
host first: `scripts/websearch-bench.py parallel-free` and `... searxng`.

### SearxNG: which engines run

`searxng/settings/settings.yml` → `use_default_settings.engines.keep_only`. Whatever
is in this list stays in the registry; every other engine in the upstream defaults
is dropped. The shipped list is the one that scored best on the reference host
(Hungarian residential IP, 2026-10-20):

```yaml
keep_only:
  - yahoo            # Bing's index; best engine measured
  - duckduckgo web   # the plain "duckduckgo" engine answers with a CAPTCHA
  - privacywall
  - marginalia
  - yep
  - bing news
  - duckduckgo news
  - yahoo news
  - wikipedia
  - github           # only for categories=it
  - arxiv            # only for categories=science
```

Engines get blocked per IP and over time, so measure on your own host before you
change the list. `scripts/websearch-bench.py` asks 20 fact questions (10 Hungarian,
10 English, three sets) and counts how often the answer is in the top 5, either
through the gateway or per engine:

```bash
scripts/websearch-bench.py searxng             # what the bot gets
scripts/websearch-bench.py yahoo 5 searx       # one engine, raw SearxNG JSON
```

A blocked engine produces no error, just missing results. Check a single engine and
the `unresponsive_engines` field:

```bash
docker exec openclaw-cli curl -sS \
  "http://searxng:8080/search?q=test&format=json&engines=yahoo" \
  | jq '{n: (.results | length), down: .unresponsive_engines}'
```

On the reference host `google`, `qwant` and `brave` were blocked or rate-limited,
and `bing` answered with almost nothing. Any engine shipped `disabled: true` in the
upstream defaults needs an explicit override in the `engines:` block:

```yaml
engines:
  - name: yahoo
    disabled: false
```

`scripts/daily-status.sh` prints which engines answered one Hungarian and one
English query each day.

### SearxNG: categories and language

Config-init (`config-init/steps/tools.mjs`) writes the SearxNG `baseUrl`, an empty
`language` and `categories: "general,news"`. Do not add `science` to the default:
it put arXiv papers on top of every answer. The agent can still pass `language` /
`categories` per query. When it sends no language, SearxNG's
`search.default_lang: "auto"` detects it from the query text.

The search wiring has no off switch. If you park `searxng` while it is the
provider, `web_search` keeps pointing at a dead URL and every search call fails.

## Turn a service off

Never comment out a service block: the optional parts already have a switch.

| Part | How to turn it off | What config-init does |
|---|---|---|
| Browser (`openclaw-browser`) | drop `browser` from `COMPOSE_PROFILES` | stops writing the browser wiring; clear `BROWSER_API_TOKEN` as well, since wiring from an earlier run stays in `openclaw.json` |
| Python sandbox | drop `python` from `COMPOSE_PROFILES` | removes the `python_sandbox` MCP server |
| MeshCore bridge | drop `meshcore` from `COMPOSE_PROFILES`, `MESHCORE_ENABLED=off` | stops registering the MeshCore agents |
| Fish TTS | leave `compose/tts-fish.yml` out of `COMPOSE_FILE`, `OPENCLAW_TTS=off` | removes the `tts` block |
| Image bot | leave `compose/claw-img.yml` out of `COMPOSE_FILE` | nothing (it talks to the bridge, not to OpenClaw) |
| ComfyUI bridge | `docker compose -f openclaw-image-comfyui/docker-compose.yml down`, clear `IMAGE_GEN_API_TOKEN` | removes the `comfyui_image` MCP server |
| Whisper STT | park it with a profile override (see below), clear `STT_API_TOKEN` | stops writing the Whisper entry; an entry already in `tools.media.models` stays |

A service whose profile you dropped keeps running until you remove it:

```bash
docker compose --profile browser rm -sf openclaw-browser
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

The core services (`vllm-llm`, `vllm-embedding`, `searxng`, `openclaw-stt-whisper`)
have no profile. Park one with an override file that gives it a profile nobody
enables, as in [the remote backend section](#use-a-remote-or-cloud-llm-backend).
The two vLLM services also need the gateway's `depends_on` replaced, because the
gateway waits for them to be healthy.

## Heartbeat and dreaming

**Heartbeat.** Every 30 minutes, in an isolated session, inside a daily window:

```dotenv
OPENCLAW_HEARTBEAT_ACTIVE_START=09:00
OPENCLAW_HEARTBEAT_ACTIVE_END=02:00     # end before start wraps past midnight
OPENCLAW_HEARTBEAT_TZ=Europe/Budapest
```

Since 2026.10 each agent's heartbeat is an automation (`heartbeat:<agent>`) whose
instructions are its private scratch, not `HEARTBEAT.md` (Doctor imports the file
once, and only when the scratch is still empty). Read and replace it with the CLI;
an empty scratch skips the poll:

```bash
docker exec openclaw-cli openclaw automations list            # the heartbeat:<agent> ids
docker exec openclaw-cli openclaw automations scratch <id>
docker exec -i openclaw-cli openclaw automations scratch <id> --file - < my-heartbeat.md
```

A heartbeat ends with the `heartbeat_respond` tool: `notify=false` stays silent,
`notify=true` sends its `notificationText`. Say in the scratch when to notify,
or the model reports "nothing to do" in prose and that prose is sent. Messages go to
the owner as a Discord DM: `OPENCLAW_HEARTBEAT_TO`, or the first ID in
`OPENCLAW_DISCORD_COMMAND_OWNERS`. An explicit target is needed because the upstream
`owner` target finds no DM on Discord (`reason=no-route`). `OPENCLAW_HEARTBEAT_TO=off`
leaves delivery to OpenClaw.

**Dreaming.** Nightly memory consolidation into `MEMORY.md` at 03:00 in
`OPENCLAW_HEARTBEAT_TZ`, after the default heartbeat window ends.
`OPENCLAW_ENABLE_DREAMING=off` removes it. The time has no knob; it is the
`frequency` cron in `config-init/steps/memory.mjs`. Dream diaries over
`OPENCLAW_DREAMS_ROTATE_BYTES` move to `memory/dreams-archive/`, which memory search
still indexes.

Both take effect on the trio recreate.

## Edit the prompt text

The workspace texts config-init writes come from `config-init/templates/`:

| Folder | What it becomes |
|---|---|
| `discord/AGENTS.d/` | marker blocks in `workspace-discord/AGENTS.md` (Discord agent policy) |
| `discord/router/` | the media lines of the skill router (an `-on` and an `-off` variant per group) |
| `discord/people-MEMORY.seed.md` | the first `people/MEMORY.md`, written only when none exists |
| `main/AGENTS.d/` | marker blocks in the main agent's `AGENTS.md` |
| `skills/` | `skills/<name>/SKILL.md` in the workspaces |
| `agents/` | seed `AGENTS.md` for the MeshCore and Valheim agents |

`{{NAME}}` placeholders are filled by the step that renders the file; a placeholder
without a value stops config-init, because a half-rendered prompt is worse than a
failed start.

What you may edit where:

- **In `AGENTS.md`, outside the markers** (`<!-- patch-config:<id>:start -->` …
  `end`): yours, kept on every run.
- **Inside the markers, and every skill file:** owned by config-init and
  overwritten on every run. Change the template in `config-init/templates/`, or add
  your own skill under a different name.

The folder is mounted read-only into config-init (`./config-init:/opt/config-init`),
so a template change needs no image rebuild: run config-init (or the trio recreate),
and new sessions get the new text. A session that is already running keeps the
`AGENTS.md` it started with.

Write rules as criteria, not keyword lists ("before stating a specific, checkable
fact you are not certain of, look it up"), with examples only as illustration; see
[`reference/discord-text-agent.md`](reference/discord-text-agent.md) for the agent
design.

## Add a knob or a step to config-init

Config-init is `config-init/patch-config.mjs`, which runs the area modules in
`config-init/steps/` in order (models, memory, gateway, tools, discord, agents,
workspaces, media, plugins). It gets the whole `.env` through `env_file`, so a
config-init knob needs no compose change.

1. **Read the knob with `config-init/lib/env.mjs`.** `str`, `bool`, `int`, `num`,
   `list`, `oneOf`, `ids` (Discord snowflakes as strings), `profile` (is a compose
   profile on). An empty value counts as unset, and an invalid switch or enum value
   throws: config-init exits non-zero and the gateway does not start until it is
   fixed. That is on purpose; a typo should not silently fall back to a default.
2. **Write through `ctx.config`** (`config-init/lib/config.mjs`): `set`, `merge`,
   `setDefault` (only when nothing is there, so the operator's value wins), `remove`,
   `addTo`, `removeFrom`. Each compares first and logs one `[patch-config]` line,
   masking secrets, so a re-run on a patched config is silent. Give an `off` value a
   removal path, so turning the knob off cleans up after it.
3. **Workspace text goes through `config-init/lib/files.mjs`**: `template`,
   `upsertBlock` / `removeBlock` for `AGENTS.md` marker blocks, `skill` /
   `removeSkill` for skill files. Put the text itself in `config-init/templates/`.
4. **A new area** is a new `config-init/steps/<area>.mjs` with a default export
   `(ctx) => void`, added to the list in `patch-config.mjs`. The order matters only
   where a later area reads what an earlier one wrote.
5. **Document the knob** in [`reference/configuration.md`](reference/configuration.md):
   default, what it does, and why the default is what it is. Add it to
   `.env.example` only if a typical install sets it.
6. **Verify.** `node --check` on the files you changed, then the test harness on
   the Docker host:

   ```bash
   export STACK=/path/to/the/deployed/checkout
   config-init/test/run.sh before        # the deployed config-init, on a copy of the live state
   config-init/test/run.sh after /path/to/your/working/copy/config-init/patch-config.mjs
   config-init/test/diff.sh before after # what changed in openclaw.json and the workspaces
   ```

   `run.sh` copies the live state to `~/pc-harness/<name>/` and redirects every
   mount config-init writes to, so the live state is never touched. It reads
   `$STACK/.env`; with no second argument it runs the config-init in `$STACK`.
   `diff.sh` expects `diff.mjs` copied to `~/pc-harness/`.

New keys under `agents.defaults.*` have crash-looped the gateway when the schema did
not accept them. Validate the shape in the Control UI first; the runbook is in
[`reference/agentic-coding.md`](reference/agentic-coding.md#adding-a-new-key-the-schema-gate).

## Optional media

- **Text-to-speech (Fish Audio S2 Pro):** `compose/tts-fish.yml` plus
  `OPENCLAW_TTS_FISH_API_KEY`. Research licence, non-commercial. It does not fit
  next to Nemotron; run it with a smaller model. Voices, cloning and tuning:
  [`openclaw-tts-fish/README.md`](../openclaw-tts-fish/README.md).
- **Image, video and music (ComfyUI bridge):** its own compose file in
  `openclaw-image-comfyui/`, against a ComfyUI you already run. Activation,
  workflows, models and the proxy setup:
  [`openclaw-image-comfyui/README.md`](../openclaw-image-comfyui/README.md).
  The Discord agent has no media tools until you set
  `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF=none` (or list only the groups to keep off);
  until then the router tells the bot to say the feature is off instead of
  calling a dead tool.
- **Slash-command image bot:** `compose/claw-img.yml`, a separate Discord bot that
  calls the bridge directly with no LLM in between:
  [`openclaw-claw-img-bot/README.md`](../openclaw-claw-img-bot/README.md).

## Discord voice

Joining a voice channel (`/vc join`), the isolated voice agent and its limits are in
[`reference/discord-voice-agent.md`](reference/discord-voice-agent.md). Creating the
bot application: [`discord-bot-setup.md`](discord-bot-setup.md). Voice replies need
the Fish TTS file above.

## See also

- Upgrading OpenClaw: [`OPENCLAW_UPGRADE.md`](OPENCLAW_UPGRADE.md).
- Browser profiles and logins: [`reference/browser-automation.md`](reference/browser-automation.md).
- Python sandbox limits and egress: [`reference/python-sandbox.md`](reference/python-sandbox.md).
