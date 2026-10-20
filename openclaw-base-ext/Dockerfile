# The upstream OpenClaw image plus what this stack adds on top. Built once,
# used by openclaw-config-init, openclaw-gateway and openclaw-cli.
#
# Nothing here patches OpenClaw itself: the plugins use the plugin SDK and
# are loaded through plugins.load.paths, the patcher writes documented
# config only. The upstream ENTRYPOINT (tini + docker-entrypoint.mjs, which
# runs the state migration before the gateway starts) is kept as is.
ARG OPENCLAW_BASE=ghcr.io/openclaw/openclaw:latest
FROM ${OPENCLAW_BASE}

USER root
# ffmpeg: OpenClaw shells out to it to turn TTS audio into the Opus/MP3 a
# Discord upload needs, and the bot uses it for media it downloads.
# git, curl, file, procps: tools the agent's exec reaches for in coding tasks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git curl ca-certificates file procps \
    && rm -rf /var/lib/apt/lists/*

# The Discord agent's people-memory digest and its guarded writer (exec'd
# by the nightly job and by the bot). It lives in the image, not in the
# config volume, so the agent cannot edit the guard that protects
# people/MEMORY.md.
COPY people-digest.mjs /opt/people-digest.mjs

# Local plugins (people-context, claim-guard, dm-guild-gate, mcp-image-saver),
# loaded through plugins.load.paths by config-init.
COPY plugins/ /opt/openclaw-plugins/

USER node
