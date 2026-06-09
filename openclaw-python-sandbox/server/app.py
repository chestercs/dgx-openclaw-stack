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
import secrets
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

# ── Whisper STT bridge (transcribe_audio tool) ────────────────────────
# The token lives ONLY in this (uvicorn) process env — kernel_pool strips
# it from the child kernel, so user-submitted Python in python_exec cannot
# read it via os.environ. This is the "dedicated MCP tool holds the token"
# isolation the operator chose over exposing the token to the sandbox.
STT_BASE_URL = os.environ.get("STT_BASE_URL", "http://openclaw-stt-whisper:8080/v1").rstrip("/")
STT_API_TOKEN = os.environ.get("STT_API_TOKEN", "").strip()
STT_MODEL = os.environ.get("STT_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
# Cap on the audio file the tool will POST to Whisper — guards against a
# user pointing the tool at a multi-GB file and OOMing the STT container.
STT_MAX_FILE_BYTES = int(os.environ.get("STT_MAX_FILE_BYTES", str(200 * 1024 * 1024)))  # 200 MB
STT_REQUEST_TIMEOUT_S = float(os.environ.get("STT_REQUEST_TIMEOUT_S", "600"))  # 10 min

# ── GitHub push bridge (git_push tool) ────────────────────────────────
# A fine-grained PAT (Contents: read+write, scoped to GITHUB_REPO ONLY) lives
# in this (uvicorn) process env. kernel_pool strips it from child kernels, and
# the tool feeds it to git via GIT_ASKPASS — never in argv (ps) or .git/config —
# so user-submitted python_exec code cannot read it. The tool can ONLY push to
# GITHUB_REPO (the agent cannot target another repo), and never force-pushes.
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()  # "owner/repo"
GIT_PUSH_TIMEOUT_S = float(os.environ.get("GIT_PUSH_TIMEOUT_S", "120"))
GIT_DEFAULT_BRANCH = os.environ.get("GIT_DEFAULT_BRANCH", "main").strip() or "main"
GIT_AUTHOR_NAME = os.environ.get("GIT_AUTHOR_NAME", "ImbulClaw").strip() or "ImbulClaw"
GIT_AUTHOR_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "bot@petyuspolisz.com").strip() or "bot@petyuspolisz.com"
# When GITHUB_REPO does not exist yet, git_push auto-creates it under the token's
# account (so a dedicated bot account only needs to hand over a token — no manual
# repo creation). This is its visibility. Default private; set false for public.
GITHUB_REPO_PRIVATE = os.environ.get("GITHUB_REPO_PRIVATE", "true").strip().lower() not in ("false", "0", "no", "off")

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
    {
        "name": "transcribe_audio",
        "description": (
            "Transcribe a local audio (or video) file to text using the "
            "self-hosted Whisper STT backend (faster-whisper turbo, "
            "autodetects EN/HU and many more). Give it a filesystem path "
            "to a file you already produced — typically with yt-dlp + "
            "ffmpeg inside python_exec (e.g. download a YouTube video, "
            "extract the audio to /workspace/audio.mp3, then call this "
            "tool with path='/workspace/audio.mp3'). The Whisper bearer "
            "token is held server-side; you never need it. Returns the "
            "full transcript text plus the detected language. Common "
            "audio/container formats work (mp3, m4a, wav, webm, mp4); "
            "Whisper decodes via its own ffmpeg."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the audio/video file to transcribe. "
                        "Use /workspace/... (bind-mounted, persistent) or /tmp/... "
                        "Must already exist — create it first via python_exec."
                    ),
                },
                "language": {
                    "type": "string",
                    "description": (
                        "Optional ISO-639-1 language hint (e.g. 'hu', 'en'). "
                        "Omit to let Whisper autodetect — recommended unless "
                        "autodetect picks the wrong language on short/noisy clips."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "git_push",
        "description": (
            "Commit (optionally) and push a LOCAL git repo to the operator's "
            "preconfigured GitHub repository. The repo, the GitHub token, and the "
            "auth are all held server-side — you never see or handle credentials. "
            "Typical flow: build a project (e.g. in /home/node/.openclaw/canvas/<name>), "
            "`git init` it via python_exec, then call git_push with repo_path=<that dir> "
            "and a commit_message, plus `repo` to name the GitHub repo for THIS project "
            "(e.g. 'max-payne-2' — created under the bot's account if it doesn't exist "
            "yet; or a full 'owner/name'). Never force-pushes. Returns the GitHub URL on "
            "success. If the server has no GITHUB_TOKEN set, it returns a clear "
            "'not configured' error — tell the user the operator must wire it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to the local git repo (must contain a .git dir; "
                        "run `git init` first). Must be under /workspace or "
                        "/home/node/.openclaw/canvas."
                    ),
                },
                "commit_message": {
                    "type": "string",
                    "description": (
                        "Optional. If given, `git add -A` + commit ALL current changes "
                        "before pushing. Omit to push already-committed work only."
                    ),
                },
                "repo": {
                    "type": "string",
                    "description": (
                        "The GitHub repo for this project. A bare name (e.g. "
                        "'max-payne-2') is created under the bot's account if missing; "
                        "or pass a full 'owner/name'. Pick a clear name per project."
                    ),
                },
                "branch": {
                    "type": "string",
                    "description": "Optional target branch. Default 'main'.",
                },
            },
            "required": ["repo_path"],
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


