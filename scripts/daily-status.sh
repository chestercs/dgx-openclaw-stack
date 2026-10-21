#!/usr/bin/env bash
# daily-status: one short, structure-only health block per day, appended to
# $DIR/status-YYYY-MM.log (and copied to $DIR/latest.txt).
#
# Why: the claim-guard plugin, the nightly people-memory job and the
# community-summary job all fail quietly. Their outcomes sit in three places
# (claim-guard.jsonl, the cron store, the thermguard CSV) and nobody looks
# until the bot misbehaves. One line per item, once a day, makes a week of
# behaviour readable at a glance.
#
# It never prints message text, people-file lines or summaries: only
# counts, sizes, job names and statuses. Run from the host's own cron:
#   15 9 * * * /path/to/scripts/daily-status.sh >/dev/null 2>&1
set -u
DIR=${DIR:-$HOME/daily-status}
GATEWAY=${GATEWAY:-openclaw-gateway}
STATE=${STATE:-/home/node/.openclaw}
HOURS=${HOURS:-24}
THERM_CSV=${THERM_CSV:-$HOME/thermguard/temps.csv}
THERM_EVENTS=${THERM_EVENTS:-$HOME/thermguard/events.log}
# Containers stopped on purpose (space-separated names), e.g. IGNORE="comfyui".
IGNORE=${IGNORE:-}
mkdir -p "$DIR"
OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT
since=$(date -d "-$HOURS hours" '+%F %T')

{
  echo "== $(date '+%F %T') (last ${HOURS} h) =="

  # Gateway and containers. A container that restarts in a loop shows up as
  # "Restarting" here long before anyone notices the bot is gone.
  hz=$(curl -sS -m 5 "${HEALTHZ_URL:-http://127.0.0.1:18789/healthz}" 2>/dev/null)
  echo "gateway: ${hz:-NO ANSWER}"
  bad=$(docker ps -a --format '{{.Names}} {{.Status}}' | grep -E 'Restarting|unhealthy|Exited \([1-9]' \
    | grep -v -- '-init' | awk -v ign=" $IGNORE " 'index(ign, " " $1 " ") == 0' | paste -sd ';' -)
  echo "containers not ok: ${bad:-none}"

  # claim-guard: outcomes per session kind (claim-guard skips heartbeats;
  # eval runs are counted apart), plus the receipt fields of each line:
  # answer latency, input tokens and how many answers each prompt version
  # (AGENTS.md hash) gave.
  docker exec "$GATEWAY" cat "$STATE/logs/claim-guard.jsonl" 2>/dev/null | python3 -c '
import sys, json, datetime as dt
cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=float(sys.argv[1]))
agg, kinds, ms, run_s, tok, pol = {}, {}, [], [], [], {}
for line in sys.stdin:
    try: r = json.loads(line)
    except ValueError: continue
    t = dt.datetime.fromisoformat(r.get("ts", "").replace("Z", "+00:00"))
    if t < cut: continue
    s = r.get("session") or ""
    k = "eval" if ":eval:" in s else "cron" if ":cron:" in s else "discord" if ":discord:" in s else "other"
    o = r.get("outcome", "?")
    agg.setdefault(k, {}).setdefault(o, 0); agg[k][o] += 1
    if o == "revise": kinds[r.get("kind", "?")] = kinds.get(r.get("kind", "?"), 0) + 1
    if r.get("ms"): ms.append(r["ms"])
    if k == "discord" and o != "revise":
        if r.get("runMs"): run_s.append(r["runMs"] / 1000)
        if r.get("tokensIn"): tok.append(r["tokensIn"])
        if r.get("policy"): pol[r["policy"]] = pol.get(r["policy"], 0) + 1
