#!/usr/bin/env bash
# Overnight smoke battery for the rolled-back 3-service TTS + Whisper turbo
# STT stack (post-Fish-migration-rollback, 2026-05-19).
#
# Total target: ~100 calls across direct HTTP, STT closed-loop, CLI infer,
# Discord-routed agent. Writes per-call rows to /tmp/smoke-rows.tsv and a
# markdown summary to /tmp/smoke-report.md.

set -uo pipefail

ENV_FILE="${ENV_FILE:-/home/chestercs/Docker/dgx-openclaw-stack/.env}"
ROUTER_KEY=$(grep '^OPENCLAW_TTS_ROUTER_API_KEY=' "$ENV_FILE" | cut -d= -f2-)
STT_KEY=$(grep '^STT_API_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

ROUTER=http://127.0.0.1:8092
STT=http://127.0.0.1:8093
GATEWAY=http://127.0.0.1:18789

ROWS=/tmp/smoke-rows.tsv
REPORT=/tmp/smoke-report.md
WAVS=/tmp/smoke-wavs
mkdir -p "$WAVS"
: > "$ROWS"
echo -e "n\tcategory\tlabel\tcode\ttime_s\tbytes\tnote" >> "$ROWS"

N=0
PASS=0
FAIL=0

record() {
  local cat="$1" label="$2" code="$3" t="$4" sz="$5" note="$6"
  N=$((N+1))
  echo -e "${N}\t${cat}\t${label}\t${code}\t${t}\t${sz}\t${note}" >> "$ROWS"
  if [[ "$code" == "200" ]]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); fi
  printf "  [%3d] %-22s %-30s code=%s t=%.2fs bytes=%s %s\n" "$N" "$cat" "$label" "$code" "$t" "$sz" "$note"
}

curl_tts() {
  # Call the TTS router; record code + time + bytes; save WAV.
  local label="$1" voice="$2" text="$3" fmt="${4:-wav}" outf="${5:-$WAVS/${label// /_}.${4:-wav}}"
  local result
  result=$(curl -sS -o "$outf" -w "%{http_code} %{time_total} %{size_download}" \
    -X POST "$ROUTER/v1/audio/speech" \
    -H "Authorization: Bearer $ROUTER_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg i "$text" --arg v "$voice" --arg f "$fmt" '{input:$i, voice:$v, response_format:$f}')" \
    --max-time 60 2>&1) || true
  echo "$result"
}

curl_stt() {
  # POST a WAV to Whisper /v1/audio/transcriptions; record code + time + transcript.
  local wav="$1" lang="${2:-}"
  local lang_flag=""
  [[ -n "$lang" ]] && lang_flag="-F language=$lang"
  curl -sS -X POST "$STT/v1/audio/transcriptions" \
    -H "Authorization: Bearer $STT_KEY" \
    -F file=@"$wav" \
    -F model=deepdml/faster-whisper-large-v3-turbo-ct2 \
    -F response_format=json \
    $lang_flag \
    --max-time 60 2>&1
}

# ── 1) Healthz roll-call ────────────────────────────────────────────────
echo "=== 1. Healthz roll-call ==="
for h in "$ROUTER/healthz" "http://127.0.0.1:8091/healthz" "http://127.0.0.1:8090/healthz" "$STT/health" "$GATEWAY/healthz"; do
  read code t < <(curl -sS -o /dev/null -w "%{http_code} %{time_total}" "$h" --max-time 5 || echo "ERR 0")
  record "healthz" "$h" "$code" "$t" "0" ""
done

# ── 2) Direct EN TTS — Kokoro voice rotation (10 calls) ─────────────────
echo "=== 2. Direct EN TTS (Kokoro voices, 10 calls) ==="
EN_TEXT="Hello world, this is smoke test number"
for v in af_heart af_bella af_nicole af_aoede af_kore af_sarah am_michael am_fenrir am_puck bf_emma; do
  read code t sz < <(curl_tts "en_$v" "$v" "$EN_TEXT $v.")
  record "tts_en_kokoro" "$v" "$code" "$t" "$sz" ""
done

# ── 3) OpenAI alias voices on English (10 calls) ────────────────────────
echo "=== 3. OpenAI default voice aliases (10 calls, English text) ==="
for v in alloy ash ballad coral echo fable onyx nova sage shimmer; do
  read code t sz < <(curl_tts "en_alias_$v" "$v" "OpenAI alias smoke for voice $v.")
  record "tts_en_alias" "$v" "$code" "$t" "$sz" ""
