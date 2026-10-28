#!/usr/bin/env bash
# Start the services that share openclaw-gateway's network namespace
# (`network_mode: service:openclaw-gateway`: openclaw-cli, openclaw-meshcore-
# bridge) once the gateway is up.
#
# Why: after a host reboot the Docker daemon starts restart-policy containers
# in no particular order. A container that joins another container's network
# namespace fails with "joining network namespace of container: No such
# container" when it comes up before the gateway, and the daemon does not
# retry that failure. openclaw-cli has no restart policy at all. Seen on
# 2026-10-19: both were down after a reboot until started by hand.
#
# The same breakage follows every gateway recreate or restart: a sharer that
# is not recreated with it keeps running inside the old, now-dead namespace
# (no network at all, and "running" in docker ps). The meshcore bridge hit
# this on each gateway recreate on 2026-10-20.
#
# Two modes:
#   netns-dependents-up.sh          one pass, then exit
#   netns-dependents-up.sh --watch  one pass, then one more after every
#                                   gateway container start (docker events)
# Run the watcher from cron; flock keeps it to one copy and the */5 line
# brings it back if docker events ever ends (daemon restart):
#   @reboot     /path/to/repo/scripts/netns-dependents-up.sh --watch
#   */5 * * * * /path/to/repo/scripts/netns-dependents-up.sh --watch
# Safe to run any time; it only starts or recreates what is not attached.
set -u
cd "$(dirname "$0")/.." || exit 1
SERVICES=${NETNS_SERVICES:-"openclaw-cli openclaw-meshcore-bridge"}
PROFILES=${NETNS_PROFILES:-"--profile meshcore"}
log() { echo "$(date '+%F %T') netns-dependents: $*"; }
started_ns() { date -d "$(docker inspect -f '{{.State.StartedAt}}' "$1")" +%s%N; }

# Recreate a sharer; on failure clear compose's half-made "<id>_<service>" container and try once more.
recreate() {
  # shellcheck disable=SC2086
  docker compose $PROFILES up -d --no-deps --force-recreate "$1" >/dev/null 2>&1 && return 0
  docker ps -aq --filter "name=_$1\$" --filter status=created | xargs -r docker rm >/dev/null 2>&1
  # shellcheck disable=SC2086
  docker compose $PROFILES up -d --no-deps --force-recreate "$1" >/dev/null 2>&1
}

reconcile() {
  local gw gw_id gw_start svc cid ns
  # One pass at a time: the watcher's pass after a gateway start and a one-shot run (mode switches, a hand
  # run after a recreate) used to recreate the bridge at the same moment; both failed and left a
  # "<id>_openclaw-meshcore-bridge" container in Created (2026-10-28, three times).
  exec 8>"${NETNS_LOCK:-/tmp/netns-dependents.lock}.pass"
  flock -w 900 8 || { log "another pass held the lock for 15 min, skipped"; return 1; }
  gw=$(docker compose ps -q openclaw-gateway 2>/dev/null)
  for _ in $(seq 1 120); do
    [ -n "$gw" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$gw" 2>/dev/null)" = healthy ] && break
    sleep 5; gw=$(docker compose ps -q openclaw-gateway 2>/dev/null)
  done
  [ -z "$gw" ] && { log "gateway not found, giving up"; flock -u 8; return 1; }
  gw_id=$(docker inspect -f '{{.Id}}' "$gw")
  gw_start=$(started_ns "$gw")

  for svc in $SERVICES; do
    # shellcheck disable=SC2086
    cid=$(docker compose $PROFILES ps -a -q "$svc" 2>/dev/null)
    [ -z "$cid" ] && { log "$svc: no container (profile off?), skipped"; continue; }
    ns=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$cid")
    if [ "$ns" != "container:$gw_id" ]; then
      # Points at an old gateway container: a plain start can never succeed.
      recreate "$svc" && log "$svc: recreated (stale namespace)" || log "$svc: recreate failed"
    elif [ "$(docker inspect -f '{{.State.Running}}' "$cid")" != true ]; then
      docker start "$cid" >/dev/null && log "$svc: started"
    elif [ "$(started_ns "$cid")" -lt "$gw_start" ]; then
      # Same gateway container, but it restarted after this one joined.
      docker restart "$cid" >/dev/null && log "$svc: restarted (gateway restarted after it)"
    else
      log "$svc: running"
    fi
  done
  flock -u 8
}

if [ "${1:-}" != --watch ]; then
  reconcile
  exit
fi

exec 9>"${NETNS_LOCK:-/tmp/netns-dependents.lock}"
flock -n 9 || exit 0
log "watcher started"
reconcile
docker events --filter type=container --filter event=start \
  --filter label=com.docker.compose.service=openclaw-gateway --format '{{.Actor.ID}}' |
  while read -r _; do
    log "gateway started"
    reconcile
  done
log "watcher ended (docker events stopped)"
