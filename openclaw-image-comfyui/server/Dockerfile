# Image-generation MCP bridge for the dgx-openclaw-stack.
#
# Thin Python service that the OpenClaw agent reaches over MCP
# (config: mcp.servers.comfyui_image, transport: streamable-http) to drive
# the operator's existing ComfyUI install. Three tools exposed:
# comfyui_image__generate, comfyui_image__list_workflows, comfyui_image__cancel.
#
# Architecture: pure HTTP wrapper. No GPU, no torch, no model weights baked
# in — all heavy lifting happens inside the user's separate ComfyUI compose
# project, reached over host-gateway. The bridge serializes calls by default
# (asyncio.Lock) so concurrent generation can't pile up on the GB10 GPU
# while vLLM is also serving tokens.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# tini = clean PID 1 + sigterm propagation; curl for the HEALTHCHECK probe.
# ffmpeg is here as a future-use lever for the video extension (Path A
# canvas wrapper: an mp4 stamped through a remux pass would be even more
# reliably browser-playable). The bridge's video path today does NOT
# invoke ffprobe/ffmpeg in the request path — bind-time width/height/
# duration is accurate enough — but having the binary in the image
# means an operator can debug a misbehaving mp4 with `docker exec` +
# ffprobe without rebuilding. Adds ~50 MB to the image, worth it.
# No build-essential needed — httpx + pillow ship wheels for ARM64 + amd64.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY app.py comfy_client.py workflow_loader.py /app/
COPY workflows /app/workflows

# Non-root user matching the rest of the stack. No bind mounts that need
# write access — the bridge only reads workflows/ (mounted ro from host
# in compose) and pipes ComfyUI bytes through memory.
RUN useradd -u 1000 -m -s /bin/bash bridge \
    && chown -R bridge:bridge /app
USER 1000:1000

EXPOSE 9095

ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9095", "--no-access-log"]
