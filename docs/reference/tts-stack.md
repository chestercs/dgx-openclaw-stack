# OpenClaw TTS — bilingual stack architecture

> **SUPERSEDED.** The TTS surface migrated to a single **Fish Audio S2 Pro**
> service (`openclaw-tts-fish`) backed by SGLang-Omni — see the
> [Unreleased] entry in `CHANGELOG.md` and `openclaw-tts-fish/README.md`
> for the current architecture. The body below documents the legacy
> 3-service Kokoro EN + F5-TTS HU + router pipeline (decommissioned) and
> is preserved for historical context, future model-swap rationale, and
> ops-recipe continuity for operators still on a pre-migration branch.

> Reference material: TTS deployment details + schema.

## Overview

Three services in the unified `llm/dgx-openclaw-stack/` compose (deployed and verified on GB10, 2026-04-22):

| Service | Model | Port | VRAM | License | Profile |
|---|---|---|---|---|---|
| `openclaw-tts-en` | Kokoro 82M (`af_heart` default) | 8091 | ~500 MB–1 GB | Apache 2.0 | default |
| `openclaw-tts-f5hun` | `sarpba/F5-TTS_V1_hun_v2` | 8090 | ~1 GB | CC-BY-NC-4.0 | `hu` (opt-in) |
| `openclaw-tts-router` | OpenAI-compat passthrough | 8092 | no GPU | MIT | default |

## Voice routing

The router routes by voice ID:

- `default_hu` / `hu_*` → `f5hun`
- `a*` / `b*` (Kokoro voice prefix) → `en`

## OpenClaw wiring

`patch-config.mjs` step 11 + `docker-compose.yml` `openclaw-config-init.environment.OPENCLAW_TTS_ROUTER_API_KEY`. Writes the `messages.tts.providers.openai.{baseUrl, apiKey, voiceId, voiceAliases}` block pointing at the router.

**Voice aliases:** `magyar` / `hungarian` → `default_hu`, `english` / `us_female` → `af_heart`, `uk_female` / `narrator` → `bf_emma`, `male` → `am_michael`. In agent prompts, `[voice:magyar]` and `[voice:af_heart]` both work.

**No-op when env is empty** — safe for partial deployments.

## Format passthrough

The backend only produces wav/flac/ogg/pcm; mp3/opus client requests silently downgrade to wav if the router has no ffmpeg. The public repo's router bundles ffmpeg and transcodes on the fly.

## Why a router instead of direct wiring

OpenClaw's `messages.tts.providers.openai` block accepts exactly one `baseUrl`. Two backends = either two separate providers (upstream still in flux), or one fronting service. The router is ~150 LOC of FastAPI + ffmpeg, no GPU. We rejected the ElevenLabs-spoof path: voice-ID regex `/^[a-zA-Z0-9]{10,40}$/`, xi-api-key, mandatory mp3 — a lot of plumbing for no gain. The OpenAI-provider `baseUrl` override is the sanctioned escape hatch (OpenClaw issues #13907 / #29224, closed with this pattern).

## v0.4.x `messages.tts` schema enums (CRITICAL)

The OpenClaw v0.4.x `openclaw.json` config validator expects these types under `messages.tts`:

- `messages.tts.enabled`: `boolean`
- `messages.tts.auto`: enum — `"off" | "always" | "inbound" | "tagged"` (NOT boolean)
- `messages.tts.mode`: enum — `"final" | "all"` (NOT `"auto"`)

With a wrong value, the gateway crash-loops at startup with `Config invalid / Invalid option: expected one of …`.

The "speak every final agent message" posture = `auto: 'always'`, `mode: 'final'`.

Patcher location: `patch-config.mjs` step 11, `desiredTopLevel` object literal.

Fix commit: public `chestercs/dgx-openclaw-stack` `81f1fa4` (2026-04-22).

### `OPENCLAW_TTS_AUTO` env knob

`auto` is env-tunable via `OPENCLAW_TTS_AUTO` (default `always`). Set it in `.env` per deploy:

| Surface | Recommended value | Why |
|---|---|---|
| Voice channel agent (Discord `/vc join`, VoiceCall) | `always` (default) | Speaks every final reply into the voice stream as designed |
| Discord **text-channel** agent | `always` (v0.11.0+) | Posts the text reply AND attaches the TTS audio file. As of v0.11.0 the gateway image ships ffmpeg via `openclaw-base-ext/Dockerfile`, so the attachment pipeline no longer crashes. Operators on older deploys (pre-v0.11.0) can keep `tagged` as a workaround — see below. |
| Heartbeat-only agents | `tagged` or `off` | Heartbeat journal entries don't need to be spoken; saves TTS-router cycles |
| Web chat UI | (any) | UI is hardwired to browser `speechSynthesis`, see below — config-level value is ignored |

