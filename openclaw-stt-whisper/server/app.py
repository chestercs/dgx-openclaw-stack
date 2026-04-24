"""OpenAI-compatible speech-to-text wrapper around faster-whisper.

Minimal FastAPI shim that exposes the endpoints OpenClaw's tools.media.audio
pipeline speaks: POST /v1/audio/transcriptions, POST /v1/audio/translations,
GET /v1/models, GET /health. Bearer-auth via STT_API_TOKEN (matches the shape
the patcher's step 14 writes into tools.media.audio.models[].headers).

Singleton WhisperModel instance: lazy-load on first request so container start
stays fast; model stays resident afterwards (no TTL eviction) because GB10
unified memory budgets the ~3 GB in one go — reloading it on every request
would waste ~10 s of cold-start per call.
"""
from __future__ import annotations

import io
import logging
import os
import subprocess
from typing import Any, Iterable

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from faster_whisper import WhisperModel


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("stt-whisper")


API_TOKEN = os.environ.get("STT_API_TOKEN", "").strip()
MODEL_ID = os.environ.get("WHISPER_MODEL", "Systran/faster-whisper-large-v3")
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
DEFAULT_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "").strip() or None

if not API_TOKEN:
    # Empty STT_API_TOKEN = user opted out of Bearer auth. This mirrors the
    # speaches behavior (unset API_KEY leaves the service open). In the
    # compose default the service is loopback-only anyway, so "open" means
    # accessible only from sibling containers on the bridge network.
    log.warning(
        "STT_API_TOKEN is not set — Bearer auth disabled. Service accepts "
        "any request. Set STT_API_TOKEN in .env for production."
    )


# Singleton WhisperModel — lazy init on first /v1/audio/* call.
_model: WhisperModel | None = None


# HF repo-ids that faster-whisper can load directly (they ship CT2 weights).
# Anything else with a "/" in it is treated as a HuggingFace transformers
# repo (safetensors), converted to CT2 on first boot, and cached.
_CT2_REPO_PREFIXES = (
    "Systran/",
    "deepdml/",
    "openai/",          # falls through to faster-whisper's HF-hub converter
)


def _resolve_model_path(model_id: str) -> str:
    """If model_id is a local filesystem path, return as-is.
    If it's a HF repo that already ships CT2 weights, return as-is.
    Otherwise (a HF transformers-format repo, e.g. a community HU fine-tune),
    run ct2-transformers-converter once, cache the CT2 output on the
    HF cache volume, and return the local CT2 directory path.

    The conversion is idempotent — subsequent boots find the cached output
    and skip straight to WhisperModel loading.
    """
    if model_id.startswith(("/", "./")):
        return model_id
    if model_id.startswith(_CT2_REPO_PREFIXES):
        return model_id

    safe_name = model_id.replace("/", "--")
    cache_root = os.environ.get("HF_HOME", "/root/.cache/huggingface")
    converted_dir = os.path.join(cache_root, "ct2-converted", safe_name)
    marker = os.path.join(converted_dir, "model.bin")

    if os.path.exists(marker):
        log.info("using cached CT2 conversion: %s -> %s", model_id, converted_dir)
        return converted_dir

    log.info(
        "converting HuggingFace transformers model %s -> CT2 float16 at %s "
        "(first boot only, ~2-5 min)",
        model_id, converted_dir,
    )
    os.makedirs(os.path.dirname(converted_dir), exist_ok=True)
    subprocess.run(
        [
            "ct2-transformers-converter",
            "--model", model_id,
            "--output_dir", converted_dir,
            "--copy_files", "tokenizer.json", "preprocessor_config.json",
            "--quantization", "float16",
        ],
        check=True,
    )
    log.info("conversion complete; CT2 artefacts cached at %s", converted_dir)
    return converted_dir


def get_model() -> WhisperModel:
    global _model
    if _model is None:
        resolved = _resolve_model_path(MODEL_ID)
        log.info(
            "loading faster-whisper: model=%s (resolved=%s) device=%s compute_type=%s",
            MODEL_ID, resolved, DEVICE, COMPUTE_TYPE,
        )
        _model = WhisperModel(resolved, device=DEVICE, compute_type=COMPUTE_TYPE)
        log.info("model loaded")
    return _model


app = FastAPI(title="openclaw-stt-whisper", version="0.1.0")


@app.middleware("http")
async def bearer_auth(request: Request, call_next):
    # /health always bypasses auth so the Docker healthcheck and loopback
    # probes keep working regardless of whether STT_API_TOKEN is set.
    if request.url.path == "/health":
        return await call_next(request)
    if API_TOKEN:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):].strip() != API_TOKEN:
            return JSONResponse(
                {"error": {"message": "unauthorized", "type": "invalid_request_error"}},
                status_code=401,
            )
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "model": MODEL_ID,
        "device": DEVICE,
        "compute_type": COMPUTE_TYPE,
        "loaded": _model is not None,
    }


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    # OpenAI-list shape. We advertise only the configured model — the server
    # isn't a multi-model registry.
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_ID,
                "object": "model",
                "created": 0,
                "owned_by": "openclaw-stt-whisper",
            }
        ],
    }


