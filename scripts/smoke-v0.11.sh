#!/usr/bin/env bash
# smoke-v0.11.sh — verify the v0.11.0 stack is fully wired end-to-end.
#
# Run this on the deploy host (not from an MCP session). Exit code 0 if
# everything passes, non-zero if any check fails. Each check prints a
# one-line PASS/FAIL/SKIP with a hint pointing at the right doc when
# something's off.
#
# Designed to be re-run safely after any redeploy / config change. No
# state mutation; only docker exec readonly probes + curl + filesystem
# stat.
#
# Usage:
#   ./scripts/smoke-v0.11.sh [--verbose]
#
#   --verbose    Print all output, not just the PASS/FAIL summary.
#
# Cross-reference: docs/reference/chat-surface-capability-matrix.md and
# docs/reference/media-bridge-checklist.md document the underlying
# expectations this script exercises.
set -uo pipefail

# ─── Setup ───────────────────────────────────────────────────────────
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found. Run from a clone with bootstrap.sh completed." >&2
    exit 2
fi

VERBOSE=0
[[ "${1:-}" == "--verbose" ]] && VERBOSE=1

# Source key env vars (avoid `source` to keep the script's namespace clean)
# Explicit empty is honored: if `CONTAINER_NAME_PREFIX=` (no value) is in
# .env the user has chosen bare container names; default `dgx-` only kicks
# in when the key is genuinely absent.
if grep -q '^CONTAINER_NAME_PREFIX=' "$ENV_FILE"; then
    PROJ=$(grep '^CONTAINER_NAME_PREFIX=' "$ENV_FILE" | cut -d= -f2- | head -1)
else
    PROJ=dgx-
fi
TTS_AUTO_ENV=$(grep '^OPENCLAW_TTS_AUTO=' "$ENV_FILE" | cut -d= -f2- | head -1)
TTS_AUTO_ENV=${TTS_AUTO_ENV:-always}
IMAGE_GEN_TOKEN=$(grep '^IMAGE_GEN_API_TOKEN=' "$ENV_FILE" | cut -d= -f2- | head -1)
COMFYUI_VIEW_TOKEN=$(grep '^COMFYUI_VIEW_TOKEN=' "$ENV_FILE" | cut -d= -f2- | head -1)
OPENCLAW_CONFIG_DIR=$(grep '^OPENCLAW_CONFIG_DIR=' "$ENV_FILE" | cut -d= -f2- | head -1)
OPENCLAW_CONFIG_DIR=${OPENCLAW_CONFIG_DIR:-/opt/dgx-openclaw/openclaw-config}

# ANSI colors (nullable when not a TTY)
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; RESET='\033[0m'; BOLD='\033[1m'
else
    GREEN=''; RED=''; YELLOW=''; CYAN=''; RESET=''; BOLD=''
fi

PASSED=0; FAILED=0; SKIPPED=0
FAIL_LINES=()

# ─── Helpers ─────────────────────────────────────────────────────────
pass()    { echo -e "${GREEN}✓ PASS${RESET} $1"; PASSED=$((PASSED+1)); }
fail()    { echo -e "${RED}✗ FAIL${RESET} $1"; if [[ -n "${2:-}" ]]; then echo -e "      ${YELLOW}hint:${RESET} $2"; fi; FAILED=$((FAILED+1)); FAIL_LINES+=("$1"); }
skip()    { echo -e "${YELLOW}- SKIP${RESET} $1${2:+ (}${2:-}${2:+)}"; SKIPPED=$((SKIPPED+1)); }
section() { echo -e "\n${BOLD}${CYAN}━━ $1 ━━${RESET}"; }

# Run a docker exec readonly probe and capture output. Returns the
# command's exit code; output goes to $LAST_OUTPUT.
LAST_OUTPUT=""
docker_exec() {
    local container=$1; shift
    LAST_OUTPUT=$(docker exec "$container" "$@" 2>&1)
    return $?
}

