# Agentic coding from Discord — exec, approvals, threads, long tasks

How this stack turns the Discord bot into a first-class coding agent: shell
access with chat-side approvals, per-task Discord threads, workboard tracking,
and the knobs that let a task genuinely run for hours (or a day) without the
gateway killing it. Companion to
[`discord-config.md`](./discord-config.md) (the per-step override table) and
[`discord-text-agent.md`](./discord-text-agent.md) (agent design).

## The exec surface — three layers

```
tools.exec (openclaw.json, patcher step 31)   ← policy: allowlist + ask-on-miss
~/.openclaw/exec-approvals.json (step 32)     ← WHAT is allowlisted, per agent
channels.discord.execApprovals (step 33)      ← WHERE approval prompts land
```

1. **`tools.exec`** (step 31) sets `security: "allowlist"` + `ask: "on-miss"`:
   commands matching an allowlist entry run immediately; anything else becomes
   an approval request instead of a denial. `strictInlineEval: true` keeps
   `python -c` / `node -e` one-liners out of persistent grants — every inline
   eval is reviewed even when the interpreter binary itself is allowlisted.
   `safeBins` (cat/grep/sed/head/tail/cut/wc) are stdin-only stream filters
   that never need approval. `applyPatch` is enabled workspace-only.

2. **`exec-approvals.json`** (step 32) is a sibling file next to
   `openclaw.json` — NOT schema-validated config, so a wrong shape degrades
   (approvals fall back to ask/deny) instead of crash-looping the gateway.
   The patcher seeds `defaults: {security: allowlist, ask: on-miss,
   askFallback: deny}` plus a developer-toolchain allowlist (git, npm, npx,
   node, python3, pip, make, cmake, go, cargo) for the Discord-routed agent.

   **Contract: the patcher never removes or rewrites existing entries.** The
   gateway persists `/approve <id> allow-always` decisions into this same
   file; the seed merge is set-union by command pattern, defaults are
   written only when undefined, and `OPENCLAW_EXEC_APPROVALS_SEED=off` skips
   seeding without deleting anything. Verify after any patcher change: grant
   an allow-always from Discord, re-run the patcher, confirm the entry
   survived.

3. **`channels.discord.execApprovals`** (step 33) routes pending approvals to
   Discord as interactive prompts in the approvers' DMs. The user answers
   `/approve <id> allow-once`, `allow-always`, or `deny`. Approvers resolve
   from `OPENCLAW_EXEC_APPROVERS` → `OPENCLAW_DISCORD_OWNER_IDS` → a concrete
   (non-`"*"`) `OPENCLAW_DISCORD_COMMAND_OWNERS` list; the step refuses to
   write a wildcard approver list and skips loudly when no concrete snowflake
   exists — an approval surface open to everyone would let any guild member
   approve the bot's own escalation.

Pending approvals expire after ~30 minutes; on expiry `askFallback: "deny"`
applies. The agent is taught (tool-policy core block) to tell the user "the
approver got a DM" instead of treating a pending approval as an error.

### What stays deliberately off

- `tools.exec.security: "full"` — removes the approval gate entirely;
  arbitrary commands from any Discord sender run immediately in the gateway
  container. Off by default, but available as an explicit homelab opt-in via
  `OPENCLAW_EXEC_SECURITY=full`: the patcher then writes `full` into both
  `tools.exec.security` and `exec-approvals.json` `defaults.security` (the
  two layers must agree — a stricter file value would silently re-gate exec).
  Learned allow-always grants are never touched; they go inert under full and
  resume on rollback. Rollback = set the knob back to `allowlist` (or unset)
  and re-run the patcher.
- `tools.codeMode` — hides normal tools behind an exec-only QuickJS bridge;
  breaks image/browser/etc. on this multi-tool bot.

## Thread-per-task

Coding and long-running tasks spawn into their own Discord thread instead of
camping on the main channel:

- The `discord-thread-tasks` AGENTS.md policy block teaches the agent:
  `sessions_spawn {thread: true, taskName, cleanup: "keep", context:
  "isolated", task: "<self-contained description>"}` — one task, one thread;
  status via `/subagents list`; summary on the main channel, detail in the
  thread.
- Step 29 (existing) enables `channels.discord.threadBindings`; step 29c
  tunes binding lifetimes (`idleHours` / `maxAgeHours` — suggested 48/336 for
  multi-day coding threads); step 29d (`session.threadBindings.spawnSessions`)
  is schema-gated off until validated (see the runbook below).

## "Work on it for a day" — the long-run stack

Five knobs decide whether an hours-long task survives:

| Layer | Knob | Why |
|---|---|---|
| Sub-agent run timeout | `agents.defaults.subagents.runTimeoutSeconds = 0` (step 5b, gated) | `0` = no timeout. This is the literal enabler for day-long tasks. |
| Gateway per-run ceiling | `agents.defaults.timeoutSeconds` 600→1800 (step 8i, gated) | Upstream 600s aborts runs the vLLM backend is still serving (cold 128K prefill alone is ~127s). |
| Context budget | `agents.defaults.contextTokens = 131072` (step 8j, gated) | Match the gateway's packing budget to the backend's real window. |
| Context hygiene | `agents.defaults.contextPruning {mode: cache-ttl}` (step 8j, gated) | Trims OLD tool results in-memory between compactions — exec/read output dominates context growth on coding runs. Transcript on disk is untouched. |
| Loop detection | `tools.loopDetection` thresholds (step 8h ext.) | Repeated `npm run build` is legitimate; relax warning/critical so the circuit breaker doesn't kill a productive run. Keep it enabled — it's still the runaway-chain backstop. |

