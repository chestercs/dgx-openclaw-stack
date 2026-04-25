#!/usr/bin/env bash
# ============================================================================
# DGX OpenClaw Stack — 1x OAuth onboarding for a browser profile.
# ============================================================================
# Spawns a HEADFUL Chromium inside the openclaw-browser container, exposes
# it via noVNC on host loopback, and waits for the operator to complete the
# auth flow. When the operator hits Enter, snapshots the storage state to a
# Docker volume, relaunches the Chromium headless on the same user-data-dir,
# and re-runs the OpenClaw config patcher so the new profile shows up under
# browser.profiles.<name>.cdpUrl.
#
# Usage:
#   ./bootstrap-browser-login.sh github-user1
#   ./bootstrap-browser-login.sh notion-personal
#
# Idempotent: re-running for an existing profile name re-opens a headful
# session on the SAME user-data-dir, so existing cookies survive (unless
# the site's session expired and forces a fresh login — which is the whole
# reason you'd re-run this anyway).
#
# After the operator finishes:
#   - storageState is persisted in the browser-storage Docker volume.
#   - BROWSER_PROFILE_NAMES in .env is appended (one comma-joined string).
#   - openclaw-config-init runs to write the profile entry under
#     browser.profiles.<name> in openclaw.json.
#   - The agent can immediately call browser.navigate(profile=<name>, ...).
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="$SCRIPT_DIR/.env"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
RESET='\033[0m'

log()  { printf '%b[browser-login]%b %s\n' "$BLUE"  "$RESET" "$*"; }
ok()   { printf '%b[ ok ]%b %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%b[warn]%b %s\n' "$YELLOW" "$RESET" "$*"; }
err()  { printf '%b[err ]%b %s\n' "$RED"   "$RESET" "$*" >&2; }

usage() {
  cat <<'EOF'
bootstrap-browser-login.sh — onboard a credential to openclaw-browser.

Usage:
  ./bootstrap-browser-login.sh PROFILE_NAME

Arguments:
  PROFILE_NAME    [a-zA-Z0-9_-]{1,32} — used as the browser.profiles.<name>
                  key in openclaw.json AND as the user-data-dir name on disk.
                  Pick something memorable and per-credential, e.g.:
                    github-user1, notion-personal, patreon, mediawiki-internal

Prerequisites:
  - openclaw-browser running:
      docker compose --profile browser up -d openclaw-browser
  - .env has BROWSER_API_TOKEN set (bootstrap.sh handles this).

What this script does NOT do:
  - It does not store your password. The credential lives only in the
    Chromium user-data-dir (/storage/PROFILE_NAME/) inside the volume,
    which is treated as secret-equivalent.
  - It cannot help with passkey-only accounts. Plain WebAuthn does not
    work over noVNC (W3C origin-bound spec); use password+TOTP instead,
    or onboard via API token if the service offers one.
EOF
}

