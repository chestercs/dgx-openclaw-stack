# strata-router

A small proxy in front of a [Strata](https://github.com/Niko1221/Strata) server that runs
DeepSeek-V4.1-Flash on its `strata-dsv41` engine. Its job is to make Claude Code's **auto mode** work against
that server.

## The problem

In auto mode, Claude Code asks a safety classifier before every non-read-only Bash command. The classifier
request is separate from the conversation:

- It has its own system prompt: a ~35K-token rule set that never changes, followed by a short transcript.
- Claude Code gives it a fixed ~60 s limit. No setting changes that limit.

`strata-dsv41` serves one request at a time. On a GB10 it reads prompts at ~590 tok/s, so the cold rule set
needs about a minute. Claude Code gives up before that, the engine stops reading, and nothing is cached. Every
Bash call is refused after a minute's wait with "temporarily unavailable (timed out), so auto mode cannot
determine the safety of Bash", and the next call starts cold again.

## What the router does

- **Everything except the classifier passes through unchanged.** A disconnect still cancels the request
  upstream, so Esc stops the conversation as before.
- **Classifier requests are recognised by a separate model name.** Claude Code is started with
  `CLAUDE_CODE_AUTO_MODE_MODEL=deepseek-v4.1-flash-classifier`. The router rewrites that name to the real
  model id.
- **Classifier requests are not cancelled when Claude Code gives up.** Strata finishes reading, the rule set
  lands in its conversation cache, and the next classifier call reuses it.
- **The rule set is replayed at start.** The router saves the classifier request without its transcript
  (`/data/classifier-warm.json`) and replays it once Strata answers. The first Bash call after a restart then
  finds it cached.

## Measured (GB10, IST-DAS 1.75 bpw, 2026-10-28)

| | |
|---|---|
| Cold classifier call, client gave up at 5 s | Strata finished reading at 54 s; the same call then took 1 s |
| Classifier in a real auto-mode run (`git commit`) | 1.8 s |
| 20-case classifier bench (10 harmless, 10 dangerous), DeepSeek warm | 20/20, stage 1 median 1.8 s, stage 2 median 14.2 s |
| Same bench, Qwen3.6-35B-A3B UD-IQ4_XS on an RTX 4090 | 38/40 (missed `wevtutil cl Security` twice), 0.2 s / 3.8 s |

The cost is that a classifier call takes the single engine. The conversation waits ~2 s per checked Bash
command, plus ~14 s when stage 2 runs (only for commands stage 1 would block).

## Use

1. In `.env`:
   - `COMPOSE_FILE=docker-compose.yml:compose/strata-router.yml`
   - `STRATA_API_KEY=<the Strata server's key>`
   - For Claude Code on another machine, add `STRATA_ROUTER_BIND=0.0.0.0`.
2. Start it with `docker compose up -d strata-router`.
3. Start Claude Code against it:

```bat
set ANTHROPIC_BASE_URL=http://<host>:8096
set ANTHROPIC_AUTH_TOKEN=<the Strata server's key>
set ANTHROPIC_MODEL=deepseek-v4.1-flash
set ANTHROPIC_DEFAULT_HAIKU_MODEL=deepseek-v4.1-flash
set CLAUDE_CODE_AUTO_MODE_MODEL=deepseek-v4.1-flash-classifier
set CLAUDE_CODE_ATTRIBUTION_HEADER=0
claude --permission-mode auto
```

`CLAUDE_CODE_ATTRIBUTION_HEADER=0` matters. Otherwise the system prompt changes with every request and
Strata's prompt cache never hits.

The router does not depend on the OpenClaw services. It can stay up while Strata is down: requests then get a
502, and the warm-up waits until Strata answers. Knobs: `STRATA_ROUTER_*`, `STRATA_HOST`, `STRATA_PORT`,
`STRATA_API_KEY` in [docs/reference/configuration.md](../docs/reference/configuration.md).
