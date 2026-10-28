#!/usr/bin/env bash
# Sustained-load thermal test: Strata generates back to back for DURATION s while the hottest acpitz zone,
# the GPU and the average clock of each core type are logged every second. A request is cut when acpitz
# reaches ABORT_AT, well before thermguard's 93 C kill. Prints tok/s and temperatures at the end.
#
#   bash openclaw-strata/soak.sh                       # 15 min, on the Docker host
#   DURATION=1800 LABEL=a725 bash openclaw-strata/soak.sh
set -u
cd "$(dirname "$0")/.." || exit 1
env_get() { grep -s "^$1=" .env | head -1 | cut -d= -f2- | tr -d "\"'"; }
DURATION=${DURATION:-900}
ABORT_AT=${ABORT_AT:-89}
LABEL=${LABEL:-run}
DIR=${SOAK_DIR:-$HOME/strata-soak}
mkdir -p "$DIR"
STAMP=$(date +%m%d-%H%M)
TEMPS=$DIR/$STAMP-$LABEL-temps.csv
REQS=$DIR/$STAMP-$LABEL-requests.csv
KEY=$(env_get STRATA_API_KEY); KEY=${KEY%%,*}
URL=http://127.0.0.1:$(env_get STRATA_PORT | grep . || echo 8097)/v1/chat/completions
MODEL=$(env_get STRATA_MODEL_ID | grep . || echo deepseek-v4.1-flash)
# A coding answer: the usual sclaude/Discord load, and DSpark drafts it.
PROMPT='Write a complete, well-commented Python module that implements a sparse matrix class (CSR) with addition, multiplication, transpose, slicing and a conjugate-gradient solver, followed by thorough pytest tests. Output only the code.'

acpi() { local a=0 v z; for z in /sys/class/thermal/thermal_zone*/temp; do v=$(( $(cat "$z") / 1000 )); [ "$v" -gt "$a" ] && a=$v; done; echo "$a"; }
mhz() { local f; f=$(cat /sys/devices/system/cpu/cpufreq/policy"$1"/cpuinfo_avg_freq 2>/dev/null); echo $(( ${f:-0} / 1000 )); }
busy() {   # per core: "<n> <busy jiffies> <total jiffies>"; the sampler turns two of these into A725/X925 load
           # (GB10: cores 0-4 and 10-14 are A725, 5-9 and 15-19 X925)
  awk '/^cpu[0-9]/ { n = substr($1, 4); b = $2+$3+$4+$7+$8; t = b+$5+$6; print n, b, t }' /proc/stat
}

echo "t,acpi,gpu_c,gpu_w,a725_mhz,x925_mhz,a725_busy,x925_busy" > "$TEMPS"
echo "t,completion_tokens,seconds,tok_s,finish" > "$REQS"
T0=$(date +%s)
END=$(( T0 + DURATION ))
rm -f "$DIR/.abort"

(   # sampler and hot-abort
  prev=$(busy)
  while [ "$(date +%s)" -lt "$END" ]; do
    sleep 1
    cur=$(busy)
    read -r ab xb <<<"$(join <(echo "$prev" | sort) <(echo "$cur" | sort) | awk '
      { d = ($4-$2) / (($5-$3) > 0 ? ($5-$3) : 1); if ($1 % 10 < 5) { a += d; na++ } else { x += d; nx++ } }
      END { printf "%.0f %.0f\n", 100*a/na, 100*x/nx }')"
    prev=$cur
    a=$(acpi)
    IFS=, read -r gc gw <<<"$(nvidia-smi --query-gpu=temperature.gpu,power.draw --format=csv,noheader,nounits | tr -d ' ')"
    echo "$(( $(date +%s) - T0 )),$a,$gc,$gw,$(mhz 0),$(mhz 5),$ab,$xb" >> "$TEMPS"
    if [ "$a" -ge "$ABORT_AT" ]; then
      touch "$DIR/.abort"; pkill -f "soak-req-$STAMP" 2>/dev/null; break
    fi
  done
) &
SAMPLER=$!

while [ "$(date +%s)" -lt "$END" ] && [ ! -e "$DIR/.abort" ]; do
  s=$(date +%s.%N)
  r=$(curl -s -m "$DURATION" "$URL" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
      -H "X-Soak: soak-req-$STAMP" \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"model":sys.argv[2],"max_tokens":1500,
            "messages":[{"role":"user","content":sys.argv[1]}],"chat_template_kwargs":{"enable_thinking":False}}))' "$PROMPT" "$MODEL")")
  e=$(date +%s.%N)
  python3 - "$r" "$s" "$e" "$T0" >> "$REQS" <<'EOF'
import json, sys
try:
    d = json.loads(sys.argv[1]); n = d["usage"]["completion_tokens"]; f = d["choices"][0]["finish_reason"]
except Exception:
    n, f = 0, "error"
s, e, t0 = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
print(f"{int(e - t0)},{n},{e - s:.1f},{n / (e - s):.1f},{f}")
EOF
done
kill "$SAMPLER" 2>/dev/null; wait 2>/dev/null

python3 - "$TEMPS" "$REQS" "$LABEL" "$([ -e "$DIR/.abort" ] && echo yes || echo no)" <<'EOF'
import csv, statistics as st, sys
temps = list(csv.DictReader(open(sys.argv[1]))); reqs = [r for r in csv.DictReader(open(sys.argv[2])) if r["finish"] != "error"]
if not temps:
    sys.exit(f"{sys.argv[3]}: no samples")
tail = [int(r["acpi"]) for r in temps if int(r["t"]) >= int(temps[-1]["t"]) - 300]
toks = sum(int(r["completion_tokens"]) for r in reqs); secs = sum(float(r["seconds"]) for r in reqs)
col = lambda k: [float(r[k] or 0) for r in temps]
print(f"{sys.argv[3]}: {len(reqs)} requests, {toks} tokens, {toks / secs if secs else 0:.1f} tok/s overall; "
      f"acpi max {max(col('acpi')):.0f} C, last-5-min median {st.median(tail):.0f} C; gpu max {max(col('gpu_c')):.0f} C, "
      f"gpu W median {st.median(col('gpu_w')):.0f}; busy a725 {st.median(col('a725_busy')):.0f}% x925 {st.median(col('x925_busy')):.0f}%; "
      f"aborted: {sys.argv[4]}")
EOF
echo "logs: $TEMPS $REQS"
