# openclaw-meshcore-bridge

Status: optional (compose profile `meshcore` plus `MESHCORE_ENABLED=on`).

Talk to your OpenClaw agent over a **MeshCore LoRa mesh** — from a handheld
(LilyGO T-Deck or any standalone MeshCore messenger) to a companion radio
(e.g. LilyGO T-Echo) plugged into the stack host over USB. Fully off-grid on
the handheld side; the agent side keeps its tools (web search, memory).

```
T-Deck (standalone MeshCore)          stack host
   │  LoRa DM (E2E encrypted)   ┌────────────────────────────────┐
   ▼                            │ /dev/serial/by-id/… (USB CDC)  │
[mesh] ─── ─── ─── ─── ───────▶ │  T-Echo (Companion Radio USB fw)│
                                │        │ serial protocol        │
                                │  openclaw-meshcore-bridge       │
                                │        │ persistent WS (RPC)    │
                                │  openclaw-gateway ── meshcore   │
                                │                      agent      │
                                └────────────────────────────────┘
```

Full operator guide: [`docs/reference/meshcore-bridge.md`](../docs/reference/meshcore-bridge.md).

Gateway image updates and serial-link recovery:
[`docs/OPENCLAW_UPGRADE.md`](../docs/OPENCLAW_UPGRADE.md).

## Why a persistent WS connection

`openclaw agent --message ...` pays ~25–45 s of gateway WS session setup on
**every** invocation (see `docs/reference/openclaw-internals.md` → "CLI
overhead"). The bridge holds one operator-role WS connection open instead, so
a mesh turn costs approximately the LLM decode time plus LoRa airtime.

## Why `network_mode: service:openclaw-gateway`

Auth, not convenience: the gateway grants a shared-token WS client its
declared operator scopes (`agent` needs `operator.write`) **only on
local-direct / loopback connections** — from a LAN or docker-bridge address
the grant is empty and every agent call fails with
`missing scope: operator.write`. Sharing the gateway's network namespace
(same pattern as `openclaw-cli`) makes the bridge a loopback client.
Corollary: when you recreate the gateway, recreate this container too
(`patterns.md` → "openclaw-cli network-namespace dependency").

## Quickstart

1. Flash the USB-attached node with the MeshCore **Companion Radio USB**
   firmware (<https://flasher.meshcore.co.uk>) — *not* the standalone
   messenger firmware the handheld runs.
2. Find the stable device path on the host:
   `ls -l /dev/serial/by-id/` → put it in `.env` as `MESHCORE_SERIAL_DEVICE`.
3. Set `MESHCORE_ENABLED=on` in `.env` (config-init registers the `meshcore`
   agent in `config-init/steps/agents.mjs`), then:

   ```bash
   docker compose up -d --force-recreate openclaw-config-init openclaw-gateway
   docker compose --profile meshcore up -d --build openclaw-meshcore-bridge
   ```

4. From the handheld, DM the companion node. The first DM is **ignored but
   logged** — copy the logged pubkey prefix into `MESHCORE_ALLOWED_PUBKEYS`
   and recreate the bridge container. (The mesh is a public radio; the agent
   is allowlist-only by design.)

## In-band commands

None of these touch the LLM — one packet each, and they work even when the
gateway is down.

| Command | Effect |
|---|---|
| `/status` | Gateway up/down, agent, busy/idle, last SNR, uptime, queued parts |
| `/whoami` | Which surface / agent / session you're on |
| `/snr` | Last packet's SNR, hop count, node battery |
| `/last` | Resend the last reply from part 1 (dropped-packet recovery) |
| `/more` | Next chunks of a long reply held back by the auto-send cap |
| `/new`, `/reset` | Fresh conversation session |
| `/stop` | Abort the in-flight agent turn |
| `/advert` | Re-announce the node so neighbours refresh routes (`/advert flood`) |
| `/ping` | Link test |
| `/help` | Command list |

Anything else goes to the agent on the sender's own session. On a channel the
trigger prefix comes first, so it's `?/status` there.

## Env knobs

All documented in the "MeshCore bridge" block of `.env.example`. The
essentials: `MESHCORE_SERIAL_DEVICE` (use the `/dev/serial/by-id/…` path),
`MESHCORE_ALLOWED_PUBKEYS` (comma-separated pubkey-prefix allowlist),
`MESHCORE_CHUNK_CHARS` / `MESHCORE_AUTO_CHUNKS` (reply budget),
`MESHCORE_ACK_AFTER_S` (late-ack ping). The gateway token is auto-read from
the read-only-mounted OpenClaw config (`gateway.remote.token`) — no manual
token copying.

`MESHCORE_GW_CONNECT_TIMEOUT_S` and `MESHCORE_GW_PING_TIMEOUT_S` default to 60
seconds. They tolerate temporary Gateway stalls without constant reconnects.
A missing serial-companion response is a separate radio/USB problem.

## Tests

```bash
docker build -t openclaw-meshcore-bridge:0.1.1 openclaw-meshcore-bridge
docker run --rm -v "$PWD/openclaw-meshcore-bridge/tests:/tests:ro" \
  --entrypoint python openclaw-meshcore-bridge:0.1.1 \
  -m unittest discover -s /tests -v
```