med = lambda xs: sorted(xs)[len(xs) // 2]
p90 = lambda xs: sorted(xs)[min(len(xs) - 1, int(len(xs) * 0.9))]
if not agg: print("claim-guard: no checked turns")
for k, v in sorted(agg.items()):
    print("claim-guard %-9s %s" % (k + ":", " ".join("%s=%d" % kv for kv in sorted(v.items()))))
if kinds: print("claim-guard revise kinds:", " ".join("%s=%d" % kv for kv in sorted(kinds.items())))
if ms: print("claim-guard judge ms: median %d, max %d" % (med(ms), max(ms)))
if run_s: print("discord answers: n=%d, run s median %.0f p90 %.0f" % (len(run_s), med(run_s), p90(run_s)))
if tok: print("discord tokens in: median %d, p90 %d" % (med(tok), p90(tok)))
if pol: print("discord prompt versions:", " ".join("%s=%d" % kv for kv in sorted(pol.items())))
' "$HOURS"

  # Scheduled jobs: last run and status of every job, flagged when the last
  # run failed or a job that should have run did not (next run over an hour
  # in the past). "skipped" is a heartbeat whose automation scratch is empty.
  docker exec "$GATEWAY" openclaw cron list --json 2>/dev/null | python3 -c '
import sys, json, time
try: j = json.load(sys.stdin)
except ValueError: print("cron: list failed"); sys.exit()
jobs = j.get("jobs", []) if isinstance(j, dict) else j
fmt = lambda ms: time.strftime("%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "never"
for x in sorted(jobs, key=lambda x: x.get("name", "")):
    st = x.get("state") or {}
    status = st.get("lastStatus") or st.get("lastRunStatus") or "-"
    nxt = st.get("nextRunAtMs") or 0
    flag = ""
    if x.get("enabled") and status not in ("ok", "skipped", "-"): flag = "  <-- CHECK"
    elif x.get("enabled") and nxt and nxt < (time.time() - 3600) * 1000: flag = "  <-- OVERDUE"
    print("job %-40s %s last %s %s next %s%s" % (x.get("name", "?")[:40], "on " if x.get("enabled") else "off",
          fmt(st.get("lastRunAtMs")), status, fmt(st.get("nextRunAtMs")), flag))
'

  # Memory files: sizes and counts only.
  docker exec "$GATEWAY" sh -c "
    p=$STATE/workspace-discord/people/MEMORY.md
    echo \"people file: \$(wc -c < \$p) bytes, \$(grep -c '^## ' \$p) people, \$(grep -c '(stílus)' \$p) style lines\"
    echo \"community summaries: \$(ls $STATE/workspace-discord/memory/community 2>/dev/null | wc -l) files, latest window \$(ls $STATE/workspace-discord/memory/community 2>/dev/null | sort | tail -1)\"
    echo \"people-digest rejected proposals: \$(find $STATE/people-digest/history -name 'rejected-*' -newermt '$since' 2>/dev/null | wc -l)\"
  " 2>/dev/null

  # Web search: which SearxNG engines answered one Hungarian and one English
  # query. A blocked engine fails silently (fewer results, no error), so this
  # line is the only place it shows. Engine list: searxng/settings/settings.yml.
  for lq in "hu:Magyarország legmagasabb hegye" "en:Mount Everest height"; do
    q=${lq#*:}
    docker exec "$GATEWAY" sh -c "curl -sS --max-time 20 --get --data-urlencode 'q=$q' \
      --data-urlencode categories=general,news 'http://searxng:8080/search?format=json'" 2>/dev/null |
      python3 -c 'import json, sys
d = json.load(sys.stdin)
ok = sorted({e for r in d["results"] for e in r["engines"]})
bad = ["%s (%s)" % (e, m) for e, m in d.get("unresponsive_engines", [])]
print("search %s: %d results, answered: %s%s" % (sys.argv[1], len(d["results"]), ", ".join(ok) or "none",
      "; failed: " + ", ".join(bad) if bad else ""))' "${lq%%:*}" 2>/dev/null || echo "search: SearxNG did not answer"
  done

  # Host: temperatures over the window (thermguard CSV: time,gpu,acpitz),
  # guard events, memory, disk, GPU clock. A clock far below the cap with no
  # load is the PD wedge described in docs/reference/gb10-power-thermal.md.
  if [ -r "$THERM_CSV" ]; then
    awk -F, -v s="$since" '$1 >= s { if ($3 > a) { a = $3; at = $1 } if ($2 > g) g = $2 }
      END { printf "temps: hottest acpitz %s C at %s, gpu max %s C\n", a + 0, at, g + 0 }' "$THERM_CSV"
  fi
  [ -r "$THERM_EVENTS" ] && echo "thermguard events: $(awk -v s="$since" '$1" "$2 >= s && !/ start /' "$THERM_EVENTS" | wc -l)"
  free -g | awk '/^Mem:/ { printf "memory: %s GiB available of %s\n", $7, $2 }'
  df -h /media/usb 2>/dev/null | awk 'NR == 2 { printf "disk /media/usb: %s used, %s free\n", $5, $4 }'
  nvidia-smi --query-gpu=clocks.gr,temperature.gpu,power.draw --format=csv,noheader 2>/dev/null | sed 's/^/gpu now: /'
  echo
} > "$OUT" 2>&1

cat "$OUT" >> "$DIR/status-$(date +%Y-%m).log"
cp "$OUT" "$DIR/latest.txt"
