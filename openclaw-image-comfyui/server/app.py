"""Image-generation MCP bridge — MCP Streamable-HTTP server.

Speaks the Model Context Protocol wire format (JSON-RPC 2.0 over HTTP)
on POST /mcp, exposing three tools that the OpenClaw agent can call:

  - comfyui_image__generate(prompt, workflow="flux-schnell", width, height,
                            steps, cfg, seed, negative, checkpoint, sampler,
                            scheduler, batch_size, timeout_s)
        Submit a workflow to the operator's ComfyUI install, poll until the
        image renders, and return one or more base64-encoded PNGs.
  - comfyui_image__list_workflows()
        Return the workflows shipped under workflows/ with their tunable
        parameter list — saves the agent a round-trip when it needs to
        pick one.
  - comfyui_image__cancel(prompt_id)
        Best-effort cancel of an in-flight prompt by id.

Auth: Authorization: Bearer ${IMAGE_GEN_API_TOKEN} on every POST. The
/healthz endpoint is intentionally unauth'd so docker healthchecks don't
need the token.

Why we hand-roll the MCP wire instead of pulling the `mcp` SDK: same
rationale as the python-sandbox sibling — one Streamable-HTTP POST
handler plus a JSON-RPC dispatch is ~250 LOC; the SDK has churned
across 1.x; freezing the protocol shape inside this file is more honest
about the contract OpenClaw will actually see.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import secrets
import time
import uuid
from io import BytesIO
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image

from comfy_client import (
    ComfyClient,
    ComfyUIError,
    ComfyUIRestartedError,
    ComfyUITimeout,
    extract_image_outputs,
)
from workflow_loader import WorkflowError, WorkflowLoader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("image-bridge")


# ── Config ────────────────────────────────────────────────────────────
TOKEN = os.environ.get("IMAGE_GEN_API_TOKEN", "").strip()
if not TOKEN:
    # Fail fast: the bridge talks to a no-auth ComfyUI on the host gateway,
    # so a token-less /mcp would let any container on the bridge network
    # drive arbitrary generation. The opt-in posture is token-gated.
    raise RuntimeError("IMAGE_GEN_API_TOKEN must be set (bridge refuses to start without auth).")

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:13036").rstrip("/")
DEFAULT_TIMEOUT_S = float(os.environ.get("IMAGE_GEN_TIMEOUT_S", "600"))
MAX_OUTPUT_BYTES = int(os.environ.get("IMAGE_GEN_MAX_OUTPUT_BYTES", str(50 * 1024 * 1024)))
MAX_CONCURRENCY = int(os.environ.get("IMAGE_GEN_MAX_CONCURRENCY", "1"))
POLL_INTERVAL_S = float(os.environ.get("IMAGE_GEN_POLL_INTERVAL_S", "0.5"))
POLL_BACKOFF_MAX_S = float(os.environ.get("IMAGE_GEN_POLL_BACKOFF_MAX_S", "2.0"))

WORKFLOWS_DIR = os.environ.get("IMAGE_GEN_WORKFLOWS_DIR", "/app/workflows")

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "openclaw-image-comfyui", "version": "0.1.0"}


TOOLS = [
    {
        "name": "comfyui_image__generate",
        "description": (
            "Generate one or more images via the operator's ComfyUI install. "
            "Returns base64-encoded PNG bytes the agent can render or save. "
            "`workflow` selects a JSON template under workflows/; the bridge "
            "binds your params (prompt, dimensions, sampler, seed, ...) to "
            "the template's tunable nodes and submits to ComfyUI's queue. "
            "Pass `checkpoint` to override the model name (must exist under "
            "ComfyUI's basedir/models/checkpoints/). Use comfyui_image__list_"
            "workflows first if you don't know which workflow to pick."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt":     {"type": "string", "description": "Positive prompt — what the image should depict."},
                "workflow":   {"type": "string", "description": "Workflow template name (default 'flux-schnell').", "default": "flux-schnell"},
                "width":      {"type": "integer", "description": "Output width in pixels.", "default": 1024},
                "height":     {"type": "integer", "description": "Output height in pixels.", "default": 1024},
                "steps":      {"type": "integer", "description": "Sampler steps (workflow default if omitted)."},
                "cfg":        {"type": "number",  "description": "Classifier-free guidance scale (workflow default if omitted)."},
                "seed":       {"type": "integer", "description": "RNG seed. -1 (default) mints a random one."},
                "negative":   {"type": "string",  "description": "Negative prompt (skipped silently if the workflow has no negative slot)."},
                "checkpoint": {"type": "string",  "description": "Checkpoint filename inside basedir/models/checkpoints/. Required if the workflow has REPLACE_ME placeholder."},
                "sampler":    {"type": "string",  "description": "KSampler `sampler_name` override."},
                "scheduler":  {"type": "string",  "description": "KSampler `scheduler` override."},
                "batch_size": {"type": "integer", "description": "Number of images per call (default 1)."},
                "timeout_s":  {"type": "number",  "description": f"Max wall-clock seconds to wait for the render. Default {DEFAULT_TIMEOUT_S:.0f}."},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "comfyui_image__list_workflows",
        "description": (
            "List the workflow templates the bridge ships, with their "
            "tunable parameters and defaults. Use this to discover which "
            "workflow fits the request before calling generate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "comfyui_image__cancel",
        "description": (
            "Best-effort cancel of an in-flight prompt by id. Useful if a "
            "long render needs to be aborted (e.g. wrong prompt). Returns "
            "the HTTP status of the queue-delete + interrupt attempts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt_id": {"type": "string", "description": "ComfyUI prompt id returned by an earlier generate call."},
            },
            "required": ["prompt_id"],
            "additionalProperties": False,
        },
    },
]


# ── App + module-level state ──────────────────────────────────────────
loader = WorkflowLoader(WORKFLOWS_DIR)
loader.load_all()

# Single-flight lock by default (MAX_CONCURRENCY=1). 0 = pass-through and
# let ComfyUI's internal queue handle ordering. The semaphore is built
# once at startup; changing MAX_CONCURRENCY needs a service restart.
_gen_sem: Optional[asyncio.Semaphore] = (
    asyncio.Semaphore(MAX_CONCURRENCY) if MAX_CONCURRENCY > 0 else None
)

comfy = ComfyClient(
    base_url=COMFYUI_URL,
    poll_interval_s=POLL_INTERVAL_S,
    poll_backoff_max_s=POLL_BACKOFF_MAX_S,
)

app = FastAPI(title="openclaw-image-comfyui", version="0.1.0")
bearer = HTTPBearer(auto_error=False)


def require_token(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")


# ── Tool implementations ──────────────────────────────────────────────
async def _tool_list_workflows(_args: dict) -> dict:
    return {"workflows": loader.list()}


async def _tool_cancel(args: dict) -> dict:
    prompt_id = args.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise ValueError("`prompt_id` is required and must be a non-empty string")
    return await comfy.cancel(prompt_id)


async def _tool_generate(args: dict) -> dict:
    workflow_name = args.get("workflow") or "flux-schnell"
    try:
        workflow = loader.get(workflow_name)
    except WorkflowError as e:
        raise ValueError(str(e)) from e

    # Seed handling: -1 (or unset) → mint random; else honor caller's value.
    raw_seed = args.get("seed", -1)
    try:
        seed_val = int(raw_seed)
    except (TypeError, ValueError):
        seed_val = -1
    if seed_val < 0:
        seed_val = random.randint(0, 2**32 - 1)

    bind_args = {
        "prompt":     args.get("prompt"),
        "negative":   args.get("negative") or workflow.defaults.get("negative"),
        "checkpoint": args.get("checkpoint") or workflow.defaults.get("checkpoint"),
        "width":      args.get("width") or workflow.defaults.get("width") or 1024,
        "height":     args.get("height") or workflow.defaults.get("height") or 1024,
        "batch_size": args.get("batch_size") or workflow.defaults.get("batch_size") or 1,
        "seed":       seed_val,
        "steps":      args.get("steps") or workflow.defaults.get("steps"),
        "cfg":        args.get("cfg") or workflow.defaults.get("cfg"),
        "sampler":    args.get("sampler") or workflow.defaults.get("sampler"),
        "scheduler":  args.get("scheduler") or workflow.defaults.get("scheduler"),
    }
    try:
        prompt_dict = workflow.bind(bind_args)
    except WorkflowError as e:
        raise ValueError(str(e)) from e

    try:
        timeout_s = float(args.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S

    client_id = uuid.uuid4().hex
    started = time.monotonic()

    async def _run() -> dict:
        prompt_id = await comfy.submit_prompt(prompt_dict, client_id)
        try:
            entry = await comfy.wait_for_completion(prompt_id, timeout_s)
        except ComfyUITimeout:
            await comfy.cancel(prompt_id)
            raise
        outputs = extract_image_outputs(entry)
        if not outputs:
            raise ComfyUIError(
                f"prompt {prompt_id} completed but produced no images "
                "(check the workflow has a SaveImage node)"
            )

        images: list[dict] = []
        total_bytes = 0
        for out in outputs:
            data = await comfy.fetch_image(
                out["filename"], image_type=out["type"], subfolder=out["subfolder"]
            )
            total_bytes += len(data)
            if total_bytes > MAX_OUTPUT_BYTES:
                raise ComfyUIError(
                    f"image batch exceeded IMAGE_GEN_MAX_OUTPUT_BYTES "
                    f"({MAX_OUTPUT_BYTES} B) — increase the cap or reduce batch_size"
                )
            try:
                with Image.open(BytesIO(data)) as im:
                    width, height, fmt = im.width, im.height, (im.format or "PNG").lower()
            except Exception:
                # Don't fail generation on a metadata read; fall back to defaults.
                width, height, fmt = 0, 0, "png"
            images.append(
                {
                    "format": fmt,
                    "base64": base64.b64encode(data).decode("ascii"),
                    "filename": out["filename"],
                    "subfolder": out["subfolder"],
                    "type": out["type"],
                    "node_id": out["node_id"],
                    "width": width,
                    "height": height,
                    "byte_size": len(data),
                }
            )

        return {
            "prompt_id": prompt_id,
            "workflow_used": workflow_name,
            "seed_used": seed_val,
            "elapsed_s": round(time.monotonic() - started, 3),
            "images": images,
        }

    if _gen_sem is None:
        return await _run()
    async with _gen_sem:
        return await _run()


TOOL_HANDLERS = {
    "comfyui_image__generate":       _tool_generate,
    "comfyui_image__list_workflows": _tool_list_workflows,
    "comfyui_image__cancel":         _tool_cancel,
}


# ── MCP wire protocol (mirrors openclaw-python-sandbox/server/app.py) ─
def _jsonrpc_result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _jsonrpc_error(id_: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


async def _handle_message(msg: dict) -> Optional[dict]:
    method = msg.get("method")
    params = msg.get("params") or {}
    id_ = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
        return None if is_notification else _jsonrpc_result(id_, result)

    if method in ("notifications/initialized", "initialized"):
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
            is_error = False
        except ValueError as e:
            return _jsonrpc_error(id_, -32602, str(e))
        except ComfyUITimeout as e:
            tool_result = {"error": "timeout", "message": str(e)}
            is_error = True
        except ComfyUIRestartedError as e:
            tool_result = {"error": "comfyui_restarted", "message": str(e)}
            is_error = True
        except ComfyUIError as e:
            tool_result = {"error": "comfyui_error", "message": str(e)}
            is_error = True
        except Exception as e:  # noqa: BLE001
            log.exception("tool call %s raised", name)
            return _jsonrpc_error(id_, -32603, f"{type(e).__name__}: {e}")
        return _jsonrpc_result(
            id_,
            {
                "content": [{"type": "text", "text": json.dumps(tool_result, default=str)}],
                "isError": is_error,
            },
        )

    if is_notification:
        return None
    return _jsonrpc_error(id_, -32601, f"method not found: {method}")


# ── HTTP endpoints ────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    """Liveness probe — unauth'd so docker's HEALTHCHECK can hit it."""
    return PlainTextResponse(f"ok workflows={len(loader.list())}\n")


def _resolve_session_header(request: Request, payload: Any) -> Optional[str]:
    """Echo or mint Mcp-Session-Id (mirrors python-sandbox)."""
    incoming = request.headers.get("Mcp-Session-Id")
    if incoming:
        return incoming
    is_initialize = False
    if isinstance(payload, dict):
        is_initialize = payload.get("method") == "initialize"
    elif isinstance(payload, list):
        is_initialize = any(isinstance(m, dict) and m.get("method") == "initialize" for m in payload)
    return secrets.token_urlsafe(16) if is_initialize else None


@app.post("/mcp", dependencies=[Depends(require_token)])
async def mcp_endpoint(request: Request) -> JSONResponse:
    try:
        body = await request.body()
        if not body:
            raise ValueError("empty request body")
        payload = json.loads(body)
    except Exception as e:
        return JSONResponse(
            status_code=400, content=_jsonrpc_error(None, -32700, f"parse error: {e}")
        )

    session_id = _resolve_session_header(request, payload)
    response_headers = {"Mcp-Session-Id": session_id} if session_id else None

    if isinstance(payload, list):
        responses = await asyncio.gather(*(_handle_message(m) for m in payload))
        responses = [r for r in responses if r is not None]
        if not responses:
            return JSONResponse(status_code=202, content=None, headers=response_headers)
        return JSONResponse(content=responses, headers=response_headers)

    if isinstance(payload, dict):
        response = await _handle_message(payload)
        if response is None:
            return JSONResponse(status_code=202, content=None, headers=response_headers)
        return JSONResponse(content=response, headers=response_headers)

    return JSONResponse(
        status_code=400,
        content=_jsonrpc_error(None, -32600, "request must be an object or array"),
    )


# ── Lifecycle ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def _on_start() -> None:
    log.info(
        "image-bridge starting; comfyui_url=%s default_timeout=%.0fs max_concurrency=%d",
        COMFYUI_URL, DEFAULT_TIMEOUT_S, MAX_CONCURRENCY,
    )


@app.on_event("shutdown")
async def _on_stop() -> None:
    log.info("image-bridge shutting down; closing comfyui client")
    await comfy.aclose()
