#!/usr/bin/env python3
"""MeshCore <-> OpenClaw gateway bridge.

Listens for direct messages arriving at a MeshCore companion radio (USB
serial, e.g. a LilyGO T-Echo flashed with the "Companion Radio USB"
firmware), forwards them to a dedicated OpenClaw agent over the gateway's
WebSocket RPC, and radios the agent's reply back to the sender as one or
more LoRa DMs.

Design notes (the "why" behind the shape of this file):

- **One persistent WS connection.** `openclaw agent --message ...` pays
  ~25-45s of gateway WS session setup on EVERY call (measured — see
  docs/reference/openclaw-internals.md "CLI overhead"). Holding the
  connection open pays that cost once at boot; per-turn latency then
  approaches the raw vLLM decode time.
- **Per-contact sessions.** sessionKey `meshcore-<pubkey_prefix>-g<n>`
  gives every mesh contact their own conversational rail with continuity
  across messages; `/new` bumps `<n>` to start fresh.
- **LoRa is an SMS-sized pipe.** Replies are markdown-stripped, chunked
  to MESHCORE_CHUNK_CHARS, and only the first MESHCORE_AUTO_CHUNKS parts
  are sent unsolicited; the rest waits behind `/more`. An inter-chunk gap
  keeps the duty cycle polite.
- **The mesh is a public radio.** Only pubkey-prefix-allowlisted contacts
  reach the agent; everyone else is logged and ignored (no reply — don't
  beacon to strangers).

Env contract is documented in .env.example ("MeshCore bridge" block) and
docs/reference/meshcore-bridge.md.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid

import websockets

from meshcore import MeshCore, EventType

BRIDGE_VERSION = "0.1.0"

log = logging.getLogger("meshcore-bridge")


# ─── configuration ───────────────────────────────────────────────────────────

def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _env_f(name, default):
    try:
        return float(_env(name) or default)
    except ValueError:
        log.warning("bad float in %s — using default %s", name, default)
        return float(default)


def _env_i(name, default):
    return int(_env_f(name, default))


def _env_on(name, default="off"):
    return (_env(name) or default).lower() in ("on", "true", "1", "yes")


SERIAL_DEVICE = _env("MESHCORE_SERIAL_DEVICE_IN", "/dev/meshcore")
SERIAL_BAUD = _env_i("MESHCORE_SERIAL_BAUD", 115200)

# Loopback by default — the bridge shares the gateway's network namespace
# (compose `network_mode: service:openclaw-gateway`, same pattern as
# openclaw-cli). This is LOAD-BEARING for auth, not just convenience: the
# gateway grants a shared-token WS client its declared operator scopes only
# on local-direct connections; from a LAN address the granted scope set is
# empty (remote clients are expected to device-pair) and every `agent` call
# would fail with "missing scope: operator.write". Verified empirically
# against the live 2026.4.15 gateway.
GATEWAY_WS_URL = _env("MESHCORE_GATEWAY_WS_URL", "ws://127.0.0.1:18789")
GATEWAY_TOKEN = _env("MESHCORE_GATEWAY_TOKEN")  # empty → auto-read from config
OPENCLAW_CONFIG_JSON = _env("MESHCORE_OPENCLAW_CONFIG_JSON",
                            "/openclaw-config/openclaw.json")
PROTO_MIN = _env_i("MESHCORE_GW_PROTOCOL_MIN", 3)
PROTO_MAX = _env_i("MESHCORE_GW_PROTOCOL_MAX", 4)

AGENT_ID = _env("MESHCORE_AGENT_ID", "meshcore")
AGENT_TIMEOUT_S = _env_f("MESHCORE_AGENT_TIMEOUT_S", 180)

ALLOWED_PUBKEYS = [p.strip().lower()
                   for p in _env("MESHCORE_ALLOWED_PUBKEYS").split(",")
                   if p.strip()]
ALLOW_ALL = _env_on("MESHCORE_ALLOW_ALL")

CHUNK_CHARS = max(40, _env_i("MESHCORE_CHUNK_CHARS", 130))
AUTO_CHUNKS = max(1, _env_i("MESHCORE_AUTO_CHUNKS", 3))
MAX_REPLY_CHARS = _env_i("MESHCORE_MAX_REPLY_CHARS", 3900)
SEND_GAP_S = _env_f("MESHCORE_SEND_GAP_S", 3.0)

ACK_AFTER_S = _env_f("MESHCORE_ACK_AFTER_S", 25)  # 0 disables the ack ping
ACK_TEXT = _env("MESHCORE_ACK_TEXT", "⏳ working on it…")


def resolve_gateway_token():
    """MESHCORE_GATEWAY_TOKEN env wins; otherwise read the token remote WS
    clients authenticate with (gateway.remote.token, kept in lockstep with
    gateway.auth.token by patcher step 12) from the read-only-mounted
    openclaw.json. NOTE: the OPENCLAW_GATEWAY_TOKEN .env value is NOT the
    right token — the onboarding wizard writes a different one into the
    config (see openclaw-internals.md, credential store #2)."""
    if GATEWAY_TOKEN:
        return GATEWAY_TOKEN
    try:
        with open(OPENCLAW_CONFIG_JSON, encoding="utf-8") as f:
            cfg = json.load(f)
        gw = cfg.get("gateway") or {}
        token = ((gw.get("remote") or {}).get("token")
                 or (gw.get("auth") or {}).get("token") or "")
        if token:
            log.info("gateway token auto-read from %s", OPENCLAW_CONFIG_JSON)
            return token
    except OSError as e:
        log.warning("cannot read %s (%s)", OPENCLAW_CONFIG_JSON, e)
    except (json.JSONDecodeError, AttributeError) as e:
        log.warning("cannot parse %s (%s)", OPENCLAW_CONFIG_JSON, e)
    log.error("no gateway token — set MESHCORE_GATEWAY_TOKEN or mount "
              "the OpenClaw config dir at /openclaw-config (ro)")
    return ""


# ─── reply post-processing ───────────────────────────────────────────────────

_MD_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n?")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_NOISE = re.compile(r"(\*\*|__|(?<!\w)[*_](?!\s)|^#{1,6}\s+|^\s*[-*]\s+)",
                       re.MULTILINE)


def plainify(text):
    """Markdown → plain text. LoRa DMs render on tiny e-ink/TFT screens with
    no markdown support; every formatting byte is wasted airtime."""
    text = _MD_FENCE.sub("", text)
    text = _MD_LINK.sub(r"\1 \2", text)  # keep the URL — it may BE the answer
    text = _MD_NOISE.sub("", text)
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def chunkify(text, size):
    """Split on word boundaries where possible; hard-split otherwise."""
    chunks = []
    while text:
        if len(text) <= size:
            chunks.append(text)
            break
        cut = text.rfind(" ", int(size * 0.6), size)
        if cut < 0:
            cut = size
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return chunks


def extract_payload_text(obj):
    """Defensively pull assistant text out of an `agent` RPC response.
    Known shape: res.payload.payloads[] with `.text` fields (same contract
    the CLI's --json output surfaces as .result.payloads[]); we recurse so a
    wrapper key added by a future gateway doesn't break the bridge."""
    texts = []

    def walk(node):
        if isinstance(node, dict):
            payloads = node.get("payloads")
            if isinstance(payloads, list):
                for p in payloads:
                    if isinstance(p, dict) and isinstance(p.get("text"), str):
                        texts.append(p["text"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return "\n".join(t for t in texts if t.strip())


# ─── OpenClaw gateway WS client ──────────────────────────────────────────────

class GatewayClient:
    """Minimal operator-role client for the OpenClaw gateway WS protocol
    (text frames, JSON: {type: req|res|event}). Reconnects with backoff and
    re-runs the connect handshake; in-flight requests fail fast on drop."""

    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.ws = None
        self.connected = asyncio.Event()
        self._pending = {}          # req id → Future(res frame)
        # runId → run record. Created lazily by WHOEVER touches a runId
        # first — the `agent` res (which tells us our runId) can arrive
        # AFTER the first streaming events for that run, so events buffer
        # into the record unconditionally and agent_turn() picks it up.
        self._runs = {}
        self._device_token = None   # issued at hello-ok, reused on reconnect

    def _run_record(self, run_id):
        rec = self._runs.get(run_id)
        if rec is None:
            rec = {"text": "", "deltas": [], "error": None,
                   "done": asyncio.Event(), "ts": time.monotonic()}
            self._runs[run_id] = rec
            # Prune abandoned records (runs nobody awaited, e.g. heartbeat
            # runs broadcast to every operator client).
            if len(self._runs) > 64:
                cutoff = time.monotonic() - 3600
                for rid in [r for r, v in self._runs.items()
                            if v["ts"] < cutoff]:
                    del self._runs[rid]
        return rec

    async def run_forever(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(
                        self.url, max_size=32 * 1024 * 1024,
                        ping_interval=20, ping_timeout=20) as ws:
                    self.ws = ws
                    await self._handshake(ws)
                    self.connected.set()
                    backoff = 1
                    log.info("gateway WS connected (%s)", self.url)
                    async for frame in ws:
                        self._dispatch(frame)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — reconnect on anything
                log.warning("gateway WS dropped: %s — retry in %ss", e, backoff)
            self.connected.clear()
            self.ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("gateway WS dropped"))
            self._pending.clear()
            # In-flight runs lose their event stream on a drop — fail them
            # now instead of letting agent_turn hang to its full timeout.
            for rec in self._runs.values():
                if not rec["done"].is_set():
                    rec["error"] = rec["error"] or "gateway WS dropped mid-run"
                    rec["done"].set()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _handshake(self, ws):
        # The gateway greets with a connect.challenge event — consume it
        # (its nonce only matters for device-identity signatures, which the
        # shared-token auth below doesn't use); tolerate its absence.
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            if first.get("event") != "connect.challenge":
                self._dispatch_obj(first)
        except asyncio.TimeoutError:
            pass

        req_id = str(uuid.uuid4())
        # Schema notes (verified against the live 2026.4.15 gateway bundle,
        # packages/gateway-protocol ConnectParamsSchema — additionalProperties
        # is false everywhere, so shape matters):
        #   - client.id / client.mode are ENUMS; "gateway-client" / "backend"
        #     is the intended pairing for a headless service like this one.
        #   - the challenge nonce is NOT echoed at the params root (it only
        #     exists inside the optional device-identity signature block).
        #   - a reconnect reuses the issued deviceToken via its DEDICATED
        #     auth.deviceToken field, not auth.token.
        auth = ({"deviceToken": self._device_token} if self._device_token
                else {"token": self.token})
        params = {
            "minProtocol": PROTO_MIN,
            "maxProtocol": PROTO_MAX,
            "client": {"id": "gateway-client", "displayName": "meshcore-bridge",
                       "version": BRIDGE_VERSION, "platform": "linux",
                       "mode": "backend"},
            "role": "operator",
            "scopes": ["operator.read", "operator.write"],
            "auth": auth,
        }
        await ws.send(json.dumps(
            {"type": "req", "id": req_id, "method": "connect",
             "params": params}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if frame.get("type") == "res" and frame.get("id") == req_id:
                if not frame.get("ok"):
                    # Stale device token from a gateway restart? Retry the
                    # shared secret once before giving up.
                    if self._device_token:
                        log.info("device token rejected — falling back to "
                                 "the shared gateway token")
                        self._device_token = None
                        return await self._handshake(ws)
                    raise ConnectionError(
                        f"gateway connect rejected: {frame.get('error')}")
                payload = frame.get("payload") or {}
                dt = (payload.get("auth") or {}).get("deviceToken")
                if dt:
                    self._device_token = dt
                proto = payload.get("protocol")
                log.info("handshake ok (protocol %s)", proto)
                return
            self._dispatch_obj(frame)
        raise ConnectionError("gateway connect: no response in 10s")

    def _dispatch(self, raw):
        try:
            self._dispatch_obj(json.loads(raw))
        except json.JSONDecodeError:
            log.debug("non-JSON frame ignored (%d bytes)", len(raw))

    def _dispatch_obj(self, frame):
        ftype = frame.get("type")
        if ftype == "res":
            fut = self._pending.pop(frame.get("id"), None)
            if fut and not fut.done():
                fut.set_result(frame)
        elif ftype == "event" and frame.get("event") == "agent":
            payload = frame.get("payload") or {}
            run_id = payload.get("runId")
            if not run_id:
                return
            rec = self._run_record(run_id)
            stream = payload.get("stream")
            data = payload.get("data") or {}
            if stream == "assistant":
                # data.text is a snapshot of the text so far, data.delta the
                # increment (verified against the gateway's emitAgentEvent
                # call sites); keep both, prefer the snapshot at the end.
                if isinstance(data.get("text"), str) and data["text"]:
                    rec["text"] = data["text"]
                if isinstance(data.get("delta"), str) and data["delta"]:
                    rec["deltas"].append(data["delta"])
            elif stream == "lifecycle":
                phase = data.get("phase")
                if phase == "error":
                    rec["error"] = str(data.get("error") or "agent run error")
                    rec["done"].set()
                elif phase == "end":
                    rec["done"].set()

    async def request(self, method, params, timeout):
        if not self.connected.is_set():
            # Give a fresh reconnect a moment before declaring the gateway
            # unreachable — a mesh user's message shouldn't die to a blip.
            try:
                await asyncio.wait_for(self.connected.wait(), timeout=30)
            except asyncio.TimeoutError:
                raise ConnectionError("gateway WS not connected") from None
        req_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self.ws.send(json.dumps(
                {"type": "req", "id": req_id, "method": method,
                 "params": params}))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def agent_turn(self, message, session_key):
        """One agent run; returns plain reply text.

        Contract (verified against the live gateway's AgentParamsSchema +
        handler): the `agent` req is validated strictly
        (additionalProperties: false; idempotencyKey REQUIRED; there is no
        runId param — the gateway assigns one) and the res arrives
        immediately with {runId, status: accepted|in_flight|...}. The reply
        text then streams as `agent` events for that runId; lifecycle
        phase end/error terminates the run."""
        res = await self.request("agent", {
            "message": message,
            "sessionKey": session_key,
            "agentId": AGENT_ID,
            "deliver": False,
            "timeout": int(AGENT_TIMEOUT_S),
            "idempotencyKey": str(uuid.uuid4()),
        }, timeout=min(60.0, AGENT_TIMEOUT_S))
        if not res.get("ok"):
            err = (res.get("error") or {})
            raise RuntimeError(err.get("message") or str(err))
        payload = res.get("payload") or {}
        run_id = payload.get("runId")
        if payload.get("status") == "timeout":
            raise asyncio.TimeoutError
        if not run_id:
            # Defensive: some completion paths may answer inline.
            return extract_payload_text(payload)
        rec = self._run_record(run_id)
        try:
            await asyncio.wait_for(rec["done"].wait(),
                                   timeout=AGENT_TIMEOUT_S)
            if rec["error"]:
                raise RuntimeError(rec["error"])
            return rec["text"].strip() or "".join(rec["deltas"]).strip()
        finally:
            self._runs.pop(run_id, None)


# ─── MeshCore side ───────────────────────────────────────────────────────────

class MeshSide:
    """Owns the companion-radio serial link: inbound DM subscription,
    contact resolution by pubkey prefix, paced chunked sends."""

    def __init__(self, on_message):
        self.mc = None
        self.on_message = on_message  # async callback(pubkey_prefix, text)
        self._send_lock = asyncio.Lock()
        self._last_rx = {}  # pubkey_prefix → (text, monotonic) dup guard

    async def connect(self):
        self.mc = await MeshCore.create_serial(SERIAL_DEVICE, SERIAL_BAUD)
        self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._handle_rx)
        # Companion firmware queues messages until the host fetches them;
        # newer meshcore_py drains automatically, older builds need a manual
        # pump on the MESSAGES_WAITING advert.
        if hasattr(self.mc, "start_auto_message_fetching"):
            await self.mc.start_auto_message_fetching()
        else:
            waiting_ev = getattr(EventType, "MESSAGES_WAITING", None)
            if waiting_ev is not None:
                self.mc.subscribe(waiting_ev, self._drain_pending)
            await self._drain_pending(None)
        await self._refresh_contacts()
        node_name = ""
        self_info = getattr(self.mc, "self_info", None)
        if isinstance(self_info, dict):
            node_name = self_info.get("name", "")
        log.info("companion radio up on %s @ %d baud%s",
                 SERIAL_DEVICE, SERIAL_BAUD,
                 f" (node: {node_name})" if node_name else "")

    async def _drain_pending(self, _event):
        get_msg = getattr(self.mc.commands, "get_msg", None)
        if get_msg is None:
            return
        for _ in range(64):  # bounded — don't spin on a firmware quirk
            result = await get_msg()
            if result is None or result.type == EventType.ERROR:
                break
            payload = getattr(result, "payload", None)
            if not payload:
                break

    async def _refresh_contacts(self):
        result = await self.mc.commands.get_contacts()
        if result.type == EventType.ERROR:
            log.warning("get_contacts failed: %s", result.payload)

    def _contacts(self):
        return getattr(self.mc, "contacts", None) or {}

    async def find_contact(self, pubkey_prefix):
        prefix = pubkey_prefix.lower()
        for attempt in range(2):
            for key, contact in self._contacts().items():
                if str(key).lower().startswith(prefix):
                    return contact
            if attempt == 0:
                await self._refresh_contacts()
        return None

    async def _handle_rx(self, event):
        payload = event.payload or {}
        prefix = str(payload.get("pubkey_prefix") or "").lower()
        text = (payload.get("text") or "").strip()
        if not prefix or not text:
            return
        # LoRa flood-retry can deliver the same DM twice in short order.
        last = self._last_rx.get(prefix)
        now = time.monotonic()
        if last and last[0] == text and now - last[1] < 30:
            log.debug("dup DM from %s suppressed", prefix)
            return
        self._last_rx[prefix] = (text, now)
        asyncio.create_task(self.on_message(prefix, text))

    async def send_text(self, pubkey_prefix, text):
        """One paced DM (single chunk). Serialized bridge-wide so replies to
        different contacts can't interleave into a duty-cycle burst."""
        contact = await self.find_contact(pubkey_prefix)
        if contact is None:
            log.warning("no contact for prefix %s — reply dropped "
                        "(radio hasn't seen their advert yet?)", pubkey_prefix)
            return False
        async with self._send_lock:
            for attempt in range(2):
                result = await self.mc.commands.send_msg(contact, text)
                if result.type != EventType.ERROR:
                    # Log every TX: on a radio link an operator needs to see
                    # what actually left the node, not just what came in.
                    log.info("TX to %s (%d B): %r",
                             pubkey_prefix, len(text.encode()), text[:120])
                    await asyncio.sleep(SEND_GAP_S)
                    return True
                log.warning("send_msg to %s failed (%s), attempt %d",
                            pubkey_prefix, result.payload, attempt + 1)
                await asyncio.sleep(5)
        return False

    async def send_chunks(self, pubkey_prefix, chunks, total=None):
        total = total if total is not None else len(chunks)
        numbered = total > 1
        base = 1 + (total - len(chunks))  # continuation offset for /more
        for i, chunk in enumerate(chunks):
            body = f"{base + i}/{total} {chunk}" if numbered else chunk
            if not await self.send_text(pubkey_prefix, body):
                break


# ─── bridge glue ─────────────────────────────────────────────────────────────

class Bridge:
    def __init__(self, gateway, mesh):
        self.gateway = gateway
        self.mesh = mesh
        self.session_gen = {}    # prefix → int, bumped by /new
        self.more_buf = {}       # prefix → (remaining chunks, total)
        self.locks = {}          # prefix → Lock (serialize turns per contact)

    def _session_key(self, prefix):
        # MUST be the canonical agent-scoped form. A bare key is resolved by
        # resolveAgentIdFromSessionKey(), which defaults to agent "main", and
        # the gateway then rejects the run with `invalid agent params: agent
        # "meshcore" does not match session key agent "main"` (verified live
        # 2026-08-03). `-g<n>` is the generation counter that /new bumps.
        gen = self.session_gen.get(prefix, 0)
        return f"agent:{AGENT_ID}:meshcore-{prefix}-g{gen}"

    def _allowed(self, prefix):
        if ALLOW_ALL:
            return True
        return any(prefix.startswith(p) for p in ALLOWED_PUBKEYS)

    async def on_mesh_message(self, prefix, text):
        if not self._allowed(prefix):
            log.warning("DM from non-allowlisted %s ignored (%r) — add the "
                        "prefix to MESHCORE_ALLOWED_PUBKEYS to admit them",
                        prefix, text[:80])
            return
        log.info("DM from %s: %r", prefix, text[:120])
        lock = self.locks.setdefault(prefix, asyncio.Lock())
        async with lock:
            if text.startswith("/"):
                await self._command(prefix, text)
            else:
                await self._agent_turn(prefix, text)

    async def _command(self, prefix, text):
        cmd = text.split()[0].lower()
        if cmd == "/ping":
            await self.mesh.send_text(prefix, "pong — bridge up, gateway "
                                      + ("up" if self.gateway.connected.is_set()
                                         else "DOWN"))
        elif cmd == "/new":
            self.session_gen[prefix] = self.session_gen.get(prefix, 0) + 1
            self.more_buf.pop(prefix, None)
            await self.mesh.send_text(prefix, "🆕 new session")
        elif cmd == "/more":
            remaining, total = self.more_buf.get(prefix, ([], 0))
            if not remaining:
                await self.mesh.send_text(prefix, "(no more buffered text)")
                return
            batch, rest = remaining[:AUTO_CHUNKS], remaining[AUTO_CHUNKS:]
            self.more_buf[prefix] = (rest, total)
            await self._send_batch(prefix, batch, rest, total)
        elif cmd == "/help":
            await self.mesh.send_text(
                prefix, "cmds: /ping /new /more /help — anything else goes "
                        "to the agent")
        else:
            await self.mesh.send_text(prefix, f"unknown cmd {cmd} — /help")

    async def _send_batch(self, prefix, batch, rest, total):
        await self.mesh.send_chunks(prefix, batch, total=total)
        if rest:
            await self.mesh.send_text(
                prefix, f"…(+{len(rest)} parts — /more)")

    async def _agent_turn(self, prefix, text):
        self.more_buf.pop(prefix, None)
        ack_task = None
        if ACK_AFTER_S > 0:
            ack_task = asyncio.create_task(self._late_ack(prefix))
        try:
            reply = await self.gateway.agent_turn(text,
                                                  self._session_key(prefix))
        except ConnectionError:
            reply = None
            await self.mesh.send_text(prefix, "📡 gateway offline — try later")
        except asyncio.TimeoutError:
            reply = None
            await self.mesh.send_text(
                prefix, f"⌛ no answer in {int(AGENT_TIMEOUT_S)}s — try /new "
                        "or a simpler question")
        except Exception as e:  # noqa: BLE001 — always answer the radio
            reply = None
            log.exception("agent turn failed")
            await self.mesh.send_text(prefix, f"⚠ agent error: {str(e)[:90]}")
        finally:
            if ack_task:
                ack_task.cancel()
        if reply is None:
            return
        reply = plainify(reply) or "(empty reply)"
        if len(reply) > MAX_REPLY_CHARS:
            reply = reply[:MAX_REPLY_CHARS].rstrip() + "…"
        chunks = chunkify(reply, CHUNK_CHARS)
        total = len(chunks)
        batch, rest = chunks[:AUTO_CHUNKS], chunks[AUTO_CHUNKS:]
        if rest:
            self.more_buf[prefix] = (rest, total)
        await self._send_batch(prefix, batch, rest, total)

    async def _late_ack(self, prefix):
        try:
            await asyncio.sleep(ACK_AFTER_S)
            await self.mesh.send_text(prefix, ACK_TEXT)
        except asyncio.CancelledError:
            pass


# ─── main ────────────────────────────────────────────────────────────────────

async def main():
    logging.basicConfig(
        level=getattr(logging, _env("MESHCORE_LOG_LEVEL", "INFO").upper(),
                      logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("meshcore-bridge %s starting (agent=%s, device=%s)",
             BRIDGE_VERSION, AGENT_ID, SERIAL_DEVICE)
    if not ALLOW_ALL and not ALLOWED_PUBKEYS:
        log.warning("MESHCORE_ALLOWED_PUBKEYS is empty — every inbound DM "
                    "will be ignored. Send yourself a DM, copy the logged "
                    "prefix into .env, and recreate the container.")

    token = resolve_gateway_token()
    gateway = GatewayClient(GATEWAY_WS_URL, token)
    gw_task = asyncio.create_task(gateway.run_forever())

    bridge_ref = {}

    async def on_message(prefix, text):
        await bridge_ref["b"].on_mesh_message(prefix, text)

    # Serial link with its own reconnect loop — nRF52840 CDC-ACM re-enumerates
    # on radio reboot and the fd goes stale; recreate rather than resurrect.
    while True:
        mesh = MeshSide(on_message)
        try:
            await mesh.connect()
            bridge_ref["b"] = Bridge(gateway, mesh)
            while True:
                await asyncio.sleep(120)
                get_bat = getattr(mesh.mc.commands, "get_bat", None)
                if get_bat is not None:
                    result = await asyncio.wait_for(get_bat(), timeout=15)
                    if result.type == EventType.ERROR:
                        raise ConnectionError("radio keepalive returned error")
        except asyncio.CancelledError:
            gw_task.cancel()
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("serial link lost (%s) — reconnecting in 10s", e)
            try:
                if mesh.mc is not None:
                    await mesh.mc.disconnect()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
