# Contributing

Thanks for considering a contribution! This is a small, opinionated repo that
wires together a known-good local AI agent stack. The most useful contributions
are bug reports, hardware-profile tunings, and small quality-of-life patches —
not large architectural changes.

## Before you start

- **Read [`CLAUDE.md`](CLAUDE.md).** It documents the working principles of
  this repo: patcher as source of truth, two-phase fresh-install, env
  overrides, and 14 implementation gotchas worth knowing before you touch
  `patch-config.mjs` or `docker-compose.yml`.
- **Skim the relevant `docs/`.** [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) for
  design rationale, [`CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) for how to
  swap models or backends, [`TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) for
  known failure modes.

## Reporting bugs

Open an issue using the **Bug report** template. Include:

- Hardware (GPU model, host OS, total RAM).
- Your `.env` with secrets redacted.
- `docker compose ps` and the failing service's logs (`docker compose logs
  --tail 200 <service>`).
- The exact command you ran.

## Proposing features

Open an issue using the **Feature request** template *before* sending a PR.
This repo prioritises being a small, well-wired reference — not every feature
belongs in-tree. A short discussion up front saves implementation time on
both sides.

Good fits: portability paths (new hardware profile, new cloud LLM backend),
patcher robustness, documentation gaps, small QoL fixes.

Less good fits: sprawling new services that warrant a companion repo, opinion
shifts away from the privacy-first / local-first defaults.

## Pull requests

- **Keep PRs focused.** One logical change per PR.
- **For changes to `patch-config.mjs` or `docker-compose.yml`**, run:
  ```bash
  node --check patch-config.mjs
  docker compose --env-file .env config --services
  ```
  and ideally bring the stack up on a real host before submitting. The repo's
  quality bar is "real verification on a real host," not "syntax-checks
  only" — see [`CLAUDE.md`](CLAUDE.md) → "Verify before declaring done".
- **Update `CHANGELOG.md`** under the `[Unreleased]` section with a one-line
  description of the change.
- **Match the existing comment style** in compose files and the patcher:
  comments explain *why* (a constraint, a benchmark number, an OpenClaw-
  specific behaviour), not *what*. See [`CLAUDE.md`](CLAUDE.md) → "Comments
  earn their place".
- **Don't commit your `.env`**, your `OPENCLAW_CONFIG_DIR` contents, or any
  model weights. The shipped `.gitignore` covers the common cases — verify
  with `git status` before staging.

## Hardware-profile patches

If you've tuned the stack for a different GPU (RTX 4090, A100, etc.) and it
works, a PR adding your numbers to `docs/CUSTOMIZATION.md` (or a row in
`README.md`'s hardware-targets table) is very welcome. Include:

- The model you ran (e.g. Gemma 4 12B BF16, Qwen 2.5 32B AWQ, …).
- The relevant `.env` values (`LLM_GPU_MEM_UTIL`, `LLM_MAX_MODEL_LEN`,
  `LLM_MAX_NUM_SEQS`).
- A measured tok/s decode rate at a representative context length.
- Anything else that surprised you (vLLM image override, kernel quirks, …).

## Licensing

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
