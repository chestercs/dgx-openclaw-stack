#!/usr/bin/env bash
# Patch the MCP SDK's hardcoded 60s default request timeout. Runs at
# every openclaw container start as the entrypoint wrapper. See
# openclaw-base-ext/Dockerfile for the full rationale.
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

# Hand off to whatever the container was supposed to run. The compose
# service either sets `command:` (gateway, config-init) or `entrypoint:`
# (cli) — in either case the original args land here as "$@".
exec "$@"
