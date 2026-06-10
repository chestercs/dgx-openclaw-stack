"""OpenAI-compatible /v1/audio/speech facade for Fish Audio S2 Pro.

Two-process container shape: this FastAPI shim runs SGLang-Omni as a child
process on loopback :9090 (its native HTTP server) and exposes an OpenAI-
shaped /v1/audio/speech endpoint on :8080. The shim's three jobs:

1) Auth — gate every /v1/* request on Bearer TTS_API_TOKEN. /healthz is
   always unauth so the Docker healthcheck and bridge-DNS probes keep working
   regardless of whether the token is set.

2) Voice → references mapping — SGLang-Omni's upstream /v1/audio/speech
   schema accepts `references[]` of {audio_path, text} (voice cloning via a
   mounted file path, NOT inline base64). The shim resolves the OpenAI-style
   `voice` field to /app/voices/<voice>.{wav,txt} at request time.

3) Optional leading-silence pad — prepend N ms of zero samples to the WAV
   reply so the Whisper STT onset doesn't clip the first phoneme (observed
   in the F5-TTS-era benchmark as "Szia" -> "Zia"). Defaults to 300 ms,
   env-tunable. Streaming responses skip the pad.

The SGLang-Omni invocation is `python -m sglang_omni.cli.cli serve
--model-path /opt/checkpoints/fish_s2_pro --config <s2pro_tts.yaml>
--port 9090`. Upstream health endpoint is /health (NOT /v1/health).

Run `python /app/app.py` to start; the shim is the supervisor.
"""
from __future__ import annotations

import asyncio
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

CHECKPOINT_DIR = Path(os.environ.get("FISH_CHECKPOINT_DIR", "/opt/checkpoints/fish_s2_pro"))
SGLANG_OMNI_DIR = Path(os.environ.get("SGLANG_OMNI_DIR", "/opt/sglang-omni"))
S2PRO_CONFIG = Path(
    os.environ.get(
        "FISH_S2PRO_CONFIG",
        str(SGLANG_OMNI_DIR / "examples" / "configs" / "s2pro_tts.yaml"),
    )
)

VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
VOICES_SEED_DIR = Path(os.environ.get("VOICES_SEED_DIR", "/app/voices_seed"))
DEFAULT_VOICE = os.environ.get("TTS_FISH_DEFAULT_VOICE", os.environ.get("DEFAULT_VOICE", "default_en"))

DEVICE = os.environ.get("TTS_FISH_DEVICE", os.environ.get("FISH_DEVICE", "cuda"))
LEADING_SILENCE_MS = int(os.environ.get("TTS_FISH_LEADING_SILENCE_MS", "300"))

# Operator-set sampling defaults, applied only when the request itself omits
# the field (request values always win — these are deploy-wide baselines, not
# overrides). Upstream S2 Pro defaults as of 2026-06: temperature 0.8,
# top_p 0.8, top_k 30, repetition_penalty 1.1, max_new_tokens 2048, speed 1.0.
# Gotcha worth knowing before tuning: top_k must be -1 or 1..30 — an
# out-of-range value FAILS the upstream pipeline instead of returning a clean
# 4xx (documented upstream limitation).
_SAMPLING_ENV_SPEC = (
    ("temperature",        "TTS_FISH_TEMPERATURE",        float),
    ("top_p",              "TTS_FISH_TOP_P",              float),
    ("top_k",              "TTS_FISH_TOP_K",              int),
    ("repetition_penalty", "TTS_FISH_REPETITION_PENALTY", float),
    ("max_new_tokens",     "TTS_FISH_MAX_NEW_TOKENS",     int),
    ("speed",              "TTS_FISH_SPEED",              float),
    ("seed",               "TTS_FISH_SEED",               int),
)
SAMPLING_DEFAULTS: dict = {}
for _field, _env, _cast in _SAMPLING_ENV_SPEC:
    _raw = os.environ.get(_env, "").strip()
    if _raw:
        try:
            SAMPLING_DEFAULTS[_field] = _cast(_raw)
        except ValueError:
            # Log-and-skip, don't crash the service over a typo'd tuning knob.
            logging.getLogger("tts-fish").warning(
                "ignoring %s=%r (not a valid %s)", _env, _raw, _cast.__name__
            )

