# OpenClaw internals

> Deep technical reference for OpenClaw: releases + schema + credential
> layout + patcher steps + persistence + CLI behavior. The stack-level
> architecture lives in a separate doc
> ([`docs/ARCHITECTURE.md`](../ARCHITECTURE.md)) — this file focuses on
> OpenClaw internals.

## Public repo (`chestercs/dgx-openclaw-stack`)

- **Repo:** https://github.com/chestercs/dgx-openclaw-stack
- **License:** MIT
- **Default branch:** `main`
- **Latest:** v0.4.2 (2026-04-22)

### Releases & tags

- `v0.1.0` (2026-04-21, `b04f250`) — Initial public release.
- `v0.2.0` (2026-04-22, `553427e`) — Remote vLLM backend, `CLAUDE.md`, SearxNG, hybrid + MMR memory.
- `v0.3.0` (2026-04-22, `0c4d799`) — Bilingual TTS surface.
- `v0.4.0` (2026-04-22, `1b5b208`) — env-driven knobs (`CONTAINER_NAME_PREFIX`, `VLLM_HF_CACHE_VOLUME_NAME`, `TTS_*_BIND/PORT` loopback default), patcher step 11 top-level `messages.tts.{enabled,auto,mode}` switches.
- `v0.4.1` (2026-04-22, `f87d3b0`) — Post-v0.4.0 polish batch: patcher **step 12** (`gateway.auth.token` → `gateway.remote.token` mirror, `bcea0a5`), patcher **step 13** (per-agent `auth-profiles.json` sync with `VLLM_API_KEY`, `dd935b0`), TTS enum normalization (`81f1fa4`), vLLM healthcheck `python3` fix (`fe6726d`), `.env` defaults convergence (`e132071`), drop `OPENCLAW_GATEWAY_TOKEN` from the CLI env (`644fe68`), `operator/` gitignore (`80a2412`), docs 11 → 13 step sync.
- `v0.4.2` (2026-04-22) — Documentation release: publishes `docs/reference/` (six deep-dive files: LLM stack, TTS stack, Hungarian TTS research, OpenClaw internals, reusable patterns), declares an English-only documentation policy in `CLAUDE.md`, translates the new reference files to English, and renames the gitignored private-artifacts folder `operator/` → `private/`.

### Current content (v0.4.2)

