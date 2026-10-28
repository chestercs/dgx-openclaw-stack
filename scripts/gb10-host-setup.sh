#!/usr/bin/env bash
# gb10-host-setup: what this stack needs on the GB10 outside Docker, installed from the repo and checked
# against it. Idempotent; a re-run changes nothing. docs/reference/host-setup.md lists every piece and why.
#
#   bash scripts/gb10-host-setup.sh check          compare the host with the repo; changes nothing, no sudo
#   bash scripts/gb10-host-setup.sh cron           (re)write this stack's crontab block (run as the operator)
#   sudo bash scripts/gb10-host-setup.sh system    install/refresh the systemd units and the polkit rule
#   sudo bash scripts/gb10-host-setup.sh resilience   sysctl, hardware watchdog, sshd OOM protection
#                                                  (scripts/gb10-resilience-setup.sh)
#
# The crontab block is generated from .env: the guards watch vllm-llm and comfyui, plus strata when
# COMPOSE_FILE has compose/strata.yml (names with CONTAINER_NAME_PREFIX). Extra guard targets:
# GB10_GUARD_EXTRA in .env; containers daily-status should not flag: DAILY_STATUS_IGNORE. Lines outside the
# block are kept, except older copies of the block's own lines (same script paths), which it replaces.
set -euo pipefail
STACK=$(cd "$(dirname "$0")/.." && pwd)
S=$STACK/scripts
BEGIN="# >>> dgx-openclaw-stack (scripts/gb10-host-setup.sh cron; edit .env, not here) >>>"
END="# <<< dgx-openclaw-stack <<<"
UNITS="gb10-clock-cap.service gb10-drop-caches.service"
RULE=/etc/polkit-1/rules.d/50-gb10-drop-caches.rules

env_get() { grep -s "^$1=" "$STACK/.env" | head -1 | cut -d= -f2- | tr -d "\"'"; }

cron_block() {
  local p watch targets ignore
  p=$(env_get CONTAINER_NAME_PREFIX)
  watch="${p}vllm-llm"; targets="${p}vllm-llm"
  case ":$(env_get COMPOSE_FILE):" in *:compose/strata.yml:*) watch="$watch ${p}strata"; targets="$targets ${p}strata" ;; esac
  watch="$watch ${p}comfyui"
  if [ -n "$(env_get GB10_GUARD_EXTRA)" ]; then watch="$watch $(env_get GB10_GUARD_EXTRA)"; targets="$targets $(env_get GB10_GUARD_EXTRA)"; fi
  ignore=$(env_get DAILY_STATUS_IGNORE)
  [ -n "$ignore" ] && ignore="IGNORE=\"$ignore\" "   # quotes inside \${...} in a here-doc would be removed
  cat <<EOF
$BEGIN
# thermal backstop (the fix is gb10-clock-cap.service) and the memory guard: they kill these containers
@reboot     WATCH="$watch" $S/gb10-thermguard.sh >/dev/null 2>&1
*/5 * * * * WATCH="$watch" $S/gb10-thermguard.sh >/dev/null 2>&1
@reboot     TARGETS="$targets" $S/gb10-memguard.sh
*/5 * * * * TARGETS="$targets" $S/gb10-memguard.sh
# brings the chat model back after a guard's kill or a reboot (Strata: after the stack has loaded)
@reboot     $S/llm-ensure.sh boot
*/5 * * * * $S/llm-ensure.sh
# re-attaches openclaw-cli and the MeshCore bridge to a recreated gateway's network namespace
@reboot     $S/netns-dependents-up.sh --watch >> $HOME/netns-dependents.log 2>&1
*/5 * * * * $S/netns-dependents-up.sh --watch >> $HOME/netns-dependents.log 2>&1
# one structure-only health block per day in ~/daily-status/
15 9 * * *  $ignore$S/daily-status.sh >/dev/null 2>&1
$END
EOF
}

# The installed crontab minus our block and minus loose copies of the block's lines.
cron_rest() {
  crontab -l 2>/dev/null | awk -v b="$BEGIN" -v e="$END" '$0 == b {skip = 1} !skip {print} $0 == e {skip = 0}' \
    | grep -vF -e "$S/gb10-thermguard.sh" -e "$S/gb10-memguard.sh" -e "$S/llm-ensure.sh" \
                -e "$S/netns-dependents-up.sh" -e "$S/daily-status.sh" || true
}