if (( $# == 0 )) || [[ "${1:-}" == "-h" ]] || [[ "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

PROFILE_NAME="${1}"

if [[ ! "$PROFILE_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$ ]]; then
  err "Invalid profile name '$PROFILE_NAME' — must match [a-zA-Z0-9][a-zA-Z0-9_-]{0,31}"
  exit 2
fi

if [[ "$PROFILE_NAME" == "default" ]]; then
  err "'default' is reserved for the anonymous throwaway profile"
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  err ".env not found at $ENV_FILE — run ./bootstrap.sh first"
  exit 1
fi

# Source .env to pick up BROWSER_API_TOKEN, BROWSER_PORT, BROWSER_VNC_PORT.
# Use `set -a` so every assignment in .env is exported. The subshell
# `export -p` round-trip protects us from arbitrary code in .env (no eval).
ENV_VARS="$(set -a; . "$ENV_FILE" >/dev/null; set +a; export -p)"
eval "$ENV_VARS"

BROWSER_API_TOKEN="${BROWSER_API_TOKEN:-}"
BROWSER_API_PORT="${BROWSER_API_PORT:-9220}"
BROWSER_VNC_PORT="${BROWSER_VNC_PORT:-5901}"
BROWSER_VNC_PASSWORD_FROM_ENV="${BROWSER_VNC_PASSWORD:-}"

if [[ -z "$BROWSER_API_TOKEN" ]] || [[ "$BROWSER_API_TOKEN" =~ ^CHANGE_ME ]]; then
  err "BROWSER_API_TOKEN is empty or still placeholder — run ./bootstrap.sh and answer 'y' at the browser prompt."
  exit 1
fi

if [[ -z "$BROWSER_VNC_PASSWORD_FROM_ENV" ]] || [[ "$BROWSER_VNC_PASSWORD_FROM_ENV" =~ ^CHANGE_ME ]]; then
  err "BROWSER_VNC_PASSWORD is empty or still placeholder — run ./bootstrap.sh to generate one,"
  err "or rotate it explicitly: ./rotate-secrets.sh BROWSER_VNC_PASSWORD"
  exit 1
fi

# Make sure the service is up. /healthz is unauthenticated by design.
if ! curl -fsS "http://127.0.0.1:${BROWSER_API_PORT}/healthz" >/dev/null 2>&1; then
  err "openclaw-browser not reachable on http://127.0.0.1:${BROWSER_API_PORT} — is it up?"
  err "  docker compose --profile browser up -d --build openclaw-browser"
  exit 1
fi

# Toggle the profile's Chromium into headful mode on the always-on VNC
# bridge. The bridge password lives in BROWSER_VNC_PASSWORD; the API
# returns the noVNC URL with that password already embedded.
log "Switching profile '${PROFILE_NAME}' to headful on the VNC bridge…"
START_RESPONSE="$(
  curl -fsS -X POST \
    -H "Authorization: Bearer ${BROWSER_API_TOKEN}" \
    "http://127.0.0.1:${BROWSER_API_PORT}/v1/sessions/${PROFILE_NAME}/login-helper" \
)" || {
  err "login-helper start failed. Is another profile in a helper session?"
  err "  curl -X POST -H 'Authorization: Bearer …' http://127.0.0.1:${BROWSER_API_PORT}/v1/sessions/<name>/login-helper/cancel"
  exit 1
}

# Pull the URL out of the JSON response. Server-rendered so we never
# guess what host/port the operator should use — handy when the operator
# overrode BROWSER_VNC_PORT in .env.
VNC_URL="$(printf '%s' "$START_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["vnc_url"])' 2>/dev/null \
  || echo "http://127.0.0.1:${BROWSER_VNC_PORT}/vnc.html?host=127.0.0.1&port=${BROWSER_VNC_PORT}&password=${BROWSER_VNC_PASSWORD_FROM_ENV}")

# Cleanup trap — if the operator Ctrl-C's, cancel the helper so we don't
# leak a headful Chromium + Xvfb + websockify until the next restart.
cleanup_on_abort() {
  log "Aborting — cancelling login helper…"
  curl -fsS -X POST \
    -H "Authorization: Bearer ${BROWSER_API_TOKEN}" \
    "http://127.0.0.1:${BROWSER_API_PORT}/v1/sessions/${PROFILE_NAME}/login-helper/cancel" \
    >/dev/null 2>&1 || true
}
trap cleanup_on_abort INT TERM

cat <<EOF

${BOLD}══════════════════════════════════════════════════════════════════════${RESET}
${BOLD}1x OAuth onboarding — profile '${PROFILE_NAME}'${RESET}
${BOLD}══════════════════════════════════════════════════════════════════════${RESET}

Open this URL in your laptop browser:

  ${BOLD}${VNC_URL}${RESET}

EOF

if [[ -n "${SSH_CONNECTION:-}" ]]; then
  cat <<EOF
You appear to be SSH'd in (\$SSH_CONNECTION is set). On your laptop, open
a tunnel first so the noVNC port is reachable:

  ${BOLD}autossh -M 0 -o "ServerAliveInterval 30" -o "ServerAliveCountMax 3" \\
    -L ${BROWSER_VNC_PORT}:127.0.0.1:${BROWSER_VNC_PORT} ${USER}@<this-host>${RESET}

Then open the URL above on your laptop's browser.

EOF
fi

cat <<EOF
In the noVNC view:
  1. Navigate to the site you want to onboard (e.g. github.com).
  2. Log in with password + TOTP (or SMS OTP). Passkeys are NOT supported
     over noVNC — W3C WebAuthn spec is origin-bound.
  3. Tick any "Trust this device" / "Stay signed in" option offered.
  4. Close the tab when done.

When you're finished, press ${BOLD}Enter${RESET} here.
Press ${BOLD}Ctrl-C${RESET} to abort without saving.

EOF

read -r _

# Operator finished. Flush state to disk and re-launch headless.
log "Finishing helper — flushing cookies, restarting headless…"
FINISH_RESPONSE="$(
  curl -fsS -X POST \
    -H "Authorization: Bearer ${BROWSER_API_TOKEN}" \
    "http://127.0.0.1:${BROWSER_API_PORT}/v1/sessions/${PROFILE_NAME}/login-helper/finish" \
)" || {
  err "login-helper finish failed. Inspect openclaw-browser logs:"
  err "  docker compose logs --tail=80 openclaw-browser"
  trap - INT TERM
  exit 1
}
trap - INT TERM
ok "session storage saved for '${PROFILE_NAME}'."