# Shim-side voice alias map. Why here and not (only) in openclaw.json's
# voiceAliases: in practice every OpenClaw client path observed so far —
# `openclaw infer tts convert` AND the Discord-routed tts tool — passes the
# agent's voice string to the provider RAW, without resolving voiceAliases
# (first live Discord test 2026-06-10: Gemma asked for OpenAI's `coral`,
# the shim 404'd, 4 retries, no audio on the channel). The shim is the one
# chokepoint every request crosses, so aliases live here. Defaults cover the
# 10 OpenAI voice names (LLMs reach for these uninvited — they're in every
# training set) plus the friendly handles the patcher advertises. Override/
# extend via TTS_FISH_VOICE_ALIASES="name:target,name2:target2".
_DEFAULT_VOICE_ALIASES = {
    # OpenAI standard voices -> closest bundled timbre
    "alloy": "default_en", "ash": "michael", "ballad": "emma",
    "coral": "bella", "echo": "michael", "fable": "emma",
    "onyx": "fenrir", "nova": "nicole", "sage": "nicole",
    "shimmer": "bella",
    # Friendly handles (same set patcher step 11 writes into openclaw.json)
    "english": "default_en", "narrator": "default_en",
    "female": "bella", "male": "michael", "british": "emma",
    "deep": "fenrir", "soft": "nicole",
    "magyar": "default_hu", "hungarian": "default_hu",
}
VOICE_ALIASES = dict(_DEFAULT_VOICE_ALIASES)
for _pair in os.environ.get("TTS_FISH_VOICE_ALIASES", "").split(","):
    if ":" in _pair:
        _name, _target = _pair.split(":", 1)
        if _name.strip():
            VOICE_ALIASES[_name.strip().lower()] = _target.strip()

# What to do when a requested voice resolves to nothing on disk:
#   fallback (default) — synthesize with TTS_FISH_DEFAULT_VOICE and log a
#                        warning. A bot surface should speak in the wrong
#                        timbre rather than stay silent.
#   reject             — 404 with the available-voices list (the pre-2026-06-10
#                        behavior; right for API-first deployments where a
#                        wrong voice means a caller bug worth surfacing).
UNKNOWN_VOICE_POLICY = os.environ.get("TTS_FISH_UNKNOWN_VOICE_POLICY", "fallback").lower()

UPSTREAM_HOST = os.environ.get("FISH_ENGINE_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("FISH_ENGINE_PORT", "9090"))
UPSTREAM_BASE = f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}"
UPSTREAM_SPEECH_URL = f"{UPSTREAM_BASE}/v1/audio/speech"
UPSTREAM_HEALTH_URL = f"{UPSTREAM_BASE}/health"
UPSTREAM_READY_DEADLINE_S = int(os.environ.get("FISH_ENGINE_READY_DEADLINE_S", "600"))

LISTEN_HOST = os.environ.get("TTS_FISH_BIND_INTERNAL", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("TTS_FISH_PORT_INTERNAL", "8080"))

API_TOKEN = os.environ.get("TTS_API_TOKEN", "").strip()

if not API_TOKEN:
    # Empty TTS_API_TOKEN is a deploy mistake on this service — unlike the STT
    # shim (which can run open on a loopback bind), the Fish Audio model is
    # non-commercial-licensed and we do not want anonymous LAN clients hitting
    # it. Refuse to start.
    log.error(
        "TTS_API_TOKEN is required. Set it in .env and re-run "
        "`docker compose up -d openclaw-tts-fish`."
    )
    sys.exit(2)


# ── Helpers ─────────────────────────────────────────────────────────────────


_gpu_compat_status: str = "not-checked"


def verify_gpu_compat() -> None:
    """Log device + compute capability. Warn if sm < 12.0 (the SGLang-Omni
    s2-pro path expects modern Blackwell kernels). Not fatal — a future CPU
    or older-GPU dev path could still produce audio, just slowly."""
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
    if not torch.cuda.is_available():
        _gpu_compat_status = "cuda-unavailable"
        log.warning(
            "FISH_DEVICE=cuda but torch.cuda.is_available() is False. "
            "The SGLang-Omni child may still start in CPU mode and be slow."
        )
        return
    cc_major, cc_minor = torch.cuda.get_device_capability(0)
    target_num = cc_major * 10 + cc_minor
    arch_list = torch.cuda.get_arch_list()
    import re as _re
    wheel_archs = sorted(
        {int(_re.match(r"sm_(\d+)", a).group(1))
         for a in arch_list if _re.match(r"sm_(\d+)", a)}
    )
    target = f"sm_{cc_major}{cc_minor}"
    if target_num in wheel_archs:
        _gpu_compat_status = f"ok exact ({target}; arch_list={arch_list})"
    elif wheel_archs and max(wheel_archs) < target_num:
        _gpu_compat_status = f"ok ptx-fwd ({target} via sm_{max(wheel_archs)} JIT)"
        log.warning(
            "PTX forward-compat: GPU %s, wheel max sm_%s — JIT cost on cold calls.",
            target, max(wheel_archs),
        )
    else:
        _gpu_compat_status = f"missing ({target} not in {arch_list})"
        log.warning(
            "GPU %s not in wheel arch_list %s. Synthesis may fail with "
            "'no kernel for sm_%d'. Rebuild with TORCH_CUDA_ARCH_LIST including %d.",
            target, arch_list, target_num, target_num,
        )
    log.info("GPU compat check: %s", _gpu_compat_status)


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


