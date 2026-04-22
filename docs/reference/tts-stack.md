# OpenClaw TTS — kétnyelvű stack architektúra

> **Public knowledge** — TTS deployment + schema, megosztható.

## Áttekintés

Három service a unified `llm/dgx-openclaw-stack/` compose-ban (deployed és verified GB10-en, 2026-04-22):

| Service | Modell | Port | VRAM | License | Profile |
|---|---|---|---|---|---|
| `openclaw-tts-en` | Kokoro 82M (af_heart default) | 8091 | ~500MB-1GB | Apache 2.0 | default |
| `openclaw-tts-f5hun` | sarpba/F5-TTS_V1_hun_v2 | 8090 | ~1GB | CC-BY-NC-4.0 | `hu` (opt-in) |
| `openclaw-tts-router` | OpenAI-compat passthrough | 8092 | no GPU | MIT | default |

## Voice routing

A router voice-id alapján routeol:
- `default_hu` / `hu_*` → f5hun
- `a*` / `b*` (Kokoro voice prefix) → en

## OpenClaw wiring

`patch-config.mjs` step 11 + `docker-compose.yml` `openclaw-config-init.environment.OPENCLAW_TTS_ROUTER_API_KEY`. A `messages.tts.providers.openai.{baseUrl, apiKey, voiceId, voiceAliases}` blokkot köti a routerre.

**Voice aliases**: `magyar`/`hungarian` → `default_hu`, `english`/`us_female` → `af_heart`, `uk_female`/`narrator` → `bf_emma`, `male` → `am_michael`. Agent prompton `[voice:magyar]` vagy `[voice:af_heart]` egyaránt használható.

**No-op ha env üres** — biztonságos részleges deploy.

## Format passthrough

Backend csak wav/flac/ogg/pcm; mp3/opus client-request silently downgrade-elődik wav-re (ha a router-ben nincs ffmpeg). A public repo router-e bundleli az ffmpeg-et és transzkódol.

## Why router és nem direkt wiring

OpenClaw `messages.tts.providers.openai` blokkban EGY baseUrl van. Két backend = vagy két separate provider (upstream még flux), vagy egy fronting service. Router ~150 LOC FastAPI + ffmpeg, no GPU. ElevenLabs-spoofot elvetettük: voiceId regex `/^[a-zA-Z0-9]{10,40}$/`, xi-api-key, mp3 mandatory — sok plumbing semmi gain. OpenAI provider baseUrl override = sanctioned escape hatch (OpenClaw issue #13907 / #29224 closed-with-this).

## v0.4.x messages.tts schema enums (KRITIKUS)

OpenClaw v0.4.x `openclaw.json` config validátor `messages.tts` mező elvárt típusai:

- `messages.tts.enabled`: `boolean`
- `messages.tts.auto`: enum — `"off" | "always" | "inbound" | "tagged"` (NEM boolean)
- `messages.tts.mode`: enum — `"final" | "all"` (NEM `"auto"`)

Hibás értékkel a gateway indulásnál `Config invalid / Invalid option: expected one of …` üzenettel crash-loopol.

"Minden végső agent üzenetet mondjon" posture = `auto: 'always'`, `mode: 'final'`.

Patcher lokáció: `patch-config.mjs` step 11, `desiredTopLevel` objektum literál.

Javító commit: public `chestercs/dgx-openclaw-stack` `81f1fa4` (2026-04-22).

## Patcher step 11 három dolgot ír

Amikor `OPENCLAW_TTS_ROUTER_API_KEY` set:

1. **Top-level `messages.tts.{enabled,auto,mode}`** — nélkülük az OpenClaw voice surfaces silently treat TTS as off, akkor is ha a provider ki van wireolva. Az eredeti step 11 csak a 2-3 pontot írta; voice playback 100% silent volt amíg a top-level switches v0.4.0-ban be nem kerültek
2. **`messages.tts.providers.openai`** — `baseUrl`, `apiKey`, `model`, `voiceId` a routerre mutatva
3. **`messages.tts.voiceAliases`** — friendly aliasok (english, narrator, male, female, magyar, hungarian) konkrét Kokoro / F5-TTS voice id-kre

Unset → step 11 skip (felhasználó kihagyhatja a TTS-t üres env-vel + `profiles: ["never"]` parking).

## Web chat UI limitáció (KRITIKUS)

A web chat (`/chat?session=...`) bundle (`index-Dba6JFRP.js`, ~700KB) **hard-wired** a browser `speechSynthesis` API-ra a "Read aloud" gombon és minden auto-play helyzetben. Bundle grep 0 találatot ad `audio/speech`, `messages.tts`, `providers.openai` mintákra — csak `speechSynthesis` (4×) és `SpeechSynthesisUtterance` (1×).

**Következmény**: `messages.tts.providers.openai.{baseUrl, apiKey, voiceId}` és `messages.tts.{enabled, auto, mode}` config kizárólag voice-surface (Discord / Slack voice channel) playback-re megy — a web chat UI nem subscriber.

### Három TTS path OpenClaw-ban (könnyű összekeverni)

1. `tts` agent skill — tool-call, audiot server-side generál, returnel text "Generated audio reply." — web UI csak ezt a szöveget jeleníti meg
2. `messages.tts.auto` — gateway-level autoplay, voice surface-ekre broadcast (Discord voice), NEM web
3. `.chat-tts-btn` "Read aloud" gomb — lokális browser `speechSynthesis.speak(new SpeechSynthesisUtterance(text))`, OS default hanggal

### How to apply

- Ha user magyar TTS-t akar **web chat-ben**: vagy Discord voice channel-en át a router, vagy userscript ami `speechSynthesis.speak()`-et monkey-patch-el `fetch('/v1/audio/speech')` + `new Audio(blob)`-ra
- Soha ne ígérd a usernek hogy `messages.tts.auto=always` magától beszélni fog a web chat UI-ban — nem fog
- Ha bundle fájlnév változik (hash rename), a 0-találat ellenőrzést újra kell futtatni

### Debugging fingerprint

- Router log: egyetlen `POST /v1/audio/speech 200 OK` assistant-reply-onként, utána csend
- Browser DevTools: 0 `<audio>` elem a DOM-ban, `new Audio()`/`decodeAudioData` hook 0 eventet kap
- `speechSynthesis.speak` hook viszont triggerel a "Read aloud" kattintáskor

## Verifikáció GB10-en (2026-04-22)

```bash
cd llm/dgx-openclaw-stack && docker compose --profile hu up -d --build
# End-to-end smoke: HU+EN audio generálás OpenClaw agent-en át — passed
```

Sorrend GB10 deploy-nál: `openclaw-tts-en` build első (long, ~Kokoro weights download + cu130 torch), aztán `openclaw-tts-router`, aztán `openclaw-config-init openclaw-gateway openclaw-cli` force-recreate.

## Memory leak mitigation Kokoro-n

`0 4 * * * docker restart openclaw-tts-en` cron ha sustained generation után RSS > 4GB (upstream hexgrad/kokoro#152).

## Csapda amit c69a9f2 fixelt

Router `.env.example` comment sora emlegette `OPENCLAW_TTS_ROUTER_API_KEY` kulcsnevet, ezért anchor nélküli `grep KEY .env | cut -d= -f2` multi-line értéket adott vissza, és a gateway configba a comment eleje került apiKey-ként. Mindig anchored grep-et használj env-mirror parancsoknál: `grep '^KEY=' .env`. Lásd: `patterns.md`.
