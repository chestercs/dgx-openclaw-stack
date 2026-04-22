"""OpenAI-compatible /v1/audio/speech router for the dgx-openclaw-stack.

A thin FastAPI proxy in front of one or more language-specific TTS backends.
The English backend (`openclaw-tts-en`, Kokoro 82M) is mandatory; the optional
Hungarian backend (`F5HUN_URL` + `F5HUN_API_TOKEN`) is wired in only when both
env vars are present, so the public stack ships EN-only out of the box and
HU lights up the moment a user brings their own f5hun-style F5-TTS service.

Why a router and not direct provider wiring: OpenClaw's
`messages.tts.providers.openai.baseUrl` accepts exactly one URL. Multiple
backends require a fronting service. This router is ~150 LOC, no GPU, and
mirrors the OpenAI shape so it slots into the gateway via the sanctioned
baseUrl override (closed OpenClaw issues #13907, #29224).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from typing import Literal, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tts-router")

ROUTER_TOKEN = os.environ.get("ROUTER_API_KEY", "").strip()
EN_TOKEN = os.environ.get("EN_API_TOKEN", "").strip()
F5HUN_TOKEN = os.environ.get("F5HUN_API_TOKEN", "").strip()

if not ROUTER_TOKEN or not EN_TOKEN:
    raise RuntimeError("ROUTER_API_KEY and EN_API_TOKEN are required (HU is optional)")

EN_URL = os.environ.get("EN_URL", "http://openclaw-tts-en:8080/v1/audio/speech")
F5HUN_URL = os.environ.get("F5HUN_URL", "").strip()
DEFAULT_VOICE = os.environ.get("ROUTER_DEFAULT_VOICE", "af_heart")
TIMEOUT_S = float(os.environ.get("ROUTER_TIMEOUT", "60"))

# A Hungarian backend is enabled only when both URL and token are present.
# Without one the router is EN-only — HU voice ids return 404 and the
# Hungarian-diacritic autodetect path is a no-op.
F5HUN_ENABLED = bool(F5HUN_TOKEN and F5HUN_URL)

# Voice id -> backend routing. The router's voice id is the public name used
# everywhere (OpenClaw config, agent prompts). The "voice" field is what we
# forward to the backend (alias-resolved).
VOICES: dict[str, dict[str, str]] = {
    # English US (Kokoro 82M, A/B-grade voices baked into the EN image)
    "af_heart":    {"backend": "en", "voice": "af_heart"},
    "af_bella":    {"backend": "en", "voice": "af_bella"},
    "af_nicole":   {"backend": "en", "voice": "af_nicole"},
    "af_aoede":    {"backend": "en", "voice": "af_aoede"},
    "af_kore":     {"backend": "en", "voice": "af_kore"},
    "af_sarah":    {"backend": "en", "voice": "af_sarah"},
    "am_michael":  {"backend": "en", "voice": "am_michael"},
    "am_fenrir":   {"backend": "en", "voice": "am_fenrir"},
    "am_puck":     {"backend": "en", "voice": "am_puck"},
    # English UK (Kokoro)
    "bf_emma":     {"backend": "en", "voice": "bf_emma"},
    # OpenAI-standard voice names — newer gpt-4o-mini-tts catalog. The
    # OpenClaw gateway picks one of these (observed: 'coral') when the agent
    # doesn't override. Mapped to the closest-matching Kokoro voice so the
    # web/voice surface gets audio instead of a 404.
    "alloy":       {"backend": "en", "voice": "af_heart"},
    "ash":         {"backend": "en", "voice": "am_puck"},
    "ballad":      {"backend": "en", "voice": "am_fenrir"},
    "coral":       {"backend": "en", "voice": "af_bella"},
    "echo":        {"backend": "en", "voice": "am_michael"},
    "fable":       {"backend": "en", "voice": "bf_emma"},
    "onyx":        {"backend": "en", "voice": "am_fenrir"},
    "nova":        {"backend": "en", "voice": "af_bella"},
    "sage":        {"backend": "en", "voice": "af_nicole"},
    "shimmer":     {"backend": "en", "voice": "af_nicole"},
    "verse":       {"backend": "en", "voice": "am_michael"},
}

if F5HUN_ENABLED:
    # F5-TTS Hungarian fine-tunes (sarpba/Maxdorger29/mp3pintyo) are
    # CC-BY-NC-licensed and not shipped in this repo. Bring your own:
    # publish an OpenAI-compatible /v1/audio/speech service on F5HUN_URL
    # with an F5HUN_API_TOKEN and the route below activates.
    VOICES["default_hu"] = {"backend": "f5hun", "voice": "default_hu"}
    VOICES["hu_diana"]   = {"backend": "f5hun", "voice": "default_hu"}

# OpenAI's "neutral" default voice catalog. When OpenClaw's openai provider
# fires without a voice override, the gateway picks one of these (observed:
# 'coral'). They all map to English Kokoro voices in VOICES, which mangles
# Hungarian phonetics. If a Hungarian backend is wired AND we detect HU
# diacritics in the input, we silently reroute to the HU backend so the
# agent doesn't have to know about voice IDs to get correct pronunciation.
OPENAI_DEFAULT_VOICES = frozenset({
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "onyx", "nova", "sage", "shimmer", "verse",
})
HU_DIACRITIC_RX = re.compile(r"[áéíóöőúüűÁÉÍÓÖŐÚÜŰ]")
HU_AUTOROUTE_VOICE = os.environ.get("HU_AUTOROUTE_VOICE", "default_hu") if F5HUN_ENABLED else None

BACKENDS: dict[str, tuple[str, str]] = {"en": (EN_URL, EN_TOKEN)}
if F5HUN_ENABLED:
    BACKENDS["f5hun"] = (F5HUN_URL, F5HUN_TOKEN)

log.info("router started: backends=%s default_voice=%s hu_autoroute=%s",
         sorted(BACKENDS.keys()), DEFAULT_VOICE, HU_AUTOROUTE_VOICE or "(disabled)")

bearer = HTTPBearer(auto_error=False)


def require_token(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
    if creds is None or not secrets.compare_digest(creds.credentials, ROUTER_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing bearer token")


class SpeechRequest(BaseModel):
    model: str = Field(default="openclaw-tts", description="ignored, present for OpenAI compat")
    input: str = Field(..., min_length=1, max_length=4000)
    voice: str = Field(default=DEFAULT_VOICE)
    response_format: Literal["wav", "flac", "ogg", "pcm", "mp3", "opus", "aac"] = "wav"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


# ffmpeg transcode specs keyed by the OpenAI response_format name the client asks for.
#   backend_fmt  = what we request from the Kokoro/F5 backend (native no-transcode)
#   ff_args      = ffmpeg output args; None means no transcode (pass backend bytes through)
#   content_type = HTTP response Content-Type the browser/SDK expects
_FMT_SPECS: dict[str, dict] = {
    "wav":  {"backend_fmt": "wav",  "ff_args": None,
             "content_type": "audio/wav"},
    "pcm":  {"backend_fmt": "wav",  "ff_args": None,
             "content_type": "audio/wav"},
    "flac": {"backend_fmt": "flac", "ff_args": None,
             "content_type": "audio/flac"},
    "ogg":  {"backend_fmt": "ogg",  "ff_args": None,
             "content_type": "audio/ogg"},
    "mp3":  {"backend_fmt": "wav",
             "ff_args": ["-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"],
             "content_type": "audio/mpeg"},
    "opus": {"backend_fmt": "wav",
             "ff_args": ["-c:a", "libopus", "-b:a", "96k", "-f", "ogg"],
             "content_type": "audio/ogg"},
    "aac":  {"backend_fmt": "wav",
             "ff_args": ["-c:a", "aac", "-b:a", "128k", "-f", "adts"],
             "content_type": "audio/aac"},
}


async def ffmpeg_transcode(wav_bytes: bytes, ff_args: list[str]) -> bytes:
    """Pipe wav in on stdin, read transcoded bytes from stdout."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", *ff_args, "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(input=wav_bytes)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"ffmpeg transcode failed: {err.decode(errors='replace')}")
    return out


