# Discord configuration reference

`openclaw-config-init` (`config-init/`) writes the Discord settings into
`openclaw.json` and the Discord agent's workspace on every `docker compose up`.
The defaults suit a small community server run by one operator: open access for
members, a mention gate, partial streaming, a wide tool surface for the bot.
Every setting below has a `.env` knob; the default of each knob is in
[`configuration.md`](./configuration.md#discord-channel).

Nothing in `config-init/steps/discord.mjs` runs until onboarding
(`openclaw channels add --channel discord`) has created an enabled
`channels.discord` block. The bot token and the accounts in that block belong
to the operator and are never touched.

Config-init writes desired state: most keys below are set on every run, so a
hand edit in `openclaw.json` is overwritten on the next `up`. Change the knob
instead. The few keys that are written only when unset are marked "if unset".

Sibling docs:

- [`discord-text-agent.md`](./discord-text-agent.md): how the text-channel bot
  works (flow, tool catalog, reactions, streaming, TTS opt-in).
- [`discord-voice-agent.md`](./discord-voice-agent.md): `/vc` voice, isolation,
  threat model, DAVE encryption.

## At a glance

Modules are under `config-init/steps/`. Templates are under
`config-init/templates/`.

### Channel and commands (`discord.mjs`)

| Setting | Knob | Why |
|---|---|---|
| `channels.discord.suppressEmbeds = false` | `OPENCLAW_DISCORD_SUPPRESS_EMBEDS` | Without an explicit `false` the plugin suppresses every embed, so no image, video or link unfurls. |
| `commands.allowFrom.discord` | `OPENCLAW_DISCORD_COMMANDS_ALLOW` | The guild slash gate. Unset upstream, so every guild slash command answers "not authorized" ([#19310](https://github.com/openclaw/openclaw/issues/19310)). |
| `commands.ownerAllowFrom` (`discord:<id>`) | `OPENCLAW_DISCORD_COMMAND_OWNERS` | Owner commands. Written channel-scoped: a bare snowflake reads as a channel target. |
| `tools.elevated.allowFrom.discord` | `OPENCLAW_TOOLS_ELEVATED_DISCORD_ALLOW` | Who may use elevated tools from Discord. |
| `allowFrom`, `dmPolicy`, `groupPolicy` | `OPENCLAW_DISCORD_AUTHZ`, `OPENCLAW_DISCORD_OWNER_IDS` | Access posture: `open`, `pairing`, `owner-only`, `allowlist`. See [Slash-command authorization](#slash-command-authorization). |
| `guilds["*"].requireMention` | `OPENCLAW_DISCORD_REQUIRE_MENTION` | The mention gate for every guild. See [Mention gate](#mention-gate-vs-activation). |
| `guilds["*"].channels` | `OPENCLAW_DISCORD_ALWAYS_CHANNELS` | Channels the bot reads in full despite the gate, plus a `*` fallback that mirrors the gate. |
| `ackReactionScope` (if unset) | `OPENCLAW_DISCORD_ACK_REACTION_SCOPE` | Auto-ack reactions off: a stale reaction queue replays them on session resume and the bot cycles emojis ([#46024](https://github.com/openclaw/openclaw/issues/46024)). |
| `actions.reactions` | `OPENCLAW_DISCORD_ACTIONS_REACTIONS` | The agent's own reaction action. |
| `streaming.mode`, `streaming.preview.toolProgress` | `OPENCLAW_DISCORD_STREAMING`, `OPENCLAW_DISCORD_STREAMING_PREVIEW_TOOL_PROGRESS` | Partial streaming: a placeholder edited as tokens arrive. Posted at once, a long answer is a minute of silence. |
| `maxLinesPerMessage` | `OPENCLAW_DISCORD_MAX_LINES` | At the upstream 17 lines a code answer came out as six messages. |
| `replyToMode` | `OPENCLAW_DISCORD_REPLY_TO_MODE` | `first`: the first message of a run is a native reply, so in a busy channel it is clear which question it answers. |
| `voice.enabled` (if unset) | `OPENCLAW_DISCORD_VOICE` | Registers `/vc`. See [Voice](#voice). |
| `threadBindings.*` | `OPENCLAW_DISCORD_THREAD_BINDINGS`, `_THREAD_IDLE_HOURS`, `_THREAD_MAX_AGE_HOURS` | Registers `/focus`, `/unfocus`, `/agents`, `/session`; `enabled` only if unset. |
| `execApprovals` | `OPENCLAW_DISCORD_EXEC_APPROVALS`, `OPENCLAW_EXEC_APPROVERS` | Exec approval prompts as DMs with buttons. Approvers are never `*`. Removed when `OPENCLAW_TOOLS_EXEC=off`. |
| `autoPresence` | `OPENCLAW_DISCORD_AUTO_PRESENCE` (+ three text knobs) | Presence mirrors runtime health. |
| `mentionAliases` | `OPENCLAW_DISCORD_MENTION_ALIASES` | Outbound `@handle` becomes a real mention; the plugin's fallback directory is empty right after a restart. |
| `allowBots` | `OPENCLAW_DISCORD_ALLOW_BOTS` | Lets other bots in (for example only when they tag this one). |
| `defaultTo` | `OPENCLAW_DISCORD_DEFAULT_TO` | Target for sends that name none; without it cron and heartbeat turns invented targets. |
| `tools.deny += video_generate` (global) | `OPENCLAW_DENY_OPENAI_VIDEO_TOOL` | The built-in tool calls OpenAI Sora and fails without a key. A global deny covers guild routes too. |
| `commands.bash` removed | (none) | No shell directive on Discord. |

### Runs, queueing, heartbeat (`gateway.mjs`)

| Setting | Knob | Why |
|---|---|---|
| `messages.queue.byChannel.discord` | `OPENCLAW_DISCORD_QUEUE_MODE` | `followup`: a guild channel is one shared session, and `steer` merged a second user's request into the first one's run. |
| `messages.queue.mode` | `OPENCLAW_MESSAGES_QUEUE_MODE` | Other surfaces: `steer` folds a mid-run message into the running turn. |
| `messages.statusReactions` | `OPENCLAW_STATUS_REACTIONS` | Run-state emoji on the user's message, the first sign a slow model is working. Driven by the run lifecycle, not the ack queue. |
| `agents.defaults.heartbeat.{target,to}` | `OPENCLAW_HEARTBEAT_TO` | Heartbeat DMs the owner. See [Heartbeat](#heartbeat). |
| `session.threadBindings.{spawnSessions,defaultSpawnContext}` | `OPENCLAW_SESSION_THREAD_SPAWN` | `sessions_spawn {thread:true}` may create a thread-bound session. |

### The Discord agent (`agents.mjs`, `memory.mjs`)

These apply to every agent a catch-all Discord binding routes to (no peer,
guild or role scope). A channel-scoped agent such as the Valheim builder keeps
its own settings.

| Setting | Knob | Why |
|---|---|---|
| `tools.profile` | `OPENCLAW_DISCORD_AGENT_TOOLS_PROFILE` | `full`. Without it the agent inherits `coding`, which has no browser, tts or canvas. |
| `tools.alsoAllow` | `OPENCLAW_DISCORD_AGENT_ALSO_ALLOW` | On guild channels `cron` drops out of the catalog even under `full`. Additive. |
| `tools.deny` | `OPENCLAW_DISCORD_AGENT_TOOLS_DENY` | Operator and desktop tools the persona never needs; a smaller catalog means better tool choice. Additive. |
| `tools.deny += comfyui_image__*` | `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` | Media groups ComfyUI does not run. The same knob removes their skills (`media.mjs`) and writes an "off" line in the router (`workspaces.mjs`), so the bot says the tool is off instead of calling it. |
| `thinkingDefault` | `OPENCLAW_DISCORD_AGENT_THINKING` | `high`; at `minimal` the model skipped tool calls and claimed it could not search. |
| `deep` sub-agent | `OPENCLAW_DISCORD_DEEP_AGENT`, `_DEEP_AGENT_ID` | Same model with full thinking, spawnable by the Discord agent. Off by default (40/40 against 39/40 at 2-2.5x the time). |
| `plugins.entries.active-memory` (`memory.mjs`) | `OPENCLAW_ACTIVE_MEMORY` | Recall before every reply of `discord-friend` (mode `always`, 30 s, thinking off). |

### Plugins and hooks (`plugins.mjs`)

Discord-only plugins are wired only while `channels.discord` exists. Their code
ships in `openclaw-base-ext/plugins/` and is mounted at `/opt/openclaw-plugins/`.
None registers a reply hook (`reply_payload_sending` / `message_sending`): any
such hook turns off Discord preview streaming for every Discord turn.

| Setting | Knob | Why |
|---|---|---|
| `hooks.internal.entries.bootstrap-extra-files.paths += people/MEMORY.md` | `OPENCLAW_DISCORD_PEOPLE_MEMORY` | Guild long-term memory. See [Long-term memory](#long-term-memory-in-guild-channels). |
| `people-context` plugin | follows `OPENCLAW_DISCORD_PEOPLE_MEMORY` | Mention IDs and the sender's people-file lines on each turn. |
| `claim-guard` plugin | `OPENCLAW_DISCORD_CLAIM_GUARD` | See [Honesty checks](#honesty-checks-claim-guard). |
| `dm-guild-gate` plugin | `OPENCLAW_DISCORD_DM_GUILD_GATE` | See [DMs only from server members](#dms-only-from-server-members-dm-guild-gate). |
| `plugins.entries.workboard` | `OPENCLAW_WORKBOARD` | `/workboard` card board for long tasks; `workspaces.mjs` writes its skill. |

### Prompt text (`workspaces.mjs`)

| What | Knob | Why |
|---|---|---|
| Marker blocks in `workspace-discord/AGENTS.md`: format rules, contract (what "done" means per request type), sender identity, delegation, skill router, image-history rule | `OPENCLAW_DISCORD_MEMBER_NAMES`, `OPENCLAW_DISCORD_ADMIN_NAME`, `OPENCLAW_DISCORD_ADMIN_ID` | Text from `templates/discord/AGENTS.d/`. Names come from `.env`, so the repo carries none. Text outside the markers is the operator's. |
| Skills: `cron-reminders`, `browser-automation`, `media-downloads`, `weather-forecast`, `coding-projects` | `OPENCLAW_SANDBOX_PUBLIC_URL` (coding) | Tool recipes the bot reads on demand; one router line each keeps AGENTS.md at ~29 KB. Skill files are wholly config-init's. |
| Skills: `people-memory`, `community-summary` | `OPENCLAW_PEOPLE_DIGEST_NSFW`, `OPENCLAW_PEOPLE_DIGEST_EXCLUDE`, `OPENCLAW_COMMUNITY_SUMMARY` | Recipes for the two cron jobs below. |
| `people/MEMORY.md` seed (written once) | `OPENCLAW_DISCORD_PEOPLE_MEMORY` | The file then belongs to the bot. |

Media skills (image, image-to-image, video, music) and the top-level `tts`
block come from `media.mjs`; see `openclaw-image-comfyui/README.md` and
`openclaw-tts-fish/README.md`.

## Narrowing toward upstream defaults

For a stricter bot, or one that behaves close to a plain
`openclaw channels add --channel discord` install, set in `.env`:

```bash
OPENCLAW_DISCORD_AUTHZ=allowlist          # keep upstream access settings
OPENCLAW_DISCORD_REQUIRE_MENTION=on       # mention gate (also the stack default)
OPENCLAW_DISCORD_STREAMING=off            # atomic delivery
OPENCLAW_DISCORD_REPLY_TO_MODE=off        # flat delivery
OPENCLAW_DISCORD_MAX_LINES=off            # leave maxLinesPerMessage alone
OPENCLAW_DISCORD_VOICE=off
OPENCLAW_DISCORD_THREAD_BINDINGS=off
OPENCLAW_DISCORD_AUTO_PRESENCE=off
OPENCLAW_STATUS_REACTIONS=off
OPENCLAW_TOOLS_EXEC=off                   # removes tools.exec and Discord approvals
OPENCLAW_DISCORD_AGENT_TOOLS_PROFILE=off  # profile not written
OPENCLAW_DISCORD_AGENT_ALSO_ALLOW=off
OPENCLAW_DISCORD_AGENT_TOOLS_DENY=off
OPENCLAW_DISCORD_AGENT_THINKING=off
OPENCLAW_ACTIVE_MEMORY=off
OPENCLAW_DISCORD_PEOPLE_MEMORY=off
OPENCLAW_DISCORD_CLAIM_GUARD=off
OPENCLAW_DISCORD_DM_GUILD_GATE=off
```

Then recreate the config-init chain:

```bash
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

`suppressEmbeds`, the command allowlists and `actions.reactions` are always
written; set their knobs to the values you want. The `off` knobs for the agent
settings stop writing them but do not remove values an earlier run wrote, and
the AGENTS.md marker blocks stay until you delete them by hand.

## Slash-command authorization

OpenClaw checks a guild slash command against two lists: the global
`channels.discord.allowFrom` and the guild's `users`. With the upstream
`groupPolicy: "allowlist"` and an empty `users` array every guild slash command
is blocked. Discord shows the rejection as an ephemeral *"You are not
authorized to use this command"* to the invoker only, so there is no
server-side log line. Symptom: `/activation`, `/discord input:` and similar work
in a DM and fail in a guild ([#19310](https://github.com/openclaw/openclaw/issues/19310)).

Slash commands are worth having in guilds: Discord acks the interaction at
once, so the user sees "thinking..." instead of dead air while the model
prefills.

`OPENCLAW_DISCORD_AUTHZ`:

- **`open`**: `allowFrom = ["*"]`, `dmPolicy = "open"`, `groupPolicy = "open"`.
  The guild member list is the trusted population. `dm-guild-gate` then limits
  DMs to people who share a server with the bot.
- **`pairing`**: `allowFrom = ["*"]`, `dmPolicy = "pairing"` (a DM stranger needs
  owner approval), `groupPolicy = "disabled"` (the handshake does not work in
  group DMs).
- **`owner-only`**: `allowFrom` = `OPENCLAW_DISCORD_OWNER_IDS`, both policies
  `allowlist`. Without valid snowflakes config-init leaves authz as it is
  rather than lock everyone out.
- **`allowlist`**: config-init leaves these three keys alone.

Check the live values:

```bash
cat $OPENCLAW_CONFIG_DIR/openclaw.json | \
  jq '.channels.discord | {allowFrom, dmPolicy, groupPolicy}, .commands.allowFrom'
```

## Slash-command buckets

A slash command's availability depends on its bucket:

1. **Always on** (`commands.native` / `commands.text`): `/help`, `/commands`,
   `/status`, `/whoami`, `/tools`, `/tasks`, `/context`, `/usage`, `/model`,
   `/models`, `/think`, `/fast`, `/reasoning`, `/verbose`, `/queue`, `/steer`,
   `/skill <name>`, `/new`, `/reset`, `/stop`, `/compact`, `/export-session`,
   `/btw`, `/trace`, `/subagents`, `/kill`, `/send`, `/approve`,
   `/activation mention|always`, `/tts`, `/voice`, `/talkvoice`, `/dreaming`,
   `/pair`, `/restart`, plus bundled-plugin commands such as `/discord input:`.
2. **Owner only** (`commands.ownerAllowFrom` plus a `commands.<feature>` flag):
   `/config` (`commands.config`), `/mcp` (`commands.mcp`), `/plugins`
   (`commands.plugins`), `/debug` (`commands.debug`), `/diagnostics`,
   `/export-trajectory`. Config-init enables none of these flags, and it
   removes `commands.bash` (`/bash` and the `!cmd` shorthand) on every run.
3. **Discord-feature gated** (`channels.discord.<feature>.enabled`): `/vc
   join|leave|status` (`voice`), `/focus`, `/unfocus`, `/agents`,
   `/session idle|max-age` (`threadBindings`). Without the flag these commands
   do not even appear in Discord's autocomplete.

The list reflects the OpenClaw release current when it was written; `/commands`
on the live bot is authoritative.

### Voice

`OPENCLAW_DISCORD_VOICE` is `stt-tts` or `off`. With no `voice.realtime` block
the plugin runs batch STT and TTS: this stack's Whisper service and the Fish
TTS shim. The realtime modes need provider credentials the stack does not ship.
Fish TTS is optional (`compose/tts-fish.yml`, off on the reference host), so
`/vc` needs it added before the bot can speak. Details:
[`discord-voice-agent.md`](./discord-voice-agent.md).

### Thread bindings

Bindings are per thread. Create a thread, run `/focus <agent-or-target>`, and
follow-up messages in it go to the bound session. `/unfocus` releases it,
`/session idle 30m` releases it after inactivity, `/session max-age 4h` expires
it regardless, `/agents` lists bindings. Upstream unfocuses after 24 h idle;
raise `OPENCLAW_DISCORD_THREAD_IDLE_HOURS` for coding threads revisited the
next day.

## Mention gate vs `/activation`

Two separate layers that are easy to mix up.

**The gate (config).** Whether a guild message reaches the agent at all is
decided by `requireMention`: the channel entry first, then the guild entry, then
the `guilds["*"]` wildcard, and `true` when none is set. The plugin's guild
lookup tries the guild ID, then its slug, then `"*"`, so the wildcard covers
every guild without putting snowflakes in the repo. An operator's
`guilds.<id>` entry wins over the wildcard.

`OPENCLAW_DISCORD_REQUIRE_MENTION=on` (the default) writes
`guilds["*"].requireMention = true` on every run, because a drifted value is
invisible. With the gate closed, a message gets through when it @mentions the
bot (a real `<@ID>` mention from Discord's picker, not typed text) or replies to
one of the bot's messages; a reply counts as an implicit mention. `off` opens
the gate in every guild.

`OPENCLAW_DISCORD_ALWAYS_CHANNELS` lists channels the bot reads in full anyway.
Once a guild entry has a `channels` map, an unlisted channel resolves to
`allowed: false`, which silences it completely, mentions included. Config-init
therefore always writes a `channels["*"]` entry that mirrors the gate next to
the listed channels. `off` removes the map.

**The hint (`/activation`).** `/activation mention|always` stores the session's
group-activation mode, which only changes a line in the model's system intro:
`always` says the agent sees every group message and may stay silent;
`mention` says it is invoked only when mentioned. It cannot open a closed gate.
Upstream closed [#22172](https://github.com/openclaw/openclaw/issues/22172)
("/activation ignored") as working as designed.

So *"I ran `/activation always` and the bot still ignores unmentioned
messages"* is expected. Open the gate in config instead, for one channel
(`OPENCLAW_DISCORD_ALWAYS_CHANNELS`) or for all (`OPENCLAW_DISCORD_REQUIRE_MENTION=off`),
then recreate the config-init chain. Do not patch the plugin to make the slash
command flip the gate; that fork breaks on every image upgrade.

## Long-term memory in guild channels

OpenClaw keeps the root `MEMORY.md` out of group and channel sessions (a
privacy filter in the workspace bootstrap), and its trigger recall only reads
that tier. A guild bot therefore has no automatic long-term memory where it is
used most. The stack closes the gap in layers:

1. **People file.** `workspace-discord/people/MEMORY.md` is injected into every
   Discord session by the bundled `bootstrap-extra-files` hook
   (`plugins.mjs`). The privacy filter only matches the root path, so the file
   reaches channels, groups and DMs; cron and sub-agent sessions still drop it.
   It has one `## Name (<@ID>)` section per person. The bot records facts only
   through `node /opt/people-digest.mjs note --id <ID> --name <name> --text <fact> [--temp]`
   and `forget --id <ID> --match <part>`: when the model edited the file
   itself, a failed `edit` fell back to a `write` that replaced every section.
   The `people-context` plugin refuses direct edits of the file. Each command
   first heals the file from `~/.openclaw/people-digest/last-good.md` if a
   section or a permanent rule went missing. The file costs prompt tokens on
   every turn, so its header asks for under ~8000 characters (the nightly gate
   refuses anything past 12000). A member may also have one `- (stílus) …`
   line, written by the nightly job: how they talk and what tone works,
   described as a pattern, never as a verdict on their character.
2. **people-context plugin.** Being in the system prompt was not enough: asked
   "what am I doing now?" right after a task in the same channel, the model
   answered about the task. Its `before_prompt_build` hook prepends the
   sender's lines, and those of people tagged in the message, to the turn. The
   Discord plugin turns `<@ID>` into `@Name` before any hook sees the message,
   so for a message with an `@` the plugin reads its mention list from the
   Discord API (one GET with the bot token) and adds a
   `@Name = <@ID>` line, which lets the bot tag those people back. The gateway
   logs a "can't verify where this plugin came from" warning for any
   `load.paths` plugin; it still loads. Check it with
   `openclaw plugins inspect people-context --runtime --json`.
3. **Nightly people-memory job.** It learns from ordinary channel traffic.
   `/opt/people-digest.mjs` (baked into `openclaw-base-ext`) digests the new
   messages per author with per-channel cursors; `--guild all` reads every
   server the bot is in. It drops its own bot's lines and, unless
   `OPENCLAW_PEOPLE_DIGEST_NSFW=on`, NSFW channels. Other bots stay, marked
   `(bot)`, as context rather than facts. Voice-log lines in `*log` channels
   are folded into one activity summary per member (`PEOPLE_DIGEST_LOGS=off`
   skips those channels). The agent proposes `people/MEMORY.next.md`;
   `people-digest.mjs apply` swaps it in only when no person section vanished,
   no permanent-rule line was lost and the file did not shrink below 70 %.
   Backups land in `~/.openclaw/people-digest/history/`. The skill excludes
   sensitive categories (politics, religion, health, sexuality, finances).
   Create the job once per install:

   ```bash
   docker exec ${PROJ}openclaw-gateway openclaw cron add \
     --name people-memory-nightly --declaration-key people-memory-nightly \
     --agent discord-friend --cron '30 4 * * *' --tz Europe/Budapest --exact \
     --session isolated --light-context --tools exec,read,write --no-deliver \
     --timeout-seconds 3600 \
     --message 'Run the people-memory recipe: read skills/people-memory/SKILL.md and follow it step by step. GUILD_ID = <your guild id, or all for every server the bot is in>. Do not message anyone.'
   ```

   `--tools exec,read,write` keeps the job from posting anywhere. Collecting
   ~30 channels takes 4-5 minutes, because each `openclaw message read` call
   is a CLI round trip.
4. **Community summaries.** The people file holds facts about people, not what
   was talked about. Every 3 days a `community-summary` job writes one summary
   of all guilds' messages to `workspace-discord/memory/community/<from>_<to>.md`.
   The bge-m3 memory index picks it up, so "what did we talk about in August?"
   is answered through `memory_search` without growing any prompt. The collect
   step is windowed (`--last-days 3`, or `--from/--to` for a backfill), so the
   nightly cursors stay untouched. Create the job once:

   ```bash
   docker exec ${PROJ}openclaw-gateway openclaw cron add \
     --name community-summary --declaration-key community-summary \
     --agent discord-friend --cron '30 5 */3 * *' --tz Europe/Budapest --exact \
     --session isolated --light-context --tools exec,read,write --no-deliver \
     --timeout-seconds 3600 \
     --message 'Run the community-summary recipe: read skills/community-summary/SKILL.md and follow it step by step, with the default --last-days 3 window. Do not message anyone.'
   ```

   The skill always collects `--guild all` and keeps health, sexual and
   financial details out. For older history, run the recipe once per
   week-long window with `--from/--to`, one turn at a time, and rerun a turn
   that comes back with a timeout or error. `OPENCLAW_PEOPLE_DIGEST_EXCLUDE`
   keeps channels (for example a feed of game patch notes) out of both jobs.
5. **Channel history on demand.** "Who won the last /ruin?" is in no summary.
   The skill router points the bot at
   `node /opt/people-digest.mjs history --channel <id> [--author <name|id>] [--grep <text>] [--limit 20] [--days N] [--before <msg id>]`.
   It pages back through Discord REST (about 1 s per 100 messages; it falls
   back to `openclaw message read` when no bot token is readable), keeps embed
   text, appends the slash-command caller as `[/command — Name]`, prints local
   dates and always states the scanned range. Raw `message read` pages were
   120-200 KB, got truncated in the tool result, and the bot then made up a
   reason for not finding the message.
6. **Active Memory.** `OPENCLAW_ACTIVE_MEMORY=on` runs a recall sub-agent before
   every reply of `discord-friend` (`memory.mjs`: mode `always`, 30 s timeout,
   thinking off). The upstream `escalate` mode runs only on recall intent, and
   its intent detector has no Hungarian patterns, so it never fired.

Dreaming storage is `separate` (`memory.mjs`). The inline mode wrote sleep-phase
blocks into the daily notes, and on a group bot those blocks were session
metadata that outranked real notes in `memory_search`.

## DMs only from server members (dm-guild-gate)

`OPENCLAW_DISCORD_AUTHZ=open` sets `dmPolicy: "open"` so members can DM the bot
without a pairing round. That also lets a stranger who finds the bot's profile
talk to it. `dmPolicy` has four modes (`pairing`, `allowlist`, `open`,
`disabled`) and none means "people who share a server with me"; `allowlist`
would need every member's ID by hand.

The `dm-guild-gate` plugin decides per message. Its `before_dispatch` hook
sees `channel`, `isGroup` and `senderId`. For a Discord DM it asks the Discord
REST API with the bot token whether the sender is a member of any guild the bot
is in (`GET /guilds/{guild}/members/{user}`: 200 = member, 404 code 10007 = not
a member, 10013 = no such user; no privileged intent needed). A non-member's
turn ends with `{ handled: true }`: no model call, no reply, no reaction. It is
not a reply hook, so preview streaming stays on.

- Cache: members 6 h, non-members 10 min (someone who just joined gets in
  soon).
- If Discord gives no clear answer (no readable token, network error, rate
  limit), the DM goes through and the error is logged. An outage should not
  silence the bot for its own members.
- Plugin config (`plugins.entries.dm-guild-gate.config`, operator-managed,
  hot-reloaded): `guilds` limits which servers count, `allow` lists user IDs
  that may always DM, `block` lists user IDs that never get a DM reply.
- Log, structure only: `logs/dm-guild-gate.jsonl` with sender ID, outcome
  (`allow` / `block` / `error_allow`) and how it was decided.

Slash commands sent in a DM take the native command path, not
`before_dispatch`; the authz settings above govern those.

## Honesty checks (claim-guard)

Prompt rules did not stop the bot from saying it had done things it had not:
it told a member "I saved it to memory" in a turn with no write call, and when
it could not find an old message it invented a reason ("messages are deleted
after 30 seconds"). Word lists did not work either: a regex for claim words
missed "jegyeztem" on its first live test. The `claim-guard` plugin
(`openclaw-base-ext/plugins/claim-guard`) asks the model itself, through the
documented `api.runtime.llm.complete` plugin API, with thinking off:

1. `after_tool_call` records which tools succeeded in the run.
2. `before_agent_finalize` sends the final answer to two judge prompts in
   parallel (~2 s together on the reference host):
   - **Actions.** List every action the answer says was done, each with a
     type (`saved`, `searched`, `read`, `sent`, `reminder`, `ran_code`). The
     plugin code, not the model, then checks each type against the successful
     tools.
   - **Facts.** Rate each statement: about the world, the conversation or an
     opinion; specific or not; common knowledge or not; hedged or not. The code
     flags a specific, uncommon, unhedged world fact only when no search or
     fetch tool ran.

   Keep the two prompts separate. Merged into one, the fact rules leaked into
   the action verdicts (36-38 of 42 cases right instead of 42).
3. On a mismatch the hook returns `revise` with `maxAttempts: 1`: one more
   model pass that either does the action or the look-up, or says plainly that
   it was not done or that the fact is from memory.

It covers the Discord-routed agents (its `config.agents`, written by config-init
from the catch-all Discord binding) and skips Active Memory's recall runs, which run under the same agent
before the real turn. The judge's JSON is read with a balanced-brace parser,
because Nemotron sometimes closes one brace too many. Measured on 42 held-in and
14 held-out cases: all correct.

What it does not catch: a successful tool result that the bot then misreads
(it once merged a detail from a neighbouring line of a `history` result into
its answer). The tool ran, so the claim is backed.

Audit the log, which holds structure only (time, session, tools, verdict kind,
outcome, latency), never message text:

```bash
docker exec ${PROJ}openclaw-gateway sh -c \
  'tail -n 20 /home/node/.openclaw/logs/claim-guard.jsonl'
```

Outcomes: `none` (nothing to fix), `revise` (a second pass was requested),
`resolved` (the second pass fixed it), `unresolved` (still wrong after it),
`judge_failed` (judge timeout or unreadable JSON; the answer goes out
unchanged). `openclaw plugins reload` cannot swap the plugin while a run holds
the old instance; after changing it, rebuild `openclaw-base-ext` and recreate
the gateway.

The skill router backs this with principle-based rules, not trigger words:
look up a doubtful fact before stating it, never refuse a search, and claim
"saved" or "read" only after a successful tool call. At temperature 0.6 and 1.0
alike, 12 honesty traps came out 24 of 24 honest, so the lower temperature
(`LLM_TEMPERATURE`, see [`configuration.md`](./configuration.md#llm--embeddings))
is for clean Hungarian text, not for honesty.

## Heartbeat

The heartbeat runs every 30 minutes in an isolated session inside the
`OPENCLAW_HEARTBEAT_ACTIVE_START`/`_END` window and follows the scratch of its
`heartbeat:<agent>` automation (an empty scratch skips it); it ends with
`heartbeat_respond`, silent unless `notify=true`. claim-guard skips heartbeat runs. Its default `owner` target finds no
DM on Discord: the plugin reads the bare owner snowflake as a channel, and
every poll skips with `reason=no-route`. `gateway.mjs` therefore sets
`target: "discord"` and `to: "user:<id>"` on `agents.defaults.heartbeat`, with
the first ID in `OPENCLAW_DISCORD_COMMAND_OWNERS` unless `OPENCLAW_HEARTBEAT_TO`
names another. It sits on the defaults because a per-agent heartbeat block
turns the heartbeat off for every other agent. `OPENCLAW_HEARTBEAT_TO=off`
leaves delivery to OpenClaw.

## Related docs

- [`configuration.md`](./configuration.md): every knob and its default.
- [`discord-text-agent.md`](./discord-text-agent.md): text-channel agent.
- [`discord-voice-agent.md`](./discord-voice-agent.md): voice-channel agent.
- [`agentic-coding.md`](./agentic-coding.md): exec, approvals, long tasks.
- `config-init/steps/discord.mjs` and its siblings: the code, with the reason
  for each setting next to it.
- Upstream OpenClaw Discord docs: <https://docs.openclaw.ai/channels/discord>.
