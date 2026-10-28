#!/bin/bash
# One line per finished Strata request, plus alerts (low memory, GPU temperature, container down, memguard
# kills). Run on the Docker host while testing: bash openclaw-strata/watch.sh
cd "$(dirname "$0")/.." || exit 1
env_get() { grep -s "^$1=" .env | head -1 | cut -d= -f2- | tr -d "\"'"; }
KEY=$(env_get STRATA_API_KEY); KEY=${KEY%%,*}
URL=http://127.0.0.1:$(env_get STRATA_PORT | grep . || echo 8097)
NAME=$(env_get CONTAINER_NAME_PREFIX)strata
MG=$HOME/thermguard/memguard.log
seen=""; minav=999999; mg=$(wc -l < "$MG" 2>/dev/null || echo 0); lastalert=0
while true; do
  av=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo); [ "$av" -lt "$minav" ] && minav=$av
  t=$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
  now=$(date +%s)
  if [ "$av" -lt 8000 ] && [ $((now-lastalert)) -gt 60 ]; then echo "ALERT low memory: ${av} MB available"; lastalert=$now; fi
  if [ "${t:-0}" -ge 85 ] && [ $((now-lastalert)) -gt 60 ]; then echo "ALERT GPU ${t} C"; lastalert=$now; fi
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != true ]; then echo "ALERT $NAME not running"; sleep 30; fi
  n=$(wc -l < "$MG" 2>/dev/null || echo 0); [ "$n" -gt "$mg" ] && { tail -n $((n-mg)) "$MG" | sed 's/^/MEMGUARD /'; mg=$n; }
  out=$(curl -s -m 5 "$URL/metrics" -H "x-api-key: $KEY" | python3 -c "
import json,sys
seen=set(sys.argv[1].split(','))
d=json.load(sys.stdin)
for r in reversed(d.get('requests',[])):
    if r['id'] in seen: continue
    pt=r.get('prompt_tokens') or 0; pm=r.get('prompt_ms') or 0; new=pt-(r.get('reused') or 0)
    pf=round(new/pm*1000) if pm>0 and new>50 else None
    print('REQ %s prompt %d (new %d, %s tok/s) out %d @ %s tok/s, %.1f s, %s' % (r['id'], pt, new, pf, r.get('output_tokens') or 0, r.get('decode_tok_s'), r.get('duration_s') or 0, r.get('finish')))
" "$seen" 2>/dev/null)
  if [ -n "$out" ]; then
    echo "$out" | sed "s/^REQ [0-9a-f]* /REQ /; s/\$/ | avail ${av} MB (min ${minav}), GPU ${t} C/"
    seen="$seen,$(echo "$out" | awk '{print $2}' | tr '\n' ',')"
  fi
  sleep 5
done
