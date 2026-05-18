# OpenClaw Fish Audio TTS — self-built for DGX Spark / ASUS GB10
# (Grace-Blackwell, sm_120/sm_121, aarch64, CUDA 13). Replaces the legacy
# 3-service Kokoro EN + F5-TTS HU + router pipeline with one OpenAI-compat
# /v1/audio/speech endpoint backed by fish-speech's tools/api_server.py
# serving fishaudio/openaudio-s1-mini.
#
# Loader / model background:
#   - First attempt was sgl-project/sglang-omni serving fishaudio/s2-pro,
#     which is the upstream-recommended path for the 5B Qwen3-omni
#     architecture. On a 2026-05 GB10 build run, SGLang-Omni's installed
#     `sgl-kernel 0.3.21` only shipped `sm_90` (Hopper) + `sm_100`
#     (Blackwell ultra / B200) prebuilt kernels, **no `sm_120` (GB10)**,
#     and the install also clobbered the cu130 torch wheel with a CPU
#     build — `torch.cuda.is_available() False` + `libnvrtc.so.12: cannot
#     open shared object file` at runtime. Tracked upstream as
#     sgl-project/sglang #9542 (still open).
#   - This Dockerfile falls back to the legacy but proven fish-speech
#     repo's `tools/api_server.py`, which is the loader the upstream
#     speech.fish.audio docs document and which uses straight PyTorch
#     (no sgl-kernel dep). Pairs with the smaller `openaudio-s1-mini`
#     checkpoint (~0.5B params, ~2 GB weights) — multilingual (EN/JP/KR/
#     ZH/FR/DE/AR/ES; Hungarian via voice cloning with a magyar reference
#     clip).
#
# License caveat: weights pulled at build time (fishaudio/openaudio-s1-mini)
# are CC-BY-NC-SA-4.0 — non-commercial Share-Alike. The repo is gated on
# HuggingFace; the operator must visit the model card once on hf.co and
# accept the license for the configured HUGGING_FACE_HUB_TOKEN. Wrapper
# code in ./openclaw-tts-fish/server/ is MIT.
FROM nvidia/cuda:13.0.0-cudnn-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    HF_HOME=/root/.cache/huggingface \
    FISH_REPO=fishaudio/openaudio-s1-mini \
    FISH_CHECKPOINT_DIR=/opt/checkpoints/fish_s1_mini \
    FISH_SPEECH_DIR=/opt/fish-speech \
    VOICES_DIR=/app/voices \
    VOICES_SEED_DIR=/app/voices_seed \
    DEFAULT_VOICE=default_hu \
    TORCH_CUDA_ARCH_LIST="8.0;12.0"

# Build tooling + runtime deps. ffmpeg + libsndfile1 cover audio I/O.
# git/curl/cmake/ninja/build-essential cover any from-source pip builds
# fish-speech transitive deps may trigger on aarch64.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-dev python3-pip \
        ffmpeg libsndfile1 \
        ninja-build build-essential cmake pybind11-dev \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3

# HF CLI for the model download step. huggingface_hub >=1.0 renamed the
# CLI from `huggingface-cli` to `hf`.
RUN pip install "huggingface_hub[cli]>=0.26" \
    && hf --version

WORKDIR /app

# 1) Blackwell-aware torch + torchaudio from the cu130 wheel index.
#    Installed FIRST so any fish-speech transitive dep that pulls in a
#    different torch will be deduped against this one (we also force-
#    reinstall after fish-speech setup in case its setup.py overrides).
RUN pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch torchaudio \
    && python -c "import torch; print('torch', torch.__version__, 'cuda_built', torch.version.cuda, \
'arch', torch.cuda.get_arch_list() if hasattr(torch,'cuda') else 'no-runtime-gpu')"

# 2) fish-speech repo install from source (editable). The upstream docs at
#    speech.fish.audio/install/ recommend `pip install -e .` against a
#    clone. `tools/api_server.py` lives inside this tree and is the
#    advertised HTTP API entry point for the S1 family.
RUN git clone --depth 1 https://github.com/fishaudio/fish-speech.git ${FISH_SPEECH_DIR} \
    && cd ${FISH_SPEECH_DIR} \
    && pip install --no-build-isolation -e . \
    && python -c "import fish_speech; print('fish_speech loaded from', fish_speech.__file__)"

# 2b) Force-reinstall cu130 torch — fish-speech's setup.py declares torch
#     as a runtime dep and the pip resolver may downgrade or swap our
#     cu130 install for a CPU wheel pulled from PyPI. --no-deps protects
#     against retriggering fish-speech's dep tree.
RUN pip install --force-reinstall --no-deps \
        --index-url https://download.pytorch.org/whl/cu130 \
        torch torchaudio \
    && python -c "import torch; print('post-fix torch', torch.__version__, \
'cuda_built', torch.version.cuda, \
'arch', torch.cuda.get_arch_list() if hasattr(torch,'cuda') else 'no-runtime-gpu')"

# 3) Bake S1-mini weights at build time (~2 GB safetensors + codec + tokenizer).
#    Gated repo: the operator must accept the license once on hf.co for
#    the configured HUGGING_FACE_HUB_TOKEN before this step succeeds.
ARG HUGGING_FACE_HUB_TOKEN=
ENV HUGGING_FACE_HUB_TOKEN=${HUGGING_FACE_HUB_TOKEN}
RUN mkdir -p ${FISH_CHECKPOINT_DIR} \
    && hf download ${FISH_REPO} \
        --local-dir ${FISH_CHECKPOINT_DIR} \
        --include "*.safetensors" "*.json" "*.txt" "*.yaml" "*.yml" "*.pth" "tokenizer*" "config*" \
    && ls -la ${FISH_CHECKPOINT_DIR}/ \
    && du -sh ${FISH_CHECKPOINT_DIR}

# 4) Seed default voices at build time — soft-fail by design (HF datasets
#    audio decode can crash on aarch64 without torchcodec; we ship the
#    default_hu seed by docker cp from the legacy F5HUN volume at deploy
#    time as the real path here).
COPY fetch_default_voices.py /app/fetch_default_voices.py
RUN mkdir -p ${VOICES_SEED_DIR} \
    && (VOICES_DIR=${VOICES_SEED_DIR} python /app/fetch_default_voices.py \
        || echo "[fetch] build-time voice seeding failed — operator must docker cp <name>.{wav,txt} into /app/voices/ at runtime") \
    && ls -la ${VOICES_SEED_DIR}/ 2>&1

# 5) Shim deps + app source.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY app.py /app/app.py

EXPOSE 8080

CMD ["python", "/app/app.py"]
