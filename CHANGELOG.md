# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/chestercs/dgx-openclaw-stack/releases/tag/v0.1.0