def _format_timestamp(seconds: float, *, vtt: bool = False) -> str:
    # SRT uses HH:MM:SS,mmm ; VTT uses HH:MM:SS.mmm . OpenAI's openai-python
    # client passes these through unchanged, so the distinction matters.
    ms = round(max(seconds, 0.0) * 1000)
    hh, ms = divmod(ms, 3_600_000)
    mm, ms = divmod(ms, 60_000)
    ss, ms = divmod(ms, 1_000)
    sep = "." if vtt else ","
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ms:03d}"


def _srt(segments: Iterable[Any]) -> str:
    parts: list[str] = []
    for i, s in enumerate(segments, start=1):
        parts.append(str(i))
        parts.append(f"{_format_timestamp(s.start)} --> {_format_timestamp(s.end)}")
        parts.append(s.text.strip())
        parts.append("")
    return "\n".join(parts)


def _vtt(segments: Iterable[Any]) -> str:
    parts: list[str] = ["WEBVTT", ""]
    for s in segments:
        parts.append(
            f"{_format_timestamp(s.start, vtt=True)} --> {_format_timestamp(s.end, vtt=True)}"
        )
        parts.append(s.text.strip())
        parts.append("")
    return "\n".join(parts)


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    return data


def _run_whisper(
    audio_bytes: bytes,
    *,
    task: str,
    language: str | None,
    temperature: float,
) -> tuple[list, Any]:
    model = get_model()
    # faster-whisper accepts a BytesIO as a path-like input; it hands the stream
    # to the bundled ffmpeg for decode, so MP3/M4A/WebM/Ogg all work the same
    # as WAV.
    buf = io.BytesIO(audio_bytes)
    segments, info = model.transcribe(
        buf,
        task=task,
        language=language,
        temperature=temperature,
        vad_filter=False,
    )
    # Generator — materialize so callers can iterate multiple times (srt/vtt
    # + json rendering both walk the list).
    return list(segments), info


def _response_for_format(
    segments: list,
    info: Any,
    *,
    response_format: str,
    task: str,
) -> Response:
    text = "".join(s.text for s in segments).strip()
    if response_format == "text":
        return PlainTextResponse(text)
    if response_format == "srt":
        return PlainTextResponse(_srt(segments), media_type="application/x-subrip")
    if response_format == "vtt":
        return PlainTextResponse(_vtt(segments), media_type="text/vtt")
    if response_format == "verbose_json":
        return JSONResponse({
            "task": task,
            "language": info.language,
            "duration": info.duration,
            "text": text,
            "segments": [
                {
                    "id": i,
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                    "avg_logprob": s.avg_logprob,
                    "no_speech_prob": s.no_speech_prob,
                    "temperature": s.temperature,
                }
                for i, s in enumerate(segments)
            ],
        })
    # Default "json" — minimal OpenAI shape: {"text": "..."}
    return JSONResponse({"text": text})


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    model: str = Form(default=MODEL_ID),  # OpenAI contract; value is advisory
    language: str | None = Form(default=None),
    response_format: str = Form(default="json"),
    temperature: float = Form(default=0.0),
) -> Response:
    audio = await _read_upload(file)
    lang = (language.strip() if language else None) or DEFAULT_LANGUAGE
    log.info(
        "transcribe request: model=%s language=%s format=%s bytes=%d",
        model, lang or "(auto)", response_format, len(audio),
    )
    segments, info = _run_whisper(audio, task="transcribe", language=lang, temperature=temperature)
    log.info(
        "transcribe done: detected=%s duration=%.2fs segments=%d",
        info.language, info.duration, len(segments),
    )
    return _response_for_format(segments, info, response_format=response_format, task="transcribe")


@app.post("/v1/audio/translations")
async def translate(
    file: UploadFile = File(...),
    model: str = Form(default=MODEL_ID),
    response_format: str = Form(default="json"),
    temperature: float = Form(default=0.0),
) -> Response:
    # OpenAI contract: /v1/audio/translations takes source-language audio and
    # returns English text. faster-whisper implements this via task="translate"
    # (encoder output fed through the same decoder with a different prompt).
    audio = await _read_upload(file)
    log.info(
        "translate request: model=%s format=%s bytes=%d",
        model, response_format, len(audio),
    )
    segments, info = _run_whisper(audio, task="translate", language=None, temperature=temperature)
    log.info(
        "translate done: source_lang=%s duration=%.2fs segments=%d",
        info.language, info.duration, len(segments),
    )
    return _response_for_format(segments, info, response_format=response_format, task="translate")
