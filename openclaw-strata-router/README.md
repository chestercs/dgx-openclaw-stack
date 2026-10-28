# Claude Code on the Spark, offline: strata-router

Claude Code can run against a local model on the GB10 instead of Anthropic's API: DeepSeek-V4.1-Flash on
Strata's engine ([`openclaw-strata/`](../openclaw-strata/README.md)), with no request leaving the LAN. This
folder has the two pieces on top of that server:

- **strata-router** (`compose/strata-router.yml`), a small proxy that makes Claude Code's **auto mode** work
  against a single-stream local server;
- **the client launchers** (`client/sclaude`, `client/sclaude.cmd`) that point Claude Code at it.

What to expect on one GB10 (measured 2026-10-28, beside the OpenClaw stack): code 20–33 tok/s (DSpark
drafts it), prose 15–17 tok/s, a 25K-token prompt read at ~450 tok/s, and a resumed turn reads only its new
tokens from Strata's conversation cache. The model is a 284B MoE at ~1.75 bits; Strata's own measurements put
it at +9–10% perplexity on code and agent work and +53% on English prose against the full model. One request
at a time: subagents, the classifier and an OpenClaw bot on the same server wait for each other.

## Set up

1. **The server:** [`openclaw-strata/README.md`](../openclaw-strata/README.md) (engine build, model, pack).
2. **The router**, in `.env`:

   ```bash
   COMPOSE_FILE=docker-compose.yml:compose/strata.yml:compose/strata-router.yml
   STRATA_ROUTER_BIND=0.0.0.0        # Claude Code on another machine; default 127.0.0.1
   ```

   `docker compose up -d strata-router`. It reads `STRATA_API_KEY` for its own warm-up calls.
3. **The client** (Claude Code installed on that machine):
   - Linux / macOS: copy `client/sclaude` to a folder on `PATH`, `chmod +x`, put the server's
     `STRATA_API_KEY` in `~/.strata/key` (`chmod 600`), `export STRATA_ROUTER_URL=http://<gb10>:8096`.
   - Windows: copy `client/sclaude.cmd` to a folder on `PATH`, `setx STRATA_ROUTER_URL http://<gb10>:8096`,
     and write the key as plain ASCII (PowerShell's `>` writes UTF-16, which `cmd` cannot read):
     `New-Item -Force -ItemType Directory $env:USERPROFILE\.strata; Set-Content -NoNewline -Encoding ascii
     $env:USERPROFILE\.strata\key '<key>'`.
   - `SCLAUDE_LANGUAGE=Hungarian` (optional) adds a language line the model follows better than the one deep
     inside Claude Code's system prompt.
4. `sclaude`, or `sclaude --permission-mode auto` for long unattended runs.

What the launchers set, and why:

| Variable | Value | Why |
|---|---|---|
| `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` | the router, the server key | |
| `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS}_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL` | `deepseek-v4.1-flash` | one model serves every slot |
| `CLAUDE_CODE_AUTO_MODE_MODEL` | `deepseek-v4.1-flash-classifier` | the router recognises the classifier by this alias |
| `CLAUDE_CODE_ATTRIBUTION_HEADER` | `0` | otherwise the system prompt changes every request and Strata's prompt cache never hits |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | 131072, 131072, 90 | the server's context; compact before it fills |
| `API_TIMEOUT_MS` | 3600000 | a long think plus a long answer takes minutes |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | 1 | nothing else leaves the machine; no beta fields the server does not know |

Thinking stays on: Claude Code asks for it per request and the server follows (`anthropic_thinking:
on_request`). A long think is 5–6K tokens, minutes at these speeds, for noticeably better answers.

## The auto-mode problem

In auto mode, Claude Code asks a safety classifier before every non-read-only Bash command. The classifier
request is separate from the conversation:

- It has its own system prompt: a ~35K-token rule set that never changes, followed by a short transcript.
- Claude Code gives it a fixed ~60 s limit. No setting changes that limit.

`strata-dsv41` serves one request at a time. On a GB10 the cold rule set takes ~54 s to read, and Claude
Code often gives up before that: the engine stops reading and nothing is cached. Every
Bash call is refused after a minute's wait with "temporarily unavailable (timed out), so auto mode cannot
determine the safety of Bash", and the next call starts cold again.

## What the router does

- **Everything except the classifier passes through unchanged.** A disconnect still cancels the request
  upstream, so Esc stops the conversation as before.
- **Classifier requests are recognised by their model name** (`CLAUDE_CODE_AUTO_MODE_MODEL`), which the router
  rewrites to the real model id.
- **Classifier requests are not cancelled when Claude Code gives up.** Strata finishes reading, the rule set
  lands in its conversation cache, and the next classifier call reuses it.
- **The rule set is replayed at start.** The router saves the classifier request without its transcript
  (`/data/classifier-warm.json`) and replays it once Strata answers. Strata parks its conversation cache on
  disk, so the first Bash call after a router or server restart finds it cached.

## Measured (GB10, ISTA GSQ-RCO IQ1_M, 2026-10-28)

| | |
|---|---|
| Cold classifier call, client gave up at 5 s | Strata finished reading at 54 s; the same call then took 1 s |
| Classifier in a real auto-mode run (`git commit`) | 1.8 s |
| `cls-bench.mjs` (10 harmless, 10 dangerous commands), DeepSeek warm | 20/20, stage 1 median 2.1 s, stage 2 median 15.1 s |
| An earlier variant of the same cases, Qwen3.6-35B-A3B UD-IQ4_XS on an RTX 4090 | 38/40 (let `wevtutil cl Security` through twice), 0.2 s / 3.8 s |

`cls-bench.mjs` sends Claude Code's own stage 1 and stage 2 classifier requests with synthetic transcripts
swapped in. The router saves both request shapes into its data dir from real classifier calls; one auto-mode
session with a blocked command is enough. Then: `node cls-bench.mjs <router data dir>`.

The cost is that a classifier call takes the single engine. The conversation waits ~2 s per checked Bash
command, plus ~15 s when stage 2 runs (only for commands stage 1 would block).

The router does not depend on the OpenClaw services. It stays up while Strata is down or loading: requests
then get a 502, and the warm-up waits until Strata answers. Knobs: `STRATA_ROUTER_*`, `STRATA_UPSTREAM_*`,
`STRATA_CLASSIFIER_ALIAS` in [docs/reference/configuration.md](../docs/reference/configuration.md).
