#!/usr/bin/env python3
"""MeshCore <-> OpenClaw gateway bridge.

Listens for direct messages arriving at a MeshCore companion radio (USB
serial, e.g. a LilyGO T-Echo flashed with the "Companion Radio USB"
firmware), forwards them to a dedicated OpenClaw agent over the gateway's
WebSocket RPC, and radios the agent's reply back to the sender as one or
more LoRa DMs. Optionally does the same on ONE shared channel.

Design notes (the "why" behind the shape of this file):

- **One persistent WS connection.** `openclaw agent --message ...` pays
  ~25-45s of gateway WS session setup on EVERY call (measured — see
  docs/reference/openclaw-internals.md "CLI overhead"). Holding the
  connection open pays that cost once at boot; per-turn latency then
  approaches the raw vLLM decode time.
- **Per-peer sessions.** sessionKey `agent:<id>:meshcore-<peer>-g<n>` gives
  every mesh contact — and the watched channel — its own conversational rail
  with continuity across messages; `/new` bumps `<n>` to start fresh.
- **LoRa is an SMS-sized pipe.** Replies are markdown-stripped, chunked
  to MESHCORE_CHUNK_CHARS, and only the first MESHCORE_AUTO_CHUNKS parts
  are sent unsolicited; the rest waits behind `/more`. An inter-chunk gap
  keeps the duty cycle polite.
- **The mesh is a public radio.** For DMs, only pubkey-prefix-allowlisted
  contacts reach the agent; everyone else is logged and ignored (no reply —
  don't beacon to strangers).
- **Channels are weaker than DMs, by protocol.** A channel message carries
  NO sender public key, so the allowlist cannot gate it: the only gate is
  knowledge of the shared 16-byte channel secret, and every member reads the
  agent's replies. Channel support is therefore off unless
  MESHCORE_CHANNEL_IDX is set, and a trigger prefix keeps the agent out of
  human-to-human chatter.

Env contract is documented in .env.example ("MeshCore bridge" block) and
docs/reference/meshcore-bridge.md.
"""

import asyncio
import hashlib
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


def parse_dm_session_groups(raw):
    """`label:prefix,prefix;label2:prefix` → {prefix: label}.

    Lets several radios owned by the same person share ONE conversation
    instead of each pubkey getting its own rail — write from the handheld,
    continue from a second node. Groups are separated by `;`, members within
    a group by `,`."""
    groups = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry or ":" not in entry:
            if entry:
                log.warning("ignoring malformed session group %r "
                            "(want label:prefix,prefix)", entry)
            continue
        label, members = entry.split(":", 1)
        label = label.strip()
        if not label:
            continue
        for prefix in members.split(","):
            prefix = prefix.strip().lower()
            if prefix:
                groups[prefix] = label
    return groups


DM_SESSION_GROUPS = parse_dm_session_groups(_env("MESHCORE_DM_SESSION_GROUPS"))

# Per-surface system-prompt add-ons. The DM rail and the channel rail hold
# different kinds of conversation (private 1:1 vs shared group), and the
# channel one is also a soft guard against the agent repeating private
# details into a room several people read. NOTE: sessions are separate but
# the agent's MEMORY is not — for hard isolation run a second agent with its
# own workspace (see docs/reference/meshcore-bridge.md).
DM_EXTRA_PROMPT = _env("MESHCORE_DM_EXTRA_PROMPT")
CHANNEL_EXTRA_PROMPT = _env(
    "MESHCORE_CHANNEL_EXTRA_PROMPT",
    "This is a shared group channel: several people receive every reply. "
    "Keep it light and sociable. Never repeat private or personal details "
    "from other conversations here.")

# ─── channel (group) support ─────────────────────────────────────────────────
# Off unless MESHCORE_CHANNEL_IDX is set. SECURITY POSTURE DIFFERS FROM DMs:
# a channel message carries NO sender public key (payload is channel_idx /
# SNR / sender_timestamp / text — verified against meshcore_py's reader), so
# the pubkey allowlist CANNOT gate it. The only gate is knowledge of the
# 16-byte channel secret, shared by every member, who also all read the
# agent's replies. Use a channel only where that's acceptable.
CHANNEL_IDX_RAW = _env("MESHCORE_CHANNEL_IDX")
CHANNEL_IDX = int(CHANNEL_IDX_RAW) if CHANNEL_IDX_RAW.isdigit() else None
CHANNEL_NAME = _env("MESHCORE_CHANNEL_NAME")
CHANNEL_SECRET_HEX = _env("MESHCORE_CHANNEL_SECRET")
CHANNEL_PASSWORD = _env("MESHCORE_CHANNEL_PASSWORD")
# Only answer channel messages addressed to the agent. Without a trigger the
# bridge would reply to every human-to-human line on the channel — airtime
# abuse and a self-sustaining chatter loop risk. Empty = answer everything.
CHANNEL_TRIGGER = _env("MESHCORE_CHANNEL_TRIGGER", "?")


