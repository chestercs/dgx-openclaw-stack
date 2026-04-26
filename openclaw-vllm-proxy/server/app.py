"""Sanitizing proxy in front of vLLM's gemma4 OpenAI server.

Why this exists
---------------

Upstream vLLM (`vllm-openai:gemma4-cu130`) ships a streaming tool-call parser
for Gemma 4 that has a known bug (vllm-project/vllm#38946 / #39468): the
`<|"|>` string-delimiter token Gemma uses for argument values gets buffered
incorrectly, and partial fragments of the delimiter end up *inside* the
emitted JSON. The result is calls like::

    {"action": "act", "kind": "fill", "request": {"ref": "<|\\"|"}, "text": "<|\\"|"}

…which fail validation downstream. The non-streaming `extract_tool_calls`
in the same parser (gemma4_tool_parser.py:377) does NOT have the bug — it
parses the complete tool-call string in one pass via `_parse_gemma4_args`,
which correctly handles the `<|"|>` delimiters.

So this proxy:

- Accepts streaming requests from the OpenClaw gateway (the only client we
  control on this stack).
- Always forces `stream=false` on the request it sends to vLLM.
- Receives the complete (non-streaming) response — which has the bug-free
  parse — and, if the client wanted streaming, fragments it into a single
  SSE-style chunk + `[DONE]` so the client's stream-reader unblocks.
- Also runs a defensive regex pass over `choices[].message.tool_calls[].function.arguments`
  to strip any leftover `<|"|>` literal that might have slipped through.

Out of scope: passthrough for /v1/models, /v1/embeddings, etc. — those
don't trigger the streaming-tool-call bug, so we just forward them
verbatim with httpx.

This is a temporary workaround. The day vLLM's streaming gemma4 parser
ships a fix (issue #38946), drop the `vllm-llm-proxy` service from the
compose file and point `LLM_BASE_URL` back at `vllm-llm:8004` directly.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vllm-proxy")

UPSTREAM_URL = os.environ.get("VLLM_UPSTREAM_URL", "http://vllm-llm:8004").rstrip("/")
PROXY_TIMEOUT_S = float(os.environ.get("VLLM_PROXY_TIMEOUT_S", "600"))

# `<|"|>` is the Gemma 4 string delimiter token. Strip any leftover that
# slipped past the upstream parser. Pattern intentionally tolerant of
# truncated forms (`<|"|`, `<|"`) since that's exactly what the bug emits.
PIPE_QUOTE_RE = re.compile(r"<\|\"\|>?|<\|\"")


app = FastAPI(title="openclaw-vllm-proxy", version="0.1.0")

# A single shared httpx client. Long timeout for streaming completions that
# may take 60-120s to finish on a 31B model with reasoning enabled.
_client = httpx.AsyncClient(timeout=httpx.Timeout(PROXY_TIMEOUT_S, connect=10.0))


@app.on_event("shutdown")
async def _shutdown() -> None:
    await _client.aclose()


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "upstream": UPSTREAM_URL}


def _normalize_browser_act(args: dict) -> bool:
    """Normalize the browser tool's `act` action arguments in place.

    Gemma 4 routinely emits incoherent shapes here:

    1. Flat top-level `kind`/`ref`/`text`/`fields` AND a `request: {...}`
       wrapper at the same time. When the wrapper is present but lacks
       its own `kind`, the typebox validator rejects the call with
       `request.kind: must have required properties kind`. We mirror the
       top-level `kind` into `request.kind` when the wrapper is partial.
    2. Sometimes the `request` wrapper is just noise (empty or only
       contains a duplicate of top-level fields). When the flat top-
       level shape already validates on its own, we drop the wrapper.

    Returns True if anything changed.
    """
    if not isinstance(args, dict) or args.get("action") != "act":
        return False

    flat_kind = args.get("kind")
    req = args.get("request")

    if not isinstance(req, dict):
        return False

    changed = False

    # Mirror flat kind -> request.kind when missing. The typebox schema
    # demands request.kind whenever request is present.
    if flat_kind and not req.get("kind"):
        req["kind"] = flat_kind
        changed = True

    # Mirror flat ref/text/fields/values into request when request is
    # present but doesn't carry them. Cheaper than dropping the wrapper
    # since it preserves whatever the model intended.
    for field in ("ref", "text", "fields", "values", "key", "url",
                  "selector", "timeMs", "timeoutMs", "submit", "values",
                  "startRef", "endRef", "modifiers", "doubleClick",
                  "button", "loadState", "textGone", "fn"):
        if field in args and field not in req:
            req[field] = args[field]
            changed = True

    return changed


def _sanitize_tool_calls(payload: dict) -> tuple[int, int]:
    """Two passes over each tool call's arguments JSON:

    1. Strip leftover `<|"|>` literals (gemma4 streaming-parser leak).
    2. Normalize the `browser.act` shape so the typebox validator on the
       gateway side accepts the call (handles the recurring
       Gemma 4 `request` wrapper without `request.kind` failure mode).

    Returns (n_pipe_quote_fixes, n_browser_act_normalizations).
    """
    n_pq = 0
    n_act = 0
    for choice in payload.get("choices", []) or []:
        msg = choice.get("message") or {}
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments")
            if not isinstance(args_raw, str):
                continue

            # Pass 1: pipe-quote literal cleanup.
            if PIPE_QUOTE_RE.search(args_raw):
                cleaned = PIPE_QUOTE_RE.sub('"', args_raw)
                # `<|"|>foo<|"|>` -> `"foo"` after one collapse of the
                # double-double-quotes the regex leaves behind.
                cleaned = cleaned.replace('""', '"')
                args_raw = cleaned
                n_pq += 1

            # Pass 2: browser.act shape repair. Best-effort JSON parse;
            # if the args aren't parseable JSON we leave them alone (the
            # gateway will surface its own error and the agent retries).
            try:
                parsed = json.loads(args_raw)
            except Exception:
                fn["arguments"] = args_raw
                continue

            if fn.get("name") == "browser" and _normalize_browser_act(parsed):
                args_raw = json.dumps(parsed, ensure_ascii=False)
                n_act += 1

            fn["arguments"] = args_raw
    return n_pq, n_act


def _wrap_as_stream_chunk(payload: dict) -> str:
    """Format a non-streaming chat completion as a single SSE chunk + [DONE].

    OpenAI streaming format is `data: <json>\\n\\n` per chunk, terminated by
    `data: [DONE]\\n\\n`. The OpenClaw gateway's stream reader is happy with
    a single chunk that carries the complete delta — it accumulates
    delta.content / delta.tool_calls into the final message just like a
    multi-chunk stream.
    """
    chunk: dict[str, Any] = {
        "id": payload.get("id") or f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion.chunk",
        "created": payload.get("created") or int(time.time()),
        "model": payload.get("model"),
        "choices": [],
    }
    for c in payload.get("choices", []) or []:
        msg = c.get("message") or {}
        delta: dict[str, Any] = {"role": msg.get("role") or "assistant"}
        if msg.get("content") is not None:
            delta["content"] = msg["content"]
        if msg.get("reasoning_content") is not None:
            delta["reasoning_content"] = msg["reasoning_content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = []
            for i, tc in enumerate(msg["tool_calls"]):
                fn = tc.get("function") or {}
                delta["tool_calls"].append(
                    {
                        "index": i,
                        "id": tc.get("id"),
                        "type": tc.get("type", "function"),
                        "function": {
                            "name": fn.get("name"),
                            "arguments": fn.get("arguments", ""),
                        },
                    }
                )
        chunk["choices"].append(
            {
                "index": c.get("index", 0),
                "delta": delta,
                "finish_reason": None,
                "logprobs": c.get("logprobs"),
            }
        )

    finish_chunk = {
        "id": chunk["id"],
        "object": "chat.completion.chunk",
        "created": chunk["created"],
        "model": chunk["model"],
        "choices": [
            {
                "index": c.get("index", 0),
                "delta": {},
                "finish_reason": c.get("finish_reason", "stop"),
                "logprobs": None,
            }
            for c in payload.get("choices", []) or []
        ],
        "usage": payload.get("usage"),
    }

    return f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(finish_chunk)}\n\ndata: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(req: Request) -> Any:
    body_bytes = await req.body()
    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    client_wants_stream = bool(body.get("stream"))
    # Force non-streaming upstream — that's the gemma4 parser's bug-free
    # path. We'll re-fragment the response into SSE if the client asked.
    body["stream"] = False
    body.pop("stream_options", None)

    headers = dict(req.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers["content-type"] = "application/json"

    upstream = await _client.post(
        f"{UPSTREAM_URL}/v1/chat/completions",
        content=json.dumps(body),
        headers=headers,
    )

    if upstream.status_code != 200:
        return JSONResponse(
            content=upstream.json() if upstream.headers.get("content-type", "").startswith("application/json") else {"error": upstream.text},
            status_code=upstream.status_code,
        )

    payload = upstream.json()
    n_pq, n_act = _sanitize_tool_calls(payload)
    if n_pq or n_act:
        log.info(
            "sanitize: pipe_quote_fixes=%d browser_act_normalizations=%d",
            n_pq,
            n_act,
        )

    if client_wants_stream:
        sse = _wrap_as_stream_chunk(payload)

        async def gen() -> AsyncIterator[bytes]:
            yield sse.encode("utf-8")

        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse(payload)


# ----------------------------------------------------------------------
# Passthrough for everything else — models list, completions, embeddings.
# These don't trigger the gemma4 streaming bug because they don't emit
# tool calls. Forward verbatim, preserve streaming if requested.
# ----------------------------------------------------------------------
@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def passthrough(full_path: str, req: Request) -> Any:
    body = await req.body()
    headers = dict(req.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    upstream_url = f"{UPSTREAM_URL}/{full_path}"
    if req.url.query:
        upstream_url = f"{upstream_url}?{req.url.query}"

    upstream = await _client.request(
        req.method,
        upstream_url,
        content=body if body else None,
        headers=headers,
    )

    media_type = upstream.headers.get("content-type", "application/octet-stream")
    return StreamingResponse(
        iter([upstream.content]),
        status_code=upstream.status_code,
        media_type=media_type,
    )