# Check container is running. PASSes silently, FAILs with hint.
require_running() {
    local container=$1
    if docker ps --filter "name=^${container}$" --format '{{.Names}}' | grep -q "^${container}$"; then
        return 0
    else
        fail "container ${BOLD}${container}${RESET} not running" \
             "docker compose up -d ${container#${PROJ}}"
        return 1
    fi
}

# ─── Section 1: openclaw-base-ext + ffmpeg ──────────────────────────
section "openclaw-base-ext (v0.11.0 ffmpeg-augmented gateway)"

if require_running "${PROJ}openclaw-gateway"; then
    docker_exec "${PROJ}openclaw-gateway" sh -c 'command -v ffmpeg && ffmpeg -version | head -1'
    if [[ "$LAST_OUTPUT" == *"ffmpeg version"* ]]; then
        pass "ffmpeg available in gateway: $(echo "$LAST_OUTPUT" | tail -1)"
    else
        fail "ffmpeg NOT in gateway image" \
             "rebuild openclaw-base-ext: docker compose build openclaw-config-init && docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli"
    fi
fi

# ─── Section 2: messages.tts.auto in openclaw.json ─────────────────
section "TTS auto-attach mode (patcher step 11)"

if require_running "${PROJ}openclaw-cli"; then
    docker_exec "${PROJ}openclaw-cli" node -e '
        const j = JSON.parse(require("fs").readFileSync("/home/node/.openclaw/openclaw.json", "utf8"));
        console.log(j.messages?.tts?.auto ?? "<unset>");
    '
    AUTO_VAL=$(echo "$LAST_OUTPUT" | tail -1 | tr -d '[:space:]')
    if [[ "$AUTO_VAL" == "$TTS_AUTO_ENV" ]]; then
        pass "messages.tts.auto = ${BOLD}$AUTO_VAL${RESET} (matches OPENCLAW_TTS_AUTO env)"
    else
        fail "messages.tts.auto = '$AUTO_VAL' but env says '$TTS_AUTO_ENV'" \
             "force-recreate openclaw-config-init to re-run patcher step 11"
    fi
fi

# ─── Section 3: patcher steps 20-22 (Discord ack/reactions) ────────
section "Discord ack patches (steps 20-22)"

if require_running "${PROJ}openclaw-cli"; then
    docker_exec "${PROJ}openclaw-cli" node -e '
        const j = JSON.parse(require("fs").readFileSync("/home/node/.openclaw/openclaw.json", "utf8"));
        const d = j.channels?.discord;
        if (!d) { console.log("DISCORD_NOT_CONFIGURED"); process.exit(0); }
        console.log("ackReactionScope=" + (d.ackReactionScope ?? "<unset>"));
        console.log("actions.reactions=" + (d.actions?.reactions ?? "<unset>"));
        const routes = j.agents?.routes ?? [];
        const discordRoutes = routes.filter(r => r?.match?.channel === "discord");
        console.log("discord_routes=" + discordRoutes.length);
        for (const r of discordRoutes) {
            const agent = (j.agents?.list ?? []).find(a => a?.id === r.agentId);
            const allow = agent?.tools?.alsoAllow ?? [];
            console.log(`agent[${r.agentId}].alsoAllow=${allow.join(",")}`);
        }
    '

    if [[ "$LAST_OUTPUT" == *"DISCORD_NOT_CONFIGURED"* ]]; then
        skip "Discord channel not configured (channels.discord absent in openclaw.json)" \
             "if you don't use Discord, this is fine"
    else
        if echo "$LAST_OUTPUT" | grep -q "ackReactionScope=off"; then
            pass "step 20: channels.discord.ackReactionScope = off (issue #46024 defended)"
        else
            fail "step 20: ackReactionScope NOT off" \
                 "force-recreate openclaw-config-init"
        fi

        if echo "$LAST_OUTPUT" | grep -q "actions.reactions=true"; then
            pass "step 21: channels.discord.actions.reactions = true"
        else
            fail "step 21: actions.reactions NOT true" \
                 "check OPENCLAW_DISCORD_ACTIONS_REACTIONS in .env"
        fi

        if echo "$LAST_OUTPUT" | grep -qE "alsoAllow=.*group:messaging"; then
            pass "step 22: discord-routed agent has alsoAllow contains group:messaging"
        elif echo "$LAST_OUTPUT" | grep -q "discord_routes=0"; then
            skip "step 22: no discord-routed agent (agents.routes[] has no channel=discord match)" \
                 "if you haven't run \`openclaw channels add --channel discord\` yet, this is expected"
        else
            fail "step 22: discord-routed agent missing group:messaging in alsoAllow" \
                 "force-recreate openclaw-config-init"
        fi
    fi
