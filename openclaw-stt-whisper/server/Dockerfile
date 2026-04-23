# OpenClaw Whisper STT — self-built for DGX Spark / ASUS GB10 (Grace-Blackwell,
# sm_120/sm_121, aarch64, CUDA 13).
#
# Why a custom thin wrapper instead of ghcr.io/speaches-ai/speaches:
#   - The upstream image's latest published CUDA variant is cuda-12.6.3.
#   - CTranslate2 in that image does NOT render tensor-core kernels for
#     sm_120: every low-precision compute_type (float16, int8_float16,
#     int8_bfloat16) is rejected with "target device or backend do not
#     support efficient ... computation". The float32 fallback runs on CUDA
#     cores but also destabilizes numerically — observed symptom:
#     "compression ratio 13.18" hallucination loops beyond file length,
#     real-time factor ~1x (2 min audio = 2 min transcribe).
#   - CUDA 13 + faster-whisper >= 1.2 + a CT2 built for Blackwell is the
#     proven path. The cu130 wheel pattern that the surrounding vLLM and
#     Kokoro TTS services use already works on GB10 — this Dockerfile
#     reuses the exact same base image + wheel index.
#   - If the speaches upstream later publishes a Blackwell-tensor-core
#     image (e.g. latest-cuda-13.x), swap `build:` back to `image:` in
#     docker-compose.yml; the wrapper is ~150 LOC and trivial to retire.
FROM nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    HF_HOME=/root/.cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-dev python3-pip \
        ffmpeg libsndfile1 \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3

WORKDIR /app

# 1) ARM64 + Blackwell torch + torchaudio from the cu130 wheel index FIRST so
#    the rest of the install resolves against the cu130-pinned torch.
#    faster-whisper will import torch at inference time for the audio preprocessor
#    chain (librosa/torchaudio resampling); the cu130 wheel has the matching
#    Blackwell kernels baked in.
RUN pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch torchaudio \
    && python -c "import torch; \
print('torch', torch.__version__, 'cuda_built', torch.version.cuda, \
'arch', torch.cuda.get_arch_list() if hasattr(torch,'cuda') else 'no-runtime-gpu')"

# 2) faster-whisper + FastAPI wrapper deps. With torch already cu130-pinned,
#    transitive resolution stays consistent; CTranslate2 is pulled in as a
#    transitive of faster-whisper and targets CUDA 13 in 1.2+ wheels.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt \
    && python -c "import ctranslate2, faster_whisper; \
print('ctranslate2', ctranslate2.__version__, 'faster_whisper', faster_whisper.__version__)"

# 3) App source. No model weights baked in — the ~3 GB large-v3 weights land
#    in the stt-whisper-hf-cache volume on first transcription request.
COPY app.py /app/app.py

EXPOSE 8080

# STT_API_TOKEN is REQUIRED at runtime — startup fails fast if missing.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