def resolve_voice(voice: str) -> tuple[str, str]:
    """Resolve <voice> to (audio_path, transcript). Returns absolute paths
    valid inside the container (the child process runs in the same fs).

    Resolution order: exact file match -> alias map -> unknown-voice policy.
    A real file always wins over an alias of the same name, so an operator
    can shadow any alias by `docker cp`-ing a clip with that exact name."""
    wav = VOICES_DIR / f"{voice}.wav"
    txt = VOICES_DIR / f"{voice}.txt"
    if not wav.exists():
        alias_target = VOICE_ALIASES.get(voice.strip().lower())
        if alias_target and (VOICES_DIR / f"{alias_target}.wav").exists():
            log.info("voice alias: %r -> %r", voice, alias_target)
            wav = VOICES_DIR / f"{alias_target}.wav"
            txt = VOICES_DIR / f"{alias_target}.txt"
        elif UNKNOWN_VOICE_POLICY == "fallback" and (VOICES_DIR / f"{DEFAULT_VOICE}.wav").exists():
            log.warning(
                "unknown voice %r — falling back to default %r "
                "(set TTS_FISH_UNKNOWN_VOICE_POLICY=reject to 404 instead)",
                voice, DEFAULT_VOICE,
            )
            wav = VOICES_DIR / f"{DEFAULT_VOICE}.wav"
            txt = VOICES_DIR / f"{DEFAULT_VOICE}.txt"
        else:
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
    return str(wav), txt.read_text(encoding="utf-8").strip()


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


# ── SGLang-Omni child process supervisor ────────────────────────────────────


_engine_proc: Optional[subprocess.Popen] = None
_engine_ready: bool = False


def start_engine() -> None:
    """Spawn SGLang-Omni's TTS server as a child process. The shim's lifecycle
    binds to this process — if the child dies, the readiness flag flips and
    /healthz starts reporting `upstream_health: false`.

    Uses the `sgl-omni` console-script entry point installed by the SGLang-Omni
    package (not `python -m sglang_omni.cli.cli` — that path doesn't resolve
    because `sglang_omni.cli` is the module and `app` is the Typer entry, not
    `.cli.cli`)."""
    global _engine_proc
    cmd = [
        "sgl-omni", "serve",
        "--model-path", str(CHECKPOINT_DIR),
        "--config", str(S2PRO_CONFIG),
        "--port", str(UPSTREAM_PORT),
    ]
    log.info("starting SGLang-Omni engine: %s", " ".join(cmd))
    _engine_proc = subprocess.Popen(
        cmd,
        cwd=str(SGLANG_OMNI_DIR),
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=os.environ.copy(),
    )


async def wait_for_engine() -> None:
    """Poll the upstream /health until 200 OK or deadline. Marks _engine_ready
    on success. Raises if the child process exits before reaching ready."""
    global _engine_ready
    deadline = time.monotonic() + UPSTREAM_READY_DEADLINE_S
    last_err: str = ""
    async with httpx.AsyncClient(timeout=3) as client:
        while time.monotonic() < deadline:
            if _engine_proc and _engine_proc.poll() is not None:
                raise RuntimeError(
                    f"SGLang-Omni engine exited prematurely "
                    f"(code {_engine_proc.returncode}) before reaching ready"
                )
            try:
                r = await client.get(UPSTREAM_HEALTH_URL)
                if r.status_code == 200:
                    _engine_ready = True
                    log.info("SGLang-Omni engine ready at %s", UPSTREAM_HEALTH_URL)
                    return
                last_err = f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                last_err = str(e)
            await asyncio.sleep(2)
    raise RuntimeError(
        f"SGLang-Omni engine not ready within {UPSTREAM_READY_DEADLINE_S}s "
        f"(last error: {last_err})"
    )


