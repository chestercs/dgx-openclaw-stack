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

## When the bot gets something wrong

A wrong answer, a made-up fact, a claimed action that did not happen, a
message in the wrong place: each one should leave the harness a little
stronger, not only the reply fixed. The loop:

1. **Observe.** The run's receipt: its line in `logs/claim-guard.jsonl`
   (tools, outcome, prompt version, sources), `openclaw audit --session <key>`
   and `scripts/daily-status.sh`. Not the chat text alone.
2. **Classify.** Which part failed: the map (the prompt did not say where to
   look), a tool (missing, unclear or wrong result), state (memory held a
   wrong fact), a sensor (claim-guard let it through), or a policy (an action
   that needed a gate).
3. **Repair** that part, as small as the failure: a contract line, a tool
   description, a memory fix with its source, a judge rule, a gate in
   harness-guard.
4. **Verify.** Add the failure as a case to the Discord eval
   ([`evals/discord/schema.md`](evals/discord/schema.md); real cases stay in
   `private/`), run it before and after (`scripts/discord-eval.sh --id …`),
   then the full set to see that nothing else broke. Compare reports under
   the same grading (`--rejudge`).
5. **Accept** with the numbers in the commit or the CHANGELOG: pass count,
   latency and tokens before and after.

A rule the model must follow belongs in two places: the prompt says it, and
a check outside the model (claim-guard, harness-guard, a tool's own refusal)
enforces it with a message that says how to fix the call. A rule that the
eval shows makes no difference is removed.

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
