#!/usr/bin/env bash
# Overnight smoke battery v2 — fixes the bash `label` unbound bug in curl_tts
# and uses plain CLI / agent output (no --json) for the CLI surfaces. Skips
# the already-passing STT roundtrip section.

set -uo pipefail

ENV_FILE="${ENV_FILE:-/home/chestercs/Docker/dgx-openclaw-stack/.env}"
ROUTER_KEY=$(grep '^OPENCLAW_TTS_ROUTER_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
STT_KEY=$(grep '^STT_API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
ROUTER=http://127.0.0.1:8092
STT=http://127.0.0.1:8093

ROWS=/tmp/smoke-rows-v2.tsv
REPORT=/tmp/smoke-report-v2.md
WAVS=/tmp/smoke-wavs-v2
mkdir -p "$WAVS"
: > "$ROWS"
echo -e "n\tcategory\tlabel\tcode\ttime_s\tbytes\tnote" >> "$ROWS"

N=0; PASS=0; FAIL=0

record() {
  local cat="$1" label="$2" code="$3" t="$4" sz="$5" note="$6"
  N=$((N+1))
  echo -e "${N}\t${cat}\t${label}\t${code}\t${t}\t${sz}\t${note}" >> "$ROWS"
  if [[ "$code" == "200" ]]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
  printf "  [%3d] %-22s %-30s code=%s t=%-7s bytes=%-7s %s\n" "$N" "$cat" "$label" "$code" "$t" "$sz" "$note"
}

curl_tts() {
  local label="$1" voice="$2" text="$3"
  local fmt="${4:-wav}"
  local outf="${5:-$WAVS/${label// /_}.${fmt}}"
  local result
  result=$(curl -sS -o "$outf" -w "%{http_code} %{time_total} %{size_download}" \
    -X POST "$ROUTER/v1/audio/speech" \
    -H "Authorization: Bearer $ROUTER_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg i "$text" --arg v "$voice" --arg f "$fmt" '{input:$i, voice:$v, response_format:$f}')" \
    --max-time 90 2>/dev/null) || result="000 0 0"
  echo "$result"
}

# ── 2) Direct EN TTS — Kokoro voices (10) ─────────────────────────────
echo "=== 2. Direct EN TTS (Kokoro voices) ==="
EN_TEXT="Hello world, this is smoke test number"
for v in af_heart af_bella af_nicole af_aoede af_kore af_sarah am_michael am_fenrir am_puck bf_emma; do
  read code t sz < <(curl_tts "en_$v" "$v" "$EN_TEXT $v.")
  record "tts_en_kokoro" "$v" "$code" "$t" "$sz" ""
done

# ── 3) OpenAI alias voices (10) ───────────────────────────────────────
echo "=== 3. OpenAI default voice aliases ==="
for v in alloy ash ballad coral echo fable onyx nova sage shimmer; do
  read code t sz < <(curl_tts "en_alias_$v" "$v" "OpenAI alias smoke for voice $v.")
  record "tts_en_alias" "$v" "$code" "$t" "$sz" ""
done

# ── 4) Direct HU TTS — F5HUN explicit (10) ────────────────────────────
echo "=== 4. Direct HU TTS (F5HUN default_hu, 10 inputs) ==="
HU_TEXTS=(
  "Szia, ez egy hangteszt magyar nyelven."
  "A repülőgép holnap reggel érkezik a budapesti reptérre."
  "Egri csillagok, a Gárdonyi-regény nyolc fejezete bemutatja a török kort."
  "Az időjárás-előrejelzés szerint ma este eshet az eső."
  "Magyarországon a paprika és a fokhagyma a gulyásleves alapja."
  "Október huszonharmadika nemzeti ünnep."
  "A számítógép gyorsabb mint egy emberi agy a matematikai műveletekben."
  "Petőfi Sándor a magyar irodalom egyik legnagyobb költője."
  "A kávézó tizennégy órakor zár, ne maradj túl sokáig."
  "Az ősz színei lenyűgöző látványt nyújtanak a Mátrában."
)
for i in 0 1 2 3 4 5 6 7 8 9; do
  read code t sz < <(curl_tts "hu_${i}" "default_hu" "${HU_TEXTS[$i]}")
  record "tts_hu_explicit" "input_$i" "$code" "$t" "$sz" ""
done

# ── 5) HU autoroute (OpenAI voice + magyar diacritics) (10) ───────────
echo "=== 5. HU autoroute (voice=<openai> + magyar text) ==="
for v in alloy ash ballad coral echo fable onyx nova sage shimmer; do
  read code t sz < <(curl_tts "ar_$v" "$v" "Üdvözöllek, az autoroute teszt voice=$v hanggal.")
  record "tts_hu_autoroute" "$v" "$code" "$t" "$sz" ""
done

# ── 6) Format variants (10) ───────────────────────────────────────────
echo "=== 6. Response format variants ==="
for fmt in mp3 ogg opus aac flac; do
  read code t sz < <(curl_tts "fmt_en_$fmt" "af_heart" "Format $fmt smoke test." "$fmt")
  record "tts_fmt_en" "$fmt" "$code" "$t" "$sz" ""
done
for fmt in mp3 ogg opus aac flac; do
  read code t sz < <(curl_tts "fmt_hu_$fmt" "default_hu" "Magyar formátum $fmt teszt." "$fmt")
  record "tts_fmt_hu" "$fmt" "$code" "$t" "$sz" ""
done

# ── 9) CLI: openclaw infer tts convert (5) ────────────────────────────
echo "=== 9. CLI openclaw infer tts convert ==="
for i in 1 2 3 4 5; do
  t_start=$(date +%s.%N)
  out=$(docker exec openclaw-cli openclaw infer tts convert \
    --text "CLI surface smoke test number $i, English voice." \
    --voice af_heart \
    --output /tmp/cli_tts_${i}.mp3 2>&1 || true)
  t_end=$(date +%s.%N)
  dt=$(awk "BEGIN{print $t_end - $t_start}")
  # Plain output: success line is "tts.convert via local" + "provider: openai" + path
  prov=$(echo "$out" | grep -oE "provider: [^ ]+" | head -1 | awk '{print $2}')
  if echo "$out" | grep -q "^tts\.convert"; then code="200"; else code="500"; fi
  note="provider=${prov:-?}"
  record "cli_infer_tts" "run_$i" "$code" "$dt" "0" "$note"
done

# ── 10) Discord-routed agent (5) ──────────────────────────────────────
echo "=== 10. Discord-routed agent ==="
PROMPTS=(
  "What is the capital of Hungary? Reply with one word."
  "Generate a brief two-sentence English greeting for a TTS smoke test."
  "Mondj egy rövid magyar köszöntést."
  "Reply with exactly: TEST OK."
  "Write a one-sentence summary of round-trip speech testing."
)
for i in 0 1 2 3 4; do
  t_start=$(date +%s.%N)
  out=$(docker exec openclaw-cli openclaw agent --agent discord-friend \
    --message "${PROMPTS[$i]}" --thinking off --timeout 120 2>&1 || true)
  t_end=$(date +%s.%N)
  dt=$(awk "BEGIN{print $t_end - $t_start}")
  reply=$(echo "$out" | tail -1 | tr -d '\n' | head -c 100)
  if [[ -n "$reply" ]] && ! echo "$out" | grep -qE "^Error|error:"; then code="200"; else code="500"; fi
  record "agent_discord" "prompt_$i" "$code" "$dt" "${#reply}" "reply=\"$reply\""
done

# ── Summary ───────────────────────────────────────────────────────────
echo ""
echo "============================================================"
echo "BATTERY v2 DONE: total=$N pass=$PASS fail=$FAIL"
echo "============================================================"

{
  echo "# Smoke battery v2 report — 2026-05-19"
  echo ""
  echo "**Total**: $N | **Pass**: $PASS | **Fail**: $FAIL"
  echo ""
  echo "## Per-category"
  echo ""
  echo "| Category | Total | Pass | Fail |"
  echo "|---|---:|---:|---:|"
  awk -F'\t' 'NR>1 {tot[$2]++; if($4=="200") pass[$2]++; else fail[$2]++} END{for(c in tot) printf "| %s | %d | %d | %d |\n", c, tot[c], pass[c]+0, fail[c]+0}' "$ROWS" | sort
  echo ""
  echo "## Row dump"
  echo '```'
  cat "$ROWS"
  echo '```'
} > "$REPORT"