def _transcribe_sync(path: str, language: Optional[str]) -> dict:
    """Blocking Whisper POST — run via asyncio.to_thread. Lives here (not in
    the kernel) so the STT token never enters user-code scope."""
    import requests  # local import: only this tool needs it

    if not STT_API_TOKEN:
        raise RuntimeError(
            "STT_API_TOKEN is not set on the sandbox server — the operator must "
            "wire it in docker-compose for transcribe_audio to work."
        )
    # Path safety: must exist, be a regular file, and stay within the dirs
    # the agent legitimately writes to. The kernel and this process share the
    # container filesystem, so a path the agent wrote is readable here.
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise ValueError(f"file not found: {path}")
    # /home/node/.openclaw/canvas is the shared mount (same path the gateway
    # sees) — files there can be attached to Discord via upload-file. /workspace
    # and /tmp are sandbox-local scratch.
    allowed_roots = ("/workspace", "/tmp", "/home/node/.openclaw/canvas")
    if not any(real == r or real.startswith(r + "/") for r in allowed_roots):
        raise ValueError(
            f"path must be under {' or '.join(allowed_roots)} (got {real})"
        )
    size = os.path.getsize(real)
    if size > STT_MAX_FILE_BYTES:
        raise ValueError(
            f"file is {size} bytes; exceeds STT_MAX_FILE_BYTES={STT_MAX_FILE_BYTES}"
        )

    url = f"{STT_BASE_URL}/audio/transcriptions"
    data = {"model": STT_MODEL}
    if language:
        data["language"] = language
    with open(real, "rb") as fh:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {STT_API_TOKEN}"},
            files={"file": (os.path.basename(real), fh)},
            data=data,
            timeout=STT_REQUEST_TIMEOUT_S,
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Whisper STT returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        body = resp.json()
    except ValueError:
        # Some whisper shims return text/plain; fall back to raw body.
        return {"text": resp.text, "language": language, "model": STT_MODEL}
    return {
        "text": body.get("text", ""),
        "language": body.get("language", language),
        "model": STT_MODEL,
        "duration": body.get("duration"),
        "file_bytes": size,
    }


async def _tool_transcribe_audio(args: dict) -> dict:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("`path` is required and must be a non-empty string")
    language = args.get("language") or None
    if language is not None and not isinstance(language, str):
        raise ValueError("`language` must be a string when provided")
    return await asyncio.to_thread(_transcribe_sync, path, language)


