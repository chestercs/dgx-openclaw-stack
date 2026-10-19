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
# Run from cron:  @reboot /path/to/repo/scripts/netns-dependents-up.sh
# Safe to run any time; it only starts or recreates what is not running.
set -u
cd "$(dirname "$0")/.." || exit 1
SERVICES=${NETNS_SERVICES:-"openclaw-cli openclaw-meshcore-bridge"}
PROFILES=${NETNS_PROFILES:-"--profile meshcore"}
log() { echo "$(date '+%F %T') netns-dependents: $*"; }

gw=$(docker compose ps -q openclaw-gateway 2>/dev/null)
for _ in $(seq 1 120); do
  [ -n "$gw" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$gw" 2>/dev/null)" = healthy ] && break
  sleep 5; gw=$(docker compose ps -q openclaw-gateway 2>/dev/null)
done
[ -z "$gw" ] && { log "gateway not found, giving up"; exit 1; }
gw_id=$(docker inspect -f '{{.Id}}' "$gw")

for svc in $SERVICES; do
  # shellcheck disable=SC2086
  cid=$(docker compose $PROFILES ps -a -q "$svc" 2>/dev/null)
  [ -z "$cid" ] && { log "$svc: no container (profile off?), skipped"; continue; }
  ns=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$cid")
  # A sharer can keep running after the gateway was recreated, inside the
  # old, now-dead namespace (no network at all), so check that first.
  if [ "$(docker inspect -f '{{.State.Running}}' "$cid")" = true ] && [ "$ns" = "container:$gw_id" ]; then
    log "$svc: running"; continue
  fi
  if [ "$ns" = "container:$gw_id" ]; then
    docker start "$cid" >/dev/null && log "$svc: started"
  else
    # Points at an old gateway container: a plain start can never succeed.
    # shellcheck disable=SC2086
    docker compose $PROFILES up -d --no-deps --force-recreate "$svc" >/dev/null 2>&1 && log "$svc: recreated (stale namespace)"
  fi
done
