#!/usr/bin/env bash
# ============================================================================
# DGX OpenClaw Stack — rotate live secrets in .env
# ============================================================================
# Overwrites the auto-generated secrets in an existing .env with fresh random
# values (`openssl rand -base64 64`), backs up the previous file, and prints
# the exact `docker compose up -d --force-recreate …` command the operator
# needs to run to pick up the new values. Rotation is explicit by design:
#   - Safe on a fresh install too (no `^CHANGE_ME` gate). Run after
#     `cp .env.example .env` to fill every placeholder in one shot.
#   - Safe on a live deployment. An atomic write + post-write `docker compose
#     config --quiet` guard means a corrupt edit never leaves .env in a bad
#     state; the timestamped backup is a belt-and-braces second layer.
#   - The script does NOT restart services. In-flight agent requests are the
#     operator's call — copy-paste the printed recreate command when ready.
#
# See docs/reference/openclaw-internals.md for the 3-store credential layout
# the patcher (steps 2/4/11/13) keeps in sync after a rotation.
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="$SCRIPT_DIR/.env"

# Color + logging helpers, intentionally matching bootstrap.sh so a reader
# flipping between the two scripts sees the same output vocabulary.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
RESET='\033[0m'

log()  { printf '%b[rotate]%b %s\n' "$BLUE"   "$RESET" "$*"; }
ok()   { printf '%b[ ok ]%b %s\n'  "$GREEN"  "$RESET" "$*"; }
warn() { printf '%b[warn]%b %s\n'  "$YELLOW" "$RESET" "$*"; }
err()  { printf '%b[err ]%b %s\n'  "$RED"    "$RESET" "$*" >&2; }

# ----------------------------------------------------------------------------
# Rotatable-key registry.
#
# DEFAULT_KEYS: added to --all and to the interactive menu.
# GATEWAY_KEY:  only rotated with --include-gateway-token (inert post-onboarding
#               — the real gateway auth is openclaw.json's gateway.auth.token).
# F5HUN_KEY:    default set includes it ONLY if already non-empty. Explicit
#               positional arg still rotates it regardless — operator intent.
#
# The restart matrix (key → affected compose services) is maintained by hand
# below in services_for(). The matching compose-file line references are in
# the plan so a future auditor can grep them.
# ----------------------------------------------------------------------------
DEFAULT_KEYS=(
  VLLM_API_KEY
  SEARXNG_SECRET
  OPENCLAW_TTS_ROUTER_API_KEY
  TTS_API_TOKEN
  STT_API_TOKEN
)
F5HUN_KEY=F5HUN_API_TOKEN
GATEWAY_KEY=OPENCLAW_GATEWAY_TOKEN

ALL_ROTATABLE=("${DEFAULT_KEYS[@]}" "$F5HUN_KEY" "$GATEWAY_KEY")

# Map a rotated key to the space-separated compose service list that reads it.
# Keep this aligned with docker-compose.yml — see plan for line-number refs.
services_for() {
  case "$1" in
    VLLM_API_KEY)                echo "vllm-llm vllm-embedding openclaw-config-init openclaw-gateway openclaw-cli" ;;
    SEARXNG_SECRET)              echo "searxng" ;;
    OPENCLAW_TTS_ROUTER_API_KEY) echo "openclaw-config-init openclaw-gateway openclaw-cli openclaw-tts-router" ;;
    TTS_API_TOKEN)               echo "openclaw-tts-en openclaw-tts-router" ;;
    STT_API_TOKEN)               echo "openclaw-stt-whisper openclaw-config-init openclaw-gateway openclaw-cli" ;;
    F5HUN_API_TOKEN)             echo "openclaw-tts-f5hun openclaw-tts-router" ;;
    OPENCLAW_GATEWAY_TOKEN)      echo "openclaw-gateway openclaw-cli" ;;
    *) return 1 ;;
  esac
}

is_rotatable() {
  local k="$1" x
  for x in "${ALL_ROTATABLE[@]}"; do
    [[ "$x" == "$k" ]] && return 0
  done
  return 1
}

