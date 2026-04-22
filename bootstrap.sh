#!/usr/bin/env bash
# ============================================================================
# DGX OpenClaw Stack — first-time bootstrap
# ============================================================================
# NON-DESTRUCTIVE & IDEMPOTENT. Running this script more than once is safe:
#   - Existing .env values are NEVER overwritten.
#   - Existing host directories are NEVER touched (no chmod, no chown).
#   - If anything already looks wrong, the script reports and asks, never fixes.
#
# What this script does:
#   1. Checks host prerequisites (docker, docker compose, nvidia runtime).
#   2. Creates a .env from .env.example (only if .env doesn't exist).
#   3. Generates strong random values for VLLM_API_KEY and OPENCLAW_GATEWAY_TOKEN
#      — only for the placeholder values shipped in .env.example.
#   4. Prompts for HUGGING_FACE_HUB_TOKEN.
#   5. Prompts for host paths (with sensible defaults) and creates the dirs
#      — only if they don't already exist.
#   6. Prints a summary and the command to start the stack.
#
# Safe to Ctrl-C at any point; re-running picks up where you left off.
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
RESET='\033[0m'

log()  { printf '%b[bootstrap]%b %s\n' "$BLUE"   "$RESET" "$*"; }
ok()   { printf '%b[ ok ]%b %s\n'     "$GREEN"  "$RESET" "$*"; }
warn() { printf '%b[warn]%b %s\n'     "$YELLOW" "$RESET" "$*"; }
err()  { printf '%b[err ]%b %s\n'     "$RED"    "$RESET" "$*" >&2; }

# ----------------------------------------------------------------------------
# 1. Prerequisites
# ----------------------------------------------------------------------------
log "Checking prerequisites…"

if ! command -v docker &>/dev/null; then
  err "docker is not installed or not in PATH. See https://docs.docker.com/engine/install/"
  exit 1
fi
ok "docker: $(docker --version)"

if ! docker compose version &>/dev/null; then
  err "docker compose plugin is not installed. On Debian/Ubuntu: sudo apt install docker-compose-plugin"
  exit 1
fi
ok "docker compose: $(docker compose version --short)"

if ! docker info 2>/dev/null | grep -q 'Runtimes:.*nvidia'; then
  warn "NVIDIA container runtime not detected. The stack will fail to start GPU services."
  warn "Install nvidia-container-toolkit: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
  warn 'Continuing anyway — you can fix this before `docker compose up`.'
else
  ok "nvidia container runtime present"
fi

if ! command -v openssl &>/dev/null; then
  err "openssl is required for generating secrets. Install it and re-run."
  exit 1
fi

# ----------------------------------------------------------------------------
# 2. .env file
# ----------------------------------------------------------------------------
ENV_FILE="$SCRIPT_DIR/.env"
ENV_TEMPLATE="$SCRIPT_DIR/.env.example"

if [[ ! -f "$ENV_TEMPLATE" ]]; then
  err ".env.example not found at $ENV_TEMPLATE — is this the right directory?"
  exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
  ok ".env already exists; leaving it untouched."
else
  log "Creating .env from .env.example…"
  cp "$ENV_TEMPLATE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok ".env created (mode 600)."
fi

# Helper: in-place substitute a key=value only if the current value matches a
# given "still-placeholder" predicate. Never overwrites a real user value.
#
#   upsert_env KEY NEWVAL PLACEHOLDER_REGEX
# Behavior:
#   - If .env has KEY= matching PLACEHOLDER_REGEX  → replace with NEWVAL.
#   - If .env has KEY= with any other value         → leave alone, report.
#   - If .env has no KEY at all                     → append KEY=NEWVAL.
#
# Two calling conventions are used below:
#   - Secrets (VLLM_API_KEY, …): regex `^CHANGE_ME` so only the placeholder
#     shipped in .env.example gets replaced — real user secrets are safe.
#   - Tokens the caller already verified are empty (HU token, HF token, host
#     paths): regex `.*` because the call site gates on "current value empty"
#     before invoking upsert_env, so unconditional overwrite is intended.
upsert_env() {
  local key="$1" newval="$2" placeholder_regex="$3"
  local current
  if current=$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-); then
    if [[ -n "$current" ]] && ! [[ "$current" =~ $placeholder_regex ]]; then
      ok "${key} already set; keeping existing value."
      return 0
    fi
    # Replace in place. Use perl for safe escaping of newval (may contain /, &, etc.).
    export _UPSERT_KEY="$key" _UPSERT_VAL="$newval"
    perl -i -pe 's/^\Q$ENV{_UPSERT_KEY}\E=.*/$ENV{_UPSERT_KEY}=$ENV{_UPSERT_VAL}/' "$ENV_FILE"
    unset _UPSERT_KEY _UPSERT_VAL
    ok "${key} set."
  else
    printf '\n%s=%s\n' "$key" "$newval" >> "$ENV_FILE"
    ok "${key} appended."
  fi
}

