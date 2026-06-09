#!/bin/bash
# Overnight stack watchdog — installed 2026-06-09 for the autonomous smoke session.
# Runs every 5 min via crontab, appends a health line, self-removes after 07:00.
LOG=/home/chestercs/Docker/dgx-openclaw-stack/overnight-healthcheck.log
NOW=$(date '+%Y-%m-%d %H:%M:%S')
HZ=$(curl -sS --max-time 5 http://127.0.0.1:18789/healthz 2>/dev/null)
GPU=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
# count unhealthy openclaw/vllm/comfyui containers (anything not Up)
# Flag only services expected to be RUNNING. Exclude intentionally-stopped:
# config-init (one-shot init), vllm-llm-dense (profile-gated off), tts-fish (not deployed).
DOWN=$(docker ps -a --filter "name=openclaw" --filter "name=vllm" --filter "name=comfyui" \
  --format '{{.Names}} {{.Status}}' \
  | grep -viE 'Up |config-init|vllm-llm-dense|openclaw-tts-fish' | tr '\n' ',' )
echo "$NOW | healthz=${HZ:-DOWN} | gpu=${GPU:-na} | not_up=[${DOWN:-none}]" >> "$LOG"
# self-cleanup after 07:00 (base-10 to avoid octal parse of leading-zero hours)
HH=$(date +%H)
if [ "$((10#$HH))" -ge 7 ] && [ "$((10#$HH))" -lt 12 ]; then
  crontab -l 2>/dev/null | grep -v overnight-healthcheck.sh | crontab -
  echo "$NOW | watchdog self-removed from crontab (>= 07:00)" >> "$LOG"
fi