def _git_push_sync(repo_path: str, commit_message: Optional[str], branch: Optional[str], repo: Optional[str]) -> dict:
    """Blocking git commit+push — run via asyncio.to_thread. The PAT lives only in
    this process env; we hand it to git via a transient GIT_ASKPASS helper so it
    never lands in argv (ps) or .git/config. The target repo is DYNAMIC: the agent
    passes `repo` per call (a bare name → qualified under the token account's login,
    or a full owner/name), falling back to the GITHUB_REPO env default if set."""
    import subprocess
    import tempfile
    import requests

    if not GITHUB_TOKEN:
        raise RuntimeError(
            "git_push is not configured: the operator must set GITHUB_TOKEN (a PAT, "
            "e.g. a classic token with `repo` scope) in docker-compose for the sandbox."
        )
    _api_headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    # Resolve the target repo: per-call `repo` wins; else the GITHUB_REPO env default.
    # A bare name (no "/") is qualified with the token account's login.
    target_repo = (repo or GITHUB_REPO or "").strip().strip("/")
    if not target_repo:
        raise RuntimeError(
            "no target repo: pass `repo` (e.g. 'max-payne-2' or 'owner/max-payne-2'), "
            "or set GITHUB_REPO on the sandbox."
        )
    if "/" not in target_repo:
        try:
            _u = requests.get("https://api.github.com/user", headers=_api_headers, timeout=20)
        except requests.RequestException as e:
            raise RuntimeError(f"GitHub API unreachable from the sandbox: {e}")
        if _u.status_code != 200:
            raise RuntimeError(
                f"cannot resolve the token's account (HTTP {_u.status_code}): "
                f"{_u.text[:200]}. Pass `repo` as 'owner/name' instead."
            )
        _login = (_u.json() or {}).get("login")
        if not _login:
            raise RuntimeError("GitHub /user returned no login; pass `repo` as 'owner/name'.")
        target_repo = f"{_login}/{target_repo}"
    real = os.path.realpath(repo_path)
    if not os.path.isdir(real):
        raise ValueError(f"repo path not found or not a directory: {repo_path}")
    allowed_roots = ("/workspace", "/home/node/.openclaw/canvas")
    if not any(real == r or real.startswith(r + "/") for r in allowed_roots):
        raise ValueError(f"repo path must be under {' or '.join(allowed_roots)} (got {real})")
    if not os.path.isdir(os.path.join(real, ".git")):
        raise ValueError(f"not a git repo (no .git dir): {real}. Run `git init` first via python_exec.")
    target_branch = (branch or GIT_DEFAULT_BRANCH).strip() or GIT_DEFAULT_BRANCH

    # Auto-create the GitHub repo if it doesn't exist yet (under the token's account),
    # so the agent can push a brand-new project with no manual repo creation. Needs a
    # token that can create repos (a classic PAT with `repo` scope works). Visibility
    # from GITHUB_REPO_PRIVATE (default private).
    repo_created = False
    try:
        _chk = requests.get(f"https://api.github.com/repos/{target_repo}", headers=_api_headers, timeout=20)
    except requests.RequestException as e:
        raise RuntimeError(f"GitHub API unreachable from the sandbox: {e}")
    if _chk.status_code == 404:
        _name = target_repo.split("/", 1)[-1]
        _cr = requests.post(
            "https://api.github.com/user/repos", headers=_api_headers,
            json={"name": _name, "private": GITHUB_REPO_PRIVATE, "auto_init": False}, timeout=30,
        )
        if _cr.status_code not in (200, 201):
            raise RuntimeError(
                f"repo {target_repo} does not exist and auto-create failed "
                f"(HTTP {_cr.status_code}): {_cr.text[:300]}. Check the token can create "
                f"repos and that the repo owner matches the token's account."
            )
        repo_created = True
    elif _chk.status_code != 200:
        raise RuntimeError(
            f"GitHub repo check failed (HTTP {_chk.status_code}): {_chk.text[:200]}. "
            f"Is GITHUB_TOKEN valid and does it have access to {target_repo}?"
        )

    # GIT_ASKPASS shim: git invokes it for the password and we echo the token from
    # an env var scoped to this subprocess only. Keeps the token out of argv.
    askpass = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, dir="/tmp")
    askpass.write("#!/bin/sh\nexec printf '%s' \"$GIT_PUSH_TOKEN\"\n")
    askpass.close()
    os.chmod(askpass.name, 0o700)
    env = dict(os.environ)
    env["GIT_PUSH_TOKEN"] = GITHUB_TOKEN
    env["GIT_ASKPASS"] = askpass.name
    env["GIT_TERMINAL_PROMPT"] = "0"
    remote = f"https://x-access-token@github.com/{target_repo}.git"

    def _git(*argv: str):
        return subprocess.run(
            ["git", "-C", real, *argv],
            env=env, capture_output=True, text=True,
            timeout=GIT_PUSH_TIMEOUT_S,
        )

    def _redact(s: str) -> str:
        return (s or "").replace(GITHUB_TOKEN, "***")

    try:
        # Commit identity (idempotent; ignore failures — repo may already have it).
        _git("config", "user.name", GIT_AUTHOR_NAME)
        _git("config", "user.email", GIT_AUTHOR_EMAIL)
        committed = False
        commit_out = ""
        if commit_message:
            _git("add", "-A")
            status = _git("status", "--porcelain")
            if status.stdout.strip():
                cr = _git("commit", "-m", commit_message)
                commit_out = _redact(cr.stdout + cr.stderr)
                committed = cr.returncode == 0
        pr = _git("push", remote, f"HEAD:{target_branch}")
    finally:
        try:
            os.unlink(askpass.name)
        except OSError:
            pass

    push_out = _redact(pr.stdout + pr.stderr)
    if pr.returncode != 0:
        raise RuntimeError(f"git push failed (exit {pr.returncode}): {push_out[:800]}")
    return {
        "ok": True,
        "repo": target_repo,
        "branch": target_branch,
        "committed": committed,
        "repo_created": repo_created,
        "url": f"https://github.com/{target_repo}",
        "commit_output": commit_out[-400:],
        "push_output": push_out[-600:],
    }