# ----------------------------------------------------------------------------
# 3. Secrets
# ----------------------------------------------------------------------------
log "Checking secrets…"

VLLM_API_KEY_NEW="$(openssl rand -base64 64 | tr -d '\n')"
GATEWAY_TOKEN_NEW="$(openssl rand -base64 64 | tr -d '\n')"
SEARXNG_SECRET_NEW="$(openssl rand -base64 64 | tr -d '\n')"
TTS_ROUTER_KEY_NEW="$(openssl rand -base64 64 | tr -d '\n')"
TTS_API_TOKEN_NEW="$(openssl rand -base64 64 | tr -d '\n')"

upsert_env VLLM_API_KEY                "$VLLM_API_KEY_NEW"    '^CHANGE_ME'
upsert_env OPENCLAW_GATEWAY_TOKEN      "$GATEWAY_TOKEN_NEW"   '^CHANGE_ME'
upsert_env SEARXNG_SECRET              "$SEARXNG_SECRET_NEW"  '^CHANGE_ME'
upsert_env OPENCLAW_TTS_ROUTER_API_KEY "$TTS_ROUTER_KEY_NEW"  '^CHANGE_ME'
upsert_env TTS_API_TOKEN               "$TTS_API_TOKEN_NEW"   '^CHANGE_ME'

