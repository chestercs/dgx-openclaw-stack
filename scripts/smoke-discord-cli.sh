#!/usr/bin/env bash
# CLI-side smoke test for the discord-friend agent (Discord forum-channel
# WebSocket inputs require a focused Chrome tab — when running headless
# / via background Chrome MCP, fall back to this CLI verification).
# Each test runs `openclaw agent --agent discord-friend --thinking minimal`
# with a prompt that should trigger a specific tool, then dumps the
# returned toolSummary so you can score PASS/FAIL.
set -euo pipefail
SSH_KEY="${SSH_KEY:-/c/Users/kallo/AppData/Local/NVIDIA Corporation/Sync/config/nvsync.key}"
HOST="${HOST:-192.168.111.100}"
USER_NAME="${USER_NAME:-chestercs}"

run() {
  local label="$1"; shift
  local prompt="$1"; shift
  local extra="${1:-}"
  echo "============================================================"
  echo "[$label]"
  echo "prompt: $prompt"
  ssh -i "$SSH_KEY" -l "$USER_NAME" "$HOST" \
    "docker exec openclaw-cli openclaw agent --agent discord-friend --message $(printf %q "$prompt") --thinking minimal --json --timeout 600 $extra 2>&1 | python3 -c '
import sys, json
out = sys.stdin.read()
# Strip ANSI noise
out = out[out.find(\"{\"):] if \"{\" in out else out
try:
  d = json.loads(out)
except Exception as e:
  print(\"JSON parse failed:\", e)
  print(out[:500])
  sys.exit(0)
print(\"toolSummary:\", json.dumps(d.get(\"toolSummary\"), indent=2))
print(\"reply:\", (d.get(\"finalAssistantVisibleText\") or \"\")[:400])
'"
  echo
}

run "T1 chat" "smoke 1: koszonj egy mondatban"
run "T2 web_search" "Hivd a web_search tool-t es derits ki Budapest jelenlegi homersekletet. Csak a tool-eredmenyt foglald ossze 1 mondatban."
run "T3 image_gen" "Hivd a comfyui_image__generate tool-t egy kis (256x256 ha lehet) kepre: hawaii inges kutya tengerparton."
run "T4 memory" "Hivd a memory_search tool-t a 'cron tool' keresoszora. Roviden mondd el mit talaltal."
run "T5 python_sandbox" "Hivd a python_sandbox__python_exec tool-t es dobj egy 20 oldalu kockat (random.randint(1,20))."
run "T6 cron" "Hivd a cron tool 'add' action-jat: at='+90s', agent='discord-friend', message='cli smoke teszt cron', deleteAfterRun=true. Roviden ack-elj."
run "T7 browser" "Hivd a browser tool-t es nezd meg startlap.hu home page kozos cimet (web_fetch is jo). Csak az oldal cimet ird ki."
run "T8 tts" "A valaszodat kezdd a [[tts:speak]] directive-vel, tartalom: 'szia smallo, ez egy tts smoke teszt'."
