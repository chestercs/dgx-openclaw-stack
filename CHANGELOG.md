# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`docs/TROUBLESHOOTING.md` → "Embedder crashed mid-index"** entry in
  the `vllm-embedding` section. Documents the observed failure mode
  where a transient `cudaErrorNotPermitted` crash leaves the vector
  store partially populated (e.g. `Indexed: 2/10 files · 14 chunks`)
  while OpenClaw still reports `Dirty: no` — and the
  `memory index --force` workaround. Upstream OpenClaw gap: the
  indexer does not re-dirty files whose embed step failed; until
  upstream fixes that, any embedder restart is a cue to run a
  forced reindex.

## [0.4.3] - 2026-04-23

Live secret rotation release. Adds `rotate-secrets.sh` as a safe,
re-runnable sibling to `bootstrap.sh` for rotating the auto-generated
secrets in an existing `.env` after install — and documents the three
operator scenarios that call for it.

### Added
- **`rotate-secrets.sh` — live secret rotation.** Sibling to `bootstrap.sh`
  for rotating the auto-generated secrets in an existing `.env` after
  install. Atomic write (temp file + `mv`), timestamped `.env.backup-*`
  before any change, post-write `docker compose config` validation with
  automatic restore on failure. Prints the deduped
  `docker compose up -d --force-recreate …` command for the services
  that consume each rotated key; the script does not restart anything
  itself so the operator picks the moment. Default set (`--all`):
  `VLLM_API_KEY`, `SEARXNG_SECRET`, `OPENCLAW_TTS_ROUTER_API_KEY`,
  `TTS_API_TOKEN`, plus `F5HUN_API_TOKEN` when already set (empty = HU
  TTS opted out of the CC-BY-NC model). `OPENCLAW_GATEWAY_TOKEN` is
  opt-in via `--include-gateway-token` because post-onboarding the real
  gateway auth lives in `openclaw.json`'s `gateway.auth.token`. The
  `^CHANGE_ME` gate `bootstrap.sh` uses is deliberately absent, so a
  fresh install can `cp .env.example .env && ./rotate-secrets.sh --all`
  to fill every placeholder, and live rotations don't need a special
  flag either.
- **`docs/CUSTOMIZATION.md` → "Rotating secrets" section** — operator
  runbook for the three rotation scenarios (routine hygiene, post-leak,
  pre-onboarding fill).

## [0.4.2] - 2026-04-22

Documentation release. Publishes the deep-dive knowledge base under
`docs/reference/`, declares an English-only documentation policy for public
repo content, translates the six reference files to English, and renames the
gitignored private-artifacts folder from `operator/` to `private/`.

### Added
- **`docs/reference/` knowledge base** — six public reference documents
  alongside the end-user docs: `llm-stack.md`, `tts-stack.md`,
  `tts-research-hungarian.md`, `openclaw-internals.md` (the deep-dive
  counterpart to `docs/ARCHITECTURE.md`: schema, 3-store credential
  layout, patcher step detail, CLI overhead), `patterns.md`, and an
  index `README.md`. The root `CLAUDE.md` "When in doubt" section
  points here as the next stop for deeper questions.
- **Documentation-language policy in `CLAUDE.md`** — new `Working principles`
  subsection declaring that every public repo file (README, SETUP, CLAUDE,
  CHANGELOG, `docs/**`, compose/patcher inline comments, commit messages,
  PR/issue templates, release notes) is written in English. Imported
  non-English material is translated before commit. No grandfathered
  exceptions — if non-English prose slips through, translate it rather
  than commit as-is.

### Changed
- **`docs/reference/` written in English.** All six files carry native
  English prose, and the former `tts-research-magyar.md` is renamed to
  `tts-research-hungarian.md` for filename consistency. The public repo
  is now linguistically uniform end to end.
- **`.gitignore` — `operator/` → `private/` rename.** The gitignored
  per-machine / private-artifacts folder is now `private/`, so the
  privacy state (not the role of the owning person) is the first-class
  label. Only the `.gitignore` line changes in the public tree; public
  repo consumers never interacted with this folder either way.

## [0.4.1] - 2026-04-22

Polish release rolling up the post-v0.4.0 fix batch. Two new idempotent patcher
steps harden the `openclaw agent` CLI path against token / credential drift, a
handful of defaults are flipped for less friction on a fresh clone, and the docs
catch up with the two patcher steps added post-tag.