#### Pre-v0.11.0 workaround

Before v0.11.0 the `ghcr.io/openclaw/openclaw` gateway image shipped **without** ffmpeg (the bundled ffmpeg lived only in `openclaw-tts-router`), so the Discord text-channel attachment pipeline crashed with `[discord] final reply failed: Error: ffmpeg not found in trusted system directories` and the bot's text payload silently never landed. Symptom: bot's typing indicator + emoji reactions fired (no ffmpeg path), but no actual reply text appeared.

The workaround was `OPENCLAW_TTS_AUTO=tagged` in `.env` — TTS only fires on `[tts]`-tagged replies, leaving normal text replies to flow through Discord's REST message API. The v0.11.0 release ships an ffmpeg-augmented gateway image (`openclaw-base-ext/Dockerfile` wraps the upstream image and apt-installs ffmpeg), so the workaround is no longer needed; `auto=always` is safe on every surface.

If you're stuck on a pre-v0.11.0 deploy and can't upgrade right away, keep `OPENCLAW_TTS_AUTO=tagged`. After upgrading, recreate `openclaw-config-init` + `openclaw-gateway` + `openclaw-cli` so the patcher rewrites `messages.tts.auto` from your override back to `always`.

## Patcher step 11 writes three things

When `OPENCLAW_TTS_ROUTER_API_KEY` is set:

1. **Top-level `messages.tts.{enabled,auto,mode}`** — without these, OpenClaw voice surfaces silently treat TTS as off even with the provider wired. The original step 11 only wrote points 2–3; voice playback was 100% silent until the top-level switches landed in v0.4.0.
2. **`messages.tts.providers.openai`** — `baseUrl`, `apiKey`, `model`, `voiceId` pointing at the router.
3. **`messages.tts.voiceAliases`** — friendly aliases (`english`, `narrator`, `male`, `female`, `magyar`, `hungarian`) mapped to concrete Kokoro / F5-TTS voice IDs.

Unset → step 11 skips cleanly (the user can opt out of TTS by leaving the env empty and parking the two TTS services with `profiles: ["never"]`).

## Web chat UI limitation (CRITICAL)

The web chat (`/chat?session=...`) bundle (`index-Dba6JFRP.js`, ~700 KB) is **hard-wired** to the browser's `speechSynthesis` API for the "Read aloud" button and every auto-play path. Grepping the bundle yields zero hits for `audio/speech`, `messages.tts`, or `providers.openai` — only `speechSynthesis` (4×) and `SpeechSynthesisUtterance` (1×).

**Implication:** `messages.tts.providers.openai.{baseUrl, apiKey, voiceId}` and `messages.tts.{enabled, auto, mode}` config is used only for voice surfaces (Discord / Slack voice channels) — the web chat UI is not a subscriber.

### Three TTS paths in OpenClaw (easy to confuse)

1. `tts` agent skill — tool call, audio generated server-side, returns text `"Generated audio reply."` — the web UI only renders the text.
2. `messages.tts.auto` — gateway-level autoplay, broadcast to voice surfaces (Discord voice), NOT web.
3. `.chat-tts-btn` "Read aloud" button — local browser `speechSynthesis.speak(new SpeechSynthesisUtterance(text))`, OS default voice.

### How to apply

- If the user wants Hungarian TTS **in the web chat**: either go through a Discord voice channel (via the router), or a userscript that monkey-patches `speechSynthesis.speak()` into `fetch('/v1/audio/speech')` + `new Audio(blob)`.
- Never promise the user that `messages.tts.auto=always` will make the web chat UI speak on its own — it won't.
- If the bundle filename changes (hash rename), re-run the zero-hit check.

### Web chat workaround — userscript path

For deploys where the web chat needs HU TTS (Kokoro EN voices are passable for English-only deploys; HU operators want the F5-TTS HU voice), the canonical workaround is a Tampermonkey/Greasemonkey userscript:

1. **Hook `window.speechSynthesis.speak()`** before the `chat-tts-btn` click handler runs.
2. **Detect HU diacritics** (`/[áéíóöőúüűÁÉÍÓÖŐÚÜŰ]/`) in the utterance text → route to `default_hu`; else → `af_heart` (Kokoro EN).
3. **Fetch from the TTS router** — needs same-origin reachability (see "Architecture choice" below).
4. **Play the returned blob** via `new Audio(URL.createObjectURL(blob))`.
5. **Fallback to the original `speechSynthesis.speak()`** on fetch failure so the OS voice still kicks in.

A reference public-deploy variant lives at `templates/userscripts/openclaw-chat-hu-tts.user.js` (token-in-userscript model: operator pastes their `OPENCLAW_TTS_ROUTER_API_KEY` once via Tampermonkey menu). For a private deploy where the `claw.<your-host>` reverse proxy can re-inject the Bearer header server-side, the userscript can drop the auth header entirely.

