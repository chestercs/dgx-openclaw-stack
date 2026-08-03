# MeshCore LoRa bridge — off-grid agent access

Design notes + operator runbook for `openclaw-meshcore-bridge`: a LoRa mesh
front-end for a dedicated OpenClaw agent. A MeshCore handheld (LilyGO T-Deck
or any standalone MeshCore messenger) DMs a companion radio (e.g. LilyGO
T-Echo) plugged into the stack host over USB; the bridge runs one agent turn
per DM and radios the reply back. The handheld side is fully off-grid — the
use case is reaching a networked agent (web search, memory) from places with
no cell coverage.

## Flow

```
Handheld (T-Deck, standalone MeshCore fw)
   │  LoRa DM — E2E encrypted to the companion node's key
   ▼
T-Echo, "Companion Radio USB" firmware ── USB CDC-ACM serial
   │  companion serial protocol (meshcore_py)
   ▼
openclaw-meshcore-bridge (profile: meshcore)
   │  1 persistent operator-role WS connection (connect handshake once)
   ▼
openclaw-gateway ── `agent` RPC {message, sessionKey, agentId: meshcore}
   │
   ▼
meshcore agent — own workspace-meshcore/, AGENTS.md radio contract,
tools.profile minimal + web_search, thinkingDefault off
```

## Design decisions (the why)

### Persistent WS, not per-call CLI

`openclaw agent --agent main --message ...` measures **~31–51 s end-to-end
of which the LLM is only ~5.5 s** — the rest is gateway WS session setup,
paid on every invocation ([`openclaw-internals.md`](./openclaw-internals.md)
→ "CLI overhead"). The bridge speaks the gateway WS protocol directly
(protocol 3/4, `connect` handshake → `agent` req → `res.payloads[]`) and
keeps the connection open, so session setup is paid once at container boot.
A mesh turn then costs ≈ LLM decode + LoRa airtime.

Two auth details that bite (both verified against the live 2026.4.15
gateway during bridge development):

1. **Token**: WS clients authenticate with **`gateway.remote.token`**
   (mirrored from `gateway.auth.token` by patcher step 12) — NOT the `.env`
   `OPENCLAW_GATEWAY_TOKEN`. The bridge auto-reads the right one from the
   ro-mounted OpenClaw config dir; no operator copying.
