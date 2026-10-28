# Runtime for Strata's DeepSeek-V4.1 engine (strata-dsv41) and its API server (compose/strata.yml).
# The engine is built on the host from a Strata checkout (README.md: "Build the engine"); this image only
# adds the Python the server needs, pinned by that checkout's requirements.txt (the strata-src build
# context). CUDA comes from the host toolkit mount, the driver from the NVIDIA runtime. Ubuntu 24.04 like
# DGX OS, so the host-built binary finds its glibc.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PATH=/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv libgomp1 libatomic1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=strata-src requirements.txt /tmp/requirements.txt
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /strata