- **Stack:** `docker-compose.yml` (8 default services + 1 opt-in HU service via `profiles: ["hu"]`), `patch-config.mjs` (13-step idempotent patcher), `bootstrap.sh` (regex-gated secret rotation + HU TTS opt-in prompt), `templates/tool_chat_template_gemma4.jinja`, `searxng/settings/settings.yml`.
- **TTS surface:** `openclaw-tts-en/` (Kokoro 82M, default), `openclaw-tts-router/` (FastAPI passthrough + ffmpeg), `openclaw-tts-f5hun/` (F5-TTS HU, opt-in via `profiles: ["hu"]`).
- **Docs:** `README.md`, `SETUP.md`, `docs/{ARCHITECTURE,CUSTOMIZATION,TROUBLESHOOTING}.md`, `docs/reference/`, `CLAUDE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `LICENSE` (MIT).
- **GitHub meta:** `.github/FUNDING.yml`, `.github/ISSUE_TEMPLATE/`, `.github/PULL_REQUEST_TEMPLATE.md`.

### Verified use cases

1. **GB10 reference profile** — `docker compose up -d` with no edits, ~6.9 tok/s decode of Gemma 4 31B NVFP4 at 256K context, multimodal, tool calling, hybrid memory, SearxNG, TTS.
2. **Remote vLLM backend** (verified on a GPU-less Windows host) — three env overrides (`OPENAI_BASE_URL`, `LLM_BASE_URL`, `EMBED_BASE_URL`) + `profiles: ["never"]` on `vllm-llm` / `vllm-embedding`. Local footprint ~1 GB. Walkthrough: `docs/CUSTOMIZATION.md` → "Run with a remote vLLM backend".

### Two-phase fresh-install onboarding (general OpenClaw flow)

The patcher `exit 0`s when `openclaw.json` doesn't yet exist. Fresh-install sequence:

1. `docker compose up -d` → patcher skips → gateway crash-loops with `Missing config. Run openclaw setup …` — **expected, not a bug**; don't try to "fix" it (the OpenClaw security model explicitly requires the onboarding step).
2. `openclaw onboard --non-interactive --token "$OPENCLAW_GATEWAY_TOKEN"` (or the Chrome extension wizard).
3. `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli` → the patcher runs, all 13 steps apply, gateway goes healthy.

The trio (`config-init + gateway + cli`) must be recreated **together** — if the CLI is left alone, it points at a dead network namespace (see `patterns.md` → "openclaw-cli network-namespace dependency").

### v0.4.0 env-driven knobs

- `CONTAINER_NAME_PREFIX=dgx-` — container-name prefix; set empty for bare names (`vllm-llm`, etc.). Bridge DNS reachability is unaffected (services resolve by compose service name + `hostname:`).
- `VLLM_HF_CACHE_VOLUME_NAME=dgx-openclaw-hf-cache` — Docker volume label.
- `TTS_{EN,F5HUN,ROUTER}_BIND=127.0.0.1` — loopback-only bind default.
- `TTS_{EN,F5HUN,ROUTER}_PORT=8091/8090/8092` — host-port override.

### How to contribute

- Edit + commit + push (`origin = github.com/chestercs/dgx-openclaw-stack`).
- For a new release tag, updating `CHANGELOG.md` is mandatory (move content from `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`).
- `gh release create vX.Y.Z --notes-file …` to cut the GitHub Release object.

## OpenClaw config schema (verified)

### Provider level

```
models.providers.vllm = {
  baseUrl, api ("openai-completions"), apiKey,
  models: [ { id, name, reasoning, input, cost, contextWindow, maxTokens, api } ]
}
```

### Agent level

```
agents.defaults.model.primary  -> "<provider>/<modelId>"
agents.defaults.models         -> { "<provider>/<modelId>": {} }  (allowlist)
agents.list[].tools            -> { profile: "full", alsoAllow: [...] }  (tools enablement lives here, NOT on the provider)
```

`agents.list[0].tools.profile: "full"` already enables tool calling on the agent side — the bottleneck was NOT the OpenClaw tool permission, but missing metadata in the provider catalog.

### `memorySearch` schema

The schema path is NOT `memory.embedding.*` (that was an assumption), but `agents.defaults.memorySearch.*`.

Provider enum: `bedrock | gemini | github-copilot | local | mistral | ollama | openai | voyage`. For a custom OpenAI-compatible endpoint (vLLM), use `provider: "openai"` + `remote.baseUrl` + `remote.apiKey` — the officially supported path.

The `remote` block also accepts `headers` and `batch.{enabled,concurrency,wait}` options.

## v0.4.x credential layout (3-store design, CRITICAL)

OpenClaw works with three separate credential stores. **After a `.env` secret rotation, all three must be kept in sync**, otherwise you get silent breakage.

### The three places

#### 1. `.env` (operator intent)

- `VLLM_API_KEY` — vLLM API key (`bootstrap.sh` rotates it with `openssl rand`, 88-char base64).
- `OPENCLAW_GATEWAY_TOKEN` — gateway WS auth token (pre-onboarding default only).

#### 2. `~/.openclaw/openclaw.json` (JSON config file)

- `models.providers.vllm.apiKey` — patcher step 2 writes it from `VLLM_API_KEY`.
- `agents.defaults.memorySearch.remote.apiKey` — patcher step 4 writes it.
- `gateway.auth.token` — written by the onboarding wizard (different from the env token!).
- `gateway.remote.token` — mirrored from `gateway.auth.token` by patcher step 12.
- `messages.tts.providers.openai.apiKey` — patcher step 11 writes it from `OPENCLAW_TTS_ROUTER_API_KEY`.
- `authProfiles.{name}` — metadata only (`{provider, mode}`), NOT the actual key.

#### 3. `~/.openclaw/agents/{agent}/agent/auth-profiles.json` (per-agent credential store)

- `profiles."vllm:default".key` — holds the actual key used by the agent runner.
- **The agent runner reads this, NOT `models.providers.vllm.apiKey`.**
- Seeded by the onboarding wizard; goes stale after a rotation → every LLM call returns HTTP 401.
- Patcher step 13 keeps it in sync with `VLLM_API_KEY`.

### Typical drift scenario (incident: 2026-04-22)

`bootstrap.sh` rotates `VLLM_API_KEY` in `.env`. Steps 2 + 4 update `openclaw.json`. But `auth-profiles.json` stays untouched — the agent runner sends the stale key → vLLM 401.

Symptom: `payloads[0].text = "HTTP 401: Unauthorized"`, `executionTrace.attempts[0].result = success` (vLLM did respond, just with a 401).

### Verify recipe (per-agent key check)

```bash
docker exec openclaw-cli node -e '
  const p=require("/home/node/.openclaw/agents/main/agent/auth-profiles.json");
  const c=require("crypto");
  const k=p.profiles["vllm:default"].key;
  console.log("len="+k.length+" sha="+c.createHash("sha256").update(k).digest("hex").slice(0,12));
