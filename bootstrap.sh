#!/usr/bin/env bash
# First-time setup: checks the host, creates .env from .env.example, fills in
# the secrets, asks for the HuggingFace token and creates the host folders.
#
# Safe to re-run: a value in .env is only replaced while it still holds the
# CHANGE_ME placeholder, and existing folders are left as they are. Without a
# terminal (CI) it asks nothing; set HUGGING_FACE_HUB_TOKEN in .env first.
set -euo pipefail
cd "$(dirname "$0")"

log()  { printf '[bootstrap] %s\n' "$*"; }
warn() { printf '[bootstrap] WARNING: %s\n' "$*" >&2; }
die()  { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

# 1. Host
command -v docker >/dev/null || die "docker is not installed: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing (apt install docker-compose-plugin)"
command -v openssl >/dev/null || die "openssl is needed to generate the secrets"
docker info 2>/dev/null | grep -q 'Runtimes:.*nvidia' \
  || warn "no NVIDIA container runtime; install nvidia-container-toolkit before 'docker compose up'"

# 2. .env
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  log ".env created from .env.example"
fi

get() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2- || true; }
# Replaces KEY's value only while it is a placeholder.
fill() {
  local key=$1 value=$2 current
  current=$(get "$key")
  [[ -z "$current" || "$current" == *CHANGE_ME* ]] || return 0
  if grep -qE "^$key=" .env; then
    local tmp; tmp=$(mktemp)
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' .env > "$tmp"
    cat "$tmp" > .env && rm -f "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
  log "$key set"
}
secret() { openssl rand -base64 "$1" | tr -d '\n=+/' | head -c "$2"; }

# 3. Secrets. The browser token travels in a cdpUrl query string, and only
# the first 8 characters of a VNC password count, so those two are shorter.
for key in VLLM_API_KEY OPENCLAW_GATEWAY_TOKEN SEARXNG_SECRET STT_API_TOKEN PYTHON_SANDBOX_API_TOKEN; do
  fill "$key" "$(secret 64 64)"
done
fill BROWSER_API_TOKEN "$(secret 48 48)"
fill BROWSER_VNC_PASSWORD "$(secret 24 24)"

# 4. HuggingFace token (model downloads)
hf=$(get HUGGING_FACE_HUB_TOKEN)
if [[ -z "$hf" || "$hf" == *CHANGE_ME* ]]; then
  if [[ -t 0 ]]; then
    model=$(get LLM_MODEL_ID)
    log "A HuggingFace token (read access) is needed to download the model."
    log "Accept the licence first: https://huggingface.co/${model:-nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4}"
    log "Tokens: https://huggingface.co/settings/tokens"
    read -r -p "HUGGING_FACE_HUB_TOKEN (hf_..., empty to skip): " hf
    [[ -n "$hf" ]] && fill HUGGING_FACE_HUB_TOKEN "$hf"
  else
    warn "HUGGING_FACE_HUB_TOKEN is not set; the model download will fail"
  fi
fi

# 5. Host folders. Created when missing; the OpenClaw containers run as
# UID 1000, so on a new folder that user must be able to write.
for key in OPENCLAW_CONFIG_DIR OPENCLAW_WORKSPACE_DIR OPENCLAW_CANVAS_DIR \
           OPENCLAW_MEDIA_INBOUND_DIR OPENCLAW_SANDBOX_DIR VLLM_HF_CACHE_DIR; do
  dir=$(get "$key" | tr -d '"')
  [[ -n "$dir" ]] || { warn "$key is empty in .env"; continue; }
  if [[ ! -d "$dir" ]]; then
    mkdir -p "$dir" || { warn "could not create $dir (try: sudo mkdir -p $dir && sudo chown 1000:1000 $dir)"; continue; }
    log "created $dir"
  fi
  [[ -w "$dir" ]] || warn "$dir is not writable by $(id -un); the containers need UID 1000 to write there"
done
# The Python sandbox mounts the Discord agent's code/ folder; created here, or
# Docker would create it owned by root and the agent could not write into it.
cfg=$(get OPENCLAW_CONFIG_DIR | tr -d '"')
[[ -n "$cfg" && -d "$cfg" ]] && mkdir -p "$cfg/workspace-discord/code"

cat <<'EOF'

[bootstrap] Done. Next:
  1. docker compose up -d          (first start downloads the model; the
                                    gateway waits for onboarding, see SETUP.md)
  2. Onboard OpenClaw (SETUP.md "Onboarding"), then:
     docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
  3. On a GB10, install the clock cap: scripts/gb10-clock-cap.service
EOF
