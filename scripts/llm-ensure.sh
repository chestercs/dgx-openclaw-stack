#!/usr/bin/env bash
# llm-ensure: keep the compose-managed vllm-llm serving the gateway.
# Why: gb10-memguard.sh and gb10-thermguard.sh SIGKILL it, and a killed
# container counts as manually stopped for `restart: unless-stopped`, so
# nothing else would bring it back.
#   ~/.llm-ensure-off  exists -> do nothing (e.g. GPU needed for something else)
# Respects thermguard: while it holds containers stopped for heat, start nothing.
# cron: @reboot /path/to/scripts/llm-ensure.sh boot
#       */5 * * * * /path/to/scripts/llm-ensure.sh
set -u
STACK=$(cd "$(dirname "$0")/.." && pwd)
DIR=$HOME/thermguard
LOG=$DIR/llm-ensure.log
mkdir -p "$DIR"
exec 9>"$DIR/.llm-ensure.lock"; flock -n 9 || exit 0
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

[ -e "$HOME/.llm-ensure-off" ] && exit 0
[ "${1:-}" = boot ] && sleep 120   # let dockerd, the gateway and thermguard settle

[ "$(timeout 10 docker inspect -f '{{.State.Running}}' vllm-llm 2>/dev/null)" = true ] && exit 0
if [ -s "$DIR/stopped.list" ]; then
  log "vllm-llm down, but thermguard holds $(tr '\n' ' ' < "$DIR/stopped.list")for heat; waiting"
  exit 0
fi
cd "$STACK" || exit 1
docker compose up -d --no-deps vllm-llm >/dev/null 2>&1 && log "vllm-llm was down -> started"
