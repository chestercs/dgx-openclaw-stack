# OpenClaw internals

Facts about OpenClaw itself that this stack depends on: config schema, where
credentials live, caches that go stale, persistence and CLI behaviour. The stack
architecture is in [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md); every knob is in
[`configuration.md`](./configuration.md). The schema notes match OpenClaw 2026.10.

## Two-phase fresh install

Config-init exits 0 when `openclaw.json` does not exist yet, so on a fresh install:

1. `docker compose up -d` starts everything; the gateway crash-loops with
   `Missing config. Run openclaw setup …`. That is expected: OpenClaw requires an
   explicit onboarding step, in which the operator picks the gateway token and
   pairs the UI.
2. Onboard (`openclaw onboard --non-interactive …`, the Chrome extension wizard, or
   `openclaw setup`). This writes `openclaw.json`.
3. `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli`.
   Config-init now writes the desired state and the gateway goes healthy.

Recreate the three together: the CLI shares the gateway's network namespace and is
left in a dead one otherwise (see [`patterns.md`](./patterns.md) → "openclaw-cli
network-namespace dependency"). The full walkthrough is in [`SETUP.md`](../../SETUP.md).

A pre-2026.10 config (one that still has `agents.list`) makes config-init stop with
"run `openclaw doctor --fix` first"; doctor migrates it to the keyed layout.

## Config schema

### Provider

```
models.providers.vllm = {
  baseUrl, api: "openai-completions", apiKey, timeoutSeconds,
  models: [ { id, name, api, reasoning, input, cost,
              contextWindow, contextTokens, maxTokens, compat } ]
}
```

`config-init/steps/models.mjs` writes exactly one catalog entry, for `LLM_MODEL_ID`,
and removes every other `vllm/...` entry: vLLM serves one model, and a stale entry
shows up in the model picker and answers 404.

**Tool calling needs the catalog entry.** The model the agent uses must appear in
`models.providers.vllm.models[]`; a `capabilities.supportsTools` flag is rejected by
the schema (`Unrecognized key: "capabilities"`). Onboarding adds only the model
picked interactively, so a later swap would leave the agent without metadata.

**Thinking needs `compat`.** A custom provider sends no thinking control at all
unless the catalog entry's `compat` says how this model family takes it (Gemma 4
and Nemotron: `thinkingFormat: "qwen-chat-template"`; Mistral Small 4:
`supportsReasoningEffort` with `none | high`). Without it the model never thinks.
Check with `usage.reasoningTokens` on `model.completed` events in the agent sqlite.

### Agents

```
agents.defaults.model                    -> "vllm/<modelId>"  (a bare string is valid)
agents.defaults.models["vllm/<id>"]      -> { params: { temperature } }  (allowlist + per-model params)
agents.entries.<id>                      -> { name, workspace, model, tools: { profile, alsoAllow, deny },
                                              thinkingDefault, ... }
```

Agents are keyed under `agents.entries` (2026.10). Tool enablement lives on the
agent entry, not on the provider.

The `agents.defaults.*` family is strict: an unknown key or an out-of-range value
fails with `agents.defaults: Invalid input` and the gateway crash-loops. See the
schema-gate runbook in [`agentic-coding.md`](./agentic-coding.md#adding-a-new-key-the-schema-gate).

### Other paths config-init writes

| Path | What | Module |
|---|---|---|
| `memory.search` | `provider: "openai"` + `remote.baseUrl` / `remote.apiKey` for the local bge-m3 | `steps/memory.mjs` |
| `tts` (top level) | `auto`, `mode`, `timeoutMs`, `providers.openai` pointing at the Fish shim | `steps/media.mjs` |
| `tools.media.models[]` | Whisper as the first entry with `capabilities: ["audio"]` | `steps/tools.mjs` |
| `tools.exec.mode` | `ask` or `full` | `steps/tools.mjs` |
| `mcp.servers.<name>` | Python sandbox, ComfyUI bridge, Valheim; `requestTimeoutMs` per server | `steps/tools.mjs`, `steps/media.mjs`, `steps/agents.mjs` |

For a custom OpenAI-compatible embedding endpoint, `provider: "openai"` plus
`remote.baseUrl` / `remote.apiKey` is the supported path; the provider enum has no
`vllm`. The `remote` block also accepts `headers` and `batch.{enabled,concurrency,wait}`.

The embedding dimension is fixed when the first chunk is indexed. Switching the
embedding model needs `openclaw memory index --force` per agent.

### 2026.10 behaviour worth knowing

- Bundles are `.mjs`; a grep for `*.js` in the image matches nothing.
- An unset `tools.toolSearch` means "on". Config-init writes it off
  (`OPENCLAW_TOOL_SEARCH`), because MCP results then come back as JSON text and
  images never reach the vision input.
- The `cron` tool is called `automations`; `cron` stays an alias.

## Credentials

Secrets live in `.env` only. Three places matter:

### 1. `.env` and the gateway environment

`bootstrap.sh` fills every `CHANGE_ME` secret, `rotate-secrets.sh` rotates them.
The gateway container gets `VLLM_API_KEY` and `OPENCLAW_TTS_FISH_API_KEY` in its
environment (`docker-compose.yml`), because `openclaw.json` refers to them by name.

### 2. `openclaw.json`

- `models.providers.vllm.apiKey`, `memory.search.remote.apiKey` and
  `tts.providers.openai.apiKey` are env SecretRefs,
  `{ source: "env", provider: "default", id: "VLLM_API_KEY" }`
  (`config-init/lib/secrets.mjs`). The gateway resolves them from its own
  environment at start, so the values stay out of the file and its backups, and
  `openclaw secrets audit` does not report them. `OPENCLAW_SECRET_REFS=off` writes
  the plain values instead.
- Bearer tokens of the STT entry and the MCP servers sit in their own `headers`
  objects as plain values. Config-init writes them as separate leaves so its log
  masks them; do not `jq` whole `tools.*` or `mcp.*` subtrees into a shared log.
- `gateway.auth.token` is written by onboarding. It is not the `.env`
  `OPENCLAW_GATEWAY_TOKEN`, which only matters before onboarding.
- `gateway.remote.token` is mirrored from `gateway.auth.token` by
  `config-init/steps/gateway.mjs`. The local CLI and the MeshCore bridge present
  it; on a mismatch `openclaw agent` falls back to an embedded runner that dials
  vLLM from the CLI's own environment.

After rotating a secret, recreate the services that read it plus
`openclaw-config-init openclaw-gateway openclaw-cli`; `rotate-secrets.sh` prints
the exact command.

### 3. `~/.openclaw/agents/<agent>/agent/auth-profiles.json`

The per-agent credential store. Onboarding seeds `profiles."vllm:default".key`
there, and config-init does not write it. If agent runs answer
`HTTP 401: Unauthorized` after a `VLLM_API_KEY` rotation while the provider entry
is current, compare the key there with the one vLLM runs with:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)
docker exec ${PROJ}openclaw-cli node -e '
  const p=require("/home/node/.openclaw/agents/main/agent/auth-profiles.json");
  const c=require("crypto");
  const k=p.profiles["vllm:default"]?.key ?? "";
  console.log("len="+k.length+" sha="+c.createHash("sha256").update(k).digest("hex").slice(0,12));
