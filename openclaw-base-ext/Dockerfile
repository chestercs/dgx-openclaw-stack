# Local extension of the upstream OpenClaw gateway image.
#
# Why this exists: the upstream image (ghcr.io/openclaw/openclaw) does NOT
# ship `ffmpeg`. The Discord text-channel TTS-attachment path
# (`messages.tts.auto = always`) shells out to ffmpeg to transcode the
# Kokoro/F5-TTS wav into the Opus/mp3 the Discord bot upload API wants —
# and crashes with `ffmpeg not found in trusted system directories` when
# it can't find the binary. The default workaround is
# `OPENCLAW_TTS_AUTO=tagged` which only TTS-attaches when the LLM
# explicitly tags a reply, so the text-channel flow never tries the
# ffmpeg path; that's a real feature regression though (no auto TTS for
# Discord text-channel agents).
#
# This Dockerfile takes the upstream image and apt-installs ffmpeg, so
# `OPENCLAW_TTS_AUTO=always` works end-to-end on every surface (web
# chat / Discord text / Discord voice / agent skill API). The bundled
# ffmpeg in `openclaw-tts-router` is unaffected — that stays as the
# narrow EN/HU autoroute + adelay layer; this new ffmpeg in the gateway
# image is what the Discord plugin's attachment pipeline shells out to.
#
# Three main-stack services pull from this image (config-init, gateway,
# cli — they all run the same OpenClaw codebase with different
# entrypoints), so we build it once and reuse via the `image:` tag.
ARG OPENCLAW_BASE=ghcr.io/openclaw/openclaw:latest
FROM ${OPENCLAW_BASE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
USER node
