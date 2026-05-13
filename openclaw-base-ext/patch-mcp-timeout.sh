#!/usr/bin/env bash
# Patch the MCP SDK's hardcoded 60s default request timeout at container
# startup. See openclaw-base-ext/Dockerfile for the rationale.
#
# Runs as the first step of every openclaw container start (gateway,
# cli, config-init — all reuse openclaw-base-ext). Idempotent: if the
# `= 60000` literal is already replaced, the grep finds no files and the
# patch is a fast no-op. We do this at runtime rather than image build
# alone because the upstream openclaw image has BOTH a top-level
# `/app/node_modules/` SDK copy AND a `/app/dist/extensions/browser/node_modules/`
# extension-bundled copy, and observation shows the extension copy can
# be (re)populated post-build (cached layer or runtime extraction
# behaviour — verified live 2026-05-14). A startup-time `grep -rl` finds
# both unconditionally, no matter how upstream organises the tree.
#
# Exec's the original CMD/entrypoint args after patching so containers
# behave exactly as they did before this wrapper.
set -u

NEW_EXPR='(parseInt(process.env.MCP_REQUEST_TIMEOUT_MS,10)||1800000)'
OLD_LITERAL='DEFAULT_REQUEST_TIMEOUT_MSEC = 60000'
NEW_LITERAL="DEFAULT_REQUEST_TIMEOUT_MSEC = ${NEW_EXPR}"

# Find every MCP SDK protocol.js (esm + cjs across both /app/node_modules
# and /app/dist/extensions/.../node_modules locations). grep -l skips
# non-text files cheaply; the redirect drops "permission denied" noise
# on hidden directories we don't care about. We also filter to `.js` so
# the `.d.ts` declaration files (where the constant also appears) aren't
# rewritten — they're TypeScript types that aren't executed but a stray
# substitution that goes wrong there would be confusing during debug.
matches=$(grep -rl --include='*.js' "${OLD_LITERAL}" /app 2>/dev/null || true)

# IMPORTANT: sed's `s` command delimiter is `|` only if we use `|` —
# but the replacement contains `||` (JS logical-or) which would be
# misparsed as the end of the replacement followed by garbage flags.
# Use `#` as the delimiter instead, since neither the pattern nor the
# replacement contains `#`.
if [ -z "${matches}" ]; then
    echo "[mcp-patch] no unpatched protocol.js found under /app (already patched, or upstream version changed)" >&2
else
    for f in ${matches}; do
        sed -i "s#${OLD_LITERAL}#${NEW_LITERAL}#" "${f}"
        echo "[mcp-patch] ${f}" >&2
    done
fi

# Hand off to whatever the container was supposed to run. The compose
# service either sets `command:` (gateway, config-init) or `entrypoint:`
# (cli) — in either case the original args land here as "$@".
exec "$@"
