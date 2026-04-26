"""Python code-execution sandbox — MCP Streamable-HTTP server.

Speaks the Model Context Protocol wire format (JSON-RPC 2.0 over HTTP)
on POST /mcp, exposing two tools that the OpenClaw agent can call:

  - python_exec(code, session_id="default", timeout_s=30)
        Run Python in the named session's persistent ipykernel and
        return stdout / stderr / result / inline plots.
  - python_session_reset(session_id="default")
        Tear down the kernel for that session; the next python_exec
        starts a fresh one (variables, imports, dataframes are lost).

Auth: Authorization: Bearer ${PYTHON_SANDBOX_API_TOKEN} on every POST.
The /healthz endpoint is intentionally unauth'd so docker healthchecks
don't need the token.

Why we hand-roll the MCP wire instead of pulling the `mcp` SDK: the
surface we need is one Streamable-HTTP POST handler plus a JSON-RPC
dispatch (initialize, tools/list, tools/call). That's ~150 LOC. The
SDK has churned across 1.x, and locking the protocol shape inside this
file is more honest about the contract OpenClaw will actually see.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from kernel_pool import KernelPool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("python-sandbox")


# ── Config ────────────────────────────────────────────────────────────
TOKEN = os.environ.get("PYTHON_SANDBOX_API_TOKEN", "").strip()
if not TOKEN:
    # Fail fast: a token-less sandbox would let any container on the
    # bridge network execute arbitrary code. The opt-in posture is
    # token-gated for a reason.
    raise RuntimeError("PYTHON_SANDBOX_API_TOKEN must be set (sandbox refuses to start without auth).")

DEFAULT_TIMEOUT_S = float(os.environ.get("PYTHON_SANDBOX_KERNEL_TIMEOUT_S", "30"))
MAX_OUTPUT_BYTES = int(os.environ.get("PYTHON_SANDBOX_MAX_OUTPUT_BYTES", str(10 * 1024 * 1024)))
IDLE_TTL_S = float(os.environ.get("PYTHON_SANDBOX_IDLE_TTL_S", "1800"))  # 30 min
REAP_INTERVAL_S = float(os.environ.get("PYTHON_SANDBOX_REAP_INTERVAL_S", "300"))  # 5 min

# Protocol version we advertise to the client. Matches the MCP spec
# revision we implement; clients that understand a newer revision will
# negotiate down to this one in the initialize handshake.
MCP_PROTOCOL_VERSION = "2025-06-18"

SERVER_INFO = {"name": "openclaw-python-sandbox", "version": "0.1.0"}

# Tool schemas mirror the MCP `tools/list` shape: name, description,
# inputSchema (JSON Schema). The agent reads these to decide when to
# call the tool and what arguments to pass.
TOOLS = [
    {
        "name": "python_exec",
        "description": (
            "Execute Python code in a persistent ipykernel sandbox. "
            "State (variables, imports, dataframes) survives across calls "
            "with the same session_id. Returns stdout, stderr, the last "
            "expression's repr, inline matplotlib plots as base64 PNGs, "
            "and any uncaught exception details. The /workspace directory "
            "(bound from the host) is the canonical place to read/write "
            "files. Network egress is disabled — pip install will fail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source to execute. Multi-line is fine; the last expression's value is returned in `result`.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session key — same id reuses the same kernel so state persists. Default: 'default'.",
                    "default": "default",
                },
                "timeout_s": {
                    "type": "number",
                    "description": f"Max wall-clock seconds before the kernel is interrupted. Default: {DEFAULT_TIMEOUT_S}.",
                    "default": DEFAULT_TIMEOUT_S,
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "python_session_reset",
        "description": (
            "Shut down the kernel for the given session. Variables, "
            "imports, and any in-memory state are discarded. The next "
            "python_exec call against this session_id starts a fresh "
            "kernel."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session key to reset. Default: 'default'.",
                    "default": "default",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
]


# ── App ───────────────────────────────────────────────────────────────
pool = KernelPool(idle_ttl_s=IDLE_TTL_S)

app = FastAPI(title="openclaw-python-sandbox", version="0.1.0")
bearer = HTTPBearer(auto_error=False)


def require_token(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
    """Bearer-token auth dependency for the /mcp route."""
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")


# ── Tool implementations ──────────────────────────────────────────────
async def _tool_python_exec(args: dict) -> dict:
    code = args.get("code")
    if not isinstance(code, str) or not code:
        raise ValueError("`code` is required and must be a non-empty string")
    sid = args.get("session_id") or "default"
    try:
        timeout_s = float(args.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    return await pool.execute(sid, code, timeout_s, MAX_OUTPUT_BYTES)


async def _tool_python_session_reset(args: dict) -> dict:
    sid = args.get("session_id") or "default"
    existed = await pool.reset(sid)
    return {"session_id": sid, "existed": existed}


TOOL_HANDLERS = {
    "python_exec": _tool_python_exec,
    "python_session_reset": _tool_python_session_reset,
}


# ── MCP wire protocol ─────────────────────────────────────────────────
def _jsonrpc_result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _jsonrpc_error(id_: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


async def _handle_message(msg: dict) -> Optional[dict]:
    """Dispatch a single JSON-RPC message. Returns the response, or None
    for notifications (per JSON-RPC 2.0, no `id` field = notification)."""
    method = msg.get("method")
    params = msg.get("params") or {}
    id_ = msg.get("id")  # absent on notifications
    is_notification = "id" not in msg

    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
        return None if is_notification else _jsonrpc_result(id_, result)

    if method == "notifications/initialized" or method == "initialized":
        # Client tells us the handshake is complete. No response per spec.
        return None

    if method == "ping":
        return None if is_notification else _jsonrpc_result(id_, {})

    if method == "tools/list":
        return None if is_notification else _jsonrpc_result(id_, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _jsonrpc_error(id_, -32601, f"tool not found: {name}")
        try:
            tool_result = await handler(args)
        except ValueError as e:
            return _jsonrpc_error(id_, -32602, str(e))
        except Exception as e:  # noqa: BLE001 — surface any failure as a tool error
            log.exception("tool call %s raised", name)
            return _jsonrpc_error(id_, -32603, f"{type(e).__name__}: {e}")
        # MCP shape: tool result wraps content as a list of typed parts.
        # We return one text part with the JSON-encoded result so the
        # agent gets structured access via toolResult.content[0].text.
        return _jsonrpc_result(
            id_,
            {
                "content": [{"type": "text", "text": json.dumps(tool_result, default=str)}],
                "isError": bool(tool_result.get("error")) if isinstance(tool_result, dict) else False,
            },
        )

    if is_notification:
        return None
    return _jsonrpc_error(id_, -32601, f"method not found: {method}")


# ── HTTP endpoints ────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    """Liveness probe — unauth'd so docker's HEALTHCHECK can hit it.

    Reports kernel-pool size in the body for trivial observability;
    nothing sensitive."""
    n = sum(1 for e in pool._sessions.values() if e.kernel_id)  # type: ignore[attr-defined]
    return PlainTextResponse(f"ok kernels={n}\n")


@app.post("/mcp", dependencies=[Depends(require_token)])
async def mcp_endpoint(request: Request) -> JSONResponse:
    """MCP Streamable-HTTP entry point. Accepts a single JSON-RPC
    request or a batch (array). Returns the matching responses; for
    notifications, returns 202 with no body."""
    try:
        body = await request.body()
        if not body:
            raise ValueError("empty request body")
        payload = json.loads(body)
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content=_jsonrpc_error(None, -32700, f"parse error: {e}"),
        )

    if isinstance(payload, list):
        # Batch request — handle each, drop None (notification) responses.
        responses = await asyncio.gather(*(_handle_message(m) for m in payload))
        responses = [r for r in responses if r is not None]
        if not responses:
            return JSONResponse(status_code=202, content=None)
        return JSONResponse(content=responses)

    if isinstance(payload, dict):
        response = await _handle_message(payload)
        if response is None:
            return JSONResponse(status_code=202, content=None)
        return JSONResponse(content=response)

    return JSONResponse(
        status_code=400,
        content=_jsonrpc_error(None, -32600, "request must be an object or array"),
    )


# ── Lifecycle ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def _on_start() -> None:
    log.info(
        "python-sandbox starting; default_timeout=%.1fs max_output=%dB idle_ttl=%.0fs",
        DEFAULT_TIMEOUT_S,
        MAX_OUTPUT_BYTES,
        IDLE_TTL_S,
    )
    asyncio.create_task(_reap_loop())


async def _reap_loop() -> None:
    while True:
        try:
            await asyncio.sleep(REAP_INTERVAL_S)
            await pool.reap_idle()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            log.warning("reap loop iteration failed: %s", e)


@app.on_event("shutdown")
async def _on_stop() -> None:
    log.info("python-sandbox shutting down; reaping kernels")
    await pool.shutdown_all()