Plus `messages.queue.mode = "steer"` (step 39): a follow-up message lands at
the next model boundary, so the user can redirect a running task without
waiting hours. `/queue interrupt` aborts.

Honesty contract (AGENTS.md): background work may only be *promised* when a
spawn actually happened (there is a runId). "I'll work on it overnight"
without a spawn is the lie the honesty block exists to prevent.

## Workboard

`OPENCLAW_WORKBOARD=on` (step 34, tri-state) enables the bundled workboard
plugin: `/workboard create|list|show|dispatch` from any command-capable
channel. Dispatched cards carry the worker run id, session key and worker
log — the "what is running right now" surface for multi-hour work. The
`long-task-workboard` skill teaches the agent the card protocol.

## Skills layout (docs diet)

`OPENCLAW_AGENT_DOCS_MODE=skills` (default) moves tool-usage recipes out of
the always-injected AGENTS.md into per-recipe workspace skills
(`<workspace>/skills/<name>/SKILL.md`):

| Skill | Source block | Gate |
|---|---|---|
| `cron-reminders` | cron-tools | always |
| `browser-automation` | browser-tools + orchestration browser chains | always (both workspaces) |
| `image-generation` | image-gen-tools | `IMAGE_GEN_DEFAULT_WORKFLOW` |
| `image-to-image` | discord-i2i | `OPENCLAW_DISCORD_AGENT_I2I_CHEATSHEET` |
| `video-generation` | ltx-video-tools | `LTX_VIDEO_ENABLED` (both workspaces) |
| `media-downloads` | orchestration (yt-dlp/transcribe/upload) | `OPENCLAW_DISCORD_AGENT_TOOL_ORCHESTRATION` |
| `weather-forecast` | orchestration (open-meteo) | `OPENCLAW_DISCORD_AGENT_TOOL_ORCHESTRATION` |
| `coding-projects` | orchestration (code/host/git_push) | `OPENCLAW_DISCORD_AGENT_TOOL_ORCHESTRATION` |
| `long-task-workboard` | new | `OPENCLAW_WORKBOARD=on` |

What stays in AGENTS.md (decision-time policy, always injected): format
rules, image-history discipline, deep-agentic protocol, honesty, sender
identity, subagent delegation, thread-tasks, the tool-policy core (output
contract / failure honesty / web_search triggers), and a short skill router
mapping trigger phrases to skill names.

Each skill surfaces as one name+description line in the prompt (~100 tokens)
and the body loads on demand. Net effect on the discord workspace: AGENTS.md
~37.5 KB → ~17 KB, total bootstrap ≈ halved — directly proportional to the
GB10's cold-prefill latency on every fresh session.

Skill files are 100% patcher-owned (header comment says so): operator edits
are overwritten on the next compose up. To customize, either flip to
`agentsmd` mode and edit the marker block, or add your own differently-named
skill alongside. `agentsmd` mode restores the legacy all-in-AGENTS.md layout
and removes the skill files — the rollback path if your model turns out not
to read skill bodies reliably (watch for the bot "knowing the skill exists
but improvising the recipe").

## Schema-gate runbook (risky keys)

For every key marked *gated* above (`agents.defaults.subagents`,
`agents.defaults.timeoutSeconds` / `contextTokens` / `contextPruning`,
`session.threadBindings`, `plugins.entries.workboard`) — the
`agents.defaults` family has crash-looped this gateway twice before:

1. **Oracle first**: set the equivalent value in the live WebGUI (Settings →
   the matching section) → Save (the GUI validates against the running
   schema) → read the persisted JSON back over SSH → confirm the path matches
   what the patcher writes.
2. **One knob per recreate**: flip exactly one knob in `.env`, then
   `docker compose run --rm openclaw-config-init` (isolated patcher run, the
   gateway is untouched), inspect with `jq`, then
   `docker compose up -d --force-recreate openclaw-config-init
   openclaw-gateway openclaw-cli`.
3. **Watch the gate**: 120s healthz loop +
   `docker logs <prefix>openclaw-gateway --since 2m | grep -Ei "invalid
   input|unrecognized key|config invalid"`.
4. **Rollback** = knob off in `.env` + the same force-recreate; the step's
   self-heal branch strips the key. Keep a timestamped `openclaw.json.bak`
   anyway.
5. After any gateway recreate: `openclaw memory status --deep` (the vector
   index is rollback-sensitive) and don't recreate while someone is
   live-testing the bot.

## Verification (functional smoke)

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}

# exec ask-flow end to end: ask the bot (on Discord) to run an UNLISTED
# command → expect an approval DM → /approve <id> allow-always → verify the
# learned entry landed, then that it SURVIVES a patcher re-run:
docker exec ${PROJ}openclaw-gateway cat /home/node/.openclaw/exec-approvals.json | jq '.agents'
docker compose run --rm openclaw-config-init
docker exec ${PROJ}openclaw-gateway cat /home/node/.openclaw/exec-approvals.json | jq '.agents'

# thread-per-task: "build me a small node script, work in a thread" →
# expect a new thread + /subagents list shows the child.

# skills loaded:
docker exec ${PROJ}openclaw-cli openclaw skills list | grep -E "cron-reminders|coding-projects|browser-automation"

# bootstrap size after the diet:
docker exec ${PROJ}openclaw-gateway sh -lc 'wc -c /home/node/.openclaw/workspace-discord/AGENTS.md'
```