fi

# ─── Section 4: canvas dir + perms (patcher step 23) ───────────────
section "Path A canvas directory (patcher step 23)"

if [[ -d "${OPENCLAW_CONFIG_DIR}/canvas" ]]; then
    PERMS=$(stat -c '%a' "${OPENCLAW_CONFIG_DIR}/canvas")
    OWNER=$(stat -c '%u:%g' "${OPENCLAW_CONFIG_DIR}/canvas")
    if [[ "$PERMS" == "755" ]] || [[ "$PERMS" == "775" ]]; then
        pass "canvas dir exists at ${OPENCLAW_CONFIG_DIR}/canvas (perms=$PERMS, owner=$OWNER)"
    else
        fail "canvas dir perms wrong: $PERMS (expected 755 or 775)" \
             "chmod 755 ${OPENCLAW_CONFIG_DIR}/canvas"
    fi
else
    fail "canvas dir does NOT exist at ${OPENCLAW_CONFIG_DIR}/canvas" \
         "force-recreate openclaw-config-init (step 23 mkdirs it idempotently)"
fi

# ─── Section 5: image-gen bridge state ─────────────────────────────
section "Image-gen MCP bridge"

if [[ -z "$IMAGE_GEN_TOKEN" ]]; then
    skip "IMAGE_GEN_API_TOKEN not set in .env" \
         "if you don't use image-gen, this is fine; otherwise run bootstrap.sh"
