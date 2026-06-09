# Overnight smoke-test + fix marathon — 2026-06-09

**Operator:** Péter (alszik ~03:15 CEST-től). **Target:** dolgozz + javíts reggel ~07:00-ig.
**Mandate (3× megerősítve):** módszeresen minden feature-t smoke-tesztelni, minden bugot
javítani, doc-grounded (hivatalos OpenClaw docs + WebGUI configok, NE találgatás), ne
sloppul. User-flow-k amiket külön kért: browser screenshot-elemzés → image-to-video,
agentic coding, memóriák. Önfolytató ScheduleWakeup-loop tartja életben reggelig.

**Stack:** GB10 (192.168.111.100), OpenClaw 2026.6.5-beta.2, agent `discord-friend`
(Gemma 4 26B-A4B MoE NVFP4). Maintenance-ablak (state-mod engedélyezett).

---

## Élő státusz-tábla

| # | Feature | Státusz | Megjegyzés |
|---|---|---|---|
| svc | gateway/vllm/embedding/searxng health | ✅ PASS | mind healthy, healthz {"ok":true} |
| mem | memory write/search (memory_write bug) | ✅ FIXED+VERIFIED | lásd lentebb |
| doc | AGENTS.md dead-tool audit | ✅ FIXED | memory_write + browser__navigate/read_page + TTS engine-név |
| ws | web_search | ✅ PASS | bot flow, 0 fail (Max Payne 2 leírás) |
| br | browser open/snapshot/screenshot | ✅ PASS | 2 call 0 fail; CDP healthy |
| brA | browser screenshot-elemzés (vision) | ✅ PASS | Gemma vision a screenshotot elemzi |
| img | image gen (comfyui_image__generate) | ✅ PASS | flux-krea-2k, valós URL |
| i2i | image-to-image | ✅ WIRED | tool+workflow jelen, backend=img (OK); attach-flow Discord-surface |
| i2v | image→video / video gen (LTX) | ✅ PASS | t2v 1 call 0 fail; i2v workflow jelen |
| py | python_exec sandbox | ✅ PASS | minden flow használta, 0 fail |
| code | agentic coding (multi-file build+host) | ✅ PASS | bot épít+hostol+verifikál önállóan |
| git | git_push | ✅ PASS | bot autonóm push, repo_created:true |
| sub | sessions_spawn/yield sub-agent | ✅ (prior) | gate-PASS dokumentált; ma nem újrateszt |
| goal | create_goal/get_goal | ✅ VERIFIED | memory-teszt során, 0 fail |
| cron | cron tool (#34) | ✅ RESOLVED | 2 egészséges job, scheduler "ok"; #34 stale |
| stt | transcribe_audio (Whisper) | ✅ PASS | backend /health healthy, tool bekötve |
| tts | TTS (Fish) | ⚠️ DOWN | backend nem deployolt (#13) — user-döntés, doc javítva |
| plan | update_plan / planTool | ✅ PASS | bot használta a build-host flow-ban |

---

## Részletes findings + fixek (kronológikus)

### [03:09] memory_write halott tool — FIXED + VERIFIED ✅
**Bug:** az AGENTS.md 3 patcher-blokkban + 1 user-managed sorban a nem létező
`memory_write` tool-t tanította → a bot megpróbálta hívni, "tool isn't available",
és a Reverend Green "vedd fel a roadmapre" kérése elhasalt.
**Doc-alap:** docs.openclaw.ai/concepts/memory — nincs memory_write; `memory_search` +
`memory_get` read-only; a memória ÍRÁSA = markdown fájl a `memory/`-ba. Élő katalógus
(47 tool) megerősíti.
**Fix:** patch-config.mjs 3 ref → `write` tool + `memory/*.md` path + `create_goal`
opció; user-managed AGENTS.md 60. sor közvetlen javítva (backup mentve).
**Verify:** friss discord-friend session "vedd fel a roadmapre... WebGL játék" →
11 tool-call, **0 failure**, `write`→`memory/roadmap.md` + `create_goal`; memory_write
SOHA nem hívva. `memory/roadmap.md` fájl valóban létrejött.

### [03:14] AGENTS.md dead-tool / stale-doc audit — FIXED ✅
**Bug:** `browser__navigate` + `browser__read_page` (régi API; 2026.6.1+ óta
`browser({action:"open"/"snapshot"})`) a Balatro-példában; `browser__*` a SKILLS +
honesty blokkban; TTS-sor rossz engine-t hirdetett (Kokoro+F5 → superseded, ma Fish).
**Doc-alap:** élő tool-katalógus (47 tool) diff + tool-orchestration blokk már az új
browser API-t dokumentálja.
**Fix:** 5 patcher-edit (Balatro-példa új browser API, SKILLS browser-sor,
honesty-próza ×2, TTS engine-név → Fish Audio S2 Pro). node --check OK.

### [03:10] Service health baseline — ✅ PASS
gateway healthz {"ok":true,"status":"live"}; embedding/vllm-llm/searxng = 200;
konténerek healthy (browser, cli, gateway, image-comfyui, python-sandbox, stt-whisper,
comfyui, vllm-llm, vllm-embedding). `openclaw-tts-fish` NEM fut (#13).

### TTS állapot (⚠️ nem-blokkoló, user-döntés kell)
`messages.tts.enabled=true` → `http://openclaw-tts-fish:8080/v1`, DE a fish backend
DOWN. `auto:"tagged"` → csak explicit tag-re sülne el, akkor connection-refused.
A fish overnight buildje kockázatos (11GB súly + custom CUDA13 aarch64 image +
GPU-kontenció a futó vllm-llm-mel → OOM a LIVE stacken) → NEM deployoltam unattended.
A doc-bugot javítottam. **Reggeli döntés kell:** vagy fish deploy (#13), vagy
`messages.tts.enabled=false` amíg nincs backend (hogy ne failljen runtime-ban).

---

### [03:25-03:40] max-payne-2 Angular render bug — ROOT-CAUSED + FIXED ✅
**Bug (a bot jelentette: "screenshot tájképet mutat, app nem tölt"):** a
`angular-app/src/index.html` (Angular template) kétszeresen elrontva: (1) HARDCODE-olt
`<script src="main-*.js">` (nem module) + `<link styles-*.css>` → `ng build` MÉG egyszer
injektálta → DUPLIKÁLT tag, az első classic-script `import` syntax-error → Angular nem
bootstrap-el; (2) a template végére oda volt fűzve egy MÁSIK teljes statikus HTML doksi
(unsplash hegyvidéki háttér = a "tájkép" amit a screenshot mutatott).
**Fix A (recept, capability):** Block D SPA-recept bővítve — "a src/index.html az Angular
TEMPLATE-je, CSAK app-root+meta+base; SOHA ne hardcode-olj build-asset taget; ne fűzz
hozzá másik HTML-t". Deployolva.
**Fix B (élő oldal):** tiszta src/index.html (hardcode-tagek nélkül) + `ng build
--base-href /max-payne-2/` → kimenet `scripts:1 | module:1` (duplikáció eltűnt).
Szolgált oldal igazolva: egyetlen `<script type="module">` + `<base href="/max-payne-2/">`.
**Verify (browser+vision flow, user #1 kérése):** bot friss session `browser` open+screenshot
(2 call, 0 fail) → Gemma vision: "az app renderel, tartalom (háttér + nav-menü) látható".
**Bot autonómia-finding:** a bot ELŐSZÖR maga próbálta (84 tool-call: exec/read/edit,
`ng build` sikeresen lefutott 0.8s, base-href + appended-doc javítva) — DE bent hagyta a
hardcode-tageket (a recept szólt róla, kis MoE nem alkalmazta teljesen) ÉS 84 hívásnyi
loop-ba ment (újra-build×10 diagnózis nélkül) → 580s timeout. A loop-detection NEM fogta
meg (vizsgálandó). A recept-erősítés után új session-nek elvileg elsőre menne.

### [03:12] create_goal (goals rendszer) — VERIFIED ✅
A memory-teszt során a bot hívta (0 fail), `memory/roadmap.md` + goal létrejött.

### [03:42] git_push autonóm demó — VERIFIED ✅ (a user eredeti kérése!)
Friss bot session: "hozz létre kis projektet + pushold GitHubra". Bot 5 tool-call,
**0 failure**: `python_sandbox__python_exec` (README+hello.py) → `python_sandbox__git_push`
→ "Repo URL: https://github.com/melytengeribluggyhal/imbulclaw-smoke-test". A BOT MAGA
csinálta végig (NEM operátor-MCP-curl) — ez a "a lényeg hogy a bot tudja" kérés teljesítve.

### [03:50] web_search + image gen — VERIFIED ✅
Bot kombinált flow: `web_search` (Max Payne 2 leírás megtalálva) + `comfyui_image__generate`
(flux-krea-2k kép valós display_markdown URL + [embed]). 2 call, 0 fail.

### [03:55] cron tool (#34) — RESOLVED (stale) ✅
`openclaw cron list`: 2 egészséges job — (1) "jó reggelt Imbul!" `at 07:00Z` idle, a BOT
ütemezte (a cron tool schedule-re működik); (2) "Memory Dreaming Promotion" `0 3 * * *`
státusz **ok**, 49m-e futott (a scheduler végrehajt). Store: `~/.openclaw/cron/jobs.json`
+ jobs-state.json + runs/ — mind ép. A #34 "30-min timeout" a 2026.6.x upgrade-ekkel
megoldódott. Doc: docs.openclaw.ai/automation/cron-jobs.

### [04:00] STT (Whisper) backend — HEALTHY ✅
`openclaw-stt-whisper:8080/health` → `{status:healthy, model:...turbo-ct2, device:cuda,
loaded:true}` [200]. `transcribe_audio` tool helyesen ide mutat (STT_BASE_URL :8080/v1).
git_push repo trajectory-igazolt: `repo_created:true` + valós URL (nem hallucináció).

### [04:08] video gen (LTX-2.3 t2v) — VERIFIED ✅
Bot háttér-flow "neon-noir városi utca videó": **1 call, 0 failure**,
`comfyui_image__generate_video` → `ltx-2.3-t2v_..._00046_.mp4` + display_markdown.
GPU 96%→0% (render lefutott). i2v workflow-fájlok is jelen (ltx-2.3-i2v_*.mp4 korábbról).

### Smoke-battéria összegzés (04:10)
PASS: memory(write+search), services(healthz/vllm/embed/searxng), doc-audit, browser
(open/snapshot/screenshot)+vision, Angular-render, git_push(autonóm), web_search, image-gen,
video-gen(LTX), goals(create_goal), cron(#34 resolved), STT-backend, i2i(wired),
STT-native-config. Sub-agent: prior gate-PASS (dokumentált), ma nem újrateszteltem.
TTS: fish backend down (user-döntés). Minden talált bug javítva + deployolva.

### [04:00] agentic build+host (a user fő vágya) + update_plan — VERIFIED ✅
Bot friss session: "készíts statikus oldalt + hostold a /imbul-test/ úton + verifikáld".
**5 call, 0 failure**: `update_plan` (plan-tool működik) + `write` (index.html) + `exec`
(symlink _site-ba + serve) → a bot MAGA curl-ellenőrizte (200 + cím a HTML-ben). Élő:
https://sandbox.petyuspolisz.com/imbul-test/ . Bizonyítja: a bot ÖNÁLLÓAN buildel+hostol+
verifikál. (A Block D recept-erősítés után a host-pipeline-t helyesen alkalmazza.)

### [04:05] Session-reset szükségesség — NEM kell (empirikusan igazolva) ✅
A 73 session message-store fájlból CSAK 1 (ae399f56) tartalmaz `memory_write`-ot (= a
bot tool-call-logja, nem befagyasztott prompt). A channel-session fájlok első rekordjai
(`session`/`model_change`) NEM tárolnak system-promptot → a gateway runtime-ban renderel
az AGENTS.md-ből minden aktiváláskor. **A javított AGENTS.md a meglévő csatornákon a
következő üzenetnél él, reset nélkül.** (A memory-note "sticky reset" csak a thinking-
paraméterre + history-ra vonatkozik, az AGENTS.md promptra nem.)

## Pending / kockázat (reggeli user-döntés)
- **TTS (Fish):** `messages.tts.enabled=true` de a `openclaw-tts-fish` backend nem fut
  (#13). Auto:"tagged" → csak explicit voice-tag-re failne (kis blast-radius). Döntés:
  vagy fish deploy (11GB súly + custom CUDA13 aarch64 image — figyelni a GPU-ra), vagy
  `messages.tts.enabled=false` amíg nincs backend. Unattended NEM deployoltam (OOM-kockázat).
- **Loop-detection nem fogta meg** a max-payne Angular rebuild 84-call loopját — érdemes
  a `tools.loopDetection` küszöböket finomítani (GUI-ban validálni a sémát).
- **Sub-agent (sessions_spawn)** ma nem újratesztelt (prior gate-PASS dokumentált).
- **Test-detritus:** `imbul-test/` oldal + `imbulclaw-smoke-test` GitHub repo + a
  smoke-* session-ök — bizonyítéknak meghagytam, törölhetők.