def resolve_channel_secret():
    """Resolve the 16-byte channel key, in precedence order:

      1. MESHCORE_CHANNEL_SECRET — explicit 32-hex-char key.
      2. MESHCORE_CHANNEL_PASSWORD — sha256(password)[:16].
      3. the channel NAME — sha256(name)[:16].

    (3) is the default because it's MeshCore's own convention for named
    channels, and it's the ONLY thing many handheld firmwares can do: their
    "edit channel" UI offers a name field and nothing else, so they derive
    the key from the name. On such a mesh the channel name IS the shared
    secret, and a separate password is not expressible — put the secret in
    the name if you want one (verified against a T-Deck build, 2026-08-03).

    Use (2)/(1) only when every participating device can import a raw key."""
    if CHANNEL_SECRET_HEX:
        raw = CHANNEL_SECRET_HEX.replace(" ", "")
        try:
            secret = bytes.fromhex(raw)
        except ValueError:
            log.error("MESHCORE_CHANNEL_SECRET is not valid hex — ignoring")
            return None
        if len(secret) != 16:
            log.error("MESHCORE_CHANNEL_SECRET must be 16 bytes (32 hex "
                      "chars), got %d — ignoring", len(secret))
            return None
        return secret
    if CHANNEL_PASSWORD:
        log.warning("channel key derived from MESHCORE_CHANNEL_PASSWORD — "
                    "other devices must import the raw key (they cannot "
                    "re-derive it from the password); a name-only handheld "
                    "UI will NOT be able to join this channel")
        return hashlib.sha256(CHANNEL_PASSWORD.encode()).digest()[:16]
    if CHANNEL_NAME:
        return hashlib.sha256(CHANNEL_NAME.encode()).digest()[:16]
    return None

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

    async def agent_turn(self, message, session_key, extra_prompt=None):
        """One agent run; returns plain reply text.

        Contract (verified against the live gateway's AgentParamsSchema +
        handler): the `agent` req is validated strictly
        (additionalProperties: false; idempotencyKey REQUIRED; there is no
        runId param — the gateway assigns one) and the res arrives
        immediately with {runId, status: accepted|in_flight|...}. The reply
        text then streams as `agent` events for that runId; lifecycle
        phase end/error terminates the run."""
        params = {
            "message": message,
            "sessionKey": session_key,
            "agentId": AGENT_ID,
            "deliver": False,
            "timeout": int(AGENT_TIMEOUT_S),
            "idempotencyKey": str(uuid.uuid4()),
        }
        if extra_prompt:
            params["extraSystemPrompt"] = extra_prompt
        res = await self.request("agent", params,
                                 timeout=min(60.0, AGENT_TIMEOUT_S))
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
    """Owns the companion-radio serial link: inbound DM + channel
    subscriptions, contact resolution by pubkey prefix, channel slot
    provisioning, paced chunked sends."""

    def __init__(self, on_message):
        self.mc = None
        self.on_message = on_message  # async callback(peer, text)
        self._send_lock = asyncio.Lock()
        self._last_rx = {}  # peer → (text, monotonic) dup guard
        self._unwatched_seen = set()  # channel slots already reported once

    async def connect(self):
        self.mc = await MeshCore.create_serial(SERIAL_DEVICE, SERIAL_BAUD)
        self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._handle_rx)
        if CHANNEL_IDX is not None:
            self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self._handle_chan_rx)
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
        await self._ensure_channel()

    async def _ensure_channel(self):
        """Idempotently provision the configured channel slot — the same
        desired-state posture patch-config.mjs uses for openclaw.json, so a
        replaced radio self-heals on the next boot instead of needing manual
        setup. Only ever writes the ONE configured slot; other slots (the
        Public channel, the operator's own channels) are never touched."""
        if CHANNEL_IDX is None:
            return
        secret = resolve_channel_secret()
        if not CHANNEL_NAME or not secret:
            log.error("MESHCORE_CHANNEL_IDX=%s but MESHCORE_CHANNEL_NAME is "
                      "missing — channel disabled", CHANNEL_IDX)
            return
        result = await self.mc.commands.get_channel(CHANNEL_IDX)
        cur_name, cur_secret = None, None
        if result.type != EventType.ERROR:
            p = result.payload or {}
            cur_name = p.get("channel_name", p.get("name"))
            cur_secret = p.get("channel_secret", p.get("secret"))
            if isinstance(cur_secret, str):
                try:
                    cur_secret = bytes.fromhex(cur_secret)
                except ValueError:
                    cur_secret = None
        fp = secret[:4].hex()
        if cur_name == CHANNEL_NAME and cur_secret == secret:
            log.info("channel %d already set: %r (key fp %s)",
                     CHANNEL_IDX, CHANNEL_NAME, fp)
            return
        if cur_name and cur_name != CHANNEL_NAME:
            log.warning("channel %d currently holds %r — overwriting with %r "
                        "(MESHCORE_CHANNEL_IDX points here)",
                        CHANNEL_IDX, cur_name, CHANNEL_NAME)
        set_result = await self.mc.commands.set_channel(
            CHANNEL_IDX, CHANNEL_NAME, secret)
        if set_result.type == EventType.ERROR:
            log.error("set_channel %d failed: %s",
                      CHANNEL_IDX, set_result.payload)
            return
        # Key fingerprint only — the full secret is a shared group key and
        # container logs are a poor place to keep it. Join other devices with
        # the value from .env / the operator handoff, not from the log.
        log.info("channel %d provisioned: %r (key fp %s), trigger %r",
                 CHANNEL_IDX, CHANNEL_NAME, fp, CHANNEL_TRIGGER or "<none>")

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

    def _dedupe(self, peer, text):
        """LoRa flood-retry can deliver the same message twice in short
        order. True = already seen, drop it."""
        last = self._last_rx.get(peer)
        now = time.monotonic()
        if last and last[0] == text and now - last[1] < 30:
            return True
        self._last_rx[peer] = (text, now)
        return False

    async def _handle_rx(self, event):
        payload = event.payload or {}
        prefix = str(payload.get("pubkey_prefix") or "").lower()
        text = (payload.get("text") or "").strip()
        if not prefix or not text:
            return
        peer = ("dm", prefix)
        if self._dedupe(peer, text):
            log.debug("dup DM from %s suppressed", prefix)
            return
        asyncio.create_task(self.on_message(peer, text))

    async def _handle_chan_rx(self, event):
        payload = event.payload or {}
        idx = payload.get("channel_idx")
        text = (payload.get("text") or "").strip()
        if idx is None or not text:
            return
        if idx != CHANNEL_IDX:
            # Actionable misconfiguration, not routine noise — but the radio
            # may sit on busy channels (slot 0 is usually Public), so say it
            # once per slot at INFO instead of flooding or hiding it at DEBUG.
            if idx not in self._unwatched_seen:
                self._unwatched_seen.add(idx)
                log.info("traffic on channel slot %s, watching slot %s — set "
                         "MESHCORE_CHANNEL_IDX=%s if that's the agent channel",
                         idx, CHANNEL_IDX, idx)
            return
        peer = ("chan", idx)
        if self._dedupe(peer, text):
            log.debug("dup channel message suppressed")
            return
        asyncio.create_task(self.on_message(peer, text))

    async def send_text(self, peer, text):
        """One paced message (single chunk) to a DM contact or a channel.
        Serialized bridge-wide so replies to different peers can't interleave
        into a duty-cycle burst."""
        kind, dest = peer
        async with self._send_lock:
            for attempt in range(2):
                if kind == "chan":
                    result = await self.mc.commands.send_chan_msg(dest, text)
                else:
                    contact = await self.find_contact(dest)
                    if contact is None:
                        log.warning("no contact for prefix %s — reply dropped "
                                    "(radio hasn't seen their advert yet?)",
                                    dest)
                        return False
                    result = await self.mc.commands.send_msg(contact, text)
                if result.type != EventType.ERROR:
                    # Log every TX: on a radio link an operator needs to see
                    # what actually left the node, not just what came in.
                    log.info("TX to %s:%s (%d B): %r", kind, dest,
                             len(text.encode()), text[:120])
                    await asyncio.sleep(SEND_GAP_S)
                    return True
                log.warning("send to %s:%s failed (%s), attempt %d",
                            kind, dest, result.payload, attempt + 1)
                await asyncio.sleep(5)
        return False

    async def send_chunks(self, peer, chunks, total=None, start=1):
        """Send `chunks` as `k/total`-numbered parts, `k` counting from
        `start` (1-based). The caller owns `start` because a batch can be the
        head of a reply or a /more continuation."""
        total = total if total is not None else len(chunks)
        numbered = total > 1
        for i, chunk in enumerate(chunks):
            body = f"{start + i}/{total} {chunk}" if numbered else chunk
            if not await self.send_text(peer, body):
                break


