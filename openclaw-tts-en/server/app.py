"""OpenAI-compatible /v1/audio/speech endpoint backed by Kokoro 82M (English).

Default model: hexgrad/Kokoro-82M (Apache 2.0). Voice prefix routes language:
  a* -> US English (lang_code='a'), b* -> UK English (lang_code='b').
KPipeline is initialized lazily per language code; both stay resident after warmup.
"""
from __future__ import annotations

import io
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Literal, Optional

import numpy as np
import soundfile as sf
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tts-en")

KOKORO_LOCAL_DIR = Path(os.environ.get("KOKORO_LOCAL_DIR", "/opt/checkpoints/kokoro"))
KOKORO_REPO = os.environ.get("KOKORO_REPO", "hexgrad/Kokoro-82M")
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "af_heart")
DEVICE = os.environ.get("KOKORO_DEVICE", "cuda")
API_TOKEN = os.environ.get("TTS_API_TOKEN", "").strip()
SAMPLE_RATE = 24000  # Kokoro emits 24 kHz mono

if not API_TOKEN:
    raise RuntimeError("TTS_API_TOKEN env var is required (no anonymous access allowed)")


_gpu_compat_status: str = "not-checked"


def _verify_gpu_compat() -> None:
    """Fail fast on GPU/kernel mismatches so we never silently degrade to a
    500 loop that looks like "broken TTS" to the caller.

    The foot-gun this exists to catch: a cu130 torch wheel whose compiled kernel
    list doesn't include the current GPU's compute capability (e.g. sm_120 on
    GB10 before the wheel index shipped Blackwell kernels). Symptom is that
    Kokoro's KPipeline raises `CUDA requested but not available` on first call
    — unhelpful and the OpenClaw TTS router quietly falls back to Microsoft
    Edge TTS, so the operator sees wrong-accent audio, not a failure. Here we
    check at import time and exit with an actionable rebuild instruction.
    """
    global _gpu_compat_status
    if not DEVICE.startswith("cuda"):
        _gpu_compat_status = f"cpu-skipped (DEVICE={DEVICE})"
        return
    try:
        import torch  # noqa: WPS433 — deferred so /healthz survives import failures
    except Exception as exc:
        _gpu_compat_status = f"torch-import-failed: {exc}"
        raise RuntimeError(f"KOKORO_DEVICE=cuda but torch import failed: {exc}") from exc
    if not torch.cuda.is_available():
        _gpu_compat_status = "cuda-unavailable"
        raise RuntimeError(
            "KOKORO_DEVICE=cuda but torch.cuda.is_available() is False. "
            "The GPU isn't reachable from inside the container — usually an "
            "nvidia-container-runtime / driver issue on the host. "
            "Set KOKORO_DEVICE=cpu for a CPU fallback, or fix the host "
            "toolchain before retrying."
        )
    arch_list = torch.cuda.get_arch_list()
    cc_major, cc_minor = torch.cuda.get_device_capability(0)
    target = f"sm_{cc_major}{cc_minor}"
    if target not in arch_list:
        _gpu_compat_status = f"missing-{target} (torch arch_list={arch_list})"
        raise RuntimeError(
            f"KOKORO_DEVICE=cuda but the installed torch wheel was built "
            f"without {target} kernels (arch_list={arch_list}). Rebuild the "
            f"image to pick up fresh cu130 wheels:\n"
            f"    docker compose build --no-cache openclaw-tts-en\n"
            f"or set KOKORO_DEVICE=cpu to run on CPU (much slower but works)."
        )
    _gpu_compat_status = f"ok ({target}; arch_list={arch_list})"
    log.info("GPU compat check: %s", _gpu_compat_status)


_verify_gpu_compat()

bearer = HTTPBearer(auto_error=False)