# ----------------------------------------------------------------------------
# 3b. Optional: Hungarian TTS opt-in (CC-BY-NC model weights)
#
# Asked once. If the user has already set F5HUN_API_TOKEN (any non-empty
# value), we treat that as "already opted in" and skip the prompt — re-runs
# never re-ask.
# ----------------------------------------------------------------------------
hu_token_existing=$(grep -E '^F5HUN_API_TOKEN=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-)
if [[ -z "$hu_token_existing" ]]; then
  printf '\n%bOptional:%b Hungarian TTS (F5-TTS) — opt-in.\n' "$BOLD" "$RESET"
  log "  Wrapper code is MIT, but the model weights pulled at build time"
  log "  (sarpba/F5-TTS_V1_hun_v2) are CC-BY-NC-4.0 — non-commercial use only."
  log "  See openclaw-tts-f5hun/README.md for license details and how to swap"
  log "  the checkpoint if you need commercial use."
  printf '%bActivate Hungarian TTS now? [y/N]:%b ' "$BOLD" "$RESET"
  read -r hu_answer
  if [[ "$hu_answer" =~ ^[Yy]$ ]]; then
    F5HUN_TOKEN_NEW="$(openssl rand -base64 64 | tr -d '\n')"
    upsert_env F5HUN_API_TOKEN "$F5HUN_TOKEN_NEW" '.*'
    upsert_env F5HUN_URL       "http://openclaw-tts-f5hun:8080/v1/audio/speech" '.*'
    # COMPOSE_PROFILES may already exist with other profiles; only set when
    # missing or empty. Users who already use this var should add `hu` by hand.
    cp_existing=$(grep -E '^COMPOSE_PROFILES=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-)
    if [[ -z "$cp_existing" ]]; then
      upsert_env COMPOSE_PROFILES "hu" '.*'
    elif [[ ! "$cp_existing" =~ (^|,)hu(,|$) ]]; then
      warn "COMPOSE_PROFILES is already set to '$cp_existing' — add 'hu' to it manually."
    else
      ok "COMPOSE_PROFILES already contains 'hu'."
    fi
    ok "Hungarian TTS opt-in: secrets set. The HU model (~5 GB) downloads on first build."
    ok "  → docker compose --profile hu up -d --build openclaw-tts-f5hun"
  else
    log "Skipped — re-run bootstrap.sh later to enable, or edit .env by hand."
  fi
else
  ok "F5HUN_API_TOKEN already present — Hungarian TTS opt-in preserved."
fi

# ----------------------------------------------------------------------------
# 4. HuggingFace token
# ----------------------------------------------------------------------------
current_hf=$(grep -E '^HUGGING_FACE_HUB_TOKEN=' "$ENV_FILE" | head -n1 | cut -d= -f2-)
if [[ -z "$current_hf" || "$current_hf" == "hf_CHANGE_ME" ]]; then
  log "You need a HuggingFace access token to download the Gemma 4 NVFP4 weights."
  log "Create one at https://huggingface.co/settings/tokens (read scope is enough)."
  log "Accept the license at https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4 first."
  printf '%bHUGGING_FACE_HUB_TOKEN%b (hf_...): ' "$BOLD" "$RESET"
  read -r hf_token
  if [[ -n "$hf_token" ]]; then
    upsert_env HUGGING_FACE_HUB_TOKEN "$hf_token" '.*'  # any value replaces placeholder
  else
    warn "No token entered; leaving placeholder. Model download will fail until you edit .env."
  fi
else
  ok "HUGGING_FACE_HUB_TOKEN already set."
fi

# ----------------------------------------------------------------------------
# 5. Host paths
# ----------------------------------------------------------------------------
ensure_host_dir() {
  local env_key="$1" default="$2"
  local current
  current=$(grep -E "^${env_key}=" "$ENV_FILE" | head -n1 | cut -d= -f2-)
  if [[ -z "$current" ]]; then
    printf '%b%s%b path [default: %s]: ' "$BOLD" "$env_key" "$RESET" "$default"
    read -r answer
    local path="${answer:-$default}"
    upsert_env "$env_key" "$path" '.*'
    current="$path"
  fi

  if [[ ! -d "$current" ]]; then
    log "Creating host directory: $current"
    mkdir -p "$current"
    ok "$env_key → $current (created)"
  else
    ok "$env_key → $current (already exists, untouched)"
  fi
}

log "Setting up host paths…"
ensure_host_dir VLLM_HF_CACHE_DIR       /opt/dgx-openclaw/hf-cache
ensure_host_dir OPENCLAW_CONFIG_DIR     /opt/dgx-openclaw/openclaw-config
ensure_host_dir OPENCLAW_WORKSPACE_DIR  /opt/dgx-openclaw/workspace

# ----------------------------------------------------------------------------
# 6. Summary
# ----------------------------------------------------------------------------
printf '\n%b══════════════════════════════════════════════════════════════════════%b\n' "$GREEN" "$RESET"
printf '%bDGX OpenClaw Stack — bootstrap complete.%b\n' "$BOLD" "$RESET"
printf '%b══════════════════════════════════════════════════════════════════════%b\n\n' "$GREEN" "$RESET"
printf 'Review your .env and adjust tunables (LLM_GPU_MEM_UTIL, heartbeat hours,\n'
printf 'LAN CIDR, etc.) before first boot:\n\n'
printf '    %b$EDITOR %s/.env%b\n\n' "$BOLD" "$SCRIPT_DIR" "$RESET"
printf 'Then start the stack:\n\n'
printf '    %bdocker compose up -d%b\n\n' "$BOLD" "$RESET"
printf 'First boot is two-phase by design: the gateway crash-loops until you\n'
printf 'complete onboarding (Chrome extension or `openclaw onboard …`), then\n'
printf 'you re-run the patcher to pick up the new openclaw.json. Full walkthrough:\n\n'
printf '    %bSETUP.md%b — step-by-step first-boot guide\n' "$BOLD" "$RESET"
printf '    %bdocs/TROUBLESHOOTING.md%b — common failure modes\n\n' "$BOLD" "$RESET"
printf 'Hungarian TTS (F5-TTS, CC-BY-NC): opt-in via --profile hu or\n'
printf 'COMPOSE_PROFILES=hu in .env — see openclaw-tts-f5hun/README.md.\n'
