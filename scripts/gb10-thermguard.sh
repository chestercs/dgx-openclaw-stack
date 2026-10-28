#!/usr/bin/env bash
# gb10-thermguard: stop the GPU load before a GB10 box powers itself off.
#
# Why: under sustained LLM load a GB10 (DGX Spark / ASUS GX10) hard-powers-off
# with nothing in the journal. The ACPI critical trip says 104.8 C, but the
# boxes die much earlier: on the reference GX10 the last sample before the
# 2026-10-19 16:02 power-off was acpitz 94 C (community reports 94.6-97.8 C).
# nvidia-smi reads 8-15 C lower than the hottest acpitz zone, so watch both.
# A guard at 97 C with 5 s polling and `docker stop -t 10` was too slow.
# The real fix is a GPU clock cap (see gb10-clock-cap.service); this guard is
# the backstop.
#
#   >= STOP_AT              : SIGKILL the watched containers that run (remember them)
#   <  RESUME_BELOW for RESUME_HOLD s : start the ones it killed, once
# Single instance via flock. Run from cron:
#   @reboot   /path/to/scripts/gb10-thermguard.sh >/dev/null 2>&1
#   */5 * * * * /path/to/scripts/gb10-thermguard.sh >/dev/null 2>&1
set -u
STOP_AT=${STOP_AT:-93}
RESUME_BELOW=${RESUME_BELOW:-75}
RESUME_HOLD=${RESUME_HOLD:-120}
INTERVAL=${INTERVAL:-2}
WATCH=${WATCH:-"vllm-llm vllm-qwen vllm-nemotron comfyui"}
DIR=${DIR:-$HOME/thermguard}
mkdir -p "$DIR"
LOG=$DIR/events.log; CSV=$DIR/temps.csv; STATE=$DIR/stopped.list

exec 9>"$DIR/.lock"
flock -n 9 || exit 0

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
temps() {
  local g a=0 v z
  g=$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  for z in /sys/class/thermal/thermal_zone*/temp; do v=$(( $(cat "$z") / 1000 )); [ "$v" -gt "$a" ] && a=$v; done
  echo "${g:-0} $a"
}

log "start (stop at ${STOP_AT}C, resume below ${RESUME_BELOW}C for ${RESUME_HOLD}s)"
# A list from before a reboot (the kill came just before a thermal power-off) must not start the models two
# minutes into boot, while the stack is still loading: llm-ensure.sh starts them in order instead.
boot=$(( $(date +%s) - $(cut -d. -f1 /proc/uptime) ))
if [ -s "$STATE" ] && [ "$(stat -c %Y "$STATE")" -lt "$boot" ]; then
  log "dropping the pre-reboot list ($(tr '\n' ' ' < "$STATE")): llm-ensure.sh starts the chat model after the stack"
  : > "$STATE"
fi
cool_since=0; last_sample=0
while true; do
  read -r g a <<<"$(temps)"; hot=$(( g > a ? g : a )); now=$(date +%s)
  if [ $(( now - last_sample )) -ge 60 ]; then
    echo "$(date '+%F %T'),$g,$a" >> "$CSV"; last_sample=$now
    [ "$(wc -l < "$CSV")" -gt 20000 ] && tail -n 10000 "$CSV" > "$CSV.tmp" && mv "$CSV.tmp" "$CSV"
  fi
  if [ "$hot" -ge "$STOP_AT" ]; then
    for c in $WATCH; do
      if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" = true ]; then
        # kill, not stop: a graceful vLLM shutdown keeps the GPU busy for seconds
        timeout 20 docker kill "$c" >/dev/null 2>&1 && { grep -qx "$c" "$STATE" 2>/dev/null || echo "$c" >> "$STATE"; log "HOT gpu=${g}C acpi=${a}C -> killed $c"; }
      fi
    done
    cool_since=0
  elif [ -s "$STATE" ]; then
    if [ "$hot" -lt "$RESUME_BELOW" ]; then
      [ "$cool_since" -eq 0 ] && cool_since=$now
      if [ $(( now - cool_since )) -ge "$RESUME_HOLD" ]; then
        while read -r c; do docker start "$c" >/dev/null 2>&1 && log "cool gpu=${g}C acpi=${a}C -> started $c"; done < "$STATE"
        : > "$STATE"; cool_since=0
      fi
    else
      cool_since=0
    fi
  fi
  sleep "$INTERVAL"
done
