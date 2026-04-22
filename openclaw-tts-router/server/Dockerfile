# OpenClaw TTS router — OpenAI-compatible /v1/audio/speech passthrough.
#
# Single endpoint that fronts one or more language-specific TTS backends and
# matches the OpenAI Audio API shape so it slots into OpenClaw via
# `messages.tts.providers.openai.baseUrl` (sanctioned override per closed
# OpenClaw issues #13907 / #29224).
#
# Backends:
#   - openclaw-tts-en   (Kokoro 82M, English, mandatory, ships in this stack)
#   - openclaw-tts-f5hun (F5-TTS Hungarian, optional — wire your own and set
#                         F5HUN_URL + F5HUN_API_TOKEN in .env to activate)
#
# No GPU. ffmpeg is bundled because OpenClaw's openai TTS provider asks for
# mp3 by default (and the browser/voice surfaces sniff content-type) — we
# transcode the backend wav to mp3 / opus / aac on the fly so playback works
# regardless of which container the client requests.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY app.py /app/app.py

EXPOSE 8080

# ROUTER_API_KEY + EN_API_TOKEN are required at runtime — startup fails fast
# if either is missing. F5HUN_URL + F5HUN_API_TOKEN are optional; the router
# logs `f5hun_enabled=False` and serves EN-only when they are unset.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