#### Tampermonkey install recipe (5 minutes)

For the typical "I want HU TTS in my browser chat right now" path:

1. Install **Tampermonkey** in your browser
   ([Chrome](https://chrome.google.com/webstore/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)
   / [Firefox](https://addons.mozilla.org/en-US/firefox/addon/tampermonkey/) /
   [Edge](https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd)).
2. Copy `templates/userscripts/openclaw-chat-hu-tts.user.js` from this repo
   (raw file: open it in GitHub, click Raw, copy URL).
3. Tampermonkey dashboard → **Create a new script** → paste the contents
   → adjust the `@match` line if your chat origin isn't `https://claw.<your-host>/*`.
4. **Save** (Ctrl+S in the editor). The script appears in Tampermonkey's
   "Installed scripts" list, enabled by default.
5. First load on the chat: open Tampermonkey's toolbar icon → click the
   script's name → menu item "Set TTS router URL + token". Paste:
   - **Router URL**: `https://tts.<your-host>/v1/audio/speech` (or the
     same-origin path if you went the gateway-proxy route — see
     "Architecture choice" below).
   - **Token**: the `OPENCLAW_TTS_ROUTER_API_KEY` value from your
     stack's `.env` (one-time paste; stored in Tampermonkey's `GM_setValue`
     keystore, scoped to this script).
6. Reload the chat tab. Click "Read aloud" on any agent message. Watch
   the router logs (`docker logs ${PROJ}openclaw-tts-router --tail 5`) —
   you should see exactly one `POST /v1/audio/speech 200 OK` per click,
   and your browser's audio output is now Kokoro / F5-TTS HU instead of
   the OS default.

If "Read aloud" is silent and the router log shows zero hits, the
userscript isn't injecting — check Tampermonkey toolbar (badge should
show "1" when the script is active on the page) and the browser
console (the script logs `[openclaw-tts] hooked speechSynthesis.speak`
on first load).

### Architecture choice — direct router URL vs gateway-proxy route

Two ways to make the router reachable from the chat-tab origin:

- **Direct router URL** (simpler — no reverse-proxy edits): expose the router on a separate hostname like `tts.<your-host>` via your existing reverse proxy. The userscript hits that URL directly, includes the Bearer token. Cross-origin → CORS preflight → router must respond `Access-Control-Allow-Origin: <your-chat-host>`. Token sits in the userscript's GM_getValue store (operator-side). Established pattern; works today.

- **Same-origin gateway-proxy route** (cleaner — eliminates external service dep): add a `location /v1/audio/speech { proxy_pass http://openclaw-tts-router:8080/v1/audio/speech; proxy_set_header Authorization "Bearer ${OPENCLAW_TTS_ROUTER_API_KEY}"; }` block in the reverse-proxy config that fronts the chat host. The userscript fetches the chat-origin path with no auth header (server-side injects). Token never leaves the host — easier rotation, easier multi-user setups. Requires reverse-proxy access (Nginx Proxy Manager / Caddy / Cloudflare Worker) — out-of-scope for the public stack repo, but documented here as the recommended pattern for production deploys.

If the deploy is ephemeral / single-operator, direct URL is fine. If it's permanent / multi-user / public-facing, gateway-proxy is the right path.

### Debugging fingerprint

- Router log: exactly one `POST /v1/audio/speech 200 OK` per assistant reply, then silence.
- Browser DevTools: zero `<audio>` elements in the DOM; `new Audio()` / `decodeAudioData` hooks receive zero events.
- `speechSynthesis.speak` hook fires on a "Read aloud" click.

## GB10 verification (2026-04-22)

```bash
cd llm/dgx-openclaw-stack && docker compose --profile hu up -d --build
# End-to-end smoke: HU + EN audio generation through an OpenClaw agent — passed.
```

Order for a GB10 deploy: build `openclaw-tts-en` first (long — Kokoro weights download + cu130 torch), then `openclaw-tts-router`, then force-recreate `openclaw-config-init openclaw-gateway openclaw-cli`.

## Memory-leak mitigation on Kokoro

`0 4 * * * docker restart openclaw-tts-en` cron, if sustained generation pushes RSS past 4 GB (upstream hexgrad/kokoro#152).

## Trap fixed by `c69a9f2`

The router's `.env.example` comment mentioned the `OPENCLAW_TTS_ROUTER_API_KEY` key name, so an un-anchored `grep KEY .env | cut -d= -f2` returned a multi-line value, and the start of the comment ended up as `apiKey` in the gateway config. Always use anchored grep for env-mirror commands: `grep '^KEY=' .env`. See `patterns.md`.