# ----------------------------------------------------------------------------
# Append profile name to BROWSER_PROFILE_NAMES in .env (idempotent — skip if
# already present). Comma-separated value; .env files don't tolerate
# newlines in scalar values.
# ----------------------------------------------------------------------------
current_names="$(grep -E '^BROWSER_PROFILE_NAMES=' "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
if [[ ",${current_names}," == *",${PROFILE_NAME},"* ]]; then
  ok "BROWSER_PROFILE_NAMES already lists '${PROFILE_NAME}'."
else
  if [[ -z "$current_names" ]]; then
    new_names="${PROFILE_NAME}"
  else
    new_names="${current_names},${PROFILE_NAME}"
  fi
  if grep -qE '^BROWSER_PROFILE_NAMES=' "$ENV_FILE"; then
    export _BPN_VAL="$new_names"
    perl -i -pe 's/^BROWSER_PROFILE_NAMES=.*/BROWSER_PROFILE_NAMES=$ENV{_BPN_VAL}/' "$ENV_FILE"
    unset _BPN_VAL
  else
    printf '\nBROWSER_PROFILE_NAMES=%s\n' "$new_names" >> "$ENV_FILE"
  fi
  ok "appended '${PROFILE_NAME}' to BROWSER_PROFILE_NAMES."
fi

# ----------------------------------------------------------------------------
# Re-run the patcher so openclaw.json picks up the new profile entry.
# ----------------------------------------------------------------------------
log "Re-running openclaw-config-init to register browser.profiles.${PROFILE_NAME}…"
if docker compose run --rm openclaw-config-init 2>&1 | tail -n 20; then
  ok "patcher ran. browser.profiles.${PROFILE_NAME} now present in openclaw.json."
else
  warn "patcher exit non-zero — check the output above. The profile is still"
  warn "usable; you can re-run the patcher manually:"
  warn "  docker compose run --rm openclaw-config-init"
fi

cat <<EOF

${BOLD}══════════════════════════════════════════════════════════════════════${RESET}
${GREEN}Profile '${PROFILE_NAME}' onboarded.${RESET}
${BOLD}══════════════════════════════════════════════════════════════════════${RESET}

Your agent can now reach authenticated content via:

  browser.navigate(url=..., profile="${PROFILE_NAME}")

When the upstream session expires (GitHub: ~14d / 28d 2FA, Notion: ~30d,
Google: variable), the agent will surface a session_expired error. Re-run
this same script to refresh:

  ./bootstrap-browser-login.sh ${PROFILE_NAME}

EOF
