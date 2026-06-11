#!/usr/bin/env bash
# Runtime dist patches for the upstream openclaw image. Runs at every
# container start as the entrypoint wrapper. See
# openclaw-base-ext/Dockerfile for the full rationale. Two patches:
#
#   1. MCP SDK hardcoded 60s default request timeout -> env-driven.
#   2. Browser screenshot tool result: append a `[saved: <path>]` header
#      line so the model learns WHERE each screenshot landed.
#
# Two-phase strategy:
#
#   Phase 1 (foreground, blocking): patches every file present right now.
#     The /app/node_modules/ SDK copy is always there from the image
#     build.
#
#   Phase 2 (background, ~30s sweep): catches files that openclaw
#     extracts LATER during its own startup. Verified live 2026-05-14:
#     /app/dist/extensions/browser/node_modules/@modelcontextprotocol/
#     ... appears AFTER the entrypoint runs (mtimes match container
#     start, not image build). A single-pass patch at entrypoint time
#     misses these, so we re-check periodically for ~30s and exit
#     early once everything's quiet.
#
# Idempotent — a re-running script finds no `= 60000` to replace.
set -u

NEW_EXPR='(parseInt(process.env.MCP_REQUEST_TIMEOUT_MS,10)||1800000)'
OLD_LITERAL='DEFAULT_REQUEST_TIMEOUT_MSEC = 60000'
NEW_LITERAL="DEFAULT_REQUEST_TIMEOUT_MSEC = ${NEW_EXPR}"

# sed delimiter: `#` rather than `|`, because the replacement contains
# `||` (JS logical-or) which would be misparsed as the end of an `s|`
# command's replacement followed by invalid trailing flags.
patch_pass() {
    local matches found_anything=0
    matches=$(grep -rl --include='*.js' "${OLD_LITERAL}" /app 2>/dev/null || true)
    if [ -z "${matches}" ]; then
        return 1
    fi
    for f in ${matches}; do
        sed -i "s#${OLD_LITERAL}#${NEW_LITERAL}#" "${f}"
        echo "[mcp-patch] ${f}" >&2
        found_anything=1
    done
    [ "${found_anything}" = 1 ]
}

# Phase 1 — synchronous pass.
patch_pass || echo "[mcp-patch] phase 1: nothing to patch (already done or pristine image)" >&2

# ── Patch 2: surface the browser screenshot's saved file path to the model ──
#
# The gateway saves every `browser screenshot` PNG under
# ~/.openclaw/media/browser/<uuid>.png, vision-describes it, and returns
# ONLY the text description to the model — the path travels in the tool
# result's `details`, which the model never sees. The agent therefore
# cannot attach the screenshot (`MEDIA:<path>` reply line) without an
# exec-ls side channel, and live runs (2026-06-11 03:21) showed Gemma
# skipping that step and HALLUCINATING paths (it copied the 16-hex id of
# the SECURITY-NOTICE wrapper as a filename -> "Media failed" on Discord).
#
# This patch appends `[saved: <path>]` to the vision header lines of the
# screenshot tool result, so every screenshot's real path is in the
# model-visible text. Anchored on the unique `[analyzed by ...]`
# headerLines template in the bundled plugin-service; the inject is
# guarded with `typeof screenshotPath` so a file that matches the anchor
# but lacks the variable degrades to a no-op instead of a ReferenceError.
# Idempotent (includes-check); loud warning when the anchor vanishes
# after an upstream bump — the skill's exec-ls fallback keeps working,
# this patch just removes the failure mode.
shot_path_patch() {
    local files f
    files=$(grep -rl --include='*.js' 'media image understanding' /app/dist 2>/dev/null || true)
    if [ -z "${files}" ]; then
        echo "[shot-path-patch] no candidate file found under /app/dist — upstream layout changed?" >&2
        return 0
    fi
    for f in ${files}; do
        node -e '
const fs = require("fs");
const f = process.argv[1];
let s = fs.readFileSync(f, "utf8");
const anchor = "\"media image understanding\"}]`];";
const inject = " if (typeof screenshotPath === \"string\" && screenshotPath && typeof headerLines !== \"undefined\") headerLines.push(\"[saved: \" + screenshotPath + \"]\");";
if (s.includes(inject)) { process.exit(0); }
const i = s.indexOf(anchor);
if (i < 0) { console.error("[shot-path-patch] anchor not found in " + f + " — skipped (upstream changed?)"); process.exit(0); }
s = s.slice(0, i + anchor.length) + inject + s.slice(i + anchor.length);
fs.writeFileSync(f, s);
console.error("[shot-path-patch] patched " + f);
' "${f}"
    done
}
shot_path_patch

# Phase 2 — background sweep. Detach so exec below replaces this
# process while the loop continues as an orphan (re-parented to PID 1).
# Loop exits as soon as a full sweep finds nothing AND no new files
# appeared since the last check — typically converges in ~6-10 s on
# GB10, but we cap at ~30 s as defense against very slow startups.
(
    sleep 2
    quiet_passes=0
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if patch_pass; then
            quiet_passes=0
        else
            quiet_passes=$((quiet_passes + 1))
            if [ "${quiet_passes}" -ge 2 ]; then
                echo "[mcp-patch] phase 2: no more unpatched files; sweep done" >&2
                exit 0
            fi
        fi
        sleep 3
    done
    echo "[mcp-patch] phase 2: 30s sweep complete (loop hit max iterations)" >&2
) &

# Put the config-volume bin dir on PATH so the `!`-bash Discord directive
# can resolve short command names like `img` (-> ~/.openclaw/bin/img, the
# patcher-written image-gen script). The directive runs `bash --noprofile
# --norc -c "<cmd>"` (verified in the gateway's shell-utils), so it sources
# NO profile/rc — the only PATH it sees is the one the gateway process
# inherits from here. Appended (not prepended) so a script in the volume
# can never shadow a system binary. Harmless in config-init / cli too.
export PATH="${PATH}:/home/node/.openclaw/bin"

# Hand off to whatever the container was supposed to run. The compose
# service either sets `command:` (gateway, config-init) or `entrypoint:`
# (cli) — in either case the original args land here as "$@".
exec "$@"
