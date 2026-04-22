# OpenClaw Kokoro 82M English TTS — OpenAI-compatible (DGX Spark / GB10 / aarch64).
#
# Why a custom thin wrapper instead of remsky/Kokoro-FastAPI:
#   - remsky/Kokoro-FastAPI issue #401: ARM64+sm_100 (Blackwell) Docker image
#     broken with "no kernel image available" / "exec format error" on DGX Spark.
#   - PR #403 (ARM64 nightly torch wrapper) is open, not merged.
#   - The cu130 wheel pattern that the surrounding vLLM services use is the
#     proven path on GB10. ~80 lines of FastAPI + KPipeline is enough.
#
# Model: hexgrad/Kokoro-82M (Apache 2.0, commercial-safe). TTS Arena #1 open
# weights at the time of writing. Default voice: af_heart (A-grade flagship
# US female from the Kokoro VOICES catalog).
FROM nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    KOKORO_REPO=hexgrad/Kokoro-82M \
    KOKORO_LOCAL_DIR=/opt/checkpoints/kokoro \
    DEFAULT_VOICE=af_heart

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-dev python3-pip \
        ffmpeg libsndfile1 \
        espeak-ng espeak-ng-data \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3

WORKDIR /app

# 1) ARM64 + Blackwell torch + torchaudio from the cu130 wheel index FIRST so
#    the rest of the install resolves against the cu130-pinned torch.
RUN pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch torchaudio \
    && python -c "import torch, torchaudio; \
print('torch', torch.__version__, 'torchaudio', torchaudio.__version__, \
'cuda_built', torch.version.cuda, 'arch', torch.cuda.get_arch_list() if torch.cuda.is_available() else 'no-runtime-gpu')"

# 2) Kokoro lib + misaki G2P + wrapper deps. With torch already cu130-pinned,
#    transitive resolution stays consistent.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt \
    && python -c "import torch, kokoro, misaki; print('OK torch', torch.__version__, 'kokoro', kokoro.__version__)"

# 3) Bake only the A/A-/B- grade voices + main weights at build time.
#    Full pack = 54 voices × ~523 KB; we ship the production-ready subset to
#    keep the image lean. Add more by extending the allow_patterns list and
#    rebuilding — voices live in /opt/checkpoints/kokoro/voices/ at runtime.
RUN mkdir -p ${KOKORO_LOCAL_DIR} \
    && python -c "from huggingface_hub import snapshot_download; \
snapshot_download(repo_id='${KOKORO_REPO}', \
local_dir='${KOKORO_LOCAL_DIR}', \
allow_patterns=['kokoro-v1_0.pth', 'config.json', 'VOICES.md', \
'voices/af_heart.pt', 'voices/af_bella.pt', 'voices/af_nicole.pt', \
'voices/af_aoede.pt', 'voices/af_kore.pt', 'voices/af_sarah.pt', \
'voices/bf_emma.pt', \
'voices/am_michael.pt', 'voices/am_fenrir.pt', 'voices/am_puck.pt'])" \
    && ls -la ${KOKORO_LOCAL_DIR}/voices/

COPY app.py /app/app.py

EXPOSE 8080

# TTS_API_TOKEN is REQUIRED at runtime — startup fails fast if missing.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