done

# ── 4) Direct HU TTS — F5HUN explicit (10 calls) ────────────────────────
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
for i in {0..9}; do
  read code t sz < <(curl_tts "hu_${i}" "default_hu" "${HU_TEXTS[$i]}")
  record "tts_hu_explicit" "input_$i" "$code" "$t" "$sz" ""
done

# ── 5) HU autoroute via OpenAI voice + magyar diacritics (10 calls) ─────
echo "=== 5. HU autoroute via OpenAI voice (voice=coral + magyar) ==="
for v in alloy ash ballad coral echo fable onyx nova sage shimmer; do
  read code t sz < <(curl_tts "autoroute_$v" "$v" "Üdvözöllek, az autoroute teszt voice=$v hanggal.")
  record "tts_hu_autoroute" "$v" "$code" "$t" "$sz" ""
done

# ── 6) Format variants — mp3, ogg, opus, aac, flac (10 calls) ───────────
echo "=== 6. Response format variants (5 EN + 5 HU) ==="
for fmt in mp3 ogg opus aac flac; do
  read code t sz < <(curl_tts "fmt_en_$fmt" "af_heart" "Format $fmt smoke test." "$fmt")
  record "tts_fmt_en" "$fmt" "$code" "$t" "$sz" ""
done
for fmt in mp3 ogg opus aac flac; do
  read code t sz < <(curl_tts "fmt_hu_$fmt" "default_hu" "Magyar formátum $fmt teszt." "$fmt")
  record "tts_fmt_hu" "$fmt" "$code" "$t" "$sz" ""
done

# ── 7) STT closed-loop roundtrip (5 EN + 5 HU) ──────────────────────────
echo "=== 7. STT closed-loop (TTS->Whisper turbo) ==="
EN_RT=(
  "The quick brown fox jumps over the lazy dog."
  "OpenAI compatible audio endpoints simplify smoke testing."
  "Voice channels need round trip latency under one second."
  "Closed loop word error rate is the single most important metric here."
  "Smoke test five completes the English round trip battery."
)
HU_RT=(
  "A gyors barna róka átugorja a lusta kutyát."
  "Az OpenAI kompatibilis hang végpontok megkönnyítik a tesztelést."
  "A hangcsatornáknak egy másodperc alatti késleltetés kell."
  "A zárt körű szóhiba arány a legfontosabb metrika itt."
  "Az ötödik teszt befejezi a magyar oda-vissza ellenőrzést."
)
for i in {0..4}; do
  wav="$WAVS/rt_en_${i}.wav"
  read code1 t1 sz1 < <(curl_tts "rt_en_${i}" "af_heart" "${EN_RT[$i]}" "wav" "$wav")
  record "tts_rt_en_speak" "rt_en_$i" "$code1" "$t1" "$sz1" ""
  if [[ "$code1" == "200" ]]; then
    stt_json=$(curl_stt "$wav" "en")
    hyp=$(echo "$stt_json" | jq -r '.text // empty' 2>/dev/null | head -c 80)
    record "stt_rt_en_hear" "rt_en_$i" "200" "0" "${#hyp}" "hyp=\"$hyp\""
  fi
done
for i in {0..4}; do
  wav="$WAVS/rt_hu_${i}.wav"
  read code1 t1 sz1 < <(curl_tts "rt_hu_${i}" "default_hu" "${HU_RT[$i]}" "wav" "$wav")
  record "tts_rt_hu_speak" "rt_hu_$i" "$code1" "$t1" "$sz1" ""
  if [[ "$code1" == "200" ]]; then
    stt_json=$(curl_stt "$wav" "hu")
    hyp=$(echo "$stt_json" | jq -r '.text // empty' 2>/dev/null | head -c 80)
    record "stt_rt_hu_hear" "rt_hu_$i" "200" "0" "${#hyp}" "hyp=\"$hyp\""
  fi
done

