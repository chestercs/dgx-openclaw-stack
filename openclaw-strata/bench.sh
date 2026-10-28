#!/bin/bash
# Same prompts for every configuration: code x2, prose x2, one ~25K-token read. Prints per-request metrics
# from the server's /metrics (prefill and decode tok/s, DSpark drafts). Run on the Docker host:
#   bash openclaw-strata/bench.sh
cd "$(dirname "$0")/.." || exit 1
env_get() { grep -s "^$1=" .env | head -1 | cut -d= -f2- | tr -d "\"'"; }
KEY=$(env_get STRATA_API_KEY); KEY=${KEY%%,*}
URL=http://127.0.0.1:$(env_get STRATA_PORT | grep . || echo 8097)
SRC=$(env_get STRATA_SRC)
[ -r "$SRC/serve/server.py" ] || { echo "bench.sh: STRATA_SRC in .env must point at the Strata checkout" >&2; exit 1; }
MODEL=$(env_get STRATA_MODEL_ID | grep . || echo deepseek-v4.1-flash)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
ask() {  # ask <max_tokens> <text-file> <label>
  python3 -c "import json,sys;print(json.dumps({'model':sys.argv[3],'max_tokens':int(sys.argv[1]),'messages':[{'role':'user','content':open(sys.argv[2]).read()}]}))" "$1" "$2" "$MODEL" > "$T/b.json"
  curl -s -m 1200 "$URL/v1/messages" -H "x-api-key: $KEY" -H "content-type: application/json" -d @"$T/b.json" > /dev/null
  curl -s "$URL/metrics" -H "x-api-key: $KEY" | python3 -c "
import json,sys;r=json.load(sys.stdin)['requests'][0]
pt=r['prompt_tokens'];new=pt-(r.get('reused') or 0)
print('%-6s prompt %6d new %6d (%5s tok/s)  out %4d  decode %5.1f tok/s  drafts %s/%s  %5.1f s' % (sys.argv[1], pt, new, round(new/r['prompt_ms']*1000) if r['prompt_ms'] else '-', r['output_tokens'], r['decode_tok_s'] or 0, r.get('drafts_accepted'), r.get('drafts_offered'), r['duration_s']))" "$3"
}
printf "%s" "Write a complete Python module: an LRU cache class with O(1) get/put (no OrderedDict, your own doubly linked list), type annotations and docstrings, then pytest tests for it. Output only the code." > "$T/code.txt"
printf "%s" "Explain in a few plain paragraphs how the mixture-of-experts architecture works in large language models, and why it runs faster than a dense model of the same size." > "$T/prose.txt"
{ printf "%s\n\n" "Here is the start of a Python source file:"; head -c 95000 "$SRC/serve/server.py"; printf "\n\n%s" "Summarise in 5 points what this server does."; } > "$T/long.txt"
ask 700 "$T/code.txt" code; ask 700 "$T/code.txt" code
ask 500 "$T/prose.txt" prose; ask 500 "$T/prose.txt" prose
ask 300 "$T/long.txt" long
echo "MemAvailable $(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo) MB"