else
    if docker ps --filter "name=^${PROJ}openclaw-image-comfyui$" --format '{{.Status}}' | grep -q "healthy"; then
        pass "openclaw-image-comfyui container healthy"

        # MCP tools/list — Bearer-protected
        TOOLS_LIST=$(docker exec "${PROJ}openclaw-image-comfyui" sh -c \
            'curl -sS -X POST http://127.0.0.1:9095/mcp \
              -H "Authorization: Bearer $IMAGE_GEN_API_TOKEN" \
              -H "Content-Type: application/json" \
              -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}"' 2>&1)
        if echo "$TOOLS_LIST" | grep -q '"name":"generate"'; then
            pass "MCP tools/list returns 3 bare tool names (generate, list_workflows, cancel)"
        else
            fail "MCP tools/list missing expected tools" \
                 "check bridge logs: docker logs ${PROJ}openclaw-image-comfyui"
        fi

        # /healthz — unauth'd
        HEALTHZ=$(docker exec "${PROJ}openclaw-image-comfyui" curl -sS http://127.0.0.1:9095/healthz 2>&1)
        if [[ "$HEALTHZ" == ok* ]]; then
            pass "/healthz returns ok ($HEALTHZ)"
        else
            fail "/healthz didn't return 'ok'" "got: $HEALTHZ"
        fi
    else
        skip "openclaw-image-comfyui not running (separate compose, opt-in)" \
             "docker compose -f openclaw-image-comfyui/docker-compose.yml --profile image-gen up -d"
    fi
fi

# ─── Section 6: ComfyUI reachability (host-gateway) ────────────────
section "ComfyUI proxy reachability"

if [[ -z "$IMAGE_GEN_TOKEN" ]]; then
    skip "image-gen disabled, skipping ComfyUI probe"
elif ! docker ps --filter "name=^${PROJ}openclaw-image-comfyui$" --format '{{.Names}}' | grep -q .; then
    skip "openclaw-image-comfyui not running"
else
    SYSTEM_STATS=$(docker exec "${PROJ}openclaw-image-comfyui" curl -sS \
        -m 5 http://host.docker.internal:13036/system_stats 2>&1)
    if echo "$SYSTEM_STATS" | grep -q '"comfyui_version"'; then
        VERSION=$(echo "$SYSTEM_STATS" | sed -n 's/.*"comfyui_version": *"\([^"]*\)".*/\1/p')
        pass "ComfyUI reachable via host-gateway (version=$VERSION)"
    else
        fail "ComfyUI not reachable from bridge" \
             "is comfyui container running? check 'docker ps | grep comfyui'"
    fi
fi

# ─── Section 7: /auth-validate (token-protected proxy) ─────────────
section "/auth-validate (token-protected proxy, NPM auth_request)"

if [[ -z "$COMFYUI_VIEW_TOKEN" ]]; then
    skip "COMFYUI_VIEW_TOKEN unset (Basic-auth-only proxy or no proxy)" \
         "set COMFYUI_VIEW_TOKEN if using the per-location split (see openclaw-image-comfyui/README.md)"
elif ! docker ps --filter "name=^${PROJ}openclaw-image-comfyui$" --format '{{.Names}}' | grep -q .; then
    skip "openclaw-image-comfyui not running — can't probe /auth-validate" \
         "docker compose -f openclaw-image-comfyui/docker-compose.yml --profile image-gen up -d"
else
    AUTH_OK=$(docker exec "${PROJ}openclaw-image-comfyui" curl -sS -o /dev/null -w "%{http_code}" \
        "http://127.0.0.1:9095/auth-validate?token=$COMFYUI_VIEW_TOKEN" 2>&1)
    AUTH_BAD=$(docker exec "${PROJ}openclaw-image-comfyui" curl -sS -o /dev/null -w "%{http_code}" \
        "http://127.0.0.1:9095/auth-validate?token=WRONG" 2>&1)
    AUTH_NONE=$(docker exec "${PROJ}openclaw-image-comfyui" curl -sS -o /dev/null -w "%{http_code}" \
        "http://127.0.0.1:9095/auth-validate" 2>&1)

    if [[ "$AUTH_OK" == "200" ]]; then
        pass "/auth-validate?token=\$COMFYUI_VIEW_TOKEN returns 200"
    else
        fail "/auth-validate?token=\$valid returns $AUTH_OK (expected 200)" \
             "check COMFYUI_VIEW_TOKEN matches between .env and bridge container env"
    fi
    if [[ "$AUTH_BAD" == "401" ]] && [[ "$AUTH_NONE" == "401" ]]; then
        pass "/auth-validate rejects wrong/missing tokens with 401"
    else
        fail "/auth-validate token rejection broken (wrong=$AUTH_BAD, none=$AUTH_NONE)" \
             "constant-time compare logic should fail closed"
    fi
fi

# ─── Summary ──────────────────────────────────────────────────────
echo
section "Summary"
echo -e "  ${GREEN}Passed:${RESET}  $PASSED"
echo -e "  ${RED}Failed:${RESET}  $FAILED"
echo -e "  ${YELLOW}Skipped:${RESET} $SKIPPED"

if [[ $FAILED -gt 0 ]]; then
    echo
    echo -e "${RED}${BOLD}Failures:${RESET}"
    for line in "${FAIL_LINES[@]}"; do
        echo "  - $line"
    done
    echo
    echo -e "Cross-reference: ${BOLD}docs/TROUBLESHOOTING.md${RESET}, ${BOLD}docs/reference/chat-surface-capability-matrix.md${RESET}"
    exit 1
fi

echo
echo -e "${GREEN}${BOLD}All checks passed.${RESET} Stack is in v0.11.0 desired state."
exit 0
