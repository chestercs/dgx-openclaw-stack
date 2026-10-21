# Agentic coding from Discord: exec, approvals, threads, long tasks

How the Discord bot works as a coding agent: a shell with chat-side approvals,
one Discord thread per task, workboard tracking, and the settings that let a task
run for hours without the gateway killing it. Companion to
[`discord-config.md`](./discord-config.md) (the Discord settings) and
[`discord-text-agent.md`](./discord-text-agent.md) (agent design). Every knob named
here, with its default, is in [`configuration.md`](./configuration.md#tools--exec).

## The exec surface: three layers

```
tools.exec                        (openclaw.json, config-init/steps/tools.mjs)    policy
~/.openclaw/exec-approvals.json   (written by the gateway)                        what is allowed
channels.discord.execApprovals    (openclaw.json, config-init/steps/discord.mjs)  where prompts land
```

1. **`tools.exec`** is written in full on every run, so a mode changed by hand
   cannot outlive a recreate. With `OPENCLAW_EXEC_SECURITY=allowlist` (default) it
   sets `mode: "ask"`: a command that matches an approved entry runs at once,
   anything else becomes an approval request instead of a refusal.
   `strictInlineEval: true` keeps `python -c` / `node -e` one-liners out of
   persistent grants, so every inline eval is reviewed even when the interpreter
   itself is approved. `safeBins` (`OPENCLAW_EXEC_SAFE_BINS`) are stdin-only
   filters that never need approval; keep interpreters out of that list.
   `applyPatch` is on, limited to the agent workspace.

2. **`exec-approvals.json`** sits next to `openclaw.json`. The gateway writes it
   when someone answers `/approve <id> allow-always`. It is not schema-validated
   config, so a wrong shape makes approvals fall back to ask/deny instead of
   crash-looping the gateway. Config-init never writes or prunes it, so learned
   grants survive every run.

3. **`channels.discord.execApprovals`** sends pending approvals as DMs with
   buttons to the approvers (`target: "dm"`). The user answers
   `/approve <id> allow-once`, `allow-always` or `deny`. Approvers come from
   `OPENCLAW_EXEC_APPROVERS`, then `OPENCLAW_DISCORD_OWNER_IDS`, then a concrete
   (not `*`) `OPENCLAW_DISCORD_COMMAND_OWNERS`. Config-init never writes a
   wildcard approver list: anyone could then approve the bot's own escalation.
   With no concrete ID it logs a warning, and prompts wait on a UI nobody
   watches until they time out to deny.

The bot's tool-policy block tells it to say "the approver got a DM" rather than
treat a pending approval as an error.

### What stays off by default

- **`OPENCLAW_EXEC_SECURITY=full`** writes `mode: "full"` and
  `strictInlineEval: false`: no gate at all. Anyone the bot answers can run
  commands in the gateway container, which mounts the config volume (secrets)
  read-write. `strictInlineEval` must be off in this mode: the gateway skips
  approval only when it is not true, so one-liners kept asking otherwise.
  Learned grants stay in `exec-approvals.json` and apply again after you switch
  back to `allowlist`.
- **`tools.codeMode`** is never written. It hides the normal tools behind an
  exec-only bridge, which breaks image, browser and the other tools on this bot.

## Thread per task

Coding and long tasks run in their own Discord thread:

- The `discord-delegation` AGENTS.md block
  (`config-init/templates/discord/AGENTS.d/`) teaches the agent
  `sessions_spawn {thread: true, taskName, cleanup: "keep", context: "isolated",
  task: "<self-contained description>"}`: one task, one thread; status via
  `/subagents list`; a summary in the main channel, the detail in the thread.
- `OPENCLAW_DISCORD_THREAD_BINDINGS` enables `channels.discord.threadBindings`
  (`/focus`, `/unfocus`); `OPENCLAW_DISCORD_THREAD_IDLE_HOURS` and
  `OPENCLAW_DISCORD_THREAD_MAX_AGE_HOURS` set how long a binding lives (coding
  threads are often revisited the next day).
- `OPENCLAW_SESSION_THREAD_SPAWN` writes `session.threadBindings.spawnSessions`,
  so a spawn may create a thread-bound session that starts from a clean transcript.

## "Work on it for a day"

These settings decide whether an hours-long task survives. All are written by
`config-init/steps/gateway.mjs` and `config-init/steps/tools.mjs`:

| Setting | Knob | Why |
|---|---|---|
| `agents.defaults.subagents.runTimeoutSeconds = 0` | `OPENCLAW_SUBAGENTS_BOUNDS` | No timeout on sub-agent runs; this is what makes day-long tasks possible. Depth 2, 5 children, 8 concurrent. |
| `agents.defaults.timeoutSeconds = 1800` | `OPENCLAW_AGENT_TIMEOUT_SECONDS` | The upstream 600 s aborted runs vLLM was still serving; a cold 128K prefill alone takes about 2 minutes. |
| `models.providers.vllm.timeoutSeconds = 1800` | `LLM_REQUEST_TIMEOUT_SECONDS` | Same reason, at the provider request level. |
| `agents.defaults.contextPruning {mode: cache-ttl}` | `OPENCLAW_AGENT_CONTEXT_PRUNING_MODE` | Trims old tool results in memory before each model call; exec and read output dominate context growth on coding runs. The transcript on disk is untouched. |
| `tools.loopDetection` thresholds | `OPENCLAW_TOOLS_LOOP_DETECTION_*` | A build-fix-build loop looks like a loop to the breaker. Raise the thresholds for long coding runs, but keep it on as the runaway backstop. |
| `tools.exec.timeoutSeconds = 1800` | `OPENCLAW_EXEC_TIMEOUT_SEC` | npm installs and cmake builds on the GB10 outrun the upstream default. |

`messages.queue.mode = "steer"` (`OPENCLAW_MESSAGES_QUEUE_MODE`) folds a message
sent mid-run into the running turn at the next model boundary, so the user can
redirect a task without waiting. Discord channels use `followup` instead
(`OPENCLAW_DISCORD_QUEUE_MODE`): a guild channel is one shared session, and `steer`
merged a second user's request into the first one's run. `/queue interrupt`
aborts.

Honesty rule (AGENTS.md): background work may only be promised when a spawn
actually happened (a runId exists).

## Workboard

`OPENCLAW_WORKBOARD=on` enables the bundled workboard plugin
(`config-init/steps/plugins.mjs`): `/workboard create|list|show|dispatch` from any
channel that takes commands. Dispatched cards carry the worker run id, session key
and log, which makes them the "what is running now" view for multi-hour work. The
`long-task-workboard` skill teaches the card protocol; it is written only while
the plugin is on, because it names tools that exist only then. `off` removes the
plugin entry and the skill; unset leaves the plugin entry alone and removes the
skill.

## Skills layout

Tool recipes live in workspace skills (`<workspace>/skills/<name>/SKILL.md`), not
in the always-injected AGENTS.md. Each skill shows up in the prompt as one
name-and-description line, and the body loads on demand. The Discord AGENTS.md is
~29 KB this way, against ~53 KB with the recipes inline, and the prefill of every
fresh session shrinks with it.

`config-init/steps/workspaces.mjs` and `config-init/steps/media.mjs` write them
from `config-init/templates/skills/`:

| Skill | Workspace | Written when |
|---|---|---|
| `cron-reminders`, `media-downloads`, `weather-forecast`, `coding-projects` | Discord | always |
| `browser-automation` | Discord and main | always |
| `long-task-workboard` | Discord | `OPENCLAW_WORKBOARD=on` |
| `people-memory` | Discord | `OPENCLAW_DISCORD_PEOPLE_MEMORY` (default on) |
| `community-summary` | Discord | people memory on and `OPENCLAW_COMMUNITY_SUMMARY` (default on) |
| `image-generation` | Discord | `IMAGE_GEN_DEFAULT_WORKFLOW` set and `image` not in `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` |
| `image-to-image` | Discord | `image` not in `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` |
| `video-generation` | Discord and main | `LTX_VIDEO_ENABLED=on` and `video` not off |
| `music-generation` | Discord | `music` not off |

AGENTS.md keeps what must be seen at decision time: format rules, image-history
rule, deep-agentic protocol, honesty, sender identity, sub-agent delegation,
thread tasks, the tool-policy core (output contract, failure honesty, when to
search) and the skill router, which maps request types to skill names and says
which media tools are off. The model decides from the router lines, so the core
of a rule must be in the router line itself, not only in the skill body.

Skill files belong to config-init entirely: edits are overwritten on the next
`docker compose up`, and a skill whose feature is turned off is deleted. To
customize, add your own skill under a different name. AGENTS.md marker blocks are
also rewritten; text outside the markers belongs to the operator.

## Adding a new key: the schema gate

The `agents.defaults.*` family rejects unknown or out-of-range keys with
`agents.defaults: Invalid input`, and the gateway then crash-loops. Before a new
key goes into a config-init step:

1. **Ask the WebGUI first.** Set the value in the Control UI (the matching
   Settings section), Save (the UI validates against the running schema), then
   read the persisted `openclaw.json` back and copy the exact path and shape.
2. **Dry-run on a copy.** `config-init/test/run.sh <name>` runs config-init on a
   copy of the live state, and `config-init/test/diff.sh <a> <b>` compares two
   such runs (before and after your change). `docker compose run --rm
   openclaw-config-init` runs it against the live config without touching the
   gateway.
3. **One change per recreate.** `docker compose up -d --force-recreate
   openclaw-config-init openclaw-gateway openclaw-cli`, then watch `healthz` for
   two minutes and
   `docker logs <prefix>openclaw-gateway --since 2m | grep -Ei "invalid input|unrecognized key|config invalid"`.
4. **Rollback** = the knob off in `.env` plus the same recreate; every step
   removes its key when its knob is `off`.
5. After a gateway recreate, run `openclaw memory status --deep`, and do not
   recreate while someone is testing the bot live.

## Verification

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)

# Ask the bot on Discord to run a command that is not approved yet: expect an
# approval DM, answer /approve <id> allow-always, then check that the grant is
# there and survives a config-init run.
docker exec ${PROJ}openclaw-gateway cat /home/node/.openclaw/exec-approvals.json | jq '.agents'
docker compose run --rm openclaw-config-init
docker exec ${PROJ}openclaw-gateway cat /home/node/.openclaw/exec-approvals.json | jq '.agents'

# Thread per task: "build me a small node script, work in a thread" should open
# a new thread, and /subagents list should show the child.

# Skills and bootstrap size:
docker exec ${PROJ}openclaw-gateway sh -lc \
  'ls /home/node/.openclaw/workspace-discord/skills/ && wc -c /home/node/.openclaw/workspace-discord/AGENTS.md'
```
