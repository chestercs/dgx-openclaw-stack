# OpenClaw F5-TTS Hungarian — OpenAI-compatible TTS service (DGX Spark / GB10 / aarch64)
#
# IMPORTANT — license disclaimer:
#   This Dockerfile is MIT-licensed (matches the rest of dgx-openclaw-stack).
#   The model weights it pulls at build time — `sarpba/F5-TTS_V1_hun_v2` — are
#   distributed under CC-BY-NC-4.0 (non-commercial only). Building this image
#   downloads the weights from HuggingFace; by doing so you accept the upstream
#   license. The maintainers of dgx-openclaw-stack ship no model weights of any
#   kind in this image — the build is an instruction set, not a redistribution.
#   If you need a commercial-grade Hungarian TTS, swap the checkpoint for one
#   you have a commercial license to (override F5_CHECKPOINT / F5_VOCAB env).
#
# Two prior bases failed:
#  1) ghcr.io/swivid/f5-tts:main → amd64-only manifest, "exec format error" on Grace.
#  2) nvcr.io/nvidia/pytorch:25.11-py3 → torchaudio ABI mismatch when pip swapped the
#     NGC-bundled torchaudio for a PyPI cu130 wheel ("undefined symbol: torch_library_impl").
#
# Clean route: minimal nvidia/cuda runtime base + install torch & torchaudio together from
# the cu130 wheel index BEFORE f5-tts, so the ABI matches and f5-tts pip install is a no-op
# for those two. NVIDIA dev-forum-verified pattern for DGX Spark / Blackwell aarch64.
#
# Default reference voice: Diana Majlinger reading "Egri csillagok" (LibriVox public domain).
FROM nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    F5_CHECKPOINT=/opt/checkpoints/sarpba_v1_hun_v2/model_927900.safetensors \
    F5_VOCAB=/opt/checkpoints/sarpba_v1_hun_v2/vocab.txt \
    VOICES_DIR=/app/voices \
    DEFAULT_VOICE=default_hu

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-dev python3-pip \
        ffmpeg libsndfile1 libsox-dev sox \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3

WORKDIR /app

# 1) Install ARM64+Blackwell torch + torchaudio from the cu130 wheel index FIRST.
#    Doing this before f5-tts ensures pip won't ABI-mismatch them later.
RUN pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch torchaudio \
    && python -c "import torch, torchaudio; \
print('torch', torch.__version__, 'torchaudio', torchaudio.__version__, \
'cuda_built', torch.version.cuda, 'arch', torch.cuda.get_arch_list() if torch.cuda.is_available() else 'no-runtime-gpu')"

# 2) Install f5-tts and our wrapper deps. f5-tts pulls many transitive deps but, with torch
#    already pinned to cu130, the matching torchaudio==X+cu130 stays put.
COPY requirements.txt /app/requirements.txt
RUN pip install f5-tts \
    && pip install -r /app/requirements.txt \
    && python -c "import torch, torchaudio, f5_tts; \
print('OK torch', torch.__version__, 'torchaudio', torchaudio.__version__, 'f5_tts loaded')"

# 3) Pull the sarpba V2 Hungarian checkpoint at build time (CC-BY-NC-4.0).
# Skip the legacy .pt files (2× 5.4 GB) — we only need the safetensors + vocab.
# Default points to the higher-step checkpoint (927900); override via F5_CHECKPOINT env
# if the 309300 variant turns out to sound better on your reference voice.
RUN mkdir -p /opt/checkpoints/sarpba_v1_hun_v2 \
    && python -c "from huggingface_hub import snapshot_download; \
snapshot_download(repo_id='sarpba/F5-TTS_V1_hun_v2', \
local_dir='/opt/checkpoints/sarpba_v1_hun_v2', \
allow_patterns=['*.safetensors', 'vocab.txt', 'setting.json', 'README.md'])" \
    && ls -la /opt/checkpoints/sarpba_v1_hun_v2/

# 4) Pull public-domain Hungarian reference voice (Diana Majlinger / LibriVox) into seed dir.
COPY fetch_default_voice.py /app/fetch_default_voice.py
RUN mkdir -p /app/voices_seed \
    && VOICES_DIR=/app/voices_seed python /app/fetch_default_voice.py \
    && ls -la /app/voices_seed/

COPY app.py /app/app.py

EXPOSE 8080

# TTS_API_TOKEN is REQUIRED at runtime — startup fails fast if missing.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