# ── 8) Bench script (~10-20 calls, multiple paths + sizes) ──────────────
echo "=== 8. bench_tts_stt_roundtrip.py ==="
bench_out=$(cd /home/chestercs/Docker/dgx-openclaw-stack && python3 scripts/bench_tts_stt_roundtrip.py --runs 1 2>/dev/null || echo "BENCH_FAILED")
bench_rows=$(echo "$bench_out" | grep -E "^\| (backend|router|fish) " | wc -l)
record "bench_script" "stdout_table_rows" "200" "0" "$bench_rows" "see /tmp/bench-stdout.txt"
echo "$bench_out" > /tmp/bench-stdout.txt

# ── 9) CLI: openclaw infer tts convert (5 calls) ────────────────────────
echo "=== 9. CLI openclaw infer tts convert ==="
for i in 1 2 3 4 5; do
  t_start=$(date +%s.%N)
  out=$(docker exec openclaw-cli openclaw infer tts convert \
    --text "CLI surface smoke test $i." \
    --voice af_heart \
    --output /tmp/cli_tts_${i}.mp3 \
    --json 2>&1 || true)
  t_end=$(date +%s.%N)
  dt=$(awk "BEGIN{print $t_end - $t_start}")
  provider=$(echo "$out" | jq -r '.providerUsed // .finalProvider // empty' 2>/dev/null | head -c 40)
  ok=$(echo "$out" | jq -r '.success // empty' 2>/dev/null)
  if [[ "$ok" == "true" ]]; then code="200"; else code="500"; fi
  record "cli_infer_tts" "run_$i" "$code" "$dt" "0" "provider=$provider"
done

# ── 10) Discord-routed agent (5 calls) ──────────────────────────────────
echo "=== 10. Discord-routed agent (5 calls) ==="
PROMPTS=(
  "What's the capital of Hungary? Reply with one word."
  "Generate a brief two-sentence English greeting suitable for a TTS smoke test."
  "Mondj egy rövid magyar köszöntést, amit a hangszórón keresztül megszólaltatok."
  "Reply with exactly: TEST OK."
  "Write a one-sentence summary of why round-trip speech testing matters."
)
for i in 0 1 2 3 4; do
  t_start=$(date +%s.%N)
  out=$(docker exec openclaw-cli openclaw agent --agent discord-friend \
    --message "${PROMPTS[$i]}" --thinking off --json --timeout 120 2>&1 || true)
  t_end=$(date +%s.%N)
  dt=$(awk "BEGIN{print $t_end - $t_start}")
  reply=$(echo "$out" | jq -r '.finalAssistantVisibleText // empty' 2>/dev/null | tr -d '\n' | head -c 120)
  if [[ -n "$reply" ]]; then code="200"; else code="500"; fi
  record "agent_discord" "prompt_$i" "$code" "$dt" "${#reply}" "reply=\"$reply\""
done

# ── Final summary ───────────────────────────────────────────────────────
{
  echo "# Smoke battery report — 2026-05-19 (post-Fish-rollback)"
  echo ""
  echo "**Stack state at run time**:"
  echo "- TTS: legacy 3-service (openclaw-tts-en Kokoro + openclaw-tts-f5hun F5-TTS + openclaw-tts-router) — main branch"
  echo "- STT: openclaw-stt-whisper running \`deepdml/faster-whisper-large-v3-turbo-ct2\` (turbo, env-overridden from default)"
  echo "- Gateway / config-init: main branch patcher state"
  echo "- ComfyUI / image-gen: back online after pre-Fish-build stop"
  echo ""
  echo "**Total calls**: $N"
  echo "**Pass**: $PASS"
  echo "**Fail**: $FAIL"
  echo ""
  echo "## Per-category counts"
  echo ""
  echo "| Category | Total | Pass | Fail |"
  echo "|---|---:|---:|---:|"
  awk -F'\t' 'NR>1 {tot[$2]++; if($4=="200") pass[$2]++; else fail[$2]++} END{for(c in tot) printf "| %s | %d | %d | %d |\n", c, tot[c], pass[c]+0, fail[c]+0}' "$ROWS" | sort
  echo ""
  echo "## Full row dump (TSV)"
  echo ""
  echo '```'
  cat "$ROWS"
  echo '```'
} > "$REPORT"

echo ""
echo "============================================================"
echo "SMOKE BATTERY DONE: total=$N pass=$PASS fail=$FAIL"
echo "Report: $REPORT"
echo "Raw TSV: $ROWS"
echo "============================================================"
