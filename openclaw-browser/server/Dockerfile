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
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

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

WORKDIR /app

# The mcr.microsoft.com/playwright/python image bundles Python + the
# browser binaries (under /ms-playwright/) but does NOT preinstall the
# `playwright` PyPI package itself — that's our job. Install our pinned
# requirements (which pulls in `playwright==1.58.0`), then run
# `playwright install --with-deps chromium` defensively in case the
# pre-baked browser binaries aren't where Playwright Python expects
# them. Re-running `playwright install` is a no-op when a matching
# binary is already present, so this is cheap and idempotent.
COPY requirements.txt /app/requirements.txt
# `--ignore-installed` because Ubuntu Noble's apt-installed Python
# packages (typing_extensions, pytz, etc., dragged in by the
# python3-novnc + python3-oslo-* dependency chain) lack pip RECORD
# files and pip refuses to uninstall them. We're inside a container,
# so layering pip's newer versions over them is safe — the apt copies
# stay on disk but Python's import resolution prefers
# /usr/local/lib (pip's target) over /usr/lib (apt's target).
RUN pip install --break-system-packages --ignore-installed -r /app/requirements.txt \
    && python -m playwright install chromium \
    && python -c "from playwright.sync_api import sync_playwright; \
        p=sync_playwright().__enter__(); \
        print('chromium:', p.chromium.executable_path); \
        p.__exit__(None,None,None)"

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