usage() {
  cat <<'EOF'
rotate-secrets.sh — rotate live secrets in .env for the DGX OpenClaw stack.

Usage:
  ./rotate-secrets.sh [FLAGS] [KEY ...]

Flags:
  -a, --all                    Rotate the default set (see below).
  -n, --dry-run                Show what would change; write nothing.
  -y, --yes                    Skip the confirmation prompt (CI-friendly).
      --include-gateway-token  Also rotate OPENCLAW_GATEWAY_TOKEN. NOTE: this
                               env var is inert post-onboarding — the real
                               gateway auth lives in openclaw.json's
                               gateway.auth.token (picked by the wizard at
                               pair time). Rotating the env alone is mostly
                               useful before onboarding has run.
  -h, --help                   Show this help.

Default set (rotated by --all or the interactive menu):
  VLLM_API_KEY, SEARXNG_SECRET, OPENCLAW_TTS_ROUTER_API_KEY, TTS_API_TOKEN,
  STT_API_TOKEN
  (+ F5HUN_API_TOKEN if it is already set in .env — empty F5HUN = opted out
  of the CC-BY-NC Hungarian TTS, and --all respects that).

Explicit positional args rotate any rotatable key regardless of current
value, including F5HUN_API_TOKEN and OPENCLAW_GATEWAY_TOKEN:

  ./rotate-secrets.sh VLLM_API_KEY TTS_API_TOKEN
  ./rotate-secrets.sh F5HUN_API_TOKEN

Out of scope:
  HUGGING_FACE_HUB_TOKEN — user-owned, cannot be generated. Set manually.

After rotation the script prints the exact `docker compose up -d
--force-recreate …` command for the services that read the rotated keys.
It does NOT run that command — you pick the moment (in-flight requests).

See docs/reference/openclaw-internals.md §"v0.4.x credential layout" for
the 3-store sync story (.env → openclaw.json → auth-profiles.json).
EOF
}

# ----------------------------------------------------------------------------
# Arg parsing. Flags may appear anywhere; positional args are env-var names.
# ----------------------------------------------------------------------------
DRY_RUN=0
YES=0
ROTATE_ALL=0
INCLUDE_GATEWAY=0
EXPLICIT_KEYS=()

while (($#)); do
  case "$1" in
    -a|--all)                   ROTATE_ALL=1 ;;
    -n|--dry-run)               DRY_RUN=1 ;;
    -y|--yes)                   YES=1 ;;
    --include-gateway-token)    INCLUDE_GATEWAY=1 ;;
    -h|--help)                  usage; exit 0 ;;
    --) shift; while (($#)); do EXPLICIT_KEYS+=("$1"); shift; done; break ;;
    -*) err "Unknown flag: $1"; echo; usage; exit 2 ;;
    *)  EXPLICIT_KEYS+=("$1") ;;
  esac
  shift
done

# Validate positional args are real rotatable keys — typos here would be the
# #1 way to waste operator time with zero feedback.
for k in "${EXPLICIT_KEYS[@]:-}"; do
  [[ -z "$k" ]] && continue
  if ! is_rotatable "$k"; then
    err "Unknown rotatable key: $k"
    err "Known keys: ${ALL_ROTATABLE[*]}"
    exit 2
  fi
done

# ----------------------------------------------------------------------------
# Prerequisites: .env present, openssl installed.
# ----------------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  err ".env not found at $ENV_FILE"
  err "Run ./bootstrap.sh first to create it from .env.example."
  exit 1
fi

if ! command -v openssl &>/dev/null; then
  err "openssl is required for generating secrets. Install it and re-run."
  exit 1
fi

# Read a KEY= line (uncommented) from .env. Empty string if absent.
current_value() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || true
}

# sha256 short fingerprint — audit-useful, no key leak.
fp() {
  if [[ -z "$1" ]]; then
    printf '(empty)'
  else
    printf '%s' "$1" | sha256sum | cut -c1-12
  fi
}

# ----------------------------------------------------------------------------
# Decide the rotation set.
#   1. Explicit positional args always win, even for F5HUN / gateway.
#   2. --all adds the default set + F5HUN (only if already set) + gateway
#      (only with --include-gateway-token).
#   3. No args + no --all → interactive per-key prompt.
# ----------------------------------------------------------------------------
declare -A ROTATE_MAP  # key → 1
# Prime + unprime so ${#ROTATE_MAP[@]} is safe under `set -u` on bash < 5.2
# where querying an uninitialised associative array errors out.
ROTATE_MAP[__init__]=1
unset 'ROTATE_MAP[__init__]'