### Added
- **Patcher step 12** — mirror `gateway.auth.token` into `gateway.remote.token`.
  The onboarding wizard writes `auth.token` but leaves `remote.token` unset, so
  the loopback CLI's WS connect failed with `unauthorized: gateway token
  mismatch` and silently fell back to an embedded-runner path (a side-car, not
  the production agent route). Step 12 keeps the two fields in lockstep on
  every `up`.
- **Patcher step 13** — sync the per-agent `auth-profiles.json`
  `vllm:default.key` with `VLLM_API_KEY`. The agent runner reads the vLLM
  credential from this per-agent store, *not* from
  `models.providers.vllm.apiKey`; drift after a `.env` rotation produced HTTP
  401 from vLLM even when the config-file apiKey was correct.
- **`operator/` gitignored** — per-operator private artifacts (local `.env`,
  `.claude/settings.local.json`, handoff notes, userscripts, TTS samples) live
  outside the public tree and are never part of a clone.

### Changed
- **`.env.example` defaults converged for public ergonomics:**
  - `CONTAINER_NAME_PREFIX=` (empty) — bare container names
    (`openclaw-gateway`, `vllm-llm`, …) in `docker ps`. Set to a prefix only
    if running multiple stacks on one host.
  - `OPENCLAW_ENABLE_DREAMING=1` — nightly memory consolidation on by default
    (the current stable OpenClaw image supports it; flip to 0 only if you've
    pinned a pre-2026.4.15 tag that rejects the memory-core plugin schema).
  - `HUGGING_FACE_HUB_TOKEN=` (empty) — replaces the `hf_CHANGE_ME` placeholder
    (HF tokens aren't openssl-generable like the rest). `bootstrap.sh` prompts
    for it on first run.
- **`openclaw-cli` service env simplified.** Dropped the redundant
  `OPENCLAW_GATEWAY_TOKEN` (the CLI prefers the env token over
  `gateway.remote.token` when both are present; drift between bootstrap-rotated
  `.env` and wizard-generated `auth.token` produced token-mismatch failures).
  Added `OPENAI_API_KEY` / `OPENAI_BASE_URL` mirror from `VLLM_API_KEY` so the
  embedded-runner fallback has a valid credential if the primary WS path ever
  hiccups.

### Fixed
- **vLLM healthcheck uses `python3`.** The `vllm/vllm-openai:gemma4-cu130`
  image ships no `python` alias; the healthcheck was red on every fresh pull
  even though the server was live.
- **Patcher step 11 `messages.tts.auto` / `messages.tts.mode` enum values.**
  Now writes the OpenClaw-accepted strings (`always` / `final`); previous
  values were silently rejected by the schema, so TTS stayed off even when
  the full provider block was wired.

### Docs
- Audience-first polish pass across `README.md`, `SETUP.md`, `CLAUDE.md`,
  `docs/*`, `docker-compose.yml`, and `patch-config.mjs`.
- Post-v0.4.0 polish: the fresh-install gateway crash-loop is documented as
  the intended two-phase onboarding flow (OpenClaw security model), the
  `--force-recreate openclaw-config-init openclaw-gateway openclaw-cli` trio
  is surfaced as the canonical recreate command, and all env-driven knobs
  are first-class in the docs.
- Patcher step count synced 11 → 13 across `README.md`, `CLAUDE.md`,
  `docker-compose.yml`, and `docs/ARCHITECTURE.md`.

## [0.4.0] - 2026-04-22

### Added
- **`CONTAINER_NAME_PREFIX` env var** — every service's `container_name:` is now
  `${CONTAINER_NAME_PREFIX:-dgx-}<service>`. Default keeps the existing
  `dgx-openclaw-gateway`, `dgx-vllm-llm`, … shape; set empty to drop the prefix
  for clean `openclaw-gateway`-style names. Bridge DNS reachability (the actual
  network plane) is unaffected — services resolve each other by compose service
  name + `hostname:` directive regardless of the container_name label.
- **`VLLM_HF_CACHE_VOLUME_NAME` env var** — Docker volume label for the shared
  HF cache is now env-driven (default `dgx-openclaw-hf-cache`). Lets sibling
  LLM stacks bind-mounting the same `VLLM_HF_CACHE_DIR` host path show up
  under one consistent label in `docker volume ls`.
- **TTS host bind/port env vars** (`TTS_EN_BIND`, `TTS_EN_PORT`,
  `TTS_F5HUN_BIND`, `TTS_F5HUN_PORT`, `TTS_ROUTER_BIND`, `TTS_ROUTER_PORT`).
  All three TTS services now publish their port on the host with a default of
  `127.0.0.1` — loopback only, ideal for `curl <port>/healthz` debugging
  without exposing the service on the LAN. Set `*_BIND=0.0.0.0` to expose any
  service to LAN clients (Bearer-token-protected via the existing TTS tokens).
  Sibling containers continue to use bridge DNS regardless of the binding.

### Changed
- **Patcher step 11 now writes the top-level `messages.tts.{enabled,auto,mode}`
  switches** (in addition to the existing `providers.openai` block and
  `voiceAliases`). Without these, the OpenClaw voice surfaces (Discord, agent
  `tts` skill) silently treat TTS as off even with the provider correctly
  wired. Web chat UI is unaffected (it's hard-wired to the browser's native
  `speechSynthesis` — known OpenClaw limitation, see CLAUDE.md).

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

[Unreleased]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.4.2...HEAD
[0.4.2]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/chestercs/dgx-openclaw-stack/releases/tag/v0.1.0
