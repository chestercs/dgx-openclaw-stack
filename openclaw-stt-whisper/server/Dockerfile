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
#   - CUDA 13 + faster-whisper >= 1.2 + a CT2 BUILT FROM SOURCE with CUDA
#     support is the proven path on aarch64. The PyPI CTranslate2 aarch64
#     wheel is CPU-only (upstream known limitation — only x86_64 has a
#     GPU-enabled wheel), so we compile from source in a CUDA-devel base
#     image. First build ~10-15 min (cmake + nvcc on ~30k LOC of C++/CUDA);
#     subsequent layer-cache hits are instant.
#   - If the speaches upstream later publishes a Blackwell-tensor-core
#     image (e.g. latest-cuda-13.x), swap `build:` back to `image:` in
#     docker-compose.yml; the wrapper is ~150 LOC and trivial to retire.
FROM nvidia/cuda:13.0.0-cudnn-devel-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    HF_HOME=/root/.cache/huggingface

# Build tooling + runtime deps. The `-devel` base already includes nvcc,
# cudnn headers, cuBLAS dev libs, and NCCL — we add cmake, gcc, git, and
# the Python toolchain the pip source builds need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-dev python3-pip \
        ffmpeg libsndfile1 \
        cmake ninja-build build-essential pybind11-dev \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3

WORKDIR /app

# 1) ARM64 + Blackwell torch + torchaudio from the cu130 wheel index FIRST so
#    the rest of the install resolves against the cu130-pinned torch.
#    faster-whisper uses torchaudio at inference for decode/resample.
RUN pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch torchaudio \
    && python -c "import torch; \
print('torch', torch.__version__, 'cuda_built', torch.version.cuda, \
'arch', torch.cuda.get_arch_list() if hasattr(torch,'cuda') else 'no-runtime-gpu')"

# 2) CTranslate2 from source, with CUDA + cuDNN on. The PyPI aarch64 wheel
#    is CPU-only AND upstream publishes no source tarball (sdist) — `pip
#    install --no-binary` therefore can't find any distribution to build.
#    We clone the release tag from github, cmake+nvcc the native lib, then
#    build and install the Python binding. CMake picks up nvcc and cudnn
#    from the cudnn-devel base and emits PTX covering Blackwell (sm_120).
#    Time budget: ~10-20 min first build, instant on cache hits.
ARG CT2_VERSION=v4.7.1
RUN git clone --depth 1 --branch ${CT2_VERSION} --recursive \
        https://github.com/OpenNMT/CTranslate2.git /tmp/ct2 \
    && cmake -S /tmp/ct2 -B /tmp/ct2/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DWITH_CUDA=ON \
        -DWITH_CUDNN=ON \
        -DWITH_MKL=OFF \
        -DOPENMP_RUNTIME=COMP \
        -DBUILD_CLI=OFF \
        -DCUDA_ARCH_LIST="80;90;100;120" \
        -DCMAKE_CUDA_ARCHITECTURES="80;90;100;120" \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -G Ninja \
    && cmake --build /tmp/ct2/build --target install -j "$(nproc)" \
    && ldconfig \
    && cd /tmp/ct2/python \
    && pip install -r install_requirements.txt \
    && python setup.py bdist_wheel \
    && pip install dist/*.whl \
    && rm -rf /tmp/ct2 \
    && python -c "import ctranslate2; print('ctranslate2', ctranslate2.__version__, 'cuda_supported=yes' if hasattr(ctranslate2,'get_cuda_device_count') else 'cuda_supported=no')"

# 3) faster-whisper + FastAPI wrapper deps. CT2 is already installed, so
#    the faster-whisper package find it pre-built and doesn't re-pull.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt \
    && python -c "import faster_whisper; print('faster_whisper', faster_whisper.__version__)"

# 4) App source. No model weights baked in — the ~3 GB large-v3 weights land
#    in the stt-whisper-hf-cache volume on first transcription request.
COPY app.py /app/app.py

EXPOSE 8080

# STT_API_TOKEN is REQUIRED at runtime — startup fails fast if missing.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