do_cron() {
  [ "$(id -u)" != 0 ] || { echo "run 'cron' as the operator, not root" >&2; exit 1; }
  local new; new=$( { cron_rest; cron_block; } )
  if [ "$new" = "$(crontab -l 2>/dev/null)" ]; then echo "crontab: already in the desired state"; return; fi
  printf '%s\n' "$new" | crontab -
  echo "crontab: written"
  echo "running guards keep their old WATCH/TARGETS until restarted (they hold a lock; see host-setup.md)"
}

do_system() {
  [ "$(id -u)" = 0 ] || { echo "run 'system' with sudo" >&2; exit 1; }
  local u
  for u in $UNITS; do install -m 644 "$S/$u" "/etc/systemd/system/$u"; done
  install -m 644 "$S/gb10-drop-caches.rules" "$RULE"
  systemctl daemon-reload
  systemctl enable --now gb10-clock-cap.service >/dev/null
  echo "systemd units and polkit rule installed; gb10-clock-cap enabled"
}

line() { printf '%-8s %s\n' "$1" "$2"; }

do_check() {
  local u f
  for u in $UNITS; do
    f=/etc/systemd/system/$u
    if [ ! -e "$f" ]; then line MISSING "$u (sudo ... system)"
    elif cmp -s "$f" "$S/$u"; then line ok "$u"
    elif diff -q <(grep -v '^#' "$f") <(grep -v '^#' "$S/$u") >/dev/null; then line ok "$u (comments differ from the repo)"
    else line DIFF "$u differs from the repo (sudo ... system)"; fi
  done
  [ "$(systemctl is-enabled gb10-clock-cap.service 2>/dev/null)" = enabled ] && [ "$(systemctl is-active gb10-clock-cap.service 2>/dev/null)" = active ] \
    && line ok "gb10-clock-cap enabled and active ($(nvidia-smi --query-gpu=clocks.sm --format=csv,noheader 2>/dev/null))" \
    || line DIFF "gb10-clock-cap not enabled/active"
  # The polkit rule cannot be read or queried with details by the operator (polkit allows that to root only);
  # the journal shows whether llm-ensure's last drop went through.
  local last; last=$(journalctl -u gb10-drop-caches.service -n 20 --no-pager -o short-iso 2>/dev/null | grep "Finished" | tail -1 | cut -d' ' -f1)
  if [ -n "$last" ] && ! tail -n 3 "$HOME/thermguard/llm-ensure.log" 2>/dev/null | grep -q "page cache not dropped"; then
    line ok "polkit: gb10-drop-caches started without a password (last $last)"
  else line UNKNOWN "polkit rule: try 'systemctl start --no-ask-password gb10-drop-caches' (if refused: sudo ... system)"; fi
  local sw mf
  sw=$(cat /proc/sys/vm/swappiness); mf=$(cat /proc/sys/vm/min_free_kbytes)
  if [ "$sw" = 10 ] && [ "$mf" = 1048576 ] && [ "$(systemctl show -p RuntimeWatchdogUSec --value)" != 0 ]; then
    line ok "resilience: swappiness 10, min_free_kbytes 1 GiB, hardware watchdog"
  else line MISSING "resilience (swappiness $sw, min_free_kbytes $mf, watchdog $(systemctl show -p RuntimeWatchdogUSec --value)): sudo ... resilience"; fi
  if diff -q <(crontab -l 2>/dev/null | awk -v b="$BEGIN" -v e="$END" '$0 == b {on = 1} on {print} $0 == e {on = 0}') <(cron_block) >/dev/null; then
    line ok "crontab block"
  else line DIFF "crontab block differs or is missing (... cron); wanted:"; cron_block | sed 's/^/           /'; fi
  local g want
  for g in gb10-thermguard gb10-memguard; do
    want=$(cron_block | grep -m1 "$g.sh" | sed -E 's/^@reboot +//; s/ \/.*//' | tr -d '"')
    # the guard itself, not cron's `sh -c VAR=... script` wrapper (which has no VAR in its environment)
    local pid; pid=$(pgrep -f "^(/usr)?(/bin/)?bash [^ ]*scripts/$g\.sh$" | head -1 || true)
    if [ -z "$pid" ]; then line MISSING "$g not running (cron starts it within 5 min)"
    elif tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qxF "$want"; then line ok "$g running with $want"
    else line DIFF "$g running with an older list (kill it; cron restarts it with $want)"; fi
  done
  [ -e "$HOME/.llm-ensure-off" ] && line NOTE "~/.llm-ensure-off exists: llm-ensure is paused" || true
}

case "${1:-check}" in
  check) do_check ;;
  cron) do_cron ;;
  system) do_system ;;
  resilience) exec bash "$S/gb10-resilience-setup.sh" ;;
  *) echo "usage: $0 check|cron|system|resilience" >&2; exit 2 ;;
esac
