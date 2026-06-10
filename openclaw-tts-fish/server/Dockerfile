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
# huggingface_hub >=1.0 renamed the CLI from `huggingface-cli` to `hf` and
# the old name is a stub that prints a deprecation note and exits 1 — we
# call the new name explicitly here and at the model download step below.
RUN pip install "uv>=0.5" "huggingface_hub[cli]>=0.26" \
    && uv --version \
    && hf --version

WORKDIR /app

# 1) Blackwell-aware torch stack from the cu130 wheel index FIRST, pinned to
#    the exact versions sglang-omni's pyproject requires (torch==2.9.1,
#    torchvision==0.24.1). On aarch64 the PyPI torch wheels are CPU-only —
#    the 2026-05-19 and 2026-06-06 builds died exactly here: the omni resolver
#    silently replaced cu130 torch with `2.9.1+cpu`, so the engine later
#    reported "CPU/No GPU detected". Pre-pinning + the index flags in step 2
#    + the re-pin in step 2c keep the GPU build in place end to end.
#
#    Wheel-naming gotcha: on this index only torch carries the `+cu130` local
#    tag for aarch64; torchvision/torchaudio publish UNSUFFIXED versions
#    (0.24.1 / 2.9.1) that are nonetheless cu130 builds. Pin them plain here
#    (sourced exclusively from the cu130 index, so they can't be the PyPI CPU
#    wheels) — `+cu130` pins on those two fail with "no matching distribution".
RUN pip install --index-url https://download.pytorch.org/whl/cu130 \
        "torch==2.9.1+cu130" "torchvision==0.24.1" "torchaudio==2.9.1" \
    && python -c "import torch; \
print('torch', torch.__version__, 'cuda_built', torch.version.cuda, \
'arch', torch.cuda.get_arch_list() if hasattr(torch,'cuda') else 'no-runtime-gpu')"

# 2a) sgl-kernel cu130 aarch64 wheel from the sgl-project whl release page.
#     The PyPI `sgl_kernel-0.3.21` aarch64 wheel is a CUDA *12* build — its
#     common_ops.abi3.so dlopen-fails inside this CUDA 13 image with
#     `libnvrtc.so.12: cannot open shared object file` (observed 2026-06-06).
#     Upstream publishes a parallel `+cu130` build on GitHub releases (this is
#     the documented install path in sglang v0.5.8's install.md for CUDA 13).
#     0.3.21 is exactly what sglang==0.5.8 (sglang-omni's pin) requires.
#     The wheel ships sm90/sm100 SASS + embedded PTX; on GB10 (sm_121) the
#     driver JIT-compiles the PTX — same mechanism the official
#     `lmsysorg/sglang:spark` image relies on. Installing it BEFORE sglang-omni
#     satisfies the `sgl-kernel==0.3.21` dep so the resolver doesn't pull the
#     broken cu12 wheel from PyPI (PEP 440: `==0.3.21` matches `0.3.21+cu130`).
RUN pip install \
    "sgl-kernel @ https://github.com/sgl-project/whl/releases/download/v0.3.21/sgl_kernel-0.3.21+cu130-cp310-abi3-manylinux2014_aarch64.whl"

# 2b) SGLang-Omni from source. The pip package name is unstable (`sgl-omni` vs
#    `sglang-omni` vs `sglang_omni` across releases) and the source install
#    only resolves cleanly when pulled from the git repo. Pinned to main for
#    now — switch to a tag once upstream cuts a stable s2-pro release. The
#    `rev-parse` line stamps the exact upstream commit into the build log so a
#    broken nightly can be identified after the fact.
#
#    No `[s2pro]` extra anymore: upstream folded the S2-Pro deps (tiktoken,
#    hydra-core, descript-audio-codec, …) into the core dependency list.
#
#    --no-build-isolation lets uv reuse the pre-installed torch instead of
#    fetching a fresh one; --system installs into the base Python env so the
#    `sgl-omni` console script resolves at runtime without activating a venv.
#    --extra-index-url + unsafe-best-match let the resolver see the cu130
#    torch wheels, so `torch==2.9.1` resolves to `2.9.1+cu130` instead of the
#    CPU-only PyPI aarch64 build (local-version tags win under best-match).
RUN git clone --depth 1 https://github.com/sgl-project/sglang-omni.git ${SGLANG_OMNI_DIR} \
    && cd ${SGLANG_OMNI_DIR} \
    && echo "sglang-omni commit: $(git rev-parse HEAD)" \
    && uv pip install --system --break-system-packages --no-build-isolation \
        --extra-index-url https://download.pytorch.org/whl/cu130 \
        --index-strategy unsafe-best-match \
        -v "." \
    && python -c "import sglang_omni; print('sglang_omni loaded from', sglang_omni.__file__)"