def stop_engine() -> None:
    global _engine_proc
    if _engine_proc and _engine_proc.poll() is None:
        log.info("stopping SGLang-Omni engine (SIGTERM)")
        _engine_proc.send_signal(signal.SIGTERM)
        try:
            _engine_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warning("SGLang-Omni did not exit on SIGTERM — sending SIGKILL")
            _engine_proc.kill()
            _engine_proc.wait(timeout=5)


# ── FastAPI app ─────────────────────────────────────────────────────────────


app = FastAPI(title="openclaw-tts-fish", version="0.1.0")


@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    # /healthz always bypasses auth so docker healthcheck + bridge probes work
    # without the token. Everything else is gated on Bearer.
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
        # Don't sys.exit — leave the shim up so /healthz can report the state.
        # Docker healthcheck will mark unhealthy and restart per compose policy.


@app.on_event("shutdown")
def _shutdown() -> None:
    stop_engine()


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    voices = sorted(p.stem for p in VOICES_DIR.glob("*.wav")) if VOICES_DIR.exists() else []
    # Probe upstream best-effort so a flapping engine surfaces in healthcheck.
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
        "engine_url": UPSTREAM_SPEECH_URL,
        "upstream_health": upstream_status,
        "checkpoint_present": CHECKPOINT_DIR.exists(),
        "default_voice_present": DEFAULT_VOICE in voices,
        "voices_available": voices,
        "leading_silence_ms": LEADING_SILENCE_MS,
    }


@app.get("/v1/voices")
def list_voices() -> dict[str, Any]:
    """Best-effort voice catalog — names derived from /app/voices/*.wav
    pairs. The default voice is whichever TTS_FISH_DEFAULT_VOICE points at."""
    pairs = []
    for w in sorted(VOICES_DIR.glob("*.wav")) if VOICES_DIR.exists() else []:
        pairs.append({"id": w.stem, "has_transcript": w.with_suffix(".txt").exists()})
    return {"voices": pairs, "default": DEFAULT_VOICE}


# Fields we pass through to SGLang-Omni unchanged. `voice` is consumed by the
# shim (resolved into references); everything else just rides along.
_PASSTHROUGH_FIELDS = (
    "input", "response_format", "speed", "stream",
    "temperature", "top_p", "top_k", "repetition_penalty",
    "seed", "max_new_tokens",
)


@app.post("/v1/audio/speech")
async def synthesize(request: Request) -> Response:
    if not _engine_ready:
        raise HTTPException(status_code=503, detail="SGLang-Omni engine not ready yet")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="request body must be JSON")

    voice = (body.get("voice") or DEFAULT_VOICE).strip()
    if not body.get("input"):
        raise HTTPException(status_code=400, detail="`input` is required")

    audio_path, ref_text = resolve_voice(voice)

    upstream_payload: dict[str, Any] = {
        k: body[k] for k in _PASSTHROUGH_FIELDS if k in body
    }
    # Deploy-wide sampling baselines from TTS_FISH_* env vars; request fields
    # take precedence (setdefault never overwrites a client-sent value).
    for k, v in SAMPLING_DEFAULTS.items():
        upstream_payload.setdefault(k, v)
    upstream_payload["references"] = [{"audio_path": audio_path, "text": ref_text}]

    stream = bool(body.get("stream", False))
    response_format = (body.get("response_format") or "wav").lower()
    pad_ms = LEADING_SILENCE_MS

    log.info(
        "synthesize voice=%s chars=%d format=%s stream=%s pad_ms=%d",
        voice, len(body["input"]), response_format, stream, pad_ms,
    )

    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
    if stream:
        # Streaming: no padding (we'd have to buffer the whole stream to splice
        # silence at the front, defeating the latency point of streaming).
        async def _stream_proxy():
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", UPSTREAM_SPEECH_URL, json=upstream_payload) as r:
                    if r.status_code != 200:
                        text = (await r.aread()).decode(errors="replace")
                        log.error("upstream stream error %d: %s", r.status_code, text[:300])
                        return
                    async for chunk in r.aiter_bytes():
                        yield chunk
        media_type = _content_type_for_format(response_format)
        return StreamingResponse(_stream_proxy(), media_type=media_type)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(UPSTREAM_SPEECH_URL, json=upstream_payload)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"SGLang-Omni engine unreachable: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"engine error: {r.text[:500]}")

    content = r.content
    media_type = _content_type_for_format(response_format)
    # Only pad WAV-shaped outputs — soundfile would mis-parse mp3/ogg/etc.
    if pad_ms > 0 and response_format in ("wav", "pcm"):
        content = prepend_silence(content, pad_ms)

    return Response(content=content, media_type=media_type)


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
