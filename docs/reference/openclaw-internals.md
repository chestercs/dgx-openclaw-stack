# OpenClaw internals

> Mélyebb technikai reference az OpenClaw-ra: releases + schema + credential
> layout + patcher lépések + persistencia + CLI viselkedés. A stack-szintű
> architektúrát külön doksi ([`docs/ARCHITECTURE.md`](../ARCHITECTURE.md)) fedi
> — ez a fájl az OpenClaw belsőre fókuszál.

## Public repo (chestercs/dgx-openclaw-stack)

- **Repo:** https://github.com/chestercs/dgx-openclaw-stack
- **License:** MIT
- **Default branch:** `main`
- **Latest:** v0.4.1 (2026-04-22)

### Releases & tag-ek

- `v0.1.0` (2026-04-21, `b04f250`) — Initial public release
- `v0.2.0` (2026-04-22, `553427e`) — Remote vLLM backend, CLAUDE.md, SearxNG, hybrid+MMR memory
- `v0.3.0` (2026-04-22, `0c4d799`) — Bilingual TTS surface
- `v0.4.0` (2026-04-22, `1b5b208`) — env-driven knobs (`CONTAINER_NAME_PREFIX`, `VLLM_HF_CACHE_VOLUME_NAME`, `TTS_*_BIND/PORT` loopback default), patcher step 11 top-level `messages.tts.{enabled,auto,mode}` switches
- `v0.4.1` (2026-04-22, `f87d3b0`) — Post-v0.4.0 polish batch: patcher **step 12** (`gateway.auth.token` → `gateway.remote.token` mirror, `bcea0a5`), patcher **step 13** (per-agent `auth-profiles.json` sync `VLLM_API_KEY`-jel, `dd935b0`), TTS enum normalizáció (`81f1fa4`), vLLM healthcheck `python3` fix (`fe6726d`), `.env` defaults konvergencia (`e132071`), openclaw-cli `OPENCLAW_GATEWAY_TOKEN` env törlés (`644fe68`), `operator/` gitignore (`80a2412`), docs `11 → 13` step sync

### Aktuális tartalom (v0.4.1)

