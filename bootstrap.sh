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
STT_API_TOKEN_NEW="$(openssl rand -base64 64 | tr -d '\n')"
# 48 bytes (64 base64 chars) is enough for the browser API. The token also
# ends up URL-encoded in `?token=…` on cdpUrl entries, so going to 64 bytes
# would make the URL noticeably longer with no extra security.
BROWSER_API_TOKEN_NEW="$(openssl rand -base64 48 | tr -d '\n')"
# noVNC bridge password. Only the first ~8 chars are effective at the RFB
# wire layer; we still ship 32 random chars so rotation matches the other
# secrets and the operator never sees the placeholder. Loopback bind is the
# real defense.
BROWSER_VNC_PASSWORD_NEW="$(openssl rand -base64 24 | tr -d '\n=+/' | head -c 32)"

upsert_env VLLM_API_KEY                "$VLLM_API_KEY_NEW"    '^CHANGE_ME'
upsert_env OPENCLAW_GATEWAY_TOKEN      "$GATEWAY_TOKEN_NEW"   '^CHANGE_ME'
upsert_env SEARXNG_SECRET              "$SEARXNG_SECRET_NEW"  '^CHANGE_ME'
upsert_env OPENCLAW_TTS_ROUTER_API_KEY "$TTS_ROUTER_KEY_NEW"  '^CHANGE_ME'
upsert_env TTS_API_TOKEN               "$TTS_API_TOKEN_NEW"   '^CHANGE_ME'
upsert_env STT_API_TOKEN               "$STT_API_TOKEN_NEW"   '^CHANGE_ME'
upsert_env BROWSER_API_TOKEN           "$BROWSER_API_TOKEN_NEW" '^CHANGE_ME'
upsert_env BROWSER_VNC_PASSWORD        "$BROWSER_VNC_PASSWORD_NEW" '^CHANGE_ME'

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
# 3c. Optional: browser automation opt-in
#
# Asked once, after the HU TTS prompt. The token is already generated above
# (so even users who skip the prompt can opt in later by just adding
# `browser` to COMPOSE_PROFILES). Here we ONLY toggle the compose profile —
# joining the activation triad (token already set, profile gate, plus the
# 1x OAuth helper run later for each credential).
# ----------------------------------------------------------------------------
existing_profiles=$(grep -E '^COMPOSE_PROFILES=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || echo "")
if [[ ",${existing_profiles}," != *",browser,"* ]]; then
  printf '\n%bOptional:%b Browser automation (Playwright Chromium) — opt-in.\n' "$BOLD" "$RESET"
  log "  Opt-in lets the agent reach login-gated, JS-heavy sites: private"
  log "  Notion pages, GitHub wikis, Patreon archives, MediaWiki, etc."
  log "  Cost: image is ~1.7 GB (Playwright + Chromium); ~200-400 MB RAM per"
  log "  warm credential."
  log ""
  log "  WORKS over noVNC: password + TOTP / SMS OTP / magic link auth flows."
  log "  DOES NOT work over noVNC: passkeys (FIDO2/WebAuthn) — W3C origin-"
  log "  bound. Use API tokens for passkey-only services instead."
  printf '%bActivate browser automation now? [y/N]:%b ' "$BOLD" "$RESET"
  read -r br_answer
  if [[ "$br_answer" =~ ^[Yy]$ ]]; then
    if [[ -z "$existing_profiles" ]]; then
      upsert_env COMPOSE_PROFILES "browser" '.*'
    elif [[ ! "$existing_profiles" =~ (^|,)browser(,|$) ]]; then
      new_profiles="${existing_profiles},browser"
      upsert_env COMPOSE_PROFILES "$new_profiles" '.*'
      ok "COMPOSE_PROFILES → ${new_profiles}"
    fi
    ok "Browser automation opt-in: profile activated."
    ok "  → docker compose --profile browser up -d --build openclaw-browser"
    ok "  → ./bootstrap-browser-login.sh github-user1     (1x per credential)"
  else
    log "Skipped — re-run bootstrap.sh later to enable, or add 'browser' to"
    log "         COMPOSE_PROFILES in .env by hand."
  fi
else
  ok "COMPOSE_PROFILES already includes 'browser' — opt-in preserved."
fi

# ----------------------------------------------------------------------------
# 3d. Optional: Python code-execution sandbox opt-in
#
# Asked once, after the browser opt-in. Token-presence guard so re-runs
# never re-ask. Wires both the secret and (best-effort) the Compose
# profile so the activation triad (token + profile + patcher step) lights
# up in one go.
# ----------------------------------------------------------------------------
py_token_existing=$(grep -E '^PYTHON_SANDBOX_API_TOKEN=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-)
if [[ -z "$py_token_existing" ]]; then
  printf '\n%bOptional:%b Python code-execution sandbox — opt-in.\n' "$BOLD" "$RESET"
  log "  Wires a self-hosted Python execution backend (persistent ipykernel"
  log "  per session, pandas/numpy/matplotlib/scikit-learn/scipy baked in)"
  log "  to OpenClaw via MCP. Two tools the agent gets: python_exec(code,"
  log "  session_id) and python_session_reset(session_id)."
  log ""
  log "  Cost: image is ~1.5-2 GB; default 8 GB RAM cap, 4 CPUs per kernel."
  log "  Threat model: trusted-prompt only (container namespaces, no gVisor)."
  log "  Egress is implicitly limited (no curl/requests in image)."
  printf '%bActivate Python sandbox now? [y/N]:%b ' "$BOLD" "$RESET"
  read -r py_answer
  if [[ "$py_answer" =~ ^[Yy]$ ]]; then
    PYTHON_SANDBOX_TOKEN_NEW="$(openssl rand -base64 48 | tr -d '\n')"
    upsert_env PYTHON_SANDBOX_API_TOKEN "$PYTHON_SANDBOX_TOKEN_NEW" '.*'
    # Best-effort COMPOSE_PROFILES toggle (same pattern as the browser block).
    py_existing_profiles=$(grep -E '^COMPOSE_PROFILES=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || echo "")
    if [[ -z "$py_existing_profiles" ]]; then
      upsert_env COMPOSE_PROFILES "python" '.*'
    elif [[ ! "$py_existing_profiles" =~ (^|,)python(,|$) ]]; then
      new_profiles="${py_existing_profiles},python"
      upsert_env COMPOSE_PROFILES "$new_profiles" '.*'
      ok "COMPOSE_PROFILES → ${new_profiles}"
    fi
    ok "Python sandbox opt-in: secrets set + profile activated."
    ok "  → docker compose --profile python up -d --build openclaw-python-sandbox"
    ok "  → docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli"
  else
    log "Skipped — re-run bootstrap.sh later to enable, or set"
    log "         PYTHON_SANDBOX_API_TOKEN in .env by hand."
  fi
else
  ok "PYTHON_SANDBOX_API_TOKEN already present — Python sandbox opt-in preserved."
fi

# ----------------------------------------------------------------------------
# 3e. Optional: Image-generation bridge opt-in (ComfyUI MCP)
#
# Same opt-in posture as 3d, with one twist: the bridge service lives in a
# SEPARATE compose file (openclaw-image-comfyui/docker-compose.yml) and the
# `image-gen` profile toggle below is advisory — `docker compose up -d` on
# the main stack does NOT start the bridge. The operator brings it up
# explicitly with the second command printed at the end of the block.
# ----------------------------------------------------------------------------
img_token_existing=$(grep -E '^IMAGE_GEN_API_TOKEN=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-)
if [[ -z "$img_token_existing" ]]; then
  printf '\n%bOptional:%b Image-generation bridge (ComfyUI MCP) — opt-in.\n' "$BOLD" "$RESET"
  log "  Wires a thin Python bridge that exposes comfyui_image__generate to"
  log "  OpenClaw via MCP. The actual generation runs on YOUR existing ComfyUI"
  log "  install (a separate compose, reached via host-gateway). The bridge is"
  log "  model-agnostic — you pick the checkpoints; this repo ships no weights."
  log ""
  log "  NOTE: this opt-in lives in a SEPARATE compose file"
  log "        (openclaw-image-comfyui/docker-compose.yml). The 'image-gen'"
  log "        profile toggle below is advisory — you bring the bridge up"
  log "        explicitly with the second 'docker compose -f …' command below."
  printf '%bActivate image-generation bridge now? [y/N]:%b ' "$BOLD" "$RESET"
  read -r img_answer
  if [[ "$img_answer" =~ ^[Yy]$ ]]; then
    IMAGE_GEN_TOKEN_NEW="$(openssl rand -base64 48 | tr -d '\n')"
    upsert_env IMAGE_GEN_API_TOKEN "$IMAGE_GEN_TOKEN_NEW" '.*'
    printf '%bComfyUI URL%b [default: http://host.docker.internal:13036]: ' "$BOLD" "$RESET"
    read -r img_url_answer
    upsert_env COMFYUI_URL "${img_url_answer:-http://host.docker.internal:13036}" '.*'
    # Best-effort COMPOSE_PROFILES toggle (same shape as 3d, advisory only).
    img_existing_profiles=$(grep -E '^COMPOSE_PROFILES=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || echo "")
    if [[ -z "$img_existing_profiles" ]]; then
      upsert_env COMPOSE_PROFILES "image-gen" '.*'
    elif [[ ! "$img_existing_profiles" =~ (^|,)image-gen(,|$) ]]; then
      new_profiles="${img_existing_profiles},image-gen"
      upsert_env COMPOSE_PROFILES "$new_profiles" '.*'
      ok "COMPOSE_PROFILES → ${new_profiles}"
    fi
    ok "Image-gen bridge opt-in: token + URL set, profile listed."
    ok "  → docker compose -f openclaw-image-comfyui/docker-compose.yml --profile image-gen up -d --build"
    ok "  → docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli"
  else
    log "Skipped — re-run bootstrap.sh later to enable, or set"
    log "         IMAGE_GEN_API_TOKEN in .env by hand."
  fi
else
  ok "IMAGE_GEN_API_TOKEN already present — image-gen bridge opt-in preserved."
fi

# ----------------------------------------------------------------------------
# 3f. Optional: Discord slash-command authorization mode
#
# Asked once. Skipped on re-run via key-presence guard. Default `open`
# (every guild member can invoke `/discord input:`, `/talkvoice input:`,
# `/activation mode:` etc.) — defends against upstream issue #19310 where
# slash commands work in DM via dmPolicy="pairing" but get silently
# blocked on guild channels with "You are not authorized to use this
# command". Operators on shared / multi-tenant / public guilds should
# pick `allowlist` (preserve upstream conservative defaults) or
# `owner-only` (locks to specific Discord snowflakes).
# ----------------------------------------------------------------------------
authz_existing=$(grep -E '^OPENCLAW_DISCORD_AUTHZ=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-)
if [[ -z "$authz_existing" ]]; then
  printf '\n%bDiscord slash-command authorization%b — defends against upstream issue #19310\n' "$BOLD" "$RESET"
  log "  Native slash commands (`/discord input:`, `/talkvoice input:`, etc.)"
  log "  give an immediate ack-dot 'thinking…' indicator that text-mention"
  log "  paths can't match. Upstream default leaves them silently blocked on"
  log "  guild channels (dual perm check, allowlist empty)."
  log ""
  log "  Modes:"
  log "    open       — every guild member can use slash commands (recommended"
  log "                  for single-operator homelab where the bot is in YOUR"
  log "                  guild(s))."
  log "    allowlist  — preserve upstream conservative defaults (dmPolicy="
  log "                  'pairing', groupPolicy='allowlist'). Pick this on"
  log "                  shared bots, multi-tenant or public guilds."
  log "    owner-only — lock to specific Discord user snowflakes."
  printf '%bDiscord slash-command authz mode? [open/allowlist/owner-only, default open]:%b ' "$BOLD" "$RESET"
  read -r authz_answer
  authz_choice="${authz_answer:-open}"
  case "$authz_choice" in
    open|allowlist|owner-only)
      upsert_env OPENCLAW_DISCORD_AUTHZ "$authz_choice" '.*'
      if [[ "$authz_choice" == "owner-only" ]]; then
        printf '%bComma-separated Discord user snowflakes (17-20 digits each)%b: ' "$BOLD" "$RESET"
        read -r owner_ids
        if [[ -n "$owner_ids" ]]; then
          upsert_env OPENCLAW_DISCORD_OWNER_IDS "$owner_ids" '.*'
        else
          warn "Empty OPENCLAW_DISCORD_OWNER_IDS — patcher step 28 will skip and you'll be locked out."
          warn "Edit .env later to set OPENCLAW_DISCORD_OWNER_IDS=<your-snowflake>."
        fi
      fi
      ;;
    *)
      warn "Unknown choice '$authz_choice' — leaving OPENCLAW_DISCORD_AUTHZ unset (patcher will use 'open' default)."
      ;;
  esac
else
  ok "OPENCLAW_DISCORD_AUTHZ already set ($authz_existing) — preserved."
fi

# ----------------------------------------------------------------------------
# 3g. Discord guild mention gate (patcher step 30)
# ----------------------------------------------------------------------------
# Sets the wildcard `channels.discord.guilds["*"].requireMention` — the
# gate that decides whether the bot even sees guild messages without an
# @mention. `off` (default) → gate open everywhere; `on` → upstream
# mention-required default. See CLAUDE.md "Discord mention gate vs
# /activation slash" for how this interacts with the in-band slash.
# ----------------------------------------------------------------------------
require_mention_existing=$(grep -E '^OPENCLAW_DISCORD_REQUIRE_MENTION=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2-)
if [[ -z "$require_mention_existing" ]]; then
  printf '\n%bDiscord guild mention gate%b — should the bot see every guild message, or only those that @mention it?\n' "$BOLD" "$RESET"
  log "  off  — gate open: bot sees EVERY message in every guild it joins"
  log "         (wildcard via guilds[\"*\"].requireMention=false; no guild"
  log "         IDs committed). The LLM still decides per-message whether"
  log "         to reply. Recommended for single-operator homelabs."
  log "  on   — preserve upstream default (mention required). Pick this on"
  log "         shared, multi-tenant or public deploys."
  printf '%bChoice [off/on] (default off)%b: ' "$BOLD" "$RESET"
  read -r require_mention_choice
  require_mention_choice="${require_mention_choice:-off}"
  case "$require_mention_choice" in
    on|off)
      upsert_env OPENCLAW_DISCORD_REQUIRE_MENTION "$require_mention_choice" '.*'
      ;;
    *)
      warn "Unknown choice '$require_mention_choice' — leaving OPENCLAW_DISCORD_REQUIRE_MENTION unset (patcher will use 'off' default)."
      ;;
  esac
else
  ok "OPENCLAW_DISCORD_REQUIRE_MENTION already set ($require_mention_existing) — preserved."
fi

# ----------------------------------------------------------------------------
# 4. HuggingFace token
# ----------------------------------------------------------------------------
current_hf=$(grep -E '^HUGGING_FACE_HUB_TOKEN=' "$ENV_FILE" | head -n1 | cut -d= -f2-)
if [[ -z "$current_hf" || "$current_hf" == "hf_CHANGE_ME" ]]; then
  log "You need a HuggingFace access token to download the Gemma 4 NVFP4 weights."
  log "Create one at https://huggingface.co/settings/tokens (read scope is enough)."
  log "Accept the license at https://huggingface.co/nvidia/Gemma-4-26B-A4B-NVFP4 first."
  log "(If you also want the dense 31B alternative, accept"
  log " https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4 with the same token.)"
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
printf 'COMPOSE_PROFILES=hu in .env — see openclaw-tts-f5hun/README.md.\n\n'
printf 'Whisper STT (EN + HU autodetect) is enabled by default. Test with:\n\n'
printf '    %bcurl -F file=@sample.wav -F model=Systran/faster-whisper-large-v3 \\\n        -H "Authorization: Bearer $STT_API_TOKEN" \\\n        http://127.0.0.1:8093/v1/audio/transcriptions%b\n\n' "$BOLD" "$RESET"
printf 'Browser automation (opt-in via --profile browser): after first boot,\n'
printf 'onboard each credential once via the noVNC helper:\n\n'
printf '    %b./bootstrap-browser-login.sh github-user1%b\n\n' "$BOLD" "$RESET"
printf 'Image generation (opt-in, ComfyUI MCP bridge): lives in a SEPARATE\n'
printf 'compose file. After your existing ComfyUI is reachable on\n'
printf 'host.docker.internal:13036, bring up the bridge:\n\n'
printf '    %bdocker compose -f openclaw-image-comfyui/docker-compose.yml \\\n        --profile image-gen up -d --build%b\n' "$BOLD" "$RESET"
