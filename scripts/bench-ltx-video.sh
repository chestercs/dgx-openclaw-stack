#!/usr/bin/env bash
# bench-ltx-video.sh — measure LTX-Video 2.3 wall-clock + peak VRAM on
# this host, append the numbers to docs/reference/ltx-video-bench.md
# as a dated markdown table.
#
# Run on the deploy host after the image-gen bridge is live with
# generate_video registered (Phase 3 of the LTX integration). Three
# scenarios, in order:
#
#   1. T2V cold     — first call after bench-start. The 46 GB checkpoint
#                     + 25 GB Gemma encoder load into VRAM here; expect
#                     3-10 minutes on GB10 the very first time the
#                     stack runs LTX-2.3.
#   2. T2V warm     — second call, immediately after cold completes.
#                     Cache is warm; expect 30-120 s for a 4-s clip at
#                     512×768.
#   3. I2V warm     — third call, with a known reference frame. Adds
#                     the LoadImage upload + LTXVImgToVideoInplace
#                     preprocessing path.
#
# Each scenario: take /system_stats snapshot before, in the middle
# (via best-effort backgrounded curl after `started`), and after.
# Capture wall-clock from the bridge's `elapsed_s` field. Capture
# `gpu.vram_used` from /system_stats max across the three samples
# (best-effort — peak may fall between samples).
#
# Designed to be a SMOKE plus a perf-snapshot, not a rigorous
# benchmark — for that, run multiple iterations and average. Each
# scenario takes one shot; the bench is meant to be "is the integration
# alive AND in the right ballpark", not "publish-grade VRAM curve".
#
# Usage:
#   ./scripts/bench-ltx-video.sh [--prompt "custom prompt"] [--no-append]
#                                 [--init-image PATH]
#
#   --prompt TEXT      Override the T2V test prompt.
#   --init-image PATH  Local image file for I2V (default: scripts/bench-ltx-init.png
#                      if present, else skip the I2V scenario).
#   --no-append        Don't append to docs/reference/ltx-video-bench.md;
#                      print to stdout only.
#
# Cross-reference: docs/reference/video-comfyui-bridge.md "GB10 bench numbers"
# section lays out the table format this script appends to.
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_FILE="$REPO_ROOT/.env"
BENCH_DOC="$REPO_ROOT/docs/reference/ltx-video-bench.md"

# ANSI colors
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; RESET='\033[0m'; BOLD='\033[1m'
else
    GREEN=''; RED=''; YELLOW=''; CYAN=''; RESET=''; BOLD=''
fi

info() { printf "${CYAN}==>${RESET} %s\n" "$*"; }
ok()   { printf "${GREEN}✓${RESET}   %s\n" "$*"; }
warn() { printf "${YELLOW}!${RESET}   %s\n" "$*" >&2; }
die()  { printf "${RED}✗${RESET}   %s\n" "$*" >&2; exit 1; }

# ─── Args ────────────────────────────────────────────────────────────
PROMPT="A red panda eating bamboo on a mossy log, cinematic shot, soft afternoon light"
INIT_IMAGE=""
APPEND=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prompt)       PROMPT="$2"; shift 2 ;;
        --init-image)   INIT_IMAGE="$2"; shift 2 ;;
        --no-append)    APPEND=0; shift ;;
        -h|--help)      sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)              die "Unknown argument: $1" ;;
    esac
done

# Auto-discover the default init image if not passed explicitly.
if [[ -z "$INIT_IMAGE" && -f "$SCRIPT_DIR/bench-ltx-init.png" ]]; then
    INIT_IMAGE="$SCRIPT_DIR/bench-ltx-init.png"
fi

# ─── Env ─────────────────────────────────────────────────────────────
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found — run from a clone with bootstrap.sh completed."
get_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-; }

IMAGE_GEN_API_TOKEN=$(get_env IMAGE_GEN_API_TOKEN)
[[ -n "$IMAGE_GEN_API_TOKEN" ]] || die "IMAGE_GEN_API_TOKEN is empty — image-gen bridge not activated."

# Bridge endpoint — loopback by default.
BRIDGE_BIND=$(get_env IMAGE_GEN_BIND); BRIDGE_BIND=${BRIDGE_BIND:-127.0.0.1}
BRIDGE_PORT=$(get_env IMAGE_GEN_PORT); BRIDGE_PORT=${BRIDGE_PORT:-9095}
BRIDGE_URL="http://${BRIDGE_BIND}:${BRIDGE_PORT}"