- **Stack:** `docker-compose.yml` (8 default services + 1 opt-in HU service via `profiles: ["hu"]`), `patch-config.mjs` (13-step idempotent patcher), `bootstrap.sh` (regex-gated secret rotation + HU TTS opt-in prompt), `templates/tool_chat_template_gemma4.jinja`, `searxng/settings/settings.yml`
- **TTS surface:** `openclaw-tts-en/` (Kokoro 82M, default), `openclaw-tts-router/` (FastAPI passthrough + ffmpeg), `openclaw-tts-f5hun/` (F5-TTS HU, opt-in via `profiles: ["hu"]`)
- **Docs:** `README.md`, `SETUP.md`, `docs/{ARCHITECTURE,CUSTOMIZATION,TROUBLESHOOTING}.md`, `CLAUDE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `LICENSE` (MIT)
- **GitHub meta:** `.github/FUNDING.yml`, `.github/ISSUE_TEMPLATE/`, `.github/PULL_REQUEST_TEMPLATE.md`

### Verified use case-ek

1. **GB10 reference profile** — `docker compose up -d` no edits, ~6.9 tok/s decode Gemma 4 31B NVFP4 256K context, multimodal, tool calling, hybrid memory, SearxNG, TTS
2. **Remote vLLM backend** (verified GPU-less Windows hoston) — három env override (`OPENAI_BASE_URL`, `LLM_BASE_URL`, `EMBED_BASE_URL`) + `profiles: ["never"]` a vllm-llm/vllm-embedding service-eken. Lokálisan ~1 GB footprint. Walkthrough: `docs/CUSTOMIZATION.md` → "Run with a remote vLLM backend"

### Két-fázisú fresh-install onboarding (általános OpenClaw flow-tanulság)

A patcher `exit 0`-val skip-el ha `openclaw.json` még nem létezik. Fresh install sequence:

1. `docker compose up -d` → patcher skip → gateway crash-loop `Missing config. Run openclaw setup …` — **expected, nem bug**, NE "fix"-eld (az OpenClaw security model expliciten igényli az onboardingot)
2. `openclaw onboard --non-interactive --token "$OPENCLAW_GATEWAY_TOKEN"` (vagy Chrome extension wizard)
3. `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli` → patcher fut, all 13 steps applied, gateway healthy

A trio (`config-init + gateway + cli`) **együtt** kell recreate-elve — ha a CLI külön marad, dead network namespace-re mutat (lásd `patterns.md` → openclaw-cli network namespace).

### v0.4.0 env-driven knobs

- `CONTAINER_NAME_PREFIX=dgx-` — container nevek prefix; üresre állítva tiszta nevek (`vllm-llm` stb.); bridge DNS reachability nem függ tőle (compose service name + `hostname:` direktíva)
- `VLLM_HF_CACHE_VOLUME_NAME=dgx-openclaw-hf-cache` — Docker volume label
- `TTS_{EN,F5HUN,ROUTER}_BIND=127.0.0.1` — loopback-only bind default
- `TTS_{EN,F5HUN,ROUTER}_PORT=8091/8090/8092` — host port override

### How to contribute

- Edit + commit + push (`origin = github.com/chestercs/dgx-openclaw-stack`)
- Új release tag-elésnél a `CHANGELOG.md` frissítés kötelező (`[Unreleased]` szekcióból `[X.Y.Z] - YYYY-MM-DD`-be)
- `gh release create vX.Y.Z --notes-file ...` GH Release object-tel
- `gh` CLI: `chestercs` account auth-elve (token scope: gist, read:org, repo)

## OpenClaw config schema (verified)

### Provider szint

```
models.providers.vllm = {
  baseUrl, api ("openai-completions"), apiKey,
  models: [ { id, name, reasoning, input, cost, contextWindow, maxTokens, api } ]
}
```

### Agent szint

```
agents.defaults.model.primary  -> "<provider>/<modelId>"
agents.defaults.models         -> { "<provider>/<modelId>": {} }  (allowlist)
agents.list[].tools            -> { profile: "full", alsoAllow: [...] }  (tools enablement itt van, nem providernél)
```

Az `agents.list[0].tools.profile: "full"` már eleve engedélyezi a tool calling-ot az agent oldalán — a bottleneck NEM az OpenClaw tool-permission, hanem a provider catalog metadata hiánya volt.

### memorySearch schema

A schema kulcsfa NEM `memory.embedding.*` (ez feltételezés volt), hanem `agents.defaults.memorySearch.*`.

Provider enum: `bedrock | gemini | github-copilot | local | mistral | ollama | openai | voyage`. Custom OpenAI-kompat endpointhoz (vLLM) `provider: "openai"` + `remote.baseUrl` + `remote.apiKey` — ez a hivatalosan támogatott útvonal.

A `remote` blokk még `headers`, `batch.{enabled,concurrency,wait}` opciókat is fogadhat.

## v0.4.x credential layout (3-store design, KRITIKUS)

OpenClaw 3 különálló credential store-ral dolgozik. **`.env` secret rotáció után mindhárom helyen szinkronizálni kell**, különben silent breakage.

### A három hely

#### 1. `.env` (operator intent)
- `VLLM_API_KEY` — vLLM API kulcs (`bootstrap.sh` rotálja `openssl rand` 88-char base64-gyel)
- `OPENCLAW_GATEWAY_TOKEN` — gateway WS auth token (csak pre-onboarding default)

#### 2. `~/.openclaw/openclaw.json` (config file, JSON)
- `models.providers.vllm.apiKey` — patcher step 2 írja `VLLM_API_KEY`-ből
- `agents.defaults.memorySearch.remote.apiKey` — patcher step 4 írja
- `gateway.auth.token` — onboarding wizard írja (különbözik az env-tokentől!)
- `gateway.remote.token` — patcher step 12 mirror-eli `gateway.auth.token`-ből
- `messages.tts.providers.openai.apiKey` — patcher step 11 írja `OPENCLAW_TTS_ROUTER_API_KEY`-ből
- `authProfiles.{name}` — csak metadata (`{provider, mode}`), NEM tartalmazza a tényleges kulcsot

#### 3. `~/.openclaw/agents/{agent}/agent/auth-profiles.json` (per-agent credential store)
- `profiles."vllm:default".key` — itt van a tényleges agent runner által használt kulcs
- **Az agent runner ezt olvassa, NEM a `models.providers.vllm.apiKey`-t**
- Onboarding wizard seed-eli, drift után stale → minden LLM hívás HTTP 401-be fut
- Patcher step 13 sync-eli `VLLM_API_KEY`-ből

### A típusos drift szcenárió (incident: 2026-04-22)

`bootstrap.sh` rotálja `.env`-ben a `VLLM_API_KEY`-t. Step 2 + 4 frissíti `openclaw.json`-t. **De a `auth-profiles.json` érintetlen marad** — az agent runner stale kulcsot küld → vLLM 401.

Tünet: `payloads[0].text = "HTTP 401: Unauthorized"`, `executionTrace.attempts[0].result = success` (mert vLLM válaszolt, csak 401-et).

### Verify recipe (per-agent kulcs ellenőrzés)

```bash
docker exec openclaw-cli node -e '
  const p=require("/home/node/.openclaw/agents/main/agent/auth-profiles.json");
  const c=require("crypto");
  const k=p.profiles["vllm:default"].key;
  console.log("len="+k.length+" sha="+c.createHash("sha256").update(k).digest("hex").slice(0,12));
