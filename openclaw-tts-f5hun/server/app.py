"""OpenAI-compatible /v1/audio/speech endpoint backed by F5-TTS Hungarian.

Default checkpoint: sarpba/F5-TTS_V1_hun_v2 (CC-BY-NC-4.0).
Personal / research use only — no commercial deployment without license change.
The wrapper code itself is MIT (see repo root LICENSE).
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
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tts")

CHECKPOINT_PATH = Path(os.environ.get("F5_CHECKPOINT", "/opt/checkpoints/sarpba_v1_hun_v2/model_927900.safetensors"))
VOCAB_PATH = Path(os.environ.get("F5_VOCAB", "/opt/checkpoints/sarpba_v1_hun_v2/vocab.txt"))
VOICES_DIR = Path(os.environ.get("VOICES_DIR", "/app/voices"))
VOICES_SEED_DIR = Path(os.environ.get("VOICES_SEED_DIR", "/app/voices_seed"))
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "default_hu")
DEVICE = os.environ.get("F5_DEVICE", "cuda")
MODEL_NAME = os.environ.get("F5_MODEL_NAME", "F5TTS_v1_Base")
API_TOKEN = os.environ.get("TTS_API_TOKEN", "").strip()

if not API_TOKEN:
    raise RuntimeError("TTS_API_TOKEN env var is required (no anonymous access allowed)")


_gpu_compat_status: str = "not-checked"


def _verify_gpu_compat() -> None:
    """Fail fast on GPU/kernel mismatches so we never silently degrade to a
    500 loop that looks like "broken TTS" to the caller.

    The foot-gun this exists to catch: a cu130 torch wheel whose compiled kernel
    list doesn't include the current GPU's compute capability (e.g. sm_120 on
    GB10 before the wheel index shipped Blackwell kernels). Symptom is that
    F5-TTS loads fine, then crashes at first synthesis with `RuntimeError: GET
    was unable to find an engine to execute this computation` — unhelpful and
    the OpenClaw TTS router quietly falls back to Microsoft Edge TTS, so the
    operator sees wrong-accent audio, not a failure. Here we check at import
    time and exit with an actionable rebuild instruction.
    """
    global _gpu_compat_status
    if not DEVICE.startswith("cuda"):
        _gpu_compat_status = f"cpu-skipped (DEVICE={DEVICE})"
        return
    try:
        import torch  # noqa: WPS433 — deferred so /healthz survives import failures
    except Exception as exc:
        _gpu_compat_status = f"torch-import-failed: {exc}"
        raise RuntimeError(f"F5_DEVICE=cuda but torch import failed: {exc}") from exc
    if not torch.cuda.is_available():
        _gpu_compat_status = "cuda-unavailable"
        raise RuntimeError(
            "F5_DEVICE=cuda but torch.cuda.is_available() is False. "
            "The GPU isn't reachable from inside the container — usually an "
            "nvidia-container-runtime / driver issue on the host. "
            "Set F5HUN_DEVICE=cpu for a CPU fallback (much slower — F5-TTS "
            "is ~50× realtime on CPU), or fix the host toolchain."
        )
    arch_list = torch.cuda.get_arch_list()
    cc_major, cc_minor = torch.cuda.get_device_capability(0)
    target = f"sm_{cc_major}{cc_minor}"
    # PTX forward-compat: exact match is ideal (no JIT cost), but a lower arch
    # in the wheel also works — CUDA driver JIT-compiles the embedded PTX to
    # the current device at first kernel launch. Only fail if the current
    # capability exceeds every arch the wheel ships (nothing to JIT from).
    # Example: GB10 reports sm_121 but cu130 wheels currently ship sm_120 max
    # — that's fine, sm_120 PTX → sm_121 JIT is standard forward-compat.
    import re
    def _sm_num(entry: str) -> int:
        m = re.match(r"sm_(\d+)", entry)
        return int(m.group(1)) if m else -1
    target_num = cc_major * 10 + cc_minor
    wheel_archs = sorted({_sm_num(a) for a in arch_list if a.startswith("sm_")} - {-1})
    if target_num in wheel_archs:
        _gpu_compat_status = f"ok exact ({target}; arch_list={arch_list})"
    elif wheel_archs and max(wheel_archs) < target_num:
        best = max(wheel_archs)
        _gpu_compat_status = (
            f"ok ptx-fwd ({target} via sm_{best} forward-compat JIT; "
            f"arch_list={arch_list})"
        )
        log.warning(
            "Running on PTX forward-compat: GPU is %s but wheel max kernel "
            "is sm_%s. First kernel launch per op JIT-compiles PTX → expect "
            "brief extra latency on cold calls. Rebuild with --no-cache once "
            "a %s-native wheel lands upstream to remove the JIT.",
            target, best, target,
        )
    else:
        _gpu_compat_status = f"missing-{target} (torch arch_list={arch_list})"
        raise RuntimeError(
            f"F5_DEVICE=cuda but the installed torch wheel has no kernel "
            f"compatible with {target} (arch_list={arch_list}). The GPU's "
            f"compute capability is newer than everything the wheel ships, "
            f"so PTX forward-compat can't help. Rebuild the image with a "
            f"newer cu130 wheel:\n"
            f"    docker compose --profile hu build --no-cache openclaw-tts-f5hun\n"
            f"or set F5HUN_DEVICE=cpu to run on CPU (much slower but works)."
        )
    log.info("GPU compat check: %s", _gpu_compat_status)


_verify_gpu_compat()


def seed_voices() -> None:
    """Copy any seed voice files into VOICES_DIR if a same-named file isn't present.

    The runtime bind-mount overlays the in-image /app/voices, so without this
    seeding the build-time default voice would be invisible.
    """
    if not VOICES_SEED_DIR.exists():
        return
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    import shutil
    for src in VOICES_SEED_DIR.iterdir():
        dst = VOICES_DIR / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            log.info("seeded voice file %s", dst)


seed_voices()

bearer = HTTPBearer(auto_error=False)


def require_token(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
    if creds is None or not secrets.compare_digest(creds.credentials, API_TOKEN):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing bearer token")


_model_lock = threading.Lock()
_model = None


def get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from f5_tts.api import F5TTS  # imported lazily so import errors surface in /healthz
        log.info("loading F5-TTS checkpoint=%s vocab=%s device=%s", CHECKPOINT_PATH, VOCAB_PATH, DEVICE)
        _model = F5TTS(
            model=MODEL_NAME,
            ckpt_file=str(CHECKPOINT_PATH),
            vocab_file=str(VOCAB_PATH),
            device=DEVICE,
            use_ema=True,
        )
        log.info("F5-TTS ready")
        return _model


def resolve_voice(voice: str) -> tuple[Path, str]:
    wav = VOICES_DIR / f"{voice}.wav"
    txt = VOICES_DIR / f"{voice}.txt"
    if not wav.exists():
        raise HTTPException(status_code=404, detail=f"voice '{voice}' not found at {wav}")
    if not txt.exists():
        raise HTTPException(status_code=400, detail=f"voice '{voice}' missing transcript at {txt}")
    return wav, txt.read_text(encoding="utf-8").strip()


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
    model: str = Field(default="f5-tts-hu", description="ignored, present for OpenAI compat")
    input: str = Field(..., min_length=1, max_length=4000)
    voice: str = Field(default=DEFAULT_VOICE)
    response_format: Literal["wav", "flac", "ogg", "pcm"] = "wav"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


app = FastAPI(title="OpenClaw F5-TTS Hungarian", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "device": DEVICE,
        "gpu_compat": _gpu_compat_status,
        "checkpoint_present": CHECKPOINT_PATH.exists(),
        "vocab_present": VOCAB_PATH.exists(),
        "default_voice_present": (VOICES_DIR / f"{DEFAULT_VOICE}.wav").exists(),
        "model_loaded": _model is not None,
    }


@app.get("/v1/voices", dependencies=[Depends(require_token)])
def list_voices() -> dict:
    voices = []
    for wav in sorted(VOICES_DIR.glob("*.wav")):
        name = wav.stem
        txt = wav.with_suffix(".txt")
        voices.append({"id": name, "has_transcript": txt.exists()})
    return {"voices": voices, "default": DEFAULT_VOICE}


@app.post("/v1/audio/speech", dependencies=[Depends(require_token)])
def synthesize(req: SpeechRequest, request: Request) -> Response:
    ref_wav, ref_text = resolve_voice(req.voice)
    model = get_model()
    log.info("synthesize voice=%s chars=%d format=%s speed=%.2f", req.voice, len(req.input), req.response_format, req.speed)
    wav, sr, _ = model.infer(
        ref_file=str(ref_wav),
        ref_text=ref_text,
        gen_text=req.input,
        speed=req.speed,
    )
    if isinstance(wav, list):
        wav = np.concatenate(wav)
    audio_bytes, content_type = encode_audio(np.asarray(wav, dtype=np.float32), int(sr), req.response_format)
    return Response(content=audio_bytes, media_type=content_type)
