# OpenClaw TTS — bilingual stack architecture

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
| Discord **text-channel** agent | `tagged` | The Discord plugin attempts a TTS audio attachment on every final reply when `auto=always`, shelling out to ffmpeg for waveform/Opus transcoding. The `ghcr.io/openclaw/openclaw` gateway image ships **without** ffmpeg (the bundled ffmpeg lives only in `openclaw-tts-router`), so the attachment pipeline crashes with `[discord] final reply failed: Error: ffmpeg not found in trusted system directories` and the bot's text payload silently never lands. With `tagged`, TTS only fires on `[tts]`-marked replies, leaving normal text replies to flow through Discord's REST message API. Verified 2026-04-27 with `@ImbulClaw` on a GB10 host. |
| Heartbeat-only agents | `tagged` or `off` | Heartbeat journal entries don't need to be spoken; saves TTS-router cycles |
| Web chat UI | (any) | UI is hardwired to browser `speechSynthesis`, see below — config-level value is ignored |

Long-term upstream fix: bundle ffmpeg in the gateway image (or split the attachment pipeline so it goes through the router which already has ffmpeg). Until then, `OPENCLAW_TTS_AUTO=tagged` is the operator-side workaround.

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
