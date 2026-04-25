"""FastAPI management surface for openclaw-browser.

Bound to BROWSER_API_PORT (default 9220) — a separate port from the
Chromium remote-debugging ports (9222 default + 9223+ named). OpenClaw's
gateway never talks to this port; it talks directly to Chromium via CDP.
This API is for the OPERATOR — the bootstrap-browser-login.sh helper, the
rotate-secrets script, and any direct curl-driven session management.

Routes:
  GET  /healthz                         — no auth, container healthcheck
  GET  /v1/sessions                     — Bearer, list profile state
  POST /v1/sessions/{name}              — Bearer, create + start headless
  DELETE /v1/sessions/{name}            — Bearer, stop + wipe user-data-dir
  POST /v1/sessions/{name}/restart      — Bearer, kill + headless relaunch
  POST /v1/sessions/{name}/login-helper        — Bearer, start noVNC bridge
  POST /v1/sessions/{name}/login-helper/finish — Bearer, flush + relaunch
  POST /v1/sessions/{name}/login-helper/cancel — Bearer, abort no-flush
  POST /v1/extract                      — Bearer, HTML→markdown trafilatura

CDP traffic (`/json/version`, `/json/list`, `WS /devtools/...`) is served
by Chromium directly on its own ports; we do NOT proxy. Loopback host bind
+ docker bridge isolation are the only auth layers on CDP — the
?token=<...> query string OpenClaw appends to its cdpUrl is sent but not
validated server-side. Acceptable for a single-operator host (where the
plan ships); if you expose any CDP port on the LAN, document the risk and
front it with a reverse proxy that does header-based bearer auth before
forwarding.
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from blocklist import Blocklist
from extract import extract_markdown
from ratelimit import RateLimiter
from supervise import (
    DEFAULT_PROFILE_NAME,
    LoginHelper,
    Supervisor,
    VncBridge,
    parse_profile_names,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("browser.app")


API_TOKEN = os.environ.get("BROWSER_API_TOKEN", "").strip()
if not API_TOKEN:
    raise RuntimeError("BROWSER_API_TOKEN env var is required (no anonymous access allowed)")

VNC_PASSWORD = os.environ.get("BROWSER_VNC_PASSWORD", "").strip()
if not VNC_PASSWORD:
    raise RuntimeError(
        "BROWSER_VNC_PASSWORD env var is required (always-on noVNC bridge has no anonymous mode). "
        "Run ./bootstrap.sh to generate one, or rotate-secrets.sh BROWSER_VNC_PASSWORD."
    )
VNC_PORT = int(os.environ.get("BROWSER_VNC_PORT", "5901"))


bearer = HTTPBearer(auto_error=False)


def require_token(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
    if creds is None or not secrets.compare_digest(creds.credentials, API_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing bearer token")


PROFILE_NAME_RE = r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}"


def _validate_profile_name(name: str) -> None:
    import re
    if not re.fullmatch(PROFILE_NAME_RE, name):
        raise HTTPException(
            status_code=400,
            detail=f"profile name must match {PROFILE_NAME_RE} (got {name!r})",
        )


supervisor = Supervisor()
vnc_bridge = VncBridge(VNC_PASSWORD, vnc_port=VNC_PORT)
login_helper = LoginHelper(supervisor, vnc_bridge)
blocklist = Blocklist()
limiter = RateLimiter()


app = FastAPI(title="openclaw-browser", version="0.1.0")


@app.on_event("startup")
def on_startup() -> None:
    """Bring up the always-on VNC bridge, then launch the default profile
    + every named profile from BROWSER_PROFILE_NAMES. Failure to launch
    one profile does NOT stop the others — agents that don't use a broken
    profile keep working, and /healthz surfaces which one fell over.

    The VNC bridge starting before any Chromium is intentional: a profile
    bumped into headful mode (login-helper or the future
    /v1/sessions/<n>/headful endpoint) needs the Xvfb display ready
    before Chromium attaches."""
    try:
        vnc_bridge.start()
    except Exception as exc:
        log.exception("vnc-bridge failed to start: %s", exc)

    log.info("startup: launching default + named profiles")
    try:
        supervisor.start_profile(DEFAULT_PROFILE_NAME)
    except Exception as exc:
        log.exception("default profile failed to launch: %s", exc)

    for name in parse_profile_names(os.environ.get("BROWSER_PROFILE_NAMES")):
        try:
            supervisor.start_profile(name)
        except Exception as exc:
            log.exception("profile %s failed to launch: %s", name, exc)


@app.on_event("shutdown")
def on_shutdown() -> None:
    log.info("shutdown: stopping all Chromium processes")
    if login_helper.is_active():
        try:
            login_helper.cancel()
        except Exception:
            pass
    supervisor.stop_all()
    try:
        vnc_bridge.stop()
    except Exception:
        log.exception("vnc-bridge shutdown failed")


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "profiles": supervisor.list_profiles(),
        "vnc_bridge_running": vnc_bridge.is_running(),
        "vnc_port": vnc_bridge.vnc_port,
        "login_helper_active": login_helper.is_active(),
        "login_helper_profile": login_helper.active_profile(),
        "uptime_s": time.time() - START_TIME,
    }


@app.get("/v1/vnc", dependencies=[Depends(require_token)])
def vnc_info() -> dict:
    """Return the always-on noVNC URL.

    The URL embeds the persistent password as `?password=<...>` for the
    noVNC client's auto-fill. Outside an active login-helper session the
    screen is blank (no headful Chromium attached); start one with
    POST /v1/sessions/{name}/login-helper to peek at a profile."""
    return {
        "vnc_url": vnc_bridge.vnc_url(),
        "vnc_port": vnc_bridge.vnc_port,
        "running": vnc_bridge.is_running(),
        "headful_profile": login_helper.active_profile(),
    }


START_TIME = time.time()


# ----------------------------------------------------------------------
# Session management
# ----------------------------------------------------------------------
class CreateSessionRequest(BaseModel):
    pass


@app.get("/v1/sessions", dependencies=[Depends(require_token)])
def list_sessions() -> dict:
    return {"profiles": supervisor.list_profiles()}


@app.post("/v1/sessions/{name}", dependencies=[Depends(require_token)])
def create_session(name: str) -> dict:
    _validate_profile_name(name)
    if name == DEFAULT_PROFILE_NAME:
        raise HTTPException(status_code=400, detail="default profile is auto-managed")
    profile = supervisor.start_profile(name)
    return {
        "name": profile.name,
        "port": profile.port,
        "user_data_dir": str(profile.user_data_dir),
        "running": profile.is_running(),
    }


@app.delete("/v1/sessions/{name}", dependencies=[Depends(require_token)])
def delete_session(name: str) -> dict:
    _validate_profile_name(name)
    if name == DEFAULT_PROFILE_NAME:
        raise HTTPException(status_code=400, detail="default profile cannot be deleted")
    deleted = supervisor.delete_profile(name)
    return {"name": name, "deleted": deleted}


@app.post("/v1/sessions/{name}/restart", dependencies=[Depends(require_token)])
def restart_session(name: str) -> dict:
    _validate_profile_name(name)
    profile = supervisor.restart_profile(name)
    return {
        "name": profile.name,
        "port": profile.port,
        "running": profile.is_running(),
    }


# ----------------------------------------------------------------------
# Login helper — toggle a profile's Chromium between headless and headful
# on the always-on VNC bridge. No request body fields needed: the bridge
# password lives in BROWSER_VNC_PASSWORD and the port is fixed at startup.
# An empty body is accepted so curl-based callers can POST without
# crafting a Content-Type header.
# ----------------------------------------------------------------------
@app.post("/v1/sessions/{name}/login-helper", dependencies=[Depends(require_token)])
def start_login_helper(name: str) -> dict:
    _validate_profile_name(name)
    if name == DEFAULT_PROFILE_NAME:
        raise HTTPException(status_code=400, detail="cannot run login-helper on the default (anonymous) profile")
    try:
        return login_helper.start(name)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.post("/v1/sessions/{name}/login-helper/finish", dependencies=[Depends(require_token)])
def finish_login_helper(name: str) -> dict:
    _validate_profile_name(name)
    active = login_helper.active_profile()
    if active != name:
        raise HTTPException(
            status_code=409,
            detail=f"active login helper is {active!r}, not {name!r}",
        )
    return login_helper.finish()


@app.post("/v1/sessions/{name}/login-helper/cancel", dependencies=[Depends(require_token)])
def cancel_login_helper(name: str) -> dict:
    _validate_profile_name(name)
    return login_helper.cancel()


# ----------------------------------------------------------------------
# Markdown extraction
# ----------------------------------------------------------------------
class ExtractRequest(BaseModel):
    html: str = Field(..., min_length=1)
    url: Optional[str] = None
    favor_recall: bool = False


@app.post("/v1/extract", dependencies=[Depends(require_token)])
def extract(req: ExtractRequest) -> dict:
    # Domain blocklist guard — even though the agent is just asking us to
    # parse HTML it already has, refuse to acknowledge a URL it shouldn't
    # have hit. Keeps the "we don't process this" boundary consistent.
    if req.url and blocklist.is_blocked(req.url):
        raise HTTPException(status_code=403, detail=blocklist.reason(req.url) or "blocked")

    result = extract_markdown(req.html, url=req.url, favor_recall=req.favor_recall)
    return {
        "markdown": result.markdown,
        "title": result.title,
        "url": result.url,
        "word_count": result.word_count,
        "extractor": result.extractor,
    }


# ----------------------------------------------------------------------
# Blocklist guard — exposed so the operator can sanity-check what's blocked.
# ----------------------------------------------------------------------
class BlocklistCheckRequest(BaseModel):
    url: str


@app.post("/v1/blocklist/check", dependencies=[Depends(require_token)])
def blocklist_check(req: BlocklistCheckRequest) -> dict:
    blocked = blocklist.is_blocked(req.url)
    return {
        "url": req.url,
        "blocked": blocked,
        "reason": blocklist.reason(req.url) if blocked else None,
    }


@app.post("/v1/blocklist/reload", dependencies=[Depends(require_token)])
def blocklist_reload() -> dict:
    blocklist.reload()
    return {"status": "reloaded"}
