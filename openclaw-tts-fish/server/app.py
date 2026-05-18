"""OpenAI-compatible /v1/audio/speech facade for Fish Audio TTS.

Two-process container shape: this FastAPI shim runs fish-speech's
`tools/api_server.py` as a child process on loopback :9090 (the upstream
native HTTP server, POST /v1/tts with ServeTTSRequest body) and exposes an
OpenAI-shaped /v1/audio/speech endpoint on :8080. The shim's three jobs:

1) Auth — gate every /v1/* request on Bearer TTS_API_TOKEN. /healthz is
   always unauth so the Docker healthcheck and bridge-DNS probes keep
   working regardless of whether the token is set.

2) OpenAI → fish-speech payload mapping. The shim translates:
     POST /v1/audio/speech { input, voice, response_format, speed }
   into the upstream native shape:
     POST /v1/tts {
       text, format, references: [{ audio: <b64-wav>, text: <transcript> }],
       streaming, normalize
     }
   Voice cloning is via inline base64 reference audio (NOT mounted file
   path — that was the SGLang-Omni convention). The shim resolves the
   OpenAI-style `voice` string to /app/voices/<voice>.{wav,txt}, reads
   the WAV bytes, b64-encodes, and forwards.

3) Optional leading-silence pad — prepend N ms of zero samples to the
   reply so the Whisper STT onset doesn't clip the first phoneme
   (observed in the F5-TTS-era benchmark as "Szia" -> "Zia"). Defaults
   to 300 ms, env-tunable. Streaming responses skip the pad.

The upstream invocation is `python tools/api_server.py --listen
0.0.0.0:9090 --llama-checkpoint-path <ckpt> --decoder-checkpoint-path
<ckpt> --device cuda --half`. Health endpoint is /v1/health.

Run `python /app/app.py` to start; the shim is the supervisor.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tts-fish")


# ── Configuration ────────────────────────────────────────────────────────────

CHECKPOINT_DIR = Path(os.environ.get("FISH_CHECKPOINT_DIR", "/opt/checkpoints/fish_s1_mini"))
FISH_SPEECH_DIR = Path(os.environ.get("FISH_SPEECH_DIR", "/opt/fish-speech"))

VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
VOICES_SEED_DIR = Path(os.environ.get("VOICES_SEED_DIR", "/app/voices_seed"))
DEFAULT_VOICE = os.environ.get("TTS_FISH_DEFAULT_VOICE", os.environ.get("DEFAULT_VOICE", "default_hu"))

DEVICE = os.environ.get("TTS_FISH_DEVICE", os.environ.get("FISH_DEVICE", "cuda"))
LEADING_SILENCE_MS = int(os.environ.get("TTS_FISH_LEADING_SILENCE_MS", "300"))

UPSTREAM_HOST = os.environ.get("FISH_ENGINE_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("FISH_ENGINE_PORT", "9090"))
UPSTREAM_BASE = f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}"
UPSTREAM_TTS_URL = f"{UPSTREAM_BASE}/v1/tts"
UPSTREAM_HEALTH_URL = f"{UPSTREAM_BASE}/v1/health"
UPSTREAM_READY_DEADLINE_S = int(os.environ.get("FISH_ENGINE_READY_DEADLINE_S", "600"))

LISTEN_HOST = os.environ.get("TTS_FISH_BIND_INTERNAL", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("TTS_FISH_PORT_INTERNAL", "8080"))

API_TOKEN = os.environ.get("TTS_API_TOKEN", "").strip()

if not API_TOKEN:
    log.error(
        "TTS_API_TOKEN is required. Set it in .env and re-run "
        "`docker compose up -d openclaw-tts-fish`."
    )
    sys.exit(2)


# ── Helpers ─────────────────────────────────────────────────────────────────


_gpu_compat_status: str = "not-checked"


def verify_gpu_compat() -> None:
    """Log device + compute capability. Warns rather than aborts — fish-speech
    + cu130 torch can sometimes still produce output even when
    `torch.cuda.is_available() == False` (the NVML init warning observed in
    the GB10 Docker setup is cosmetic and doesn't actually break the
    CUDA runtime path)."""
    global _gpu_compat_status
    if not DEVICE.startswith("cuda"):
        _gpu_compat_status = f"cpu-skipped (DEVICE={DEVICE})"
        log.warning("FISH_DEVICE=%s — GPU compat check skipped. Expect very slow synthesis.", DEVICE)
        return
    try:
        import torch
    except ImportError:
        _gpu_compat_status = "torch-missing"
        log.warning("torch unavailable — GPU compat check skipped.")
        return
    cuda_built = getattr(torch.version, "cuda", None)
    avail = bool(getattr(torch.cuda, "is_available", lambda: False)())
    try:
        arch_list = torch.cuda.get_arch_list()
    except Exception:
        arch_list = []
    _gpu_compat_status = (
        f"torch={torch.__version__} cuda_built={cuda_built} "
        f"available={avail} arch_list={arch_list}"
    )
    log.info("GPU compat: %s", _gpu_compat_status)


def seed_voices() -> None:
    """Copy bundled default voices from /app/voices_seed/ into /app/voices/
    on first start. Idempotent — never overwrites a user-mounted voice on
    the tts-fish-voices volume."""
    if not VOICES_SEED_DIR.exists():
        return
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    for src in VOICES_SEED_DIR.iterdir():
        dst = VOICES_DIR / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            log.info("seeded voice file %s", dst)


def resolve_voice(voice: str) -> tuple[bytes, str]:
    """Resolve <voice> to (wav_bytes, transcript). The shim sends WAV bytes
    inline (base64) to fish-speech's ServeTTSRequest schema — no shared
    filesystem with the upstream engine needed."""
    wav = VOICES_DIR / f"{voice}.wav"
    txt = VOICES_DIR / f"{voice}.txt"
    if not wav.exists():
        available = sorted(p.stem for p in VOICES_DIR.glob("*.wav"))
        raise HTTPException(
            status_code=404,
            detail=(
                f"voice '{voice}' not found at {wav}. "
                f"Available voices: {available}. "
                f"Add one with `docker cp <name>.wav openclaw-tts-fish:/app/voices/`."
            ),
        )
    if not txt.exists():
        raise HTTPException(
            status_code=400,
            detail=(
                f"voice '{voice}' is missing its transcript at {txt}. "
                f"Voice cloning requires both <name>.wav and <name>.txt."
            ),
        )
    return wav.read_bytes(), txt.read_text(encoding="utf-8").strip()


def prepend_silence(audio_bytes: bytes, ms: int) -> bytes:
    """Read WAV from bytes, prepend `ms` ms of silence at the same rate/
    channels/dtype, return WAV bytes. Best-effort: if parsing fails (the
    upstream returned a non-WAV blob), return the input unchanged."""
    if ms <= 0:
        return audio_bytes
    try:
        in_buf = io.BytesIO(audio_bytes)
        data, sr = sf.read(in_buf, dtype="float32", always_2d=False)
    except Exception as e:
        log.warning("silence pad: could not parse upstream WAV (%s) — returning unpadded", e)
        return audio_bytes
    pad_samples = int(sr * ms / 1000)
    if pad_samples <= 0:
        return audio_bytes
    if data.ndim == 1:
        pad = np.zeros(pad_samples, dtype=data.dtype)
    else:
        pad = np.zeros((pad_samples, data.shape[1]), dtype=data.dtype)
    padded = np.concatenate([pad, data], axis=0)
    out_buf = io.BytesIO()
    sf.write(out_buf, padded, sr, format="WAV", subtype="PCM_16")
    return out_buf.getvalue()


# ── fish-speech api_server child process supervisor ─────────────────────────


_engine_proc: Optional[subprocess.Popen] = None
_engine_ready: bool = False


def start_engine() -> None:
    """Spawn fish-speech's tools/api_server.py as a child process. The shim's
    lifecycle binds to this process — if the child dies, the readiness
    flag flips and /healthz starts reporting `upstream_health` failures."""
    global _engine_proc
    cmd = [
        sys.executable, "tools/api_server.py",
        "--listen", f"127.0.0.1:{UPSTREAM_PORT}",
        "--llama-checkpoint-path", str(CHECKPOINT_DIR),
        "--decoder-checkpoint-path", str(CHECKPOINT_DIR),
        "--device", DEVICE,
    ]
    if DEVICE.startswith("cuda"):
        cmd.append("--half")
    log.info("starting fish-speech engine: %s (cwd=%s)", " ".join(cmd), FISH_SPEECH_DIR)
    _engine_proc = subprocess.Popen(
        cmd,
        cwd=str(FISH_SPEECH_DIR),
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=os.environ.copy(),
    )


async def wait_for_engine() -> None:
    """Poll the upstream /v1/health until 200 OK or deadline. Marks
    _engine_ready on success. Raises if the child process exits before
    reaching ready."""
    global _engine_ready
    deadline = time.monotonic() + UPSTREAM_READY_DEADLINE_S
    last_err: str = ""
    async with httpx.AsyncClient(timeout=3) as client:
        while time.monotonic() < deadline:
            if _engine_proc and _engine_proc.poll() is not None:
                raise RuntimeError(
                    f"fish-speech engine exited prematurely "
                    f"(code {_engine_proc.returncode}) before reaching ready"
                )
            try:
                r = await client.get(UPSTREAM_HEALTH_URL)
                if r.status_code == 200:
                    _engine_ready = True
                    log.info("fish-speech engine ready at %s", UPSTREAM_HEALTH_URL)
                    return
                last_err = f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                last_err = str(e)
            await asyncio.sleep(2)
    raise RuntimeError(
        f"fish-speech engine not ready within {UPSTREAM_READY_DEADLINE_S}s "
        f"(last error: {last_err})"
    )


def stop_engine() -> None:
    global _engine_proc
    if _engine_proc and _engine_proc.poll() is None:
        log.info("stopping fish-speech engine (SIGTERM)")
        _engine_proc.send_signal(signal.SIGTERM)
        try:
            _engine_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("fish-speech did not exit on SIGTERM — sending SIGKILL")
            _engine_proc.kill()
            _engine_proc.wait(timeout=5)


# ── FastAPI app ─────────────────────────────────────────────────────────────


app = FastAPI(title="openclaw-tts-fish", version="0.1.0")


@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or not secrets.compare_digest(
        auth[len("Bearer "):].strip(), API_TOKEN
    ):
        return JSONResponse(
            {"error": {"message": "unauthorized", "type": "invalid_request_error"}},
            status_code=401,
        )
    return await call_next(request)


@app.on_event("startup")
async def _startup() -> None:
    seed_voices()
    verify_gpu_compat()
    start_engine()
    try:
        await wait_for_engine()
    except Exception as e:
        log.error("engine startup failed: %s", e)


@app.on_event("shutdown")
def _shutdown() -> None:
    stop_engine()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    voices = sorted(p.stem for p in VOICES_DIR.glob("*.wav")) if VOICES_DIR.exists() else []
    upstream_status: str
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(UPSTREAM_HEALTH_URL)
            upstream_status = f"http_{r.status_code}"
    except httpx.HTTPError as e:
        upstream_status = f"unreachable ({type(e).__name__})"
    return {
        "status": "ok" if _engine_ready else "starting",
        "device": DEVICE,
        "gpu_compat": _gpu_compat_status,
        "engine_ready": _engine_ready,
        "engine_pid": _engine_proc.pid if _engine_proc else None,
        "engine_alive": _engine_proc is not None and _engine_proc.poll() is None,
        "engine_url": UPSTREAM_TTS_URL,
        "upstream_health": upstream_status,
        "checkpoint_present": CHECKPOINT_DIR.exists(),
        "default_voice_present": DEFAULT_VOICE in voices,
        "voices_available": voices,
        "leading_silence_ms": LEADING_SILENCE_MS,
    }


@app.get("/v1/voices")
def list_voices() -> dict[str, Any]:
    pairs = []
    for w in sorted(VOICES_DIR.glob("*.wav")) if VOICES_DIR.exists() else []:
        pairs.append({"id": w.stem, "has_transcript": w.with_suffix(".txt").exists()})
    return {"voices": pairs, "default": DEFAULT_VOICE}


@app.post("/v1/audio/speech")
async def synthesize(request: Request) -> Response:
    if not _engine_ready:
        raise HTTPException(status_code=503, detail="fish-speech engine not ready yet")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be JSON")

    voice = (body.get("voice") or DEFAULT_VOICE).strip()
    text_input = body.get("input")
    if not text_input:
        raise HTTPException(status_code=400, detail="`input` is required")

    ref_wav_bytes, ref_text = resolve_voice(voice)
    ref_audio_b64 = base64.b64encode(ref_wav_bytes).decode("ascii")

    response_format = (body.get("response_format") or "wav").lower()
    stream = bool(body.get("stream", False))
    pad_ms = LEADING_SILENCE_MS

    # fish-speech ServeTTSRequest shape — `text` (not `input`), inline
    # base64 reference audio under `references[].audio`, format string,
    # streaming boolean, normalize boolean.
    upstream_payload: dict[str, Any] = {
        "text": text_input,
        "format": response_format if response_format in ("wav", "pcm", "mp3") else "wav",
        "references": [{"audio": ref_audio_b64, "text": ref_text}],
        "streaming": stream,
        "normalize": True,
    }
    # Pass-through optional generation knobs that fish-speech accepts.
    for key in ("temperature", "top_p", "repetition_penalty", "max_new_tokens", "seed", "chunk_length"):
        if key in body:
            upstream_payload[key] = body[key]

    log.info(
        "synthesize voice=%s chars=%d format=%s stream=%s pad_ms=%d",
        voice, len(text_input), response_format, stream, pad_ms,
    )

    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
    if stream:
        async def _stream_proxy():
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", UPSTREAM_TTS_URL, json=upstream_payload) as r:
                    if r.status_code != 200:
                        text = (await r.aread()).decode(errors="replace")
                        log.error("upstream stream error %d: %s", r.status_code, text[:300])
                        return
                    async for chunk in r.aiter_bytes():
                        yield chunk
        return StreamingResponse(_stream_proxy(), media_type=_content_type_for_format(response_format))

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(UPSTREAM_TTS_URL, json=upstream_payload)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"fish-speech engine unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"engine error: {r.text[:500]}")

    content = r.content
    if pad_ms > 0 and response_format in ("wav", "pcm"):
        content = prepend_silence(content, pad_ms)

    return Response(content=content, media_type=_content_type_for_format(response_format))


def _content_type_for_format(fmt: str) -> str:
    return {
        "wav": "audio/wav",
        "pcm": "audio/wav",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
        "mp3": "audio/mpeg",
        "opus": "audio/ogg",
        "aac": "audio/aac",
    }.get(fmt, "application/octet-stream")


# ── Entrypoint ──────────────────────────────────────────────────────────────


def main() -> None:
    uvicorn.run(
        "app:app",
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        workers=1,
        log_level="info",
    )


if __name__ == "__main__":
    main()