'
docker exec vllm-llm sh -c 'cat /proc/1/cmdline' | tr '\0' '\n' | awk '/^--api-key$/{getline; printf "%s", $0}' | sha256sum | cut -c1-12
# The two hashes must match.
```

### Which patcher step manages which key

| Step | Location | Field |
|---|---|---|
| 2 | `openclaw.json` | `models.providers.vllm.apiKey` |
| 4 | `openclaw.json` | `agents.defaults.memorySearch.remote.apiKey` |
| 11 | `openclaw.json` | `messages.tts.providers.openai.apiKey` |
| 12 | `openclaw.json` | `gateway.remote.token` ← `gateway.auth.token` |
| 13 | `agents/*/agent/auth-profiles.json` | `profiles."vllm:default".key` |

### CLI agent runner behavior

`openclaw agent --agent main` runs by default with **`runner: embedded`** (a loopback fast path, not a WS round-trip). The embedded runner reads the key from `auth-profiles.json`; the `OPENAI_API_KEY` env in the CLI container **does not** override it. That's why step 13 is required.

The gateway WS route is used by the remote CLI and the Chrome extension — for them, `gateway.remote.token` (step 12) is what matters.

### Public commits that fixed this

- `81f1fa4` — step 11 enum bug (`auto`, `mode` field enum values).
- `bcea0a5` — step 12 + `openclaw-cli` `OPENAI_API_KEY` env.
- `644fe68` — drop `OPENCLAW_GATEWAY_TOKEN` from the CLI env.
- `dd935b0` — step 13 (`auth-profiles.json` sync).

## Patch-config 6-step base (idempotent init service)

`openclaw-config-init` runs before `openclaw-gateway` (`depends_on: service_completed_successfully`):

1. **Cleanup** — remove the legacy `models.providers.vllm.capabilities` key (old schema).
2. **Ensure vLLM provider core** — `models.providers.vllm.{baseUrl,api,apiKey}` from the `VLLM_API_KEY` env. The onboarding wizard puts a 12-char placeholder here, which causes the gateway to error with "Profile vllm:default timed out" — the patcher overwrites it with the real 139-char key on every `up`.
3. **Ensure** — `nvidia/Gemma-4-31B-IT-NVFP4` entry in the provider's `models[]`.
4. **Ensure memorySearch** — `agents.defaults.memorySearch = { enabled: true, provider: "openai", model: "BAAI/bge-m3", remote: { baseUrl: "${EMBED_BASE_URL:-http://vllm-embedding:8005/v1/}", apiKey: process.env.VLLM_API_KEY } }`. After phase 6 the embedding service is in-stack (`vllm-embedding`), reachable via bridge DNS.
5. **Ensure heartbeat** (always-on) — `agents.defaults.heartbeat = { every: "30m", includeReasoning: true, isolatedSession: true, activeHours: { start: "09:00", end: "02:00", timezone: "Europe/Budapest" } }`.
6. **Ensure dreaming** (env-gated) — if `OPENCLAW_ENABLE_DREAMING=1`, set `plugins.entries.memory-core = { enabled: true, config: { dreaming: { enabled: true, frequency: "0 3 * * *", timezone: "Europe/Budapest", storage.mode: "both", phases: { light, deep, rem } } } }`. Otherwise, cleanup mode.

Successful plugin-load log line on the 2026.4.15 gateway: `[plugins] memory-core: created managed dreaming cron job.`

The v0.4.x patcher has **13 steps** (6 base + 7 higher; steps 7–13 cover trustedProxies, idleTimeout, hybrid + MMR, SearxNG enable, TTS `messages.tts`, `gateway.remote.token` mirror, `auth-profiles.json` sync). Steps 12–13 landed as part of the v0.4.0 → v0.4.1 polish batch.

### Higher steps (7–13) in brief

7. **trustedProxies** — ensure `gateway.trustedProxies` (loopback + `172.16.0.0/12` + optional `OPENCLAW_LAN_CIDR`).
8. **idleTimeout** — `agents.defaults.llm.idleTimeoutSeconds = 300`. The default 120s is too tight for 31B + reasoning + multi-tool chains.
9. **hybrid + MMR** — `memorySearch.query.hybrid` block (BM25 + vector + MMR re-rank). Native OpenClaw feature, upgrade-safe.
10. **SearxNG enable** — `tools.web.search.provider = searxng` + `plugins.entries.searxng.enabled = true`. The bundled SearxNG plugin ships disabled by default.
11. **TTS wiring** — env-gated (`OPENCLAW_TTS_ROUTER_API_KEY`). Detailed spec (enum values, `voiceAliases`, top-level switches): `tts-stack.md` → "v0.4.x `messages.tts` schema enums" + "Patcher step 11 writes three things".
12. **gateway.remote.token mirror** — `gateway.auth.token` → `gateway.remote.token`. Otherwise the loopback CLI WS-connect hits a "gateway token mismatch" and silently falls back to the embedded runner (a side-car path, not the production agent route).

    ```
    [patch-config] Mirroring gateway.auth.token → gateway.remote.token
    ```

13. **auth-profiles.json sync** — per-agent `~/.openclaw/agents/*/agent/auth-profiles.json` `vllm:default.key` ← `VLLM_API_KEY`. The agent runner reads from this per-agent store, NOT from `models.providers.vllm.apiKey` — drift after a `.env` rotation produces HTTP 401 from vLLM even when the config-file apiKey is correct. Skipped if `VLLM_API_KEY` is empty or the `agents/` dir doesn't yet exist (pre-onboarding).

    ```
    [patch-config] Synced vllm:default.key in agents/main/agent/auth-profiles.json
    ```

For the full credential-flow table, see "Which patcher step manages which key" above.

## Persistence (verified 2026-04-20)

After a recreate test (`docker compose down && up -d`), everything survives — the `${OPENCLAW_CONFIG_DIR}` bind mount persists:

- `paired.json`, `pending.json`
- `agents/main/sessions/sessions.json` (chat history)
- `agents/main/agent/auth-profiles.json` (provider credentials)
- `openclaw.json` (+ patch-config re-ensures it on every `up`, idempotently)
- Memory SQLite, `workspace/.dreams/`

## Tool-calling fix gotcha

NOT the `models.providers.vllm.capabilities.supportsTools` flag (the schema validator rejects it: `Unrecognized key: "capabilities"`). The fix: the currently used model must appear in `models.providers.vllm.models[]`.

The onboarding wizard only adds what you interactively pick — a later model swap doesn't land in the catalog automatically, and the agent's tool loop can't find its metadata.

The fix is `patch-config.mjs` step 3, which ensures the following entry before every `up`:

```json
{
  "id": "nvidia/Gemma-4-31B-IT-NVFP4",
  "name": "nvidia/Gemma-4-31B-IT-NVFP4",
  "reasoning": false,
  "input": ["text"],
  "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 },
  "contextWindow": 262144,
  "maxTokens": 8192
}
```

## Known non-critical warnings

- `agents.main.tools.allow allowlist contains unknown entries (gateway, nodes)` — the onboarding wizard mixed unknown tool names into `alsoAllow`. Doesn't block anything.
- `[agent] context-overflow-diag … auto-compaction succeeded` — preemptive compaction on long sessions (780+ messages). A feature, not a bug, but adds multi-minute latency.
- `Gateway bound to "lan" (0.0.0.0)` — intentional (LAN + nginx proxy). `doctor` warns about it.

## CLI overhead (measurements)

- `openclaw --version`: ~0.18 s.
- `openclaw health`: ~5.2 s (WS handshake + auth).
- `openclaw doctor`: ~45 s (multiple pre-checks).
- `openclaw agent --agent main --thinking off`: ~31–51 s end-to-end (backend vLLM ~5.5 s; the rest is gateway WS session setup).
- No simple env-var trick to bring this down — the overhead is gateway-side. For interactive use, the Chrome UI is faster.

## Browser UI behavior

- The chat `agent:main:main` session is a single stream; the `/new` command (New session button) only partially clears it, and a stale context can produce odd replies. Starting via the CLI gives you a clean session.
- Tool traces render in the chat UI (`memory_search` invocation + tool output + agent-summary blocks) — toggle with "Toggle tool calls and tool results".