async def _tool_git_push(args: dict) -> dict:
    repo_path = args.get("repo_path")
    if not isinstance(repo_path, str) or not repo_path:
        raise ValueError("`repo_path` is required and must be a non-empty string")
    commit_message = args.get("commit_message") or None
    if commit_message is not None and not isinstance(commit_message, str):
        raise ValueError("`commit_message` must be a string when provided")
    branch = args.get("branch") or None
    if branch is not None and not isinstance(branch, str):
        raise ValueError("`branch` must be a string when provided")
    repo = args.get("repo") or None
    if repo is not None and not isinstance(repo, str):
        raise ValueError("`repo` must be a string when provided")
    return await asyncio.to_thread(_git_push_sync, repo_path, commit_message, branch, repo)


TOOL_HANDLERS = {
    "python_exec": _tool_python_exec,
    "python_session_reset": _tool_python_session_reset,
    "transcribe_audio": _tool_transcribe_audio,
    "git_push": _tool_git_push,
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


def _resolve_session_header(request: Request, payload: Any) -> Optional[str]:
    """Decide what to put in the response's Mcp-Session-Id header.

    MCP spec: the client may send Mcp-Session-Id on any request, and the
    server echoes it back. On a fresh `initialize` request without one,
    the server may mint a new id and the client uses it on subsequent
    requests. We don't track session state server-side (kernel sessions
    are application-level via tool args), but echoing keeps clients that
    do care about transport-level sessions happy.
    """
    incoming = request.headers.get("Mcp-Session-Id")
    if incoming:
        return incoming
    # Mint a new id only on `initialize` (with or without batching).
    is_initialize = False
    if isinstance(payload, dict):
        is_initialize = payload.get("method") == "initialize"
    elif isinstance(payload, list):
        is_initialize = any(isinstance(m, dict) and m.get("method") == "initialize" for m in payload)
    return secrets.token_urlsafe(16) if is_initialize else None


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

    session_id = _resolve_session_header(request, payload)
    response_headers = {"Mcp-Session-Id": session_id} if session_id else None

    if isinstance(payload, list):
        # Batch request — handle each, drop None (notification) responses.
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
