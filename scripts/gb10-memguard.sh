#!/usr/bin/env bash
# gb10-memguard: SIGKILL a big trial LLM container before the unified memory
# pool runs dry.
#
# Why: on GB10 the GPU and the OS share one 121 GiB pool. Once MemAvailable
# reaches zero the kernel thrashes in reclaim instead of OOM-killing: on
# 2026-10-19 a Qwen3.5-122B load took the box to load 500+ with no SSH for
# ~30 min, and only a `pkill -9` from the local console recovered it. Swap
# makes this slower, not safer. Kill early, while docker still answers.
#
# Run from cron:
#   @reboot   /path/to/scripts/gb10-memguard.sh
#   */5 * * * * /path/to/scripts/gb10-memguard.sh
set -u
MIN_AVAIL_MB=${MIN_AVAIL_MB:-6144}
TARGETS=${TARGETS:-"vllm-llm vllm-qwen vllm-nemotron"}
INTERVAL=${INTERVAL:-2}
DIR=${DIR:-$HOME/thermguard}
LOG=${LOG:-$DIR/memguard.log}
mkdir -p "$DIR"
exec 9>"$DIR/.memguard.lock"; flock -n 9 || exit 0
echo "$(date '+%F %T') start (kill below ${MIN_AVAIL_MB} MB available)" >> "$LOG"
low=0
while true; do
  avail=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
  # two samples in a row, so a short page-cache dip does not kill the model
  if [ "$avail" -lt "$MIN_AVAIL_MB" ]; then low=$((low+1)); else low=0; fi
  if [ "$low" -ge 2 ]; then
    for c in $TARGETS; do
      if [ "$(timeout 5 docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ]; then
        timeout 20 docker kill "$c" >/dev/null 2>&1
        echo "$(date '+%F %T') LOW avail=${avail}MB -> killed $c" >> "$LOG"
      fi
    done
    low=0
  fi
  sleep "$INTERVAL"
done
