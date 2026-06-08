# Python code-execution sandbox for the dgx-openclaw-stack.
#
# Self-hosted Python execution backend that the OpenClaw agent reaches over
# MCP (config: mcp.servers.python_sandbox, transport: streamable-http). Two
# tools exposed: python_exec(code, session_id, timeout_s) and
# python_session_reset(session_id). State persists per session_id across
# calls — the agent can load a CSV, then in subsequent calls reduce, plot,
# and export it without rebuilding the dataframe each time.
#
# Architecture: one container, one uvicorn process. Kernels are managed
# in-process via jupyter_client (not a separate Jupyter Kernel Gateway
# subprocess) — saves a HTTP hop per call and keeps lifecycle
# straightforward. ipykernel children live as sibling Python processes
# under uvicorn's PID; tini reaps them on shutdown.
#
# No GPU. Image targets CPU-only data-science workloads (pandas, numpy,
# matplotlib, scikit-learn). PYTHON_SANDBOX_GPU=1 is the documented opt-in
# escape hatch for future CUDA-Python use cases — not wired in this v0.1.0.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
# Note: MPLBACKEND is intentionally NOT set to Agg. ipykernel ships with
# matplotlib_inline (the inline backend) and selects it automatically; that
# backend is what publishes figure PNGs as iopub `display_data` messages so
# the agent receives plots in `result.plots[]`. Forcing Agg via env would
# silently kill plot delivery — a `<Figure size ...>` repr would land in
# `result.result` but `plots` would be empty (verified 2026-04-26 smoke).

# tini = clean PID 1 + sigterm propagation; curl for the healthcheck;
# build-essential is needed for some scientific-Python wheels on ARM64
# slim base (scipy, numpy fall back to compiling from sdist if no wheel).
# ffmpeg (2026-06-08): the agent's "download a YouTube video → extract audio →
# transcribe" chain needs a real ffmpeg binary — yt-dlp shells out to it for
# audio extraction / format conversion, and it's apt-only (the sandbox user is
# non-root, so it cannot be installed at agent runtime). Pairs with the yt-dlp
# pip dep in requirements.txt.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini curl build-essential gfortran ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Install the kernel spec system-wide so child kernels can find it. Done
# under root before we drop privileges; the kernel spec is stored under
# /usr/local/share/jupyter/kernels/ which is world-readable.
RUN python -m ipykernel install --name=python3 --display-name='Python 3 (sandbox)'

COPY app.py kernel_pool.py /app/

# Non-root user matching the rest of the stack. The /workspace bind mount
# (set up in docker-compose.yml) is owned by 1000:1000 on the host.
RUN useradd -u 1000 -m -s /bin/bash sandbox \
    && mkdir -p /workspace \
    && chown -R sandbox:sandbox /app /workspace
USER 1000:1000

EXPOSE 8094

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8094", "--no-access-log"]