# 2c) Re-pin the GPU stack + fail the BUILD (not the 2am runtime) if the omni
#     resolution still swapped anything for a CPU/cu12 build. `--no-deps`
#     keeps this surgical — only the four packages move, nothing else
#     re-resolves. `--force-reinstall` is mandatory, not paranoia: the aarch64
#     torchvision/torchaudio wheels on the cu130 index are version-identical
#     to the PyPI CPU builds (no `+cu130` local tag — see step 1), so a plain
#     `pip install` would report "already satisfied" and happily keep a CPU
#     wheel the omni resolve smuggled in.
#     sgl_kernel itself can't be imported here (its loader dlopens libcuda,
#     which only exists at runtime via the NVIDIA container toolkit), so the
#     check asserts on package metadata instead of an import.
RUN pip install --no-deps --force-reinstall --index-url https://download.pytorch.org/whl/cu130 \
        "torch==2.9.1+cu130" "torchvision==0.24.1" "torchaudio==2.9.1" \
    && pip install --no-deps --force-reinstall \
        "sgl-kernel @ https://github.com/sgl-project/whl/releases/download/v0.3.21/sgl_kernel-0.3.21+cu130-cp310-abi3-manylinux2014_aarch64.whl" \
    && python -c "import torch, pathlib, importlib.metadata as md; \
assert torch.version.cuda and torch.version.cuda.startswith('13'), f'torch {torch.__version__} is not a cu13 build'; \
d = md.distribution('sgl-kernel'); du = d.read_text('direct_url.json'); \
assert du and 'cu130' in du, f'sgl-kernel not installed from the cu130 wheel (direct_url={du})'; \
p = pathlib.Path(str(d.locate_file('sgl_kernel'))); \
print('build-verify OK: torch', torch.__version__, '| sgl-kernel', md.version('sgl-kernel'), \
'variants', sorted(x.name for x in p.iterdir() if x.name.startswith('sm') or 'common_ops' in x.name))"
# ^ provenance check via direct_url.json, NOT the version string: the +cu130
#   wheel's internal metadata says plain `0.3.21` (the local tag exists only in
#   the filename), so `md.version()` can't tell the cu12 PyPI wheel from the
#   cu130 release wheel. pip records the install-source URL for direct-URL
#   installs — that string is the only reliable discriminator. (PyPI installs
#   have no direct_url.json at all, so a swapped wheel fails the assert too.)

# 3) Bake s2-pro weights at build time (~11 GB: 2 safetensors shards + codec.pth
#    + tokenizer). Public HF repo, no token required at time of writing, but
#    we pass HUGGING_FACE_HUB_TOKEN through as a build arg so a gated mirror
#    or a token-bound rate limit works the same way.
ARG HUGGING_FACE_HUB_TOKEN=
ENV HUGGING_FACE_HUB_TOKEN=${HUGGING_FACE_HUB_TOKEN}
RUN mkdir -p ${FISH_CHECKPOINT_DIR} \
    && hf download ${FISH_REPO} \
        --local-dir ${FISH_CHECKPOINT_DIR} \
        --include "*.safetensors" "*.json" "*.txt" "*.jinja" "tokenizer*" "*.pth" "config*" \
    && ls -la ${FISH_CHECKPOINT_DIR}/ \
    && du -sh ${FISH_CHECKPOINT_DIR}

