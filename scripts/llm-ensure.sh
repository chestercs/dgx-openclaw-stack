#!/usr/bin/env bash
# llm-ensure: keep the chat model serving the gateway: the compose-managed vllm-llm, or the Strata server
# when COMPOSE_FILE has compose/strata.yml.
# Why: gb10-memguard.sh SIGKILLs it, and a killed container counts as manually stopped for
# `restart: unless-stopped`, so nothing else would bring it back. Strata has no restart policy at all (it
# must size its memory after the stack has loaded), so this also starts it after a reboot.
#   ~/.llm-ensure-off  exists -> do nothing (e.g. GPU needed for something else)
# Respects thermguard: while it holds containers stopped for heat, start nothing (it restarts them).
# At most three starts per hour: a guard that keeps killing it gets no restart loop.
# COMPOSE_FILE is read from .env, which is what cron runs with.
# cron: @reboot /path/to/scripts/llm-ensure.sh boot
#       */5 * * * * /path/to/scripts/llm-ensure.sh
set -u
STACK=$(cd "$(dirname "$0")/.." && pwd)
DIR=$HOME/thermguard
LOG=$DIR/llm-ensure.log
STARTS=$DIR/llm-ensure.starts
mkdir -p "$DIR"
exec 9>"$DIR/.llm-ensure.lock"; flock -n 9 || exit 0
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
env_get() { grep -s "^$1=" .env | head -1 | cut -d= -f2- | tr -d "\"'"; }

[ -e "$HOME/.llm-ensure-off" ] && exit 0
[ "${1:-}" = boot ] && sleep 120   # let dockerd, the gateway and thermguard settle
cd "$STACK" || exit 1

PREFIX=$(env_get CONTAINER_NAME_PREFIX)
FILES=$(env_get COMPOSE_FILE)
case ":$FILES:" in
  *:compose/strata.yml:*) svc=strata ;;
  *:compose/strata-llm.yml:*) exit 0 ;;   # the chat model is a Strata server elsewhere
  *) svc=vllm-llm ;;
esac
running() { [ "$(timeout 10 docker inspect -f '{{.State.Running}}' "$PREFIX$1" 2>/dev/null)" = true ]; }

running "$svc" && exit 0
if [ -s "$DIR/stopped.list" ]; then
  log "$svc down, but thermguard holds $(tr '\n' ' ' < "$DIR/stopped.list")for heat; waiting"
  exit 0
fi
now=$(date +%s)
if [ "$(awk -v t=$((now - 3600)) '$1 > t' "$STARTS" 2>/dev/null | wc -l)" -ge 3 ]; then
  log "$svc down, but started 3 times within the last hour: waiting (docker logs $PREFIX$svc, $DIR/*.log)"
  exit 0
fi
if [ "$svc" = strata ]; then
  # Strata sizes its GPU tier from the memory free at start: let the stack finish loading first (boot).
  # Settled = the gateway is healthy (or deliberately stopped, or there is none) and no container of the
  # project is still in its health start period (vllm-embedding, whisper load beside the gateway).
  for _ in $(seq 60); do
    g=$(timeout 10 docker inspect -f '{{.State.Running}} {{.State.Health.Status}}' "${PREFIX}openclaw-gateway" 2>/dev/null)
    case "$g" in
      "true healthy"|"false "*|"")
        timeout 20 docker compose ps --format '{{.Health}}' 2>/dev/null | grep -qx starting || break ;;
    esac
    sleep 10
  done
fi
echo "$now" >> "$STARTS"; tail -n 20 "$STARTS" > "$STARTS.tmp" && mv "$STARTS.tmp" "$STARTS"
if out=$(docker compose up -d --no-deps "$svc" 2>&1); then
  log "$svc was down -> started"
else
  log "$svc was down, start failed (exit $?): $(echo "$out" | tail -n 1)"
fi