'
docker exec vllm-llm sh -c 'cat /proc/1/cmdline' | tr '\0' '\n' | awk '/^--api-key$/{getline; printf "%s", $0}' | sha256sum | cut -c1-12
# A két hash kell hogy egyezzen
```

### Patcher step-ek mely kulcsokat kezelik

| Step | Hely | Mező |
|---|---|---|
| 2 | openclaw.json | `models.providers.vllm.apiKey` |
| 4 | openclaw.json | `agents.defaults.memorySearch.remote.apiKey` |
| 11 | openclaw.json | `messages.tts.providers.openai.apiKey` |
| 12 | openclaw.json | `gateway.remote.token` ← `gateway.auth.token` |
| 13 | agents/*/agent/auth-profiles.json | `profiles."vllm:default".key` |

### CLI agent runner viselkedés

`openclaw agent --agent main` alapértelmezetten **`runner: embedded`** módban fut (loopback fast-path, nem WS round-trip). Az embedded runner az `auth-profiles.json` kulcsot olvassa, és a CLI containerben futó `OPENAI_API_KEY` env-vel **nem** felülírható. Ezért step 13 elengedhetetlen.

A gateway WS route-ot a remote CLI / Chrome extension használja — `gateway.remote.token` (step 12) számukra fontos.

### Public commit-ok ami megoldotta

- `81f1fa4` — step 11 enum bug (`auto`, `mode` mezők enum értékei)
- `bcea0a5` — step 12 + openclaw-cli OPENAI_API_KEY env
- `644fe68` — openclaw-cli env-jéből OPENCLAW_GATEWAY_TOKEN törlés
- `dd935b0` — step 13 (auth-profiles.json sync)

## Patch-config 6-step base (idempotent init service)

`openclaw-config-init` init service futása `openclaw-gateway` előtt (`depends_on: service_completed_successfully`):

1. **Cleanup**: eltávolítja a korábbi rossz `models.providers.vllm.capabilities` kulcsot (legacy)
2. **Ensure vLLM provider core**: `models.providers.vllm.{baseUrl,api,apiKey}` a `VLLM_API_KEY` env-ből. Az onboarding wizard placeholder apiKey-t (12 char) tesz ide, ami miatt "Profile vllm:default timed out" hibával kapcsolódik a gateway — a patch a valódi 139 char kulccsal felülírja minden `up` előtt
3. **Ensure**: `nvidia/Gemma-4-31B-IT-NVFP4` entry a provider `models[]`-ban
4. **Ensure memorySearch**: `agents.defaults.memorySearch = { enabled: true, provider: "openai", model: "BAAI/bge-m3", remote: { baseUrl: "${EMBED_BASE_URL:-http://vllm-embedding:8005/v1/}", apiKey: process.env.VLLM_API_KEY } }`. Phase 6 után az embedding service in-stack (`vllm-embedding`), bridge DNS-en érhető el
5. **Ensure heartbeat** (always-on): `agents.defaults.heartbeat = { every: "30m", includeReasoning: true, isolatedSession: true, activeHours: { start: "09:00", end: "02:00", timezone: "Europe/Budapest" } }`
6. **Ensure dreaming** (env-gated): ha `OPENCLAW_ENABLE_DREAMING=1`, `plugins.entries.memory-core = { enabled: true, config: { dreaming: { enabled: true, frequency: "0 3 * * *", timezone: "Europe/Budapest", storage.mode: "both", phases: { light, deep, rem } } } }`. Ha NEM 1 (default), cleanup mode

A 2026.4.15 gateway-nél sikeres plugin load visszaigazolás: `[plugins] memory-core: created managed dreaming cron job.`

A v0.4.x patcher **13-step** (6 base + 7 higher; steps 7-13: trustedProxies, idleTimeout, hybrid+MMR, SearxNG enable, TTS messages.tts, gateway.remote.token mirror, auth-profiles.json sync). Steps 12-13 a v0.4.0 → v0.4.1 polish batch részeként kerültek be.

### Higher steps (7-13) röviden

7. **trustedProxies** — `gateway.trustedProxies` ensure (loopback + `172.16.0.0/12` + opcionális `OPENCLAW_LAN_CIDR`).
8. **idleTimeout** — `agents.defaults.llm.idleTimeoutSeconds = 300`. Default 120s túl szűk 31B + reasoning + multi-tool chain-ekre.
9. **hybrid + MMR** — `memorySearch.query.hybrid` blokk (BM25 + vector + MMR re-rank). Native OpenClaw feature, upgrade-safe.
10. **SearxNG enable** — `tools.web.search.provider = searxng` + `plugins.entries.searxng.enabled = true`. A bundled SearxNG plugin default-disabled jön.
11. **TTS wiring** — env-gated (`OPENCLAW_TTS_ROUTER_API_KEY`). Részletes spec (enum értékek, voiceAliases, top-level switches): `tts-stack.md` → "v0.4.x messages.tts schema enums" + "Patcher step 11 három dolgot ír" szakasz.
12. **gateway.remote.token mirror** — `gateway.auth.token` → `gateway.remote.token`. A loopback CLI WS-connect-je különben "gateway token mismatch"-csel némán embedded runner-be esik (side-car path, nem a production agent route).

    ```
    [patch-config] Mirroring gateway.auth.token → gateway.remote.token
    ```

13. **auth-profiles.json sync** — per-agent `~/.openclaw/agents/*/agent/auth-profiles.json` `vllm:default.key` ← `VLLM_API_KEY`. Az agent runner ezt a per-agent store-t olvassa, NEM a `models.providers.vllm.apiKey`-t — drift egy `.env` rotáció után HTTP 401-et ad vLLM-től, akkor is ha a config-file apiKey korrekt. Skip ha `VLLM_API_KEY` üres vagy az agents dir nem létezik (pre-onboarding).

    ```
    [patch-config] Synced vllm:default.key in agents/main/agent/auth-profiles.json
    ```

A részletes credential-flow táblázat fent: "Patcher step-ek mely kulcsokat kezelik".

## Persistencia (igazolva 2026-04-20)

Recreate teszt (`docker compose down && up -d`) után minden megmarad — `${OPENCLAW_CONFIG_DIR}` bind mount perzisztál:
- paired.json, pending.json
- agents/main/sessions/sessions.json (chat history)
- agents/main/agent/auth-profiles.json (provider credentials)
- openclaw.json (+ patch-config ensure-öli minden `up`-kor, idempotensen)
- memory sqlite, workspace/.dreams/

## Tool calling fix gotcha

NEM a `models.providers.vllm.capabilities.supportsTools` flag (ezt a schema validator elutasítja: `Unrecognized key: "capabilities"`). A fix: a `models.providers.vllm.models[]` tömbben szerepelnie kell az aktuálisan használt modellnek.

Az onboarding wizard csak azt veszi fel, amit interaktívan választasz — későbbi modell-csere a catalog-ba nem kerül be automatikusan, és az agent tool loop nem találja a metadatát.

A javítás a `patch-config.mjs` step 3-ban van, ami minden `up` előtt ensure-öli:

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

## Ismert nem-kritikus warning-ok

- `agents.main.tools.allow allowlist contains unknown entries (gateway, nodes)` — onboarding wizard kevert be ismeretlen tool-neveket az `alsoAllow`-ba. Nem blokkol semmit
- `[agent] context-overflow-diag … auto-compaction succeeded` — hosszú session-eknél (780+ üzenet) preemptive compaction. Feature, nem bug, de több perces késleltetést okoz
- `Gateway bound to "lan" (0.0.0.0)` — szándékos (LAN + nginx proxy), doctor warnol

## CLI overhead (mérés)

- `openclaw --version`: ~0.18 s
- `openclaw health`: ~5.2 s (ws handshake + auth)
- `openclaw doctor`: ~45 s (több pre-check)
- `openclaw agent --agent main --thinking off`: ~31–51 s end-to-end (backend vLLM ~5.5 s; különbözet gateway ws session setup)
- Nincs egyszerű env-var trükk amivel csökkenthető lenne — overhead gateway-oldali. Interaktív használathoz a Chrome UI gyorsabb.

## Browser UI viselkedés

- Chat `agent:main:main` session single-stream; `/new` parancs (New session gomb) részben tisztít, néha maradvány context miatt furcsa választ adhat. CLI-n indítva tiszta sessiont lehet kapni
- Tool trace a chat UI-ban renderelődik (`memory_search` invocation + tool output + agent summary blokkokkal) — toggle "Toggle tool calls and tool results" gombbal
