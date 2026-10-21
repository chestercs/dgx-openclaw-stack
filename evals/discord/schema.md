# Discord agent eval cases

Regression cases for the Discord-routed agent, run by `scripts/discord-eval.mjs`.
Real cases come from the agent's own Discord history and stay out of this repo
(`private/evals/discord/cases.yaml` on the Docker host): they quote community
members. This directory holds the schema and synthetic examples
(`examples.yaml`), which the runner also accepts.

## Why

A prompt or harness change is only better if it scores better on cases taken
from real failures, and does not break the cases next to them. Each failure
gets a control case: one where the behaviour must NOT happen (no search for
small talk, no memory write for a joke).

## Case fields

```yaml
- id: person-identity-01          # unique, kebab-case
  kind: fact                      # fact | discord-lookup | action-claim | memory |
                                  # small-talk | schedule | honesty | tagging
  smoke: true                     # part of the quick set run after every change
  context: "#smopen-ai, Imbul a Bakterház"   # prefixed to the prompt as channel context
  prompt: "szereted matolcsy gyorgyot?"
  expect:
    tools_any: [web_search]       # at least one of these must run
    tools_none: [write]           # none of these may run
    must_match: ["jegybank|MNB|közgazdász|nem ellenőriztem"]   # regex, case-insensitive
    must_not_match: ["előadó"]
    abstain_ok: false             # "nem tudom / nem ellenőriztem" counts as a pass
  rubric: >-
    Correct: identifies him as an economist / former central bank governor, or
    says it does not know. Wrong: calls him a performer or invents a role.
```

All `expect` keys are optional. The machine checks (`tools_*`, `must_*`) decide
first; the rubric goes to a judge model only when they pass. The judge answers
`pass`, `fail` or `unknown` per case, and `unknown` is reported, not counted as
a pass.

## Running

```bash
# on the Docker host, from the stack checkout
scripts/discord-eval.sh --smoke             # quick set, one run per case
scripts/discord-eval.sh --runs 3            # full set, three runs per case
scripts/discord-eval.sh --id person-identity-01
scripts/discord-eval.sh --cases evals/discord/examples.yaml   # public sample
```

`memory` and `schedule` cases write the live people file or create real
automations, so they run only with `--side-effects`.

Each run uses a fresh session key (`agent:discord-friend:eval:<id>:<stamp>-<n>`),
deleted afterwards, so cases never see each other or a live channel. Limits of
the CLI path: Active Memory does not run in `openclaw agent` turns, and
people-context needs a real Discord sender; cases that depend on them are tested
live in the test channel.

Each run prints pass/fail, the tools that ran, latency and input tokens for the
whole turn; the summary gives the pass rate, cases that passed every run
(pass^k), median/p90 latency and median input tokens. The full report goes to
`private/evals/discord/runs/<timestamp>.json` for comparing runs.