add_key() { ROTATE_MAP["$1"]=1; }

for k in "${EXPLICIT_KEYS[@]:-}"; do
  [[ -z "$k" ]] && continue
  add_key "$k"
done

if (( ROTATE_ALL )); then
  for k in "${DEFAULT_KEYS[@]}"; do add_key "$k"; done
  # F5HUN auto-include only if a non-empty value is already present. Empty =
  # HU TTS opted out (CC-BY-NC model); auto-generating would be user-hostile
  # — half of a 3-lever opt-in materializes silently.
  if [[ -n "$(current_value "$F5HUN_KEY")" ]]; then
    add_key "$F5HUN_KEY"
  fi
  if (( INCLUDE_GATEWAY )); then
    add_key "$GATEWAY_KEY"
  fi
fi

# Interactive mode: no args, no --all.
if (( ${#ROTATE_MAP[@]} == 0 )); then
  log "No keys selected. Pick interactively (Ctrl-C to abort)."
  printf '\n'
  for k in "${DEFAULT_KEYS[@]}"; do
    cur="$(current_value "$k")"
    cur_state="(empty)"
    [[ -n "$cur" ]] && cur_state="fp=$(fp "$cur")"
    printf '  %s — %s\n' "$k" "$cur_state"
    printf '    Rotate? [y/N]: '
    read -r ans
    [[ "$ans" =~ ^[Yy]$ ]] && add_key "$k"
  done

  # F5HUN only offered if already set. No accidental CC-BY-NC opt-in via
  # an interactive "yes to everything" habit.
  if [[ -n "$(current_value "$F5HUN_KEY")" ]]; then
    printf '  %s — fp=%s\n' "$F5HUN_KEY" "$(fp "$(current_value "$F5HUN_KEY")")"
    printf '    Rotate? [y/N]: '
    read -r ans
    [[ "$ans" =~ ^[Yy]$ ]] && add_key "$F5HUN_KEY"
  fi

  # Gateway offered only if the flag was set — mirrors --all behavior so
  # the two entry paths match.
  if (( INCLUDE_GATEWAY )); then
    cur="$(current_value "$GATEWAY_KEY")"
    cur_state="(empty)"
    [[ -n "$cur" ]] && cur_state="fp=$(fp "$cur")"
    printf '  %s — %s\n' "$GATEWAY_KEY" "$cur_state"
    printf '    Rotate? [y/N]: '
    read -r ans
    [[ "$ans" =~ ^[Yy]$ ]] && add_key "$GATEWAY_KEY"
  fi
  printf '\n'
fi

if (( ${#ROTATE_MAP[@]} == 0 )); then
  warn "Nothing selected for rotation. Exiting."
  exit 0
fi

# Soft teach: mention the gateway-token gotcha even without the flag, so the
# operator learns where the real gateway auth actually lives.
if (( ROTATE_ALL )) && (( ! INCLUDE_GATEWAY )); then
  log "Note: OPENCLAW_GATEWAY_TOKEN is NOT rotated by --all. Post-onboarding,"
  log "      the real gateway auth is openclaw.json's gateway.auth.token"
  log "      (picked by the wizard). Use --include-gateway-token to rotate"
  log "      the env var anyway (useful only pre-onboarding)."
fi

# ----------------------------------------------------------------------------
# Generate new values and stash them. Build the planned-change summary before
# touching the file so dry-run and real-run share one code path.
# ----------------------------------------------------------------------------
declare -A NEW_VALUES     # key → new secret
declare -A OLD_FP         # key → old fingerprint for summary
declare -A NEW_FP         # key → new fingerprint
declare -A SERVICE_SET    # service → 1 (dedup)

ROTATE_ORDER=()
for k in "${ALL_ROTATABLE[@]}"; do
  [[ -n "${ROTATE_MAP[$k]:-}" ]] && ROTATE_ORDER+=("$k")
done

HU_TOUCHED=0

for key in "${ROTATE_ORDER[@]}"; do
  new="$(openssl rand -base64 64 | tr -d '\n')"
  NEW_VALUES[$key]="$new"
  OLD_FP[$key]="$(fp "$(current_value "$key")")"
  NEW_FP[$key]="$(fp "$new")"

  svcs="$(services_for "$key")"
  for s in $svcs; do
    SERVICE_SET[$s]=1
  done
  [[ "$key" == "$F5HUN_KEY" ]] && HU_TOUCHED=1
done

# ----------------------------------------------------------------------------
# Show the planned changes + recreate command.
# ----------------------------------------------------------------------------
printf '\n%bPlanned rotations:%b\n' "$BOLD" "$RESET"
for key in "${ROTATE_ORDER[@]}"; do
  printf '  %-30s old=sha256:%s  new=sha256:%s\n' \
    "$key" "${OLD_FP[$key]}" "${NEW_FP[$key]}"
done

# Sort affected services by the docker-compose declaration order for a readable
# recreate command (matches the restart-matrix rows in the plan).
SERVICE_ORDER=(
  vllm-llm
  vllm-embedding
  searxng
  openclaw-tts-en
  openclaw-tts-f5hun
  openclaw-tts-router
  openclaw-config-init
  openclaw-gateway
  openclaw-cli
)
sorted_services=()
for s in "${SERVICE_ORDER[@]}"; do
  [[ -n "${SERVICE_SET[$s]:-}" ]] && sorted_services+=("$s")
done

compose_cmd="docker compose"
if (( HU_TOUCHED )); then
  compose_cmd+=" --profile hu"
fi
recreate_cmd="${compose_cmd} up -d --force-recreate ${sorted_services[*]}"

printf '\n%bRestart command (run when you are ready):%b\n' "$BOLD" "$RESET"
printf '  %s\n' "$recreate_cmd"

# ----------------------------------------------------------------------------
# Dry-run stops here. Nothing written to disk.
# ----------------------------------------------------------------------------
if (( DRY_RUN )); then
  printf '\n'
  log "Dry run — .env unchanged, no backup written."
  exit 0
fi

# ----------------------------------------------------------------------------
# Confirmation gate. -y skips.
# ----------------------------------------------------------------------------
if (( ! YES )); then
  printf '\n'
  printf '%bProceed with rotating %d secret(s) in .env? [y/N]:%b ' "$BOLD" "${#ROTATE_ORDER[@]}" "$RESET"
  read -r confirm
  if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
    log "Aborted by operator. No changes made."
    exit 0
  fi
fi

# ----------------------------------------------------------------------------
# Backup. One timestamped copy per run, mode 600.
# ----------------------------------------------------------------------------
BACKUP_FILE="$ENV_FILE.backup-$(date +%Y%m%d-%H%M%S)"
cp -p "$ENV_FILE" "$BACKUP_FILE"
chmod 600 "$BACKUP_FILE"
ok "Backup: $(basename "$BACKUP_FILE")"

# ----------------------------------------------------------------------------
# Atomic write. Build the new content in a temp file under the same directory
# (so the final mv is atomic on the same filesystem), chmod 600 before the
# rename, then mv into place. A trap cleans up a stray temp file on SIGINT.
# ----------------------------------------------------------------------------
TMP_FILE="$ENV_FILE.tmp.$$"
cleanup_tmp() { [[ -f "$TMP_FILE" ]] && rm -f "$TMP_FILE"; }
trap cleanup_tmp EXIT

# Walk existing .env line by line. For every rotated key whose uncommented
# KEY= line exists, replace its value in place. Keys that don't have an
# uncommented line yet (e.g., F5HUN_API_TOKEN shipped commented in
# .env.example and never enabled) get appended at the end. A commented
# reference line stays as-is — it's documentation.
declare -A WROTE  # key → 1 once replaced in the stream
: > "$TMP_FILE"
chmod 600 "$TMP_FILE"

while IFS= read -r line || [[ -n "$line" ]]; do
  matched=""
  # Only an uncommented assignment qualifies; lines starting with `#` are
  # left untouched. The match is anchored on the exact key name to avoid
  # e.g., `VLLM_API_KEY2=` accidentally matching `VLLM_API_KEY`.
  if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
    k="${BASH_REMATCH[1]}"
    if [[ -n "${NEW_VALUES[$k]:-}" ]]; then
      matched="$k"
    fi
  fi
  if [[ -n "$matched" ]]; then
    printf '%s=%s\n' "$matched" "${NEW_VALUES[$matched]}" >> "$TMP_FILE"
    WROTE[$matched]=1
  else
    printf '%s\n' "$line" >> "$TMP_FILE"
  fi
done < "$ENV_FILE"

# Append any rotated key that wasn't present as an uncommented line.
for key in "${ROTATE_ORDER[@]}"; do
  if [[ -z "${WROTE[$key]:-}" ]]; then
    printf '%s=%s\n' "$key" "${NEW_VALUES[$key]}" >> "$TMP_FILE"
    WROTE[$key]=1
  fi
done

# Swap into place. mv within the same dir is atomic on POSIX filesystems.
mv "$TMP_FILE" "$ENV_FILE"
trap - EXIT
chmod 600 "$ENV_FILE"

# ----------------------------------------------------------------------------
# Post-write validation. `docker compose config --quiet` parses compose.yml
# with the new .env. If it fails (malformed edit, unbalanced quotes in a
# rotated value — unlikely with openssl base64 but defense in depth), we
# restore the backup and exit non-zero.
# ----------------------------------------------------------------------------
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
  if ! docker compose --env-file "$ENV_FILE" config --quiet 2>/dev/null; then
    err "docker compose config --quiet failed after write — .env appears malformed."
    err "Restoring backup: $(basename "$BACKUP_FILE")"
    cp "$BACKUP_FILE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    exit 1
  fi
  ok "docker compose config validates."
else
  warn "docker compose not available — skipping post-write validation."
fi

# ----------------------------------------------------------------------------
# Summary + recreate + verify hints. Printing the recreate command (again,
# post-write) means the operator doesn't have to scroll up.
# ----------------------------------------------------------------------------
printf '\n%b═══════════════════════════════════════════════════════════════%b\n' "$GREEN" "$RESET"
printf '%bRotated %d secret(s) in .env.%b\n' "$BOLD" "${#ROTATE_ORDER[@]}" "$RESET"
printf '%b═══════════════════════════════════════════════════════════════%b\n' "$GREEN" "$RESET"

printf '\nNext — restart the affected services:\n\n'
printf '    %b%s%b\n\n' "$BOLD" "$recreate_cmd" "$RESET"

# Reproduce the verify recipes from CLAUDE.md so the operator has them
# inline after a rotation. These are the same commands as the root
# CLAUDE.md §"Verification recipes (copy-paste ready)".
printf 'Verify after containers go healthy:\n\n'
printf '    PROJ=$(grep ^CONTAINER_NAME_PREFIX= .env | cut -d= -f2); PROJ=${PROJ:-dgx-}\n'
printf '    curl -sS http://127.0.0.1:18789/healthz\n'
if [[ -n "${NEW_VALUES[VLLM_API_KEY]:-}" ]]; then
  printf '    # VLLM_API_KEY rotation — confirm per-agent auth-profiles.json matches:\n'
  printf '    docker exec ${PROJ}openclaw-cli node -e '"'"'\n'
  printf '      const p=require("/home/node/.openclaw/agents/main/agent/auth-profiles.json");\n'
  printf '      const c=require("crypto"); const k=p.profiles["vllm:default"].key;\n'
  printf '      console.log("len="+k.length+" sha="+c.createHash("sha256").update(k).digest("hex").slice(0,12));\n'
  printf '    '"'"'\n'
fi

if (( INCLUDE_GATEWAY )); then
  printf '\n'
  warn "You rotated OPENCLAW_GATEWAY_TOKEN. Post-onboarding this env var is inert;"
  warn "to rotate the real gateway auth token used by Chrome extension / remote CLI"
  warn "pairings, re-onboard or use the OpenClaw CLI to rotate the pair token."
  warn "Reference: docs/reference/openclaw-internals.md §\"3-store credential layout\""
fi

printf '\n'