# ─── bridge glue ─────────────────────────────────────────────────────────────

# Channel clients prepend the sender's display name to the text ("Name: msg")
# because channel messages carry no cryptographic sender identity. Used only
# for logging and to find the trigger after the name — never for authz.
_CHAN_SENDER_RE = re.compile(r"^([^:\n]{1,32}):\s*(.*)$", re.DOTALL)


class Bridge:
    def __init__(self, gateway, mesh):
        self.gateway = gateway
        self.mesh = mesh
        self.session_gen = {}    # peer → int, bumped by /new
        self.more_buf = {}       # peer → (remaining chunks, total)
        self.locks = {}          # peer → Lock (serialize turns per peer)

    @staticmethod
    def _session_id(peer):
        """Conversation identity, which is NOT the same as transport identity:
        the channel is one rail, and several DM radios can map to one shared
        rail via MESHCORE_DM_SESSION_GROUPS. Paging buffers stay keyed by the
        actual peer (they belong to the device that's reading), while sessions
        and locks key off this."""
        kind, dest = peer
        if kind == "chan":
            return f"chan{dest}"
        for prefix, label in DM_SESSION_GROUPS.items():
            if dest.startswith(prefix):
                return f"grp-{label}"   # `grp-` avoids colliding with chanN
        return dest

    def _session_key(self, peer):
        # MUST be the canonical agent-scoped form. A bare key is resolved by
        # resolveAgentIdFromSessionKey(), which defaults to agent "main", and
        # the gateway then rejects the run with `invalid agent params: agent
        # "meshcore" does not match session key agent "main"` (verified live
        # 2026-08-03). `-g<n>` is the generation counter that /new bumps.
        sid = self._session_id(peer)
        gen = self.session_gen.get(sid, 0)
        return f"agent:{AGENT_ID}:meshcore-{sid}-g{gen}"

    @staticmethod
    def _extra_prompt(peer):
        return CHANNEL_EXTRA_PROMPT if peer[0] == "chan" else DM_EXTRA_PROMPT

    def _allowed(self, peer):
        kind, dest = peer
        if kind == "chan":
            # Channel membership IS the gate — see the CHANNEL_IDX comment.
            return dest == CHANNEL_IDX
        if ALLOW_ALL:
            return True
        return any(dest.startswith(p) for p in ALLOWED_PUBKEYS)

    @staticmethod
    def _channel_body(text):
        """Extract the message meant for the agent from a channel line.
        Returns (sender_or_None, body) or (sender_or_None, None) when the
        message isn't addressed to the agent. Without a trigger the bridge
        would answer every line humans exchange on the channel."""
        t = text.strip()
        if not CHANNEL_TRIGGER:
            m = _CHAN_SENDER_RE.match(t)
            return (m.group(1).strip(), m.group(2).strip()) if m else (None, t)
        if t.startswith(CHANNEL_TRIGGER):
            return None, t[len(CHANNEL_TRIGGER):].strip()
        # Trigger may sit after the "Name: " prefix the sender's client added.
        m = _CHAN_SENDER_RE.match(t)
        if m:
            rest = m.group(2).strip()
            if rest.startswith(CHANNEL_TRIGGER):
                return m.group(1).strip(), rest[len(CHANNEL_TRIGGER):].strip()
        return None, None

    async def on_mesh_message(self, peer, text):
        kind, dest = peer
        if not self._allowed(peer):
            log.warning("%s from non-allowlisted %s ignored (%r) — add the "
                        "prefix to MESHCORE_ALLOWED_PUBKEYS to admit them",
                        kind.upper(), dest, text[:80])
            return
        if kind == "chan":
            sender, body = self._channel_body(text)
            if body is None:
                log.debug("channel line not addressed to the agent: %r",
                          text[:80])
                return
            if not body:
                return
            log.info("CHAN %s from %s: %r", dest, sender or "?", body[:120])
            text = body
        else:
            sid = self._session_id(peer)
            log.info("DM from %s (session %s): %r", dest, sid, text[:120])
        # Lock on the CONVERSATION, not the device: grouped radios share one
        # session, and two concurrent runs against one sessionKey would race
        # in the gateway.
        lock = self.locks.setdefault(self._session_id(peer), asyncio.Lock())
        async with lock:
            if text.startswith("/"):
                await self._command(peer, text)
            else:
                await self._agent_turn(peer, text)

    async def _command(self, peer, text):
        cmd = text.split()[0].lower()
        if cmd == "/ping":
            await self.mesh.send_text(peer, "pong — bridge up, gateway "
                                      + ("up" if self.gateway.connected.is_set()
                                         else "DOWN"))
        elif cmd == "/new":
            # Bumps the CONVERSATION's generation, so /new from any radio in a
            # session group resets that shared rail (and only it).
            sid = self._session_id(peer)
            self.session_gen[sid] = self.session_gen.get(sid, 0) + 1
            self.more_buf.pop(peer, None)
            await self.mesh.send_text(peer, "🆕 new session")
        elif cmd == "/more":
            remaining, total = self.more_buf.get(peer, ([], 0))
            if not remaining:
                await self.mesh.send_text(peer, "(no more buffered text)")
                return
            batch, rest = remaining[:AUTO_CHUNKS], remaining[AUTO_CHUNKS:]
            self.more_buf[peer] = (rest, total)
            await self._send_batch(peer, batch, rest, total)
        elif cmd == "/help":
            await self.mesh.send_text(
                peer, "cmds: /ping /new /more /help — anything else goes "
                        "to the agent")
        else:
            await self.mesh.send_text(peer, f"unknown cmd {cmd} — /help")

    async def _send_batch(self, peer, batch, rest, total):
        # `batch` is either the head of a fresh reply or a /more continuation,
        # so derive its first part number from what is neither in `batch` nor
        # in `rest` — that's what already went out. (The previous
        # `1 + (total - len(batch))` only held for a tail slice and numbered
        # the FIRST batch of a 4-part reply "2/4, 3/4, 4/4", then repeated
        # "4/4" after /more — reported from the T-Deck, 2026-08-03.)
        start = total - len(batch) - len(rest) + 1
        await self.mesh.send_chunks(peer, batch, total=total, start=start)
        if rest:
            noun = "part" if len(rest) == 1 else "parts"
            await self.mesh.send_text(
                peer, f"…(+{len(rest)} {noun} — /more)")

    async def _agent_turn(self, peer, text):
        self.more_buf.pop(peer, None)
        ack_task = None
        if ACK_AFTER_S > 0:
            ack_task = asyncio.create_task(self._late_ack(peer))
        try:
            reply = await self.gateway.agent_turn(
                text, self._session_key(peer), self._extra_prompt(peer))
        except ConnectionError:
            reply = None
            await self.mesh.send_text(peer, "📡 gateway offline — try later")
        except asyncio.TimeoutError:
            reply = None
            await self.mesh.send_text(
                peer, f"⌛ no answer in {int(AGENT_TIMEOUT_S)}s — try /new "
                        "or a simpler question")
        except Exception as e:  # noqa: BLE001 — always answer the radio
            reply = None
            log.exception("agent turn failed")
            await self.mesh.send_text(peer, f"⚠ agent error: {str(e)[:90]}")
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
            self.more_buf[peer] = (rest, total)
        await self._send_batch(peer, batch, rest, total)

    async def _late_ack(self, peer):
        try:
            await asyncio.sleep(ACK_AFTER_S)
            await self.mesh.send_text(peer, ACK_TEXT)
        except asyncio.CancelledError:
            pass


# ─── main ────────────────────────────────────────────────────────────────────

async def main():
    # force=True is load-bearing: importing `meshcore` installs a root
    # handler, and basicConfig() is a silent no-op once the root logger has
    # one — MESHCORE_LOG_LEVEL=DEBUG then has no effect whatsoever, which
    # cost a debugging round on 2026-08-03.
    logging.basicConfig(
        level=getattr(logging, _env("MESHCORE_LOG_LEVEL", "INFO").upper(),
                      logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True)
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

    async def on_message(peer, text):
        await bridge_ref["b"].on_mesh_message(peer, text)

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
