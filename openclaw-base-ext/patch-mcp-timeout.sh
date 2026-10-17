#!/usr/bin/env bash
# Runtime dist patches for the upstream openclaw image. Runs at every
# container start as the entrypoint wrapper. See
# openclaw-base-ext/Dockerfile for the full rationale. Three active patches:
#
#   1. MCP SDK hardcoded 60s default request timeout -> env-driven.
#   3. Discord inbound mention prettify: keep the user ID visible
#      (`@Name (<@ID>)`) so the model can tag users back.
#   4. Active Memory recall-intent detection: add a Hungarian pattern.
#
# (Patch 2, the browser screenshot `[saved: <path>]` header, was retired
# with 2026.10: upstream now appends `[Screenshot saved to "<path>". …]`
# to the screenshot tool result itself.)
#
# Since 2026.10 every bundle is ESM (`.mjs`); a `--include='*.js'` grep
# silently matches nothing there, which is how patch 3 went dead after the
# 2026.10.3 upgrade. Keep every grep on JS_INCLUDES.
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
# Idempotent — a re-running script finds no `= 60000` / `= 6e4` to replace.
set -u

NEW_EXPR='(parseInt(process.env.MCP_REQUEST_TIMEOUT_MS,10)||1800000)'
NEW_LITERAL="DEFAULT_REQUEST_TIMEOUT_MSEC = ${NEW_EXPR}"
# The legacy SDK spells the default `60000`; the minified
# @modelcontextprotocol/client 2.x bundles (2026.10+) spell it `6e4`.
OLD_LITERALS='DEFAULT_REQUEST_TIMEOUT_MSEC = 60000|DEFAULT_REQUEST_TIMEOUT_MSEC = 6e4'

# Where patchable dist bundles live. /app/dist is the image's own code;
# the two config-volume roots hold INSTALLED PLUGINS, which bundle their
# own copies of the same modules — the Discord plugin
# (~/.openclaw/npm/projects/openclaw-discord-*/node_modules/@openclaw/
# discord/dist/) ships its own message-utils, and THAT copy serves the
# live inbound path (verified 2026-06-11: the /app/dist mention patch
# applied cleanly but inbound text stayed unpatched until the plugin
# copy was patched too). Config-volume patches persist across recreates;
# a plugin upgrade lands in a fresh dir and gets re-patched at the next
# container start. Missing dirs are fine (grep skips them).
PATCH_ROOTS="/app/dist /home/node/.openclaw/npm/projects /home/node/.openclaw/extensions"
JS_INCLUDES="--include=*.js --include=*.mjs --include=*.cjs"

# sed delimiter: `#` rather than `|`, because the replacement contains
# `||` (JS logical-or) which would be misparsed as the end of an `s|`
# command's replacement followed by invalid trailing flags.
patch_pass() {
    local matches found_anything=0
    matches=$(grep -rlE ${JS_INCLUDES} "${OLD_LITERALS}" /app 2>/dev/null || true)
    if [ -z "${matches}" ]; then
        return 1
    fi
    for f in ${matches}; do
        sed -i -E "s#(${OLD_LITERALS})#${NEW_LITERAL}#" "${f}"
        echo "[mcp-patch] ${f}" >&2
        found_anything=1
    done
    [ "${found_anything}" = 1 ]
}

# Phase 1 — synchronous pass.
patch_pass || echo "[mcp-patch] phase 1: nothing to patch (already done or pristine image)" >&2

