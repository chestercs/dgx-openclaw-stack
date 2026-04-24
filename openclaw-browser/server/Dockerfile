# OpenClaw Browser — self-hosted Playwright Chromium exposed over Chrome
# DevTools Protocol, attached by OpenClaw's built-in `browser` tool via
# browser.profiles.<name>.cdpUrl.
#
# Why mcr.microsoft.com/playwright/python (and not the stock Ubuntu base
# our other Python services use): the Microsoft image ships a
# version-locked Chromium with all the system-library glue (fonts, X libs,
# audio stubs) Chromium needs even in headless mode. Reproducing that on
# a bare Ubuntu base means installing ~80 apt packages — and the version
# pin would still drift from Playwright's expectations on every refresh.
# Multiarch manifest gives us aarch64 Chromium 141 on GB10 for free.
#
# Layered installs we add on top of the base:
#   - Xvfb + x11vnc + websockify + novnc — for the 1x OAuth onboarding
#     helper (headful Chromium rendered to operator's laptop browser).
#   - Python deps (FastAPI + trafilatura + Playwright client + a couple
#     of small utilities). Playwright Python is preinstalled in the base
#     but we pin our version in requirements.txt to keep the launch-side
#     and the client-side in sync.
#
# WHY the loose `--no-cache` on apt-get and pip: this image is rebuilt
# rarely (only when bumping Playwright or Chromium); cache invalidation
# storms aren't the threat model. Reproducibility (same versions on
# every fresh build) is.
FROM mcr.microsoft.com/playwright/python:v1.58.2-noble

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BROWSER_STORAGE_DIR=/storage \
    BROWSER_PORT_BASE=9222 \
    BROWSER_API_PORT=9220

# noVNC + websockify for the headful login-helper bridge. Xvfb + x11vnc
# come from the same apt index and are stable on aarch64 (Ubuntu noble).
# tini gives us proper PID-1 signal handling (Chromium spawns helper
# processes that need clean SIGCHLD reaping).
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc \
        novnc websockify \
        tini \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Sanity check: Chromium binary on PATH (Playwright installs it during
# image build via `playwright install chromium`; verify it's present).
RUN python -c "from playwright.sync_api import sync_playwright; \
    p=sync_playwright().__enter__(); \
    print('chromium:', p.chromium.executable_path); \
    p.__exit__(None,None,None)"

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# App source
COPY app.py supervise.py extract.py blocklist.py ratelimit.py /app/

# Storage + diagnostics dirs are bind-mounted at runtime; create the
# mountpoints so the bind survives even when the volume is empty.
RUN mkdir -p /storage /diagnostics /config

# FastAPI management API on 9220. Chromium debug ports open at runtime
# in the 9222-9241 range; they're declared in docker-compose.yml.
EXPOSE 9220 9222

# tini as PID 1 → uvicorn → FastAPI. Chromium subprocesses inherit a
# clean SIGCHLD path through tini.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9220", "--workers", "1"]
