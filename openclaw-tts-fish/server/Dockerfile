# OpenClaw Fish Audio S2 Pro TTS — self-built for DGX Spark / ASUS GB10
# (Grace-Blackwell, sm_120/sm_121, aarch64, CUDA 13). Replaces the legacy
# 3-service Kokoro EN + F5-TTS HU + router pipeline with one OpenAI-compat
# /v1/audio/speech endpoint backed by SGLang-Omni serving fishaudio/s2-pro.
#
# Why this shape:
#   - Fish Audio S2 Pro (fish_qwen3_omni arch, 5B params, ~11 GB weights) is
#     officially served by the sgl-project/sglang-omni inference engine.
#     The legacy fish-speech `tools/api_server.py` targets the 1.x LLaMA2 arch
#     and does NOT load the Qwen3-omni s2-pro checkpoint.
#   - SGLang-Omni's reference Docker image (`frankleeeee/sglang-omni:dev`) is
#     amd64-only; aarch64 + Blackwell sm_120 + CUDA 13 requires a custom build
#     on a CUDA 13 devel base (cudnn + nvcc + headers, because sgl-kernel and
#     friends may need to compile from source on aarch64).
#   - Two-process container: this shim runs SGLang-Omni as a child process on
#     loopback :9090 and exposes an OpenAI-compat /v1/audio/speech facade on
#     :8080 that adds Bearer auth + voice→references mapping + onset silence
#     pad. SGLang-Omni's upstream schema takes references[].audio_path (file
#     path on disk, not inline base64) — the shim resolves voice "<name>" to
#     /app/voices/<name>.{wav,txt} at request time.
#
# License caveat: model weights pulled at build time (fishaudio/s2-pro) are
# under the Fish Audio Research License — non-commercial use only. The wrapper
# code in ./openclaw-tts-fish/server/ is MIT. By building this image you
# accept the upstream model license. For commercial deployments, swap the
# FISH_REPO build arg to a checkpoint you have a commercial license to.
FROM nvidia/cuda:13.0.0-cudnn-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    HF_HOME=/root/.cache/huggingface \
    FISH_REPO=fishaudio/s2-pro \
    FISH_CHECKPOINT_DIR=/opt/checkpoints/fish_s2_pro \
    SGLANG_OMNI_DIR=/opt/sglang-omni \
    VOICES_DIR=/app/voices \
    VOICES_SEED_DIR=/app/voices_seed \
    DEFAULT_VOICE=default_en \
    TORCH_CUDA_ARCH_LIST="8.0;12.0"

# Build tooling + runtime deps. cmake/ninja/build-essential cover the from-
# source path for sgl-kernel on aarch64 (no Blackwell aarch64 wheel published
# upstream as of 2026-05). ffmpeg + libsndfile1 cover the shim's audio I/O
# helpers (raw WAV in/out — the shim doesn't shell out to ffmpeg in the hot
# path, but soundfile depends on libsndfile1 at runtime).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-dev python3-pip \
        ffmpeg libsndfile1 \
        ninja-build build-essential cmake pybind11-dev \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3

# uv + HF CLI. We use uv for the SGLang-Omni install (it's what the upstream
# cookbook recommends and resolves the s2pro extras faster than vanilla pip).
RUN pip install "uv>=0.5" "huggingface_hub[cli]>=0.26" \
    && uv --version \
    && huggingface-cli --version

WORKDIR /app

# 1) Blackwell-aware torch + torchaudio from the cu130 wheel index FIRST.
#    SGLang-Omni's .[s2pro] extra resolves against an installed torch; with
#    torch already present from the cu130 index, the resolver picks up the
#    Blackwell-ready binary instead of pulling a PyPI default that lacks sm_120
#    kernels. Same pattern as openclaw-tts-en / openclaw-stt-whisper / vllm-llm.
RUN pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch torchaudio \
    && python -c "import torch; \
print('torch', torch.__version__, 'cuda_built', torch.version.cuda, \
'arch', torch.cuda.get_arch_list() if hasattr(torch,'cuda') else 'no-runtime-gpu')"

# 2) SGLang-Omni from source. The pip package name is unstable (`sgl-omni` vs
#    `sglang-omni` vs `sglang_omni` across releases) and the .[s2pro] extra
#    only resolves cleanly when pulled from the git repo. Pinned to main for
#    now — switch to a tag once upstream cuts a stable s2-pro release.
#
#    --no-build-isolation lets uv reuse the pre-installed torch instead of
#    fetching a fresh one; --system installs into the base Python env so
#    `python -m sglang_omni.cli.cli` resolves at runtime without activating
#    a venv. -v keeps the from-source kernel build output visible in `docker
#    build` logs (sgl-kernel can take 10-20 min on aarch64 — silent stalls
#    confuse "is it hung or just busy" diagnostics).
RUN git clone --depth 1 https://github.com/sgl-project/sglang-omni.git ${SGLANG_OMNI_DIR} \
    && cd ${SGLANG_OMNI_DIR} \
    && uv pip install --system --no-build-isolation -v ".[s2pro]" \
    && python -c "import sglang_omni; print('sglang_omni loaded from', sglang_omni.__file__)"

# 3) Bake s2-pro weights at build time (~11 GB: 2 safetensors shards + codec.pth
#    + tokenizer). Public HF repo, no token required at time of writing, but
#    we pass HUGGING_FACE_HUB_TOKEN through as a build arg so a gated mirror
#    or a token-bound rate limit works the same way.
ARG HUGGING_FACE_HUB_TOKEN=
ENV HUGGING_FACE_HUB_TOKEN=${HUGGING_FACE_HUB_TOKEN}
RUN mkdir -p ${FISH_CHECKPOINT_DIR} \
    && huggingface-cli download ${FISH_REPO} \
        --local-dir ${FISH_CHECKPOINT_DIR} \
        --include "*.safetensors" "*.json" "*.txt" "*.jinja" "tokenizer*" "*.pth" "config*" \
    && ls -la ${FISH_CHECKPOINT_DIR}/ \
    && du -sh ${FISH_CHECKPOINT_DIR}

# 4) Seed default voices at build time. The fetcher writes to
#    /app/voices_seed/; the runtime shim copies any missing files into
#    /app/voices/ on first start (without overwriting user-mounted voices
#    on the tts-fish-voices volume).
COPY fetch_default_voices.py /app/fetch_default_voices.py
RUN mkdir -p ${VOICES_SEED_DIR} \
    && VOICES_DIR=${VOICES_SEED_DIR} python /app/fetch_default_voices.py \
    && ls -la ${VOICES_SEED_DIR}/

# 5) Shim deps + app source.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY app.py /app/app.py

EXPOSE 8080

# TTS_API_TOKEN is REQUIRED at runtime — startup fails fast if missing.
CMD ["python", "/app/app.py"]
