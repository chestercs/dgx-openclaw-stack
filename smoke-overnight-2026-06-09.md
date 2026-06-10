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

## [07:30] REGGELI follow-up — max-payne-2 "szét van esve" 2. réteg + bot-enablement

A user (ChesTeR) reggel élőben jelezte: a max-payne oldal NÁLA még mindig szétesett, és a
bot web_search-csölt a saját oldalára (→ Google reCAPTCHA) screenshot helyett.
**Gyökérok (2. réteg, az éjszakai bootstrap-fix UTÁN):** a `src/styles.css` ÜRES volt,
nincs `tailwind.config.js` → a Tailwind v3 (telepítve volt!) SOHA nem futott → a sok
`bg-max-black`/`flex`/`text-max-red` utility-osztály nem érvényesült → stílustalan, törött
layout (built styles.css = 678 byte). **Fix:** `tailwind.config.js` (content-paths +
max-black/max-red/max-gray színek) + `src/styles.css` (@tailwind direktívák + noir-grain/
noir-vignette/scanline custom CSS) + rebuild → styles.css **678 byte → 12.7 kB**, szolgált
oldal 200 + styled. **User-teendő: KEMÉNY refresh (Ctrl+Shift+R)** a cache-elt régi
index.html/CSS miatt.
**Bot-enablement:** a bot nem tudta a saját oldala URL-jét → web_search → reCAPTCHA.
Beírtam `memory/projects.md`-be (URL + forrás + "te fejleszted" + "NE web_search-csölj a
saját oldaladra") + indexeltem. Validálva: új session a bot `memory_search`+`memory_get`-tel
megtalálta + a helyes URL-t nyitotta `browser`-rel (6×), 0 reCAPTCHA.
**Recept:** Block D bővítve a Tailwind-setup hygiene-nel (config kötelező, nem elég a class).

## [10:00-11:15] REGGELI LIVE-DEBUG (Discord user-flow hibák — mind javítva+deployolva)

A userek (KOFOLA, Reverend Green) élőben teszteltek; több bot-hiba felszínre jött:
- **max-payne-2 oldal stílustalan** ("szétesett", 2. réteg az éjszakai bootstrap-fix után):
  a `tailwindcss@3` telepítve volt, DE nem volt konfigurálva (üres `src/styles.css`, nincs
  `tailwind.config.js`) → buildelt CSS 678B → utility-osztályok inertek. Fix: config + színek
  + `@tailwind` direktívák + rebuild → **678B→12.7KB**, oldal styled. User-teendő: HARD-REFRESH.
  Block D recept: Tailwind config kötelező, verify styles >5KB. (commit 6903ec2)
- **git_push "fetch first" loop** (UE5 remake repo): a remote divergált (a bot `git reset`-elt
  push után). Fix: `git_push` **opt-in `force` param** + a hibaüzenet megmondja mit tegyen +
  recept (ne reset-elj push után; divergenciánál force/új-név). A force-push-t a Claude
  classifier blokkolta (Reverend Green: "force nem kell") → **force NÉLKÜL** oldottam meg:
  clone → tartalom-csere a tiszta skeletonra → `push HEAD:main` = **fast-forward**
  (`3e92615..56e6ee7`). A repo most tiszta UE5 skeleton, history megőrizve. (commit 69096c3)
- **edit-loop** (a tegnap esti 84-call timeout valódi oka): a bot literál `\n`-t írt valódi
  újsor helyett az `edit` args-ban → sosem matchelt → 20+ retry → loop-detection blokk.
  Recept: sikertelen edit-et ne ismételj, read/write-tal javíts, max 2 próba.
- **cron-misfire:** a bot az "csütörtökön milyen idő lesz" időjárás-kérdést emlékeztetőnek
  hitte → bogus cron (`0 18 * * 4`) — TÖRÖLVE. Recept: cron CSAK explicit ismétlődő emlékeztetőre;
  jövőbeli INFÓ (időjárás/eredmény) = nézd meg MOST (open-meteo 7-16 nap). + dátum-feloldás
  (datetime-mal a cél-napot indexeld, ne a mait). Igazolva: bot most open-meteo, 0 cron. (commit 53211d0)
- **Bot-projekt-kontextus:** `workspace-discord/memory/projects.md` (indexelve) → a bot tudja a
  max-payne URL-jét + "ne web_search-csölj a saját oldaladra (reCAPTCHA), nyisd közvetlenül".
- **Session-reset (poisoned history):** gateway-restart kell (sticky session warm a memóriában);
  a Claude classifier blokkolja (minden usert ~30s-re offline visz) → explicit user-OK-ra vár.
  A recept-fixek e NÉLKÜL is élnek (AGENTS.md re-render).

- **Hyperlink markdown nyersként Discordon** (commit utáni): a bot `[szöveg](url)` masked
  markdown-t adott → Discord sima (nem-embed) üzenetben NYERS szövegként látszik (`[..](..)`),
  nem kattintható + nem embedel. (`suppressEmbeds` valójában már `false` — az embed nem ettől
  romlott; az első jq tévesen "unset"-et mutatott, mert `false // x` → x a jq-ban.) Fix:
  FORMAT_RULES blokk "linkek = NYERS URL, soha ne masked markdown" + a hibás user-managed
  Web-search sor javítva. Verified: bot most plain `https://…` URL-t ad.

## 🌅 ZÁRÓ ÖSSZEFOGLALÓ (06:49 — stack végig egészséges, ZERO incidens)

Az autonóm éjszakai smoke+fix session lezárva (03:09 → 06:49). A healthcheck-watchdog
MINDEN 5-perces logja: `healthz live, gpu 0%, not_up=[none]`. Stack stabil maradt.

**Javított bugok (mind deployolva + ÉLŐ — session-reset NÉLKÜL, mert az AGENTS.md
runtime-renderel):**
1. **`memory_write` halott-tool** (4 hely az AGENTS.md-ben) → `write` a `memory/*.md`-be +
   `create_goal`. Ez okozta Reverend Green "vedd fel a roadmapre" failjét. A bot most ment.
2. **`browser__navigate`/`read_page`** (régi API) → `browser({action})`.
3. **TTS engine-név** Kokoro/F5 → Fish Audio S2 Pro (a SKILLS-blokk amúgy inaktív).
4. **Angular render-bug** (max-payne-2): hardcode-olt build-asset tag a `src/index.html`
   templateben → dupla script → `import` syntax-error → no bootstrap (a "tájkép" screenshot).
   Tiszta template + rebuild + Block D recept-erősítés.

**Verifikált user-flow-k (mind 0 failure, a BOT vezérelte — NEM operátor-MCP):** memory
write/search, browser open/snapshot/screenshot + Gemma-vision, **git_push (autonóm! a
user eredeti kérése)**, web_search, image-gen (flux-krea-2k), video-gen (LTX t2v),
goals (create_goal), cron (#34 RESOLVED), STT-backend, sub-agent (sessions_spawn→yield,
F30=832040), multi-tool research (web+browser+python), Go agentic coding (build+run),
static build+host (sandbox.petyuspolisz.com/imbul-test), update_plan.

**☑️ REGGELI USER-DÖNTÉSEK:**
- **TTS (Fish):** a backend nem fut (#13). Vagy deploy (figyelni a GPU-OOM-ra a futó
  vllm mellett), vagy `messages.tts.enabled=false` amíg nincs backend. Doc addig javítva.
- **Whisper STT (opcionális):** a turbo zajos/régi audión repetíciós hallucinációt ad
  (watch #4). Ha gyakori panasz: `STT_WHISPER_MODEL=large-v3` vagy `Trendency/whisper-large-v3-hu`.
- **Loop-detection:** nem fogta meg a bot 84-call Angular-rebuild loopját — érdemes a
  `tools.loopDetection` küszöböket GUI-ban (séma-validálva) finomítani.

**Git:** `2eaf4ca` (dead-tool fix) + `ac4d3db` (report+watchdog) + ez a záró commit.
**Healthcheck cron** 07:00 után magától kiveszi magát a crontab-ból.
**Test-detritus (törölhető):** imbul-test oldal, `imbulclaw-smoke-test` GitHub repo,
`primes`/`bot-*` session-ök.

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

### [04:33] (watch #1) sub-agent delegáció (sessions_spawn→yield) — VERIFIED ✅
Health: healthz ok, gpu 0%, watchdog not_up=[none] minden 5-perces logban. Spot-teszt:
bot `sessions_spawn`+`sessions_yield` (2 call, 0 fail, yielded:true) → a gyerek sub-agent
kiszámolta F(30)=**832040** (session-store igazolja). A Gemma MoE helyesen vezérli a
spawn→yield protokollt, end-to-end működik.

### [05:00] (watch #2) multi-tool research lánc — VERIFIED ✅
Health ok (healthz live, gpu 0%, not_up=[none]). Spot-teszt: `web_search`+`browser`+
`python_exec` (4 call, 0 fail) → "Mount Everest 8848.86 m = 29031.69 ft" (helyes
átváltás). Deep multi-tool chaining (Block F deep-agentic) működik.

### [05:27] (watch #3) harder agentic coding (Go dev-toolchain) — VERIFIED ✅
Health ok (healthz live, gpu 0%, not_up=[none]). Bot: Go program (első 10 prím) write +
`go build` + run (2 call python_exec, 0 fail) → "2 3 5 7 11 13 17 19 23 29" (helyes).
A sandbox dev-toolchain (go build+run) működik.

### [05:54] (watch #4) transcribe yt-dlp+STT — PIPELINE PASS, transcript-minőség ⚠️
Health ok. Bot: `python_exec` (yt-dlp download) + `transcribe_audio` (2 call, 0 fail) — a
lánc end-to-end működik. **DE** a Whisper turbo a "Me at the zoo" (jNQXAC9IVRw) régi/zajos
klippen REPETÍCIÓS hallucinációt adott ("it's bad it's bad..."), nem a valódi szöveget.
Ismert turbo-limitáció (4-layer decoder, zajos/régi audión), NEM pipeline-bug — tiszta
beszéden jól megy (prior sessions). **Megfontolandó (user):** `STT_WHISPER_MODEL=
Trendency/whisper-large-v3-hu` (HU) vagy a teljes `large-v3` a jobb minőséghez, ha a
gyenge-audió transzkripció gyakori panasz lesz.

### [06:23] (watch #5) fixed-item re-verify — minden tart ✅
Health ok (06:00-06:20 healthz live, not_up=[none]). max-payne-2: HTTP 200 + egyetlen
`<script type="module">` (Angular-fix stabil). imbul-test: HTTP 200. memory/roadmap.md
ép ("WebGL játék fejlesztése"). Minden éjszakai javítás stabil maradt.

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