# ComfyUI endpoint — for /system_stats VRAM probe.
COMFYUI_URL=$(get_env COMFYUI_URL); COMFYUI_URL=${COMFYUI_URL:-http://localhost:13036}
# COMFYUI_URL inside .env is typically the bridge-side URL
# (`host.docker.internal:...`) which doesn't resolve from this script's
# perspective on the host. Substitute the loopback form for /system_stats.
COMFYUI_PROBE_URL=${COMFYUI_URL/host.docker.internal/localhost}
COMFYUI_PROBE_URL=${COMFYUI_PROBE_URL/openclaw-image-comfyui/localhost}

# Curl helper — JSON content type + bearer.
mcp_call() {
    local body="$1"
    curl -sS --max-time 1200 \
         -X POST "$BRIDGE_URL/mcp" \
         -H "Authorization: Bearer $IMAGE_GEN_API_TOKEN" \
         -H 'Content-Type: application/json' \
         -H 'Mcp-Session-Id: bench-ltx-video' \
         -d "$body"
}

# VRAM probe — best-effort. Many ComfyUI deploys gate /system_stats
# behind their own auth or don't expose it on the loopback URL; the
# bench then records "n/a" instead of failing the whole run.
probe_vram() {
    local raw
    raw=$(curl -sS --max-time 5 "$COMFYUI_PROBE_URL/system_stats" 2>/dev/null) || { echo "n/a"; return 0; }
    # Pull the first occurrence of vram_used (or "vram_total - vram_free").
    # ComfyUI's /system_stats shape: {"devices":[{"vram_used": <bytes>, ...}]}
    # We don't have jq guaranteed; node is.
    if command -v node >/dev/null 2>&1; then
        echo "$raw" | node -e '
let s = "";
process.stdin.on("data", c => s += c);
process.stdin.on("end", () => {
    try {
        const d = JSON.parse(s);
        const dev = (d.devices || [])[0] || {};
        const used = dev.vram_used;
        if (typeof used !== "number") { console.log("n/a"); return; }
        // ComfyUI returns bytes — convert to GB string with 1 decimal.
        console.log((used / (1024 ** 3)).toFixed(1) + " GB");
    } catch (e) { console.log("n/a"); }
});
' 2>/dev/null || echo "n/a"
    else
        echo "n/a"
    fi
}

# ─── Probe bridge alive ──────────────────────────────────────────────
info "Bridge $BRIDGE_URL ... "
if ! curl -fsS --max-time 5 "$BRIDGE_URL/healthz" >/dev/null; then
    die "Bridge not reachable. Start with: docker compose -f openclaw-image-comfyui/docker-compose.yml --profile image-gen up -d"
fi
ok "Bridge healthy"

# Probe tools/list to confirm generate_video is registered.
TOOLS_RESP=$(mcp_call '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
if ! echo "$TOOLS_RESP" | grep -q '"generate_video"'; then
    die "generate_video tool not registered. Did the bridge rebuild to v0.12.0?"
fi
ok "generate_video tool registered"

# ─── Scenario runner ─────────────────────────────────────────────────
TMP=$(mktemp -d)
trap "rm -rf $TMP" EXIT

# scenarios collect into an array; printed as a markdown table at the end.
declare -a SCENARIO_ROWS=()

run_scenario() {
    local label="$1" body="$2"
    info "[$label] starting..."
    local v_before v_after
    v_before=$(probe_vram)
    local out_file="$TMP/${label// /_}.json"
    local rc=0
    mcp_call "$body" > "$out_file" || rc=$?
    v_after=$(probe_vram)

    if [[ $rc -ne 0 ]]; then
        warn "[$label] curl exit $rc — see $out_file"
        SCENARIO_ROWS+=("| $label | ERROR | curl rc=$rc | n/a | $(date -u +%FT%TZ) |")
        return
    fi

    # Bridge wraps the tool result in MCP shape: result.content[0].text holds
    # the JSON-stringified tool output. Extract via node.
    local parsed
    parsed=$(node -e '
const fs = require("fs");
const env = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
const txt = env?.result?.content?.[0]?.text;
if (!txt) { console.log(JSON.stringify({error: "no text content", raw: env})); return; }
try {
    const r = JSON.parse(txt);
    console.log(JSON.stringify({
        elapsed_s: r.elapsed_s ?? null,
        workflow: r.workflow_used ?? null,
        mode: r.mode ?? null,
        len: r.length_frames ?? null,
        fps: r.fps ?? null,
        duration_s: r.duration_s ?? null,
        audio: r.audio_enabled ?? null,
        error: r.error ?? null,
        message: r.message ?? null,
    }));
} catch (e) { console.log(JSON.stringify({error: "parse failed: " + e.message, raw: txt.slice(0, 500)})); }
' "$out_file" 2>/dev/null || echo '{"error":"node parse failed"}')

    local elapsed mode workflow
    elapsed=$(echo "$parsed" | node -e 'let s=""; process.stdin.on("data",c=>s+=c); process.stdin.on("end",()=>{ try{ const d=JSON.parse(s); console.log(d.elapsed_s ?? "?"); }catch{console.log("?")}})')
    mode=$(echo "$parsed" | node -e 'let s=""; process.stdin.on("data",c=>s+=c); process.stdin.on("end",()=>{ try{ const d=JSON.parse(s); console.log(d.mode ?? "?"); }catch{console.log("?")}})')
    workflow=$(echo "$parsed" | node -e 'let s=""; process.stdin.on("data",c=>s+=c); process.stdin.on("end",()=>{ try{ const d=JSON.parse(s); console.log(d.workflow ?? "?"); }catch{console.log("?")}})')

    ok "[$label] elapsed_s=$elapsed mode=$mode workflow=$workflow"
    SCENARIO_ROWS+=("| $label | ${elapsed}s | $workflow ($mode) | $v_after | $(date -u +%FT%TZ) |")
}

# T2V cold — keep payload small (512×768, 4s) so a sane first call doesn't
# eat the whole bench-time budget on cold-cache load.
T2V_BODY=$(cat <<EOF
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"generate_video","arguments":{"prompt":"${PROMPT//\"/\\\"}","width":512,"height":768,"length":96,"fps":24,"timeout_s":900,"include_base64":false,"attach_image_content":false}}}
EOF
)

run_scenario "T2V cold (512×768, 4s, audio on)" "$T2V_BODY"
run_scenario "T2V warm (512×768, 4s, audio on)" "$T2V_BODY"

# I2V — only if an init image was provided / discovered.
if [[ -n "$INIT_IMAGE" && -f "$INIT_IMAGE" ]]; then
    INIT_B64=$(base64 -w0 < "$INIT_IMAGE" 2>/dev/null || base64 < "$INIT_IMAGE" | tr -d '\n')
    I2V_BODY=$(cat <<EOF
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"generate_video","arguments":{"prompt":"${PROMPT//\"/\\\"}, animated","init_image_base64":"$INIT_B64","width":512,"height":768,"length":96,"fps":24,"timeout_s":900,"include_base64":false,"attach_image_content":false}}}
EOF
)
    run_scenario "I2V warm (512×768, 4s, audio on)" "$I2V_BODY"