2. **Scopes are loopback-gated**: a shared-token client is granted its
   declared operator scopes (`operator.read`/`operator.write` — `agent` is
   a write-scoped method) **only on local-direct connections**. From a LAN
   or docker-bridge address the granted scope set is empty (remote clients
   are expected to go through device pairing) and every `agent` call fails
   with `missing scope: operator.write`, even though the connect handshake
   succeeds. That's why the bridge runs with
   `network_mode: service:openclaw-gateway` (loopback, same pattern as
   `openclaw-cli`) instead of joining the compose bridge network — and why
   it must be **recreated together with the gateway** (see
   [`patterns.md`](./patterns.md) → "openclaw-cli network-namespace
   dependency").

Protocol facts the bridge relies on (extracted from the gateway bundle's
`ConnectParamsSchema` / `AgentParamsSchema`, both
`additionalProperties: false`):

- `connect.params.client.id` and `.mode` are closed enums — the bridge
  declares `gateway-client` / `backend` (`displayName: meshcore-bridge`).
- The challenge nonce is NOT echoed at the params root (device-identity
  signatures only); a reconnect reuses the issued deviceToken via the
  dedicated `auth.deviceToken` field.
- The `agent` req requires `idempotencyKey`, has **no** `runId` param, and
  responds immediately with `{runId, status}` — the reply text arrives as
  `agent` events (`stream: "assistant"`, `data.text` = snapshot so far,
  `data.delta` = increment) and `stream: "lifecycle"` `phase: end|error`
  terminates the run. The bridge buffers events by runId BEFORE the res
  arrives (they can race) and waits on the lifecycle terminal.
- `sessionKey` MUST be the canonical agent-scoped form
  `agent:<agentId>:<rest>`. A bare key is NOT auto-scoped to the requested
  agent: `resolveAgentIdFromSessionKey()` defaults it to `main`, and the run
  is then rejected with `invalid agent params: agent "meshcore" does not
  match session key agent "main"` (hit live on first end-to-end test,
  2026-08-03).

### Dedicated agent, not main

Same isolation argument as the Discord agent: the radio persona (max ~2
sentences, plain text, no tool marathons) is enforced in
`workspace-meshcore/AGENTS.md` and must not leak into main's or
discord-friend's behavior. Patcher **step 42** owns the registration —
there's no channel-onboarding CLI for MeshCore, so unlike Discord the
`agents.list[]` entry comes from the patcher (env-gated by
`MESHCORE_ENABLED=on`; write-if-absent, operator edits survive).

`thinkingDefault: off` is deliberate: reasoning tokens multiply decode time
and the mesh user is staring at an e-ink screen.

### Per-contact sessions

sessionKey = `agent:meshcore:meshcore-<pubkey_prefix>-g<n>` — each mesh
contact gets their own conversational rail (memory continuity across DMs),
`/new` bumps `<n>`. Sessions live gateway-side; a bridge restart keeps the
same keys. The `agent:<id>:` prefix is mandatory — see the protocol facts
below.

### The pipe is SMS-sized

A MeshCore DM carries ~130–180 payload bytes. The bridge:

- strips markdown (fences, bold, headings, `[text](url)` → `text url`),
- chunks to `MESHCORE_CHUNK_CHARS` (130) on word boundaries with `k/n`
  numbering,
- auto-sends only `MESHCORE_AUTO_CHUNKS` (3) parts; the tail is buffered
  behind `/more` (`…(+N parts — /more)` trailer),
- paces sends with `MESHCORE_SEND_GAP_S` (3 s) for duty-cycle politeness,
- radios a one-packet ack (`⏳ working on it…`) if the turn is still running
  at `MESHCORE_ACK_AFTER_S` (25 s) — silence provokes resends, and a resend
  costs more airtime than the ack.

The AGENTS.md contract asks the model for < 300 chars; the bridge-side caps
are the hard layer for when it disobeys.

## Threat model

The mesh is a **public radio**: anyone in RF range with the firmware can DM
the companion node. Gates, outermost first:

1. **Bridge allowlist** (`MESHCORE_ALLOWED_PUBKEYS`, pubkey-prefix match) —
   the only inbound gate. Empty default = deny-all; denied senders are
   logged (prefix + first 80 chars) but get **no reply** — don't beacon to
   strangers. `MESHCORE_ALLOW_ALL=on` exists for bench tests only.
2. **Narrow tool surface** — profile `minimal` + `web_search` only. No
   browser, no exec, no sandbox, no messaging tools: a prompt-injected mesh
   contact should have nothing interesting to drive. Widen deliberately via
   `MESHCORE_AGENT_ALSO_ALLOW` if you accept the trade.
3. **AGENTS.md soft policy** — style contract, not a security boundary.

MeshCore DMs are E2E encrypted contact↔node; the plaintext exists on the
companion node, the USB link, and the bridge container. Room-server
broadcast mode was deliberately NOT used — rooms are shared-key, every
member would read the agent's replies.

## Operator runbook

1. **Firmware**: flash the USB node with MeshCore **"Companion Radio USB"**
   (<https://flasher.meshcore.co.uk>). A node running the standalone
   messenger firmware exposes no companion serial protocol — the bridge
   log will show a connect timeout.
2. **Device path** (on the stack host):
   `ls -l /dev/serial/by-id/` → `.env`: `MESHCORE_SERIAL_DEVICE=<by-id path>`.
3. **`.env`**: `MESHCORE_ENABLED=on` (+ the device path).
4. **Apply** (the init container must actually re-run — bind-mount content
   changes don't trigger recreation on their own):

   ```bash
   docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
   docker compose --profile meshcore up -d --build openclaw-meshcore-bridge
   ```

5. **Pair + allowlist**: on the handheld, discover the companion node
   (advert), send it any DM. Bridge log prints
   `DM from non-allowlisted <prefix> ignored` → copy the prefix into
   `MESHCORE_ALLOWED_PUBKEYS`, then
   `docker compose --profile meshcore up -d openclaw-meshcore-bridge`
   (env change → recreate).
6. **Smoke**: DM `/ping` → `pong — bridge up, gateway up` without touching
   the LLM (link-only test), then a real question.

### In-band commands

| Command | Effect |
|---|---|
| `/ping` | Bridge + gateway liveness, no LLM |
| `/new` | Fresh session (new sessionKey generation) |
| `/more` | Next `AUTO_CHUNKS` parts of a buffered long reply |
| `/help` | Command list |

### Debug recipes

```bash
docker logs openclaw-meshcore-bridge --tail 50        # rx/tx, allowlist hits
docker logs openclaw-meshcore-bridge 2>&1 | grep -i "handshake ok"  # WS auth OK?
```

- `gateway connect rejected` → token drift: is patcher step 12 applied?
  (`gateway.remote.token` must equal `gateway.auth.token`.)
- `no contact for prefix` → the companion node hasn't seen the handheld's
  advert yet; send an advert from the handheld and retry.
- Serial connect timeout → wrong firmware (standalone instead of companion)
  or wrong device path (check `ls -l /dev/serial/by-id/`).
- The bridge survives radio replug (serial reconnect loop) and gateway
  restarts (WS backoff + re-handshake, device-token fallback to shared
  secret) without a container restart.

## Failure-mode UX (what the mesh user sees)

| Condition | Reply |
|---|---|
| Gateway WS down > 30 s | `📡 gateway offline — try later` |
| Turn over `MESHCORE_AGENT_TIMEOUT_S` | `⌛ no answer in 180s — try /new or a simpler question` |
| Agent/tool exception | `⚠ agent error: <first 90 chars>` |
| Long reply | first 3 chunks + `…(+N parts — /more)` |

Every inbound from an allowlisted contact gets *some* reply — on a radio
link, silence is indistinguishable from packet loss.
