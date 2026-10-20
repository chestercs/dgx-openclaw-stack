# Contributing

Thanks for considering a contribution! This is a small, opinionated repo that
wires together a known-good local AI agent stack. The most useful contributions
are bug reports, hardware-profile tunings, and small quality-of-life patches —
not large architectural changes.

## Before you start

- **Read [`CLAUDE.md`](CLAUDE.md).** It documents the working principles of
  this repo (config-init as the source of truth, the two-phase fresh install,
  env overrides) and the implementation gotchas worth knowing before you touch
  `config-init/` or the compose files.
- **Skim the relevant `docs/`.** [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) for
  design rationale, [`CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) for how to
  swap models or backends and how to add a config-init knob,
  [`TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) for known failure modes, and
  [`reference/configuration.md`](docs/reference/configuration.md) for every knob.

## Reporting bugs

Open an issue using the **Bug report** template. Include:

- Hardware (GPU model, host OS, total RAM).
- Your `.env` with secrets redacted.
- `docker compose ps` and the failing service's logs (`docker compose logs
  --tail 200 <service>`). For OpenClaw-side problems, the `[patch-config]`
  lines from `docker logs openclaw-config-init` help too; they mask secrets.
- The exact command you ran.

## Proposing features

Open an issue using the **Feature request** template *before* sending a PR.
This repo prioritises being a small, well-wired reference — not every feature
belongs in-tree. A short discussion up front saves implementation time on
both sides.

Good fits: portability paths (new hardware profile, new cloud LLM backend),
config-init robustness, documentation gaps, small QoL fixes.

Out of scope for this repo (better as a companion project): large new service
stacks that significantly expand the footprint, or direction changes that move
away from the self-hosted, local-first defaults. Both are legitimate — they
just belong in their own repo, not this one.

## Pull requests

- **Keep PRs focused.** One logical change per PR.
- **For changes to `config-init/` or the compose files**, run:
  ```bash
  node --check config-init/patch-config.mjs   # and every file you changed under config-init/
  docker compose --env-file .env config --services
  ```
  For config-init changes, also run the test harness in `config-init/test/` on a
  copy of a real state and include the diff summary; see
  [`CUSTOMIZATION.md`](docs/CUSTOMIZATION.md#add-a-knob-or-a-step-to-config-init).
  Ideally bring the stack up on a real host before submitting. The repo's
  quality bar is "real verification on a real host," not "syntax-checks
  only" — see [`CLAUDE.md`](CLAUDE.md) → "Verify before declaring done".
- **Document new knobs** in `docs/reference/configuration.md` (default, effect,
  and why the default is what it is).
- **Update `CHANGELOG.md`** with a one-line description of the change under an
  `## [Unreleased]` heading at the top (add the heading if it is not there).
- **Match the existing comment style** in compose files and config-init:
  comments explain *why* (a constraint, a benchmark number, an OpenClaw-
  specific behaviour), not *what*. See [`CLAUDE.md`](CLAUDE.md) → "Comments
  earn their place".
- **Write in English.** Docs, comments and commit messages in this repo are
  English only.
- **Don't commit your `.env`**, your `OPENCLAW_CONFIG_DIR` contents, or any
  model weights. The shipped `.gitignore` covers the common cases — verify
  with `git status` before staging.

## Hardware-profile patches

If you've tuned the stack for a different GPU and it works, a PR with your
numbers is very welcome: a model file under `compose/` if the `vllm-llm` command
changes, plus the measurements in `docs/reference/llm-stack.md` or a row in the
README's hardware section. Include:

- The model you ran (e.g. `nvidia/Gemma-4-26B-A4B-NVFP4`, or a quantization
  that fits your GPU).
- The relevant `.env` values (`LLM_MODEL_ID`, `LLM_GPU_MEM_UTIL`,
  `LLM_MAX_MODEL_LEN`, `LLM_MAX_NUM_SEQS`).
- A measured tok/s decode rate at a representative context length.
- Anything else that surprised you (vLLM image override, kernel quirks, …).

## Licensing

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