else
    info "Skipping I2V scenario (no init image — pass --init-image PATH or place scripts/bench-ltx-init.png)"
fi

# ─── Output ──────────────────────────────────────────────────────────
DATESTAMP=$(date -u +%Y-%m-%d)
HOSTNAME_=$(hostname 2>/dev/null || echo "unknown")

# Build the markdown chunk.
TABLE=$(cat <<EOF

### $DATESTAMP ($HOSTNAME_)

| Scenario | Wall-clock | Workflow (mode) | VRAM after | Timestamp |
|----------|------------|-----------------|------------|-----------|
EOF
)
for row in "${SCENARIO_ROWS[@]}"; do
    TABLE+="$row"$'\n'
done

if [[ $APPEND -eq 1 ]]; then
    if [[ ! -f "$BENCH_DOC" ]]; then
        cat > "$BENCH_DOC" <<EOF
# LTX-Video 2.3 — bench numbers

Each section is a run of \`scripts/bench-ltx-video.sh\`. Format:
three scenarios per run (T2V cold, T2V warm, I2V warm if init image
available). Wall-clock from the bridge's \`elapsed_s\` field; VRAM
from ComfyUI's \`/system_stats\` (best-effort, "n/a" when the
endpoint isn't reachable from the bench host).

For the full architecture / workflow / chat-surface context see
[\`video-comfyui-bridge.md\`](video-comfyui-bridge.md).

EOF
    fi
    echo "$TABLE" >> "$BENCH_DOC"
    ok "Appended results to $BENCH_DOC"
else
    echo "$TABLE"
fi