app = FastAPI(title="OpenClaw TTS Router", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict:
    """Probe wired backends; reports per-backend status without auth so the docker healthcheck stays simple."""
    out: dict = {"status": "ok", "f5hun_enabled": F5HUN_ENABLED, "backends": {}}
    async with httpx.AsyncClient(timeout=5) as client:
        for name, (url, _) in BACKENDS.items():
            health_url = url.replace("/v1/audio/speech", "/healthz")
            try:
                r = await client.get(health_url)
                out["backends"][name] = {"http": r.status_code, "url": health_url}
            except Exception as e:
                out["backends"][name] = {"error": str(e), "url": health_url}
                out["status"] = "degraded"
    return out


@app.get("/v1/audio/voices", dependencies=[Depends(require_token)])
@app.get("/v1/voices", dependencies=[Depends(require_token)])
def list_voices() -> dict:
    return {
        "voices": [{"id": vid, "backend": v["backend"]} for vid, v in VOICES.items()],
        "default": DEFAULT_VOICE,
        "f5hun_enabled": F5HUN_ENABLED,
    }


@app.post("/v1/audio/speech", dependencies=[Depends(require_token)])
async def speech(req: SpeechRequest) -> Response:
    log.info("incoming speech request: voice=%r model=%r format=%r speed=%r chars=%d",
             req.voice, req.model, req.response_format, req.speed, len(req.input))
    if HU_AUTOROUTE_VOICE and req.voice in OPENAI_DEFAULT_VOICES and HU_DIACRITIC_RX.search(req.input):
        log.info("HU autodetect: voice=%s + Hungarian diacritics -> rerouting to %s",
                 req.voice, HU_AUTOROUTE_VOICE)
        req.voice = HU_AUTOROUTE_VOICE
    if req.voice not in VOICES:
        # Graceful fallback: we never want to silently drop audio in the UI.
        # Log loudly and proxy to DEFAULT_VOICE so the user at least hears something.
        log.warning("unknown voice '%s' — falling back to default '%s' (register it in VOICES to silence this)",
                    req.voice, DEFAULT_VOICE)
        route = VOICES[DEFAULT_VOICE]
    else:
        route = VOICES[req.voice]
    url, token = BACKENDS[route["backend"]]
    spec = _FMT_SPECS[req.response_format]
    backend_fmt = spec["backend_fmt"]
    payload = {
        "input": req.input,
        "voice": route["voice"],
        "response_format": backend_fmt,
        "speed": req.speed,
    }
    log.info("route voice=%s -> backend=%s voice=%s chars=%d backend_fmt=%s client_fmt=%s",
             req.voice, route["backend"], route["voice"], len(req.input), backend_fmt, req.response_format)
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        try:
            r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"backend '{route['backend']}' unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    content = r.content
    if spec["ff_args"] is not None:
        log.info("transcoding %d bytes backend wav -> %s", len(content), req.response_format)
        content = await ffmpeg_transcode(content, spec["ff_args"])
        log.info("transcoded to %d bytes (%s)", len(content), spec["content_type"])
    return Response(content=content, media_type=spec["content_type"])
