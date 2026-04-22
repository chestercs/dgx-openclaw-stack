# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-04-22

### Added
- **Bilingual TTS surface** wired into OpenClaw via the sanctioned
  `messages.tts.providers.openai.baseUrl` override (closed upstream issues
  #13907 / #29224). Three new services:
  - `openclaw-tts-en` — Kokoro 82M (Apache 2.0, TTS Arena #1 open-weights),
    OpenAI-compatible `/v1/audio/speech` on the bridge network. ~500 MB – 1 GB
    VRAM, coexists with the Gemma 4 NVFP4 LLM on the same GB10 GPU. Ten A/A-/B-
    grade Kokoro voices baked into the image at build time (`af_heart` default,
    plus `af_bella`, `af_nicole`, `af_aoede`, `af_kore`, `af_sarah`,
    `am_michael`, `am_fenrir`, `am_puck`, `bf_emma`).
  - `openclaw-tts-router` — ~150 LOC FastAPI router that fronts the EN backend
    (mandatory) and the optional Hungarian backend. ffmpeg bundled for
    wav→mp3/opus/aac transcoding. Maps the OpenAI default voice catalog
    (`alloy`, `coral`, `shimmer`, …) to closest Kokoro voices so the gateway
    never gets a 404.
  - `openclaw-tts-f5hun` — **OPT-IN** Hungarian TTS backed by
    `sarpba/F5-TTS_V1_hun_v2`. Wrapper code is MIT; model weights are
    CC-BY-NC-4.0 (non-commercial only). Gated three ways: `profiles: ["hu"]`
    on the service block (does not start without `--profile hu` or
    `COMPOSE_PROFILES=hu`), router needs `F5HUN_API_TOKEN` + `F5HUN_URL` set,
    and `bootstrap.sh` prompts once on first run. Default reference voice
    (Diana Majlinger / "Egri csillagok", LibriVox public domain) baked in;
    drop custom `<name>.wav` + `<name>.txt` into the `tts-f5hun-voices`
    Docker volume to add cloned voices.
- **Hungarian autodetect.** When the HU backend is active and the gateway
  sends one of the OpenAI default voices on input that contains Hungarian
  diacritics (`áéíóöőúüű`), the router silently re-routes to the HU backend
  so phonetics aren't mangled by the EN G2P. No-op when the HU profile isn't
  active.
- **Patcher step 11** — env-gated TTS provider wiring. When
  `OPENCLAW_TTS_ROUTER_API_KEY` is set, writes `messages.tts.providers.openai`
  (baseUrl, apiKey, model, voiceId) and `voiceAliases` (`english`, `narrator`,
  `male`, `female`, `magyar`, `hungarian`). Unset → step skips cleanly so users
  can opt out of TTS by simply leaving the var empty (and parking the two TTS
  services with `profiles: ["never"]`).
- **`bootstrap.sh`** — generates `OPENCLAW_TTS_ROUTER_API_KEY` and
  `TTS_API_TOKEN` alongside the existing secrets, regex-gated so re-runs never
  overwrite real values. Also asks once whether to opt into Hungarian TTS;
  on yes, generates `F5HUN_API_TOKEN`, sets `F5HUN_URL` to the in-compose
  service hostname, and adds `COMPOSE_PROFILES=hu` to `.env` so the next
  `docker compose up -d` brings up the HU service automatically.

## [0.2.0] - 2026-04-22

### Added
- **Remote vLLM / cloud-LLM backend support.** Three new env overrides
  (`OPENAI_BASE_URL`, `LLM_BASE_URL`, `EMBED_BASE_URL`) wire the gateway and
  the patcher to off-host LLM endpoints. Park the local `vllm-llm` /
  `vllm-embedding` services with `profiles: ["never"]` to skip them. Verified
  end-to-end on a GPU-less host pointing at a remote vLLM over LAN.
  Walkthrough: [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) → "Run with a
  remote vLLM backend".
- **`CLAUDE.md`**: contributor / coding-agent guide. Covers the patcher
  contract, the two-phase fresh-install flow, 14 implementation gotchas
  (network namespace sharing, bridge DNS, `profiles: ["never"]`, SearxNG
  `keep_only` quirk, baseUrl trailing-slash asymmetry, …) and copy-paste
  verification recipes.
- **SearxNG meta-search** (privacy-respecting, self-hosted) wired into
  OpenClaw's native `webSearch` provider with a strict engine whitelist
  (DuckDuckGo, Brave, Mojeek, Qwant, Startpage, Wikipedia family, Reddit,
  GitHub, arXiv) — queries never reach Google / Bing / Yandex / Yahoo / Baidu.
- **Hybrid (BM25 + vector) `memorySearch`** with MMR re-rank.
- **Gemma 4 31B Superchip emphasis** in the README — keyword discovery,
  hardware-targets table, NVFP4 / 128 GB unified memory tuning rationale.
- **Funding** (`.github/FUNDING.yml`).

### Changed
- **`patch-config.mjs` no longer writes
  `plugins.entries.searxng.config.webSearch.categories`.** The gateway
  forwarded the static string as a Python-list literal in the SearxNG POST
  form, which SearxNG rejected with a validation warning (search still worked
  via fallback defaults — log noise only). Per-query categories come from the
  agent's tool call instead.

## [0.1.0] - 2026-04-21

### Added
- Initial public release.
- One-file `docker-compose.yml` for OpenClaw + vLLM (Gemma 4 31B NVFP4) +
  bge-m3 multilingual embeddings, calibrated for the NVIDIA GB10 Superchip
  (DGX Spark / ASUS Ascent GB10).
- Idempotent `patch-config.mjs` — 10-step deep-merge patcher that survives
  re-runs of the OpenClaw onboarding wizard.
- `bootstrap.sh` non-destructive first-time setup (regex-gated secret
  rotation, host-path prompts, prerequisite checks).
- `templates/tool_chat_template_gemma4.jinja` so vLLM emits proper
  `tool_calls` JSON instead of raw `call:tool{...}` content.
- Documentation: `README.md`, `SETUP.md`, `docs/ARCHITECTURE.md`,
  `docs/CUSTOMIZATION.md`, `docs/TROUBLESHOOTING.md`.

[Unreleased]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/chestercs/dgx-openclaw-stack/releases/tag/v0.1.0