'
docker exec ${PROJ}vllm-llm sh -c 'cat /proc/1/cmdline' | tr '\0' '\n' \
  | awk '/^--api-key$/{getline; printf "%s", $0}' | sha256sum | cut -c1-12
# The two hashes must match.
```

## Per-agent `models.json` (stale-prone cache)

The `image` / vision tool reads a separate provider catalog at
`~/.openclaw/agents/<id>/agent/models.json`, not the live `models.providers.*`.
Text generation reads the live config, so it is unaffected. The file is
regenerated only when it is missing; config edits do not invalidate it.

When a provider's `baseUrl` changes (service rename, port move), text keeps working
but the image tool fails with `Image model failed (vllm/...): Connection error`
against the old host. A fallback to a cloud model then fails with 401 as well,
because the stack has no cloud key. Fix:

```bash
docker exec ${PROJ}openclaw-cli sh -c 'rm /home/node/.openclaw/agents/*/agent/models.json'
# the next agent run or image attachment regenerates it from current config
```

Do the same when you add a provider and want agents to see it.
`agents.defaults.imageModel.primary` picks the image model but still resolves its
`baseUrl` through this cache, so setting it alone does not fix a stale entry.

## Persistence

Everything below lives in the `${OPENCLAW_CONFIG_DIR}` bind mount and survives
`docker compose down && up -d`:

- `paired.json`, `pending.json`
- `agents/<id>/sessions/` (chat history) and `agents/<id>/agent/auth-profiles.json`
- `openclaw.json` (config-init re-applies its desired state on every `up`)
- `exec-approvals.json` (learned exec grants)
- memory SQLite and `workspace*/` (AGENTS.md, skills, memory, DREAMS.md)
- `.node-compile-cache/` (`NODE_COMPILE_CACHE`), so a recreate does not pay the
  compile again

`OPENCLAW_CANVAS_DIR` and `OPENCLAW_MEDIA_INBOUND_DIR` are separate mounts (by
default under the config dir).

## Known non-critical warnings

- `agents.main.tools.allow allowlist contains unknown entries (gateway, nodes)` —
  onboarding mixed tool names that are not available into `alsoAllow`. It blocks
  nothing.
- `[agent] context-overflow-diag … auto-compaction succeeded` — preemptive
  compaction on long sessions. A feature, but it adds minutes of latency.
- `Gateway bound to "lan" (0.0.0.0)` — intentional (LAN plus reverse proxy);
  `doctor` warns about it.

## CLI overhead

- `openclaw --version`: ~0.2 s.
- `openclaw health`: ~5 s (WebSocket handshake and auth).
- `openclaw doctor`: ~45 s.
- `openclaw agent --agent main --thinking off`: ~31–51 s end to end on a short
  prompt, of which vLLM was ~5.5 s; the rest is gateway session setup, paid on
  every invocation. There is no env trick to bring it down. For interactive use the
  Control UI is faster, and the MeshCore bridge keeps one WebSocket open for the
  same reason ([`meshcore-bridge.md`](./meshcore-bridge.md)).
- Tool-using runs need `--timeout 600`; the default 60 s trips on a cold cache.

## Control UI behaviour

- The `agent:main:main` chat session is a single stream; `/new` (New session)
  only partly clears it, and stale context can produce odd replies. The CLI starts
  a clean session.
- Tool traces render in the chat (tool call, output, summary blocks); toggle them
  with "Toggle tool calls and tool results".
- The WebGUI doubles as a schema oracle: set a value in Settings, Save (it validates
  against the running schema), then read the persisted JSON. That is how new
  config-init keys are checked before they ship.