# ── Patch 3: keep Discord user IDs visible in inbound mention prettify ──────
#
# Inbound Discord messages arrive with raw `<@123456…>` mentions; the
# pipeline's resolveDiscordMentions() replaces them with a readable
# `@DisplayName` using message.mentionedUsers — and throws the ID away.
# The model therefore never learns any user's snowflake, so it cannot emit
# the canonical `<@ID>` outbound mention; its plain `@Name` text only
# becomes a real ping if the mentionAliases map or the in-memory directory
# (which empties on every gateway restart) happens to know the handle.
# Operator expectation (2026-06-11): "I tagged the user in my message —
# the bot should be able to tag them back." This patch rewrites the
# replacement to `@DisplayName (<@ID>)`, so the ID rides along in the
# model-visible text (and survives in session history across restarts);
# the format-rules cheatsheet teaches the bot to reuse the `<@ID>` form.
mention_id_patch() {
    local files f
    files=$(grep -rl ${JS_INCLUDES} "mentionedUsers" ${PATCH_ROOTS} 2>/dev/null || true)
    if [ -z "${files}" ]; then
        echo "[mention-id-patch] no candidate file found under ${PATCH_ROOTS} — upstream layout changed?" >&2
        return 0
    fi
    # One anchor per upstream spelling: 2026.6.x passed a template string,
    # the 2026.10 @openclaw/discord plugin passes an arrow replacer.
    # The node helper exits 0 when a file carries (or now carries) the
    # patch, so we can warn loudly if no file matched any spelling.
    local anchored=0
    for f in ${files}; do
        if node -e '
const fs = require("fs");
const f = process.argv[1];
let s = fs.readFileSync(f, "utf8");
const pairs = [
  ["new RegExp(`<@!?${user.id}>`, \"g\"), `@${label}`)", "new RegExp(`<@!?${user.id}>`, \"g\"), `@${label} (<@${user.id}>)`)"],
  ["new RegExp(`<@!?${user.id}>`, \"g\"), () => `@${label}`)", "new RegExp(`<@!?${user.id}>`, \"g\"), () => `@${label} (<@${user.id}>)`)"],
];
let hit = false, changed = false;
for (const [anchor, patched] of pairs) {
  if (s.includes(patched)) { hit = true; continue; }
  if (s.includes(anchor)) { s = s.split(anchor).join(patched); hit = changed = true; }
}
if (changed) {
  const tmp = f + ".patch-tmp-" + process.pid;
  fs.writeFileSync(tmp, s);
  fs.renameSync(tmp, f);
  console.error("[mention-id-patch] patched " + f);
}
process.exit(hit ? 0 : 1);
' "${f}"; then
            anchored=1
        fi
    done
    if [ "${anchored}" = 0 ]; then
        echo "[mention-id-patch] WARNING: mentionedUsers found but no known replacer spelling — upstream changed, patch inactive" >&2
    fi
}
mention_id_patch

# ── Patch 4: Hungarian recall intent for Active Memory escalation ──────────
#
# Active Memory's default `escalate` mode only starts its deep-recall
# sub-agent when hasRecallIntent() matches the message. Upstream ships
# English, Spanish, Chinese, Japanese, Korean, Russian and Portuguese
# patterns — a Hungarian guild ("emlékszel?", "múltkor", "mit beszéltünk")
# never escalates, so the feature is silently inert. This prepends one
# Hungarian pattern to RECALL_INTENT_PATTERNS. Pure past-reference words
# only: future phrasing ("emlékeztess holnap") must not match.
hu_recall_intent_patch() {
    local files f anchored=0
    files=$(grep -rl ${JS_INCLUDES} "const RECALL_INTENT_PATTERNS = \[" /app/dist 2>/dev/null || true)
    if [ -z "${files}" ]; then
        echo "[hu-recall-patch] WARNING: RECALL_INTENT_PATTERNS not found under /app/dist — upstream changed, patch inactive" >&2
        return 0
    fi
    for f in ${files}; do
        if node -e '
const fs = require("fs");
const f = process.argv[1];
let s = fs.readFileSync(f, "utf8");
const marker = "/*openclaw-base-ext:hu-recall*/";
if (s.includes(marker)) process.exit(0);
const anchor = "const RECALL_INTENT_PATTERNS = [";
if (!s.includes(anchor)) process.exit(1);
const hu = String.raw`/(?<![\p{L}\p{N}_])(?:eml[ée]k(?:szel|szik|sz[üu]nk|eztek|szetek)|m[úu]ltkor|legut[óo]bb|kor[áa]bban|r[ée]gebben|tegnap(?:el[őo]tt)?|a\s+minap|tavaly|m[úu]lt\s+(?:h[ée]t|h[óo]nap|alkalom)\p{L}*|(?:\d+|egy|k[ée]t|h[áa]rom|p[áa]r|n[ée]h[áa]ny)\s+(?:napja|hete|h[óo]napja|[ée]ve)|megbesz[ée]lt[üu]k|besz[ée]lt[üu]nk|mondtad|eml[ií]tetted|d[öo]nt[öo]tt[üu]nk|megjegyezted|tudod\s+m[ée]g)(?![\p{L}\p{N}_])/iu`;
s = s.replace(anchor, anchor + "\n\t" + marker + " " + hu + ",");
const tmp = f + ".patch-tmp-" + process.pid;
fs.writeFileSync(tmp, s);
fs.renameSync(tmp, f);
console.error("[hu-recall-patch] patched " + f);
' "${f}"; then
            anchored=1
        fi
    done
    if [ "${anchored}" = 0 ]; then
        echo "[hu-recall-patch] WARNING: anchor found by grep but not patched — check the node helper" >&2
    fi
}
hu_recall_intent_patch

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

# Current upstream images migrate retained state under exclusive maintenance
# ownership in this adapter. Replacing their ENTRYPOINT must not bypass that
# activation step. Older images (including the deployed 2026.6.1) have no
# adapter, so keep their direct execution path.
if [ -f /app/docker-entrypoint.mjs ]; then
    exec node /app/docker-entrypoint.mjs "$@"
fi

# The compose service's original command lands here unchanged.
exec "$@"
