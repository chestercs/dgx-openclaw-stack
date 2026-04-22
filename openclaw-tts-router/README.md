# openclaw-tts-router

OpenAI-compatible `/v1/audio/speech` router that fronts one or more
language-specific TTS backends and exposes a single endpoint for OpenClaw to
point at via `messages.tts.providers.openai.baseUrl` (the sanctioned escape
hatch documented in OpenClaw issues #13907 and #29224).

The router is ~150 LOC of FastAPI + httpx, runs without a GPU, and bundles
ffmpeg so it can transcode the backend's wav into the format the client asked
for (mp3 / opus / aac / flac / ogg / pcm) without losing anything in the
gateway's content-type sniffing.

## Backends

| Backend | Language | Image | License | Default? |
|---|---|---|---|---|
| `openclaw-tts-en` | English (US/UK) | Kokoro 82M (`hexgrad/Kokoro-82M`) | Apache 2.0 | Yes — ships in this stack. |
| `openclaw-tts-f5hun` | Hungarian | F5-TTS HU fine-tunes (sarpba / Maxdorger29 / mp3pintyo) | CC-BY-NC | No — bring your own. |

The Hungarian backend is **optional**. If you set `F5HUN_URL` +
`F5HUN_API_TOKEN` in `.env`, the router activates the HU voice ids and the
diacritic-based autodetect path. Leave them unset and the router runs EN-only
(HU voice ids return 404, the autodetect is a no-op, and `/healthz` reports
`f5hun_enabled: false`).

The reason it ships HU-disabled by default is the F5-TTS Hungarian fine-tunes
are **CC-BY-NC** — non-commercial use only — and we can't assume that license
fits every consumer of this repo. If you want HU and your usage is private /
non-commercial, point `F5HUN_URL` at a service that exposes the
[same OpenAI `/v1/audio/speech` shape](https://platform.openai.com/docs/api-reference/audio/createSpeech)
with an F5-TTS HU model behind it. (See the
`llm/openclaw-tts-f5hun/` directory in the parent
[arm_server_installer_guide](https://github.com/chestercs/arm_server_installer_guide)
repo for one such reference deployment.)

## Voice catalog

The router publishes a stable voice id surface that the OpenClaw config can
target. All English voices are baked into the Kokoro EN image at build time
(only the A/A-/B-grade voices from the Kokoro VOICES catalog ship — full pack
is 54 voices, we keep the production-ready ones).

| Voice id | Backend | Notes |
|---|---|---|
| `af_heart` | en | A-grade, US female. **Router default.** |
| `af_bella`, `af_nicole`, `af_aoede`, `af_kore`, `af_sarah` | en | A- to B-grade US female alternates. |
| `am_michael`, `am_fenrir`, `am_puck` | en | A- to B-grade US male. |
| `bf_emma` | en | B-grade UK female. |
| `alloy`, `ash`, `ballad`, `coral`, `echo`, `fable`, `onyx`, `nova`, `sage`, `shimmer`, `verse` | en | OpenAI default catalog (newer `gpt-4o-mini-tts`); mapped to closest Kokoro voices so OpenClaw never gets a 404 from picking one of these. |
| `default_hu`, `hu_diana` | f5hun | **Only present when `F5HUN_URL` is set.** |

`GET /v1/audio/voices` returns the live list (always reflects whether the HU
backend is wired) and the current default voice id.

## Hungarian autodetect

When the Hungarian backend is enabled and OpenClaw fires a request with one of
the OpenAI default voices (`coral` is what the gateway picks if the agent
didn't override) **and** the input contains Hungarian diacritics
(`áéíóöőúüűÁÉÍÓÖŐÚÜŰ`), the router silently re-routes to `default_hu` so the
agent doesn't have to know about voice ids to get correct pronunciation.

Without this, Kokoro reads Hungarian phonetically through its English G2P,
producing the unmistakable "magyar szöveg angol akcentussal" sound. The
autodetect is a no-op when the HU backend isn't wired (no point reading
diacritic input on a Kokoro voice when no fallback exists).

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/audio/speech` | Bearer | OpenAI Audio API. Returns audio bytes in the requested `response_format`. |
| `GET` | `/v1/audio/voices` | Bearer | Voice catalog with backend hint + `f5hun_enabled` flag. |
| `GET` | `/v1/voices` | Bearer | Alias of `/v1/audio/voices` (matches Kokoro-FastAPI's path). |
| `GET` | `/healthz` | _none_ | Probes wired backends, reports per-backend HTTP status. |

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `ROUTER_API_KEY` | yes | — | Bearer token clients must present (OpenClaw uses this as `apiKey` in the openai TTS provider config). |
| `EN_API_TOKEN` | yes | — | Bearer token for the EN backend. Same value the EN service expects in its `TTS_API_TOKEN`. |
| `EN_URL` | no | `http://openclaw-tts-en:8080/v1/audio/speech` | Where to reach the EN backend. |
| `F5HUN_API_TOKEN` | no | _(unset → HU disabled)_ | Bearer token for the HU backend. |
| `F5HUN_URL` | no | _(unset → HU disabled)_ | Where to reach the HU backend. |
| `ROUTER_DEFAULT_VOICE` | no | `af_heart` | Voice used when the request omits one. |
| `HU_AUTOROUTE_VOICE` | no | `default_hu` | Voice the autodetect re-routes to (only honored when HU is enabled). |
| `ROUTER_TIMEOUT` | no | `60` | Seconds to wait for the backend before returning 502. |

## How it slots into the stack

The OpenClaw gateway is configured (by `patch-config.mjs` step 11) with:

```
messages.tts.providers.openai.baseUrl = http://openclaw-tts-router:8080/v1
messages.tts.providers.openai.apiKey  = ${OPENCLAW_TTS_ROUTER_API_KEY}
messages.tts.providers.openai.model   = openclaw-tts        # router ignores it
messages.tts.providers.openai.voiceId = af_heart            # default
messages.tts.providers.openai.voiceAliases.{english,narrator,male,...}
```

…and step 11 only writes those keys when `OPENCLAW_TTS_ROUTER_API_KEY` is
present in `.env`. Drop the var and the patcher leaves the openai TTS
provider untouched — the router becomes opt-in.

## Caveats

- The OpenClaw **web chat** UI is hard-wired to the browser's native
  `speechSynthesis` API and does not call this router. Voice surfaces that
  use the gateway's TTS pipeline (Discord, Slack, the agent `tts` skill) do.
  See the parent repo's `llm/openclaw-tts-router/userscript/` for a
  Tampermonkey bridge that hijacks `speechSynthesis.speak()` if you want HU
  audio in the web chat too.
- The router is a thin proxy. It does not buffer audio across requests, does
  not cache, and is single-tenant by design — one bearer token per stack.
- ffmpeg adds ~50 MB to the image. If you only ever request `wav` / `flac` /
  `ogg` you could strip it; we ship it because OpenClaw's openai provider
  defaults to mp3.
