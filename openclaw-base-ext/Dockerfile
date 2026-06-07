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
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       build-essential procps file curl ca-certificates git sudo \
    && rm -rf /var/lib/apt/lists/*

# Install Homebrew (Linuxbrew) so the WebUI's "manual MCP install" buttons
# can shell out to `brew install <pkg>` in the gateway / cli containers.
# Without it, the WebUI shows skills as "blocked: brew not installed" for
# every Homebrew-only CLI dep (gog/gogcli, plus any other tap-distributed
# tool the operator wants).
#
# NONINTERACTIVE=1 + CI=1 skip the brew install script's TTY prompts.
# The script auto-creates a `linuxbrew` user and unpacks into
# `/home/linuxbrew/.linuxbrew/`. We then symlink the `brew` binary into
# `/usr/local/bin/` (already on PATH for the node user) so skills don't
# need to know the brew home path.
#
# Size cost: ~400 MB extra layer (brew core + ruby + the cellar
# bootstrap). Cached after first build, so subsequent
# `docker compose build --no-cache openclaw-config-init` rebuilds re-use
# this layer unless this RUN changes.
RUN useradd -m -s /bin/bash linuxbrew \
    && echo "linuxbrew ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

USER linuxbrew
RUN NONINTERACTIVE=1 CI=1 /bin/bash -c \
    "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

USER root
# Install the brew wrapper at /usr/local/bin/brew (NOT a symlink — a symlink
# would make Homebrew detect /usr/local as its prefix and abort on aarch64
# with "Intel default prefix" error). The wrapper sets HOMEBREW_PREFIX
# explicitly and adds steipete/tap → openclaw/tap migration fallback for
# OpenClaw skill installs with stale formula paths.
COPY brew-wrapper.sh /usr/local/bin/brew
RUN chmod +x /usr/local/bin/brew \
    && ln -sf /home/linuxbrew/.linuxbrew/Homebrew/Library /home/linuxbrew/.linuxbrew/Library \
    && chmod -R o+rx /home/linuxbrew \
    && usermod -aG linuxbrew node \
    && chmod -R g+rwx /home/linuxbrew/.linuxbrew \
    && find /home/linuxbrew/.linuxbrew -type d -exec chmod g+s {} \;

# Pre-tap both repos at image-build time so the first WebGUI install doesn't
# pay the tap-clone cost (~5 s each) and `brew search` works immediately.
USER linuxbrew
RUN /home/linuxbrew/.linuxbrew/bin/brew tap openclaw/tap \
    && /home/linuxbrew/.linuxbrew/bin/brew tap steipete/tap \
    && /home/linuxbrew/.linuxbrew/bin/brew --version

USER root
# HOMEBREW_PREFIX must be on the runtime env too — some skill install scripts
# spawn brew without inheriting from the wrapper's exec env. Belt-and-braces.
ENV PATH="/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:${PATH}" \
    HOMEBREW_PREFIX=/home/linuxbrew/.linuxbrew \
    HOMEBREW_CELLAR=/home/linuxbrew/.linuxbrew/Cellar \
    HOMEBREW_REPOSITORY=/home/linuxbrew/.linuxbrew/Homebrew \
    HOMEBREW_NO_ANALYTICS=1 \
    HOMEBREW_NO_AUTO_UPDATE=1 \
    HOMEBREW_NO_ENV_HINTS=1

# Patch the MCP SDK's hardcoded 60-second default request timeout.
#
# The `@modelcontextprotocol/sdk` ships with `DEFAULT_REQUEST_TIMEOUT_MSEC =
# 60000` in `shared/protocol.js`. OpenClaw's MCP runtime calls
# `session.client.callTool({name, arguments})` WITHOUT a third options
# argument (verified live 2026-05-14 by grepping `pi-bundle-mcp-runtime-*.js`),
# so the timeout falls through to the SDK constant unchanged. Any tool that
# takes longer than 60 s — LTX-Video cold-cache renders (~130 s), FLUX-Krea
# cold-cache (~90 s), large image gens at high resolution, future high-res
# 4K video renders that can run multiple minutes — surfaces `MCP error -32001:
# Request timed out` to the agent BEFORE the bridge can return the result,
# even though the underlying render usually succeeds on disk.
#
# The right fix is upstream (have OpenClaw pass `options.timeout` per server,
# or honor `mcp.servers.<name>.requestTimeoutMs` if added to the schema).
# Until that lands, our `patch-mcp-timeout.sh` entrypoint wrapper runs at
# every container start, grep-finds every `DEFAULT_REQUEST_TIMEOUT_MSEC =
# 60000` literal under `/app`, and sed-replaces it with
# `(parseInt(process.env.MCP_REQUEST_TIMEOUT_MS,10)||1800000)` — env-driven
# at runtime, defaults to 30 minutes.
#
# Why a runtime hook rather than a build-time RUN: empirical observation
# (2026-05-14) showed the upstream openclaw image has BOTH a top-level
# `/app/node_modules/...` SDK copy AND a `/app/dist/extensions/browser/node_modules/...`
# extension-bundled copy, and a build-time `find` only matched one of the
# locations — the other got (re)populated later in image layering or at
# runtime, leaving its 60s default in place. Starting the patch at runtime
# via `ENTRYPOINT` finds and rewrites both unconditionally.
#
# Idempotent: a restart on an already-patched container finds no `60000`
# literal to replace and exits clean in ~50ms. Scope reminder: this
# affects ALL MCP tool calls from the gateway — comfyui_image__generate(_video),
# python_sandbox, browser, etc. A 30-minute ceiling is overhead-only for
# fast calls; for genuinely-hung tools it delays the error surface by ~29
# minutes vs the previous default. The tradeoff favors long-running media
# generators.
COPY patch-mcp-timeout.sh /opt/patch-mcp-timeout.sh
RUN chmod +x /opt/patch-mcp-timeout.sh

# Wrap the upstream image's entrypoint via a JSON-form ENTRYPOINT so any
# `command:` from compose services is passed through verbatim. The
# upstream openclaw image declares no ENTRYPOINT (just CMD), so this
# overrides cleanly without losing default behaviour.
ENTRYPOINT ["/opt/patch-mcp-timeout.sh"]

USER node