# 4) Seed default voices from the repo-bundled reference library. The seed
#    dir is copied into /app/voices/ by the runtime shim on first start
#    (without overwriting user-mounted voices on the tts-fish-voices volume).
#
#    Why bundled files instead of the old HF-dataset fetcher: the fetcher
#    (fetch_default_voices.py, removed 2026-06-10) soft-failed on every
#    aarch64 build — `datasets` audio decoding needs torchcodec, which has no
#    aarch64 wheel — so images shipped with an EMPTY seed dir and every
#    deployment started life with zero voices until an operator docker-cp'd
#    clips in. Bundled clips make builds deterministic and offline-safe.
#
#    Provenance (safe to redistribute):
#      default_en / bella / nicole / michael / fenrir / emma — synthesized
#        with Kokoro 82M (Apache-2.0) from the retired openclaw-tts-en
#        service; ~12-18 s reads of a fixed pangram-style paragraph.
#      default_hu — public-domain LibriVox recording via the
#        KTH/hungarian-single-speaker-tts dataset (the same clip the legacy
#        F5-TTS HU service used as its reference voice).
#    Each <name>.wav must have a matching <name>.txt holding its exact
#    transcript — S2 Pro voice cloning conditions on both.
COPY voices/ ${VOICES_SEED_DIR}/
RUN ls -la ${VOICES_SEED_DIR}/

# 5) Shim deps + app source + pipeline configs.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# GB10-calibrated SGLang-Omni pipeline config is the image default (see the
# comments inside the yaml for why each override exists — short version: the
# upstream defaults size the KV pool for a dedicated datacenter GPU and
# torch-compile/CUDA-graph-capture at startup, which on a shared 128 GB
# unified-memory box wedged the first PoC run into swap before ready).
# Override per deployment via FISH_S2PRO_CONFIG (compose passes it through).
COPY configs/ /opt/configs/
ENV FISH_S2PRO_CONFIG=/opt/configs/s2pro_tts_gb10.yaml

# 6) Build-time patch: low-memory audio-decoder load. Upstream
#    load_audio_decoder() (models/fishaudio_s2_pro/bootstrap.py) calls a bare
#    `FishQwen3OmniForCausalLM.from_pretrained(checkpoint_dir, config=config)`
#    just to extract `.audio_decoder` — the HF legacy load path materializes
#    the FULL ~5B model with fp32 random-init weights (~20 GB), then layers
#    the ~11 GB checkpoint on top, then `.to(bf16)` copies it AGAIN: a
#    ~30-40 GB transient CPU-RAM spike for a component worth ~2 GB. On a
#    128 GB unified-memory box sharing RAM with vLLM that spike is exactly
#    the available headroom -> direct-reclaim livelock; this wedged the
#    WHOLE HOST twice on 2026-06-10 (frozen userspace for 1.5 h) before the
#    cause was isolated. `torch_dtype=bf16 + low_cpu_mem_usage=True` makes
#    from_pretrained meta-init + stream the checkpoint: peak ~= final size.
#    Same pattern as the vllm-llm gemma4 loader patch: patch the installed
#    site-packages copy, fail the build loudly if upstream moved the line.
RUN BOOTSTRAP=/usr/local/lib/python3.12/dist-packages/sglang_omni/models/fishaudio_s2_pro/bootstrap.py \
    && grep -q "FishQwen3OmniForCausalLM.from_pretrained(checkpoint_dir, config=config)" "$BOOTSTRAP" \
        || { echo "PATCH TARGET MISSING — upstream bootstrap.py changed; re-derive the low-mem patch" >&2; exit 1; } \
    && sed -i 's/FishQwen3OmniForCausalLM.from_pretrained(checkpoint_dir, config=config)/FishQwen3OmniForCausalLM.from_pretrained(checkpoint_dir, config=config, torch_dtype=torch.bfloat16, low_cpu_mem_usage=True)/' "$BOOTSTRAP" \
    && grep -q "low_cpu_mem_usage=True" "$BOOTSTRAP" \
    && python -c "import ast,sys; ast.parse(open('/usr/local/lib/python3.12/dist-packages/sglang_omni/models/fishaudio_s2_pro/bootstrap.py').read()); print('low-mem decoder-load patch applied + parses')"

COPY app.py /app/app.py

EXPOSE 8080

# TTS_API_TOKEN is REQUIRED at runtime — startup fails fast if missing.
CMD ["python", "/app/app.py"]