def require_token(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
    if creds is None or not secrets.compare_digest(creds.credentials, API_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing bearer token")


_pipelines: dict[str, object] = {}
_pipeline_lock = threading.Lock()


def get_pipeline(lang_code: str):
    """Lazy per-language KPipeline init. lang_code='a' (US), 'b' (UK)."""
    if lang_code in _pipelines:
        return _pipelines[lang_code]
    with _pipeline_lock:
        if lang_code in _pipelines:
            return _pipelines[lang_code]
        from kokoro import KPipeline  # lazy import so /healthz survives import errors
        log.info("loading Kokoro pipeline lang_code=%s repo=%s device=%s", lang_code, KOKORO_REPO, DEVICE)
        pipe = KPipeline(lang_code=lang_code, repo_id=KOKORO_REPO, device=DEVICE)
        _pipelines[lang_code] = pipe
        log.info("Kokoro pipeline ready lang_code=%s", lang_code)
        return pipe


def voice_to_lang_code(voice: str) -> str:
    """Voice naming convention: <lang><gender>_<name>. a=US, b=UK."""
    if not voice:
        raise HTTPException(status_code=400, detail="voice id is required")
    prefix = voice[0].lower()
    if prefix == "a":
        return "a"
    if prefix == "b":
        return "b"
    raise HTTPException(status_code=400, detail=f"unsupported voice prefix '{prefix}' (expected a*=US, b*=UK)")


def resolve_voice_path(voice: str) -> str:
    """Voice .pt files were baked into the image at build time."""
    pt = KOKORO_LOCAL_DIR / "voices" / f"{voice}.pt"
    if not pt.exists():
        available = sorted(p.stem for p in (KOKORO_LOCAL_DIR / "voices").glob("*.pt"))
        raise HTTPException(status_code=404, detail=f"voice '{voice}' not baked into image. Available: {available}")
    return str(pt)


def encode_audio(samples: np.ndarray, sample_rate: int, fmt: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    fmt = fmt.lower()
    if fmt in ("wav", "pcm"):
        sf.write(buf, samples, sample_rate, format="WAV", subtype="PCM_16")
        return buf.getvalue(), "audio/wav"
    if fmt == "flac":
        sf.write(buf, samples, sample_rate, format="FLAC")
        return buf.getvalue(), "audio/flac"
    if fmt == "ogg":
        sf.write(buf, samples, sample_rate, format="OGG", subtype="VORBIS")
        return buf.getvalue(), "audio/ogg"
    raise HTTPException(status_code=400, detail=f"unsupported response_format '{fmt}' (try wav, flac, ogg)")


class SpeechRequest(BaseModel):
    model: str = Field(default="kokoro", description="ignored, present for OpenAI compat")
    input: str = Field(..., min_length=1, max_length=4000)
    voice: str = Field(default=DEFAULT_VOICE)
    response_format: Literal["wav", "flac", "ogg", "pcm"] = "wav"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


app = FastAPI(title="OpenClaw Kokoro 82M English TTS", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    voices_dir = KOKORO_LOCAL_DIR / "voices"
    baked = sorted(p.stem for p in voices_dir.glob("*.pt")) if voices_dir.exists() else []
    return {
        "status": "ok",
        "device": DEVICE,
        "gpu_compat": _gpu_compat_status,
        "checkpoint_dir_present": KOKORO_LOCAL_DIR.exists(),
        "default_voice_present": DEFAULT_VOICE in baked,
        "voices_baked": baked,
        "pipelines_loaded": sorted(_pipelines.keys()),
    }


@app.get("/v1/voices", dependencies=[Depends(require_token)])
def list_voices() -> dict:
    voices_dir = KOKORO_LOCAL_DIR / "voices"
    voices = []
    for pt in sorted(voices_dir.glob("*.pt")):
        name = pt.stem
        voices.append({"id": name, "lang": "en-US" if name.startswith("a") else "en-GB" if name.startswith("b") else "?"})
    return {"voices": voices, "default": DEFAULT_VOICE}


@app.post("/v1/audio/speech", dependencies=[Depends(require_token)])
def synthesize(req: SpeechRequest) -> Response:
    voice_path = resolve_voice_path(req.voice)
    lang_code = voice_to_lang_code(req.voice)
    pipeline = get_pipeline(lang_code)
    log.info("synthesize voice=%s lang=%s chars=%d format=%s speed=%.2f",
             req.voice, lang_code, len(req.input), req.response_format, req.speed)
    chunks: list[np.ndarray] = []
    for _, _, audio in pipeline(req.input, voice=voice_path, speed=req.speed):
        if audio is None:
            continue
        # KPipeline emits torch.Tensor on device; force to CPU numpy.
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        chunks.append(np.asarray(audio, dtype=np.float32))
    if not chunks:
        raise HTTPException(status_code=500, detail="Kokoro produced no audio chunks")
    wav = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    audio_bytes, content_type = encode_audio(wav, SAMPLE_RATE, req.response_format)
    return Response(content=audio_bytes, media_type=content_type)
