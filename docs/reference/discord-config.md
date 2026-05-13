# Discord patcher overrides — operator configuration reference

This stack writes Discord-specific configuration into `openclaw.json` through
the idempotent `patch-config.mjs`. The defaults assume a single-operator
homelab deploy: open access, full tool surface, partial streaming. **None of
the overrides are mandatory** — every step is env-gated, and disabling a step
restores the upstream OpenClaw default.

Sibling docs that go deep on *how* the bot behaves, not just *what* the
patcher writes:

- [`discord-text-agent.md`](./discord-text-agent.md) — text-channel agent
  design (mention-pill, `tools.profile`, `message` tool, ackReaction cycle bug,
  progressive streaming UX, TTS opt-in).
- [`discord-voice-agent.md`](./discord-voice-agent.md) — voice-channel
  deployment, isolation design, threat model, DAVE encryption.

If you only want to know *which knob to flip to disable an override*, scroll
to the "At a glance" table below.

## At a glance — every override and its escape hatch

Each row maps one `patch-config.mjs` step to its `.env` knob and to the
upstream behaviour you get when you disable the step. **Setting the knob to
the empty string disables the step entirely** (the patcher leaves the field
unset, OpenClaw's docs default applies); some knobs also accept a value that
replicates upstream defaults without disabling the step.

| Step | What it writes | Env knob | Default | Disable / vanilla |
|---|---|---|---|---|
| 20 | `channels.discord.ackReactionScope = "off"` — defends against upstream auto-ack cycle bug ([#46024](https://github.com/openclaw/openclaw/issues/46024)). | `OPENCLAW_DISCORD_ACK_REACTION_SCOPE` | `off` | Set to `own` / `all` / `group-mentions` / `allowlist` to re-enable auto-ack. Vanilla upstream default is `off` too, so this row is belt-and-braces. |
| 21 | `channels.discord.actions.reactions = true` — keeps the reaction tool in the agent's catalog. Needs the local `vllm-llm` Gemma4 parser patch ([`vllm-llm/Dockerfile`](../../vllm-llm/Dockerfile)). | `OPENCLAW_DISCORD_ACTIONS_REACTIONS` | `true` | Set to `false` when running unpatched upstream `vllm/vllm-openai` (colon-namespace tool names get rejected by the parser; reactions break). |
| 22 | Discord-routed agent `tools.alsoAllow += ["browser", "tts", "canvas", "cron"]`. Belt-and-braces with step 25. | `OPENCLAW_DISCORD_AGENT_ALSO_ALLOW` | `browser,tts,canvas,cron` | Empty string disables the step. Stricter list (e.g. `cron`) for a narrower bot. |
| 24 | `channels.discord.streaming = "partial"` + optional `draftChunk` sub-knobs. Without this, a 500-token reply on Gemma 4 produces ~80s of silence. | `OPENCLAW_DISCORD_STREAMING` | `partial` | `off` to restore vanilla atomic delivery. `block` / `progress` for alternative cadences. Empty string skips the step. Detail: [`discord-text-agent.md` → Progressive streaming](./discord-text-agent.md#progressive-streaming-channelsdiscordstreaming). |
| 24 (sub) | `draftChunk.minChars` / `_MAX_CHARS` / `_BREAK_PREFERENCE`. | `OPENCLAW_DISCORD_DRAFTCHUNK_*` | unset (OpenClaw docs default: 200 / 800 / `paragraph`) | Each knob is independently optional. |
| 24 (sub) | `streaming.preview.toolProgress`. | `OPENCLAW_DISCORD_STREAMING_PREVIEW_TOOL_PROGRESS` | unset (upstream default `true`) | Set to `false` to suppress mid-stream tool-name lines (workaround for Discord italic-mangling of `__` names). |
| 25 | Discord-routed agent `tools.profile = "full"`. Without this the agent inherits the global `coding` default, which excludes `browser`, `tts`, `canvas`. | `OPENCLAW_DISCORD_AGENT_TOOLS_PROFILE` | `full` | Set to `coding` / `messaging` / `minimal` for a narrower bot. Empty string disables the step (operator picks). |
| 25c | Discord-routed agent reasoning preset. | `OPENCLAW_DISCORD_AGENT_THINKING` | `minimal` | Set to `off` to disable reasoning on Discord replies entirely. `low` / `medium` / `high` / `xhigh` for more. Empty string disables the step. |
| 26 | `<!-- patch-config:cron-tools:* -->` and `<!-- patch-config:browser-tools:* -->` cheatsheet blocks in `workspace-discord/AGENTS.md`. Doc-side fix because Gemma 4 doesn't surface tools from the catalog alone. | (no env knob) | always on when file exists | Delete the marked block by hand if you don't want it. The patcher won't rewrite it once removed inside the markers. |
| 27 | `<!-- patch-config:image-gen-tools:* -->` workflow-picker block in `workspace-discord/AGENTS.md`. Tells the agent which workflow to pass for SFW / adult / fast / etc. | `IMAGE_GEN_DEFAULT_WORKFLOW` (presence) | written iff env is set | Unset `IMAGE_GEN_DEFAULT_WORKFLOW` to skip. |
| 28 | `channels.discord.{allowFrom, dmPolicy, groupPolicy}` — opens guild access for the bot (homelab default). Without this, slash commands work in DM but fail in guild ([#19310](https://github.com/openclaw/openclaw/issues/19310)). | `OPENCLAW_DISCORD_AUTHZ` | `open` | `allowlist` to skip the step entirely and keep upstream's locked-down default. `owner-only` to lock to `OPENCLAW_DISCORD_OWNER_IDS` snowflakes. |
| 29 | `channels.discord.voice.enabled` + `channels.discord.threadBindings.enabled`. Without this, `/vc`, `/focus`, `/agents`, `/session` slash commands don't even register in Discord's autocomplete. | `OPENCLAW_DISCORD_VOICE` / `OPENCLAW_DISCORD_THREAD_BINDINGS` | `stt-tts` / `on` | Set either to `off` to disable that feature gate. |
| 30 | `channels.discord.guilds["*"].requireMention = false` — opens the message gate for every guild the bot joins. Without this, the bot only replies to explicit mentions. | `OPENCLAW_DISCORD_REQUIRE_MENTION` | `off` | Set to `on` to restore upstream mention-required default. See "Discord mention gate vs `/activation` slash" below. |

`patch-config.mjs`'s top docblock has the full per-step rationale; the env
knobs in `.env.example` have a one-paragraph trade-off comment each. Anything
this stack writes goes through user-managed protection: if you hand-set the
field in `openclaw.json`, the patcher leaves it alone.

## How much do we override upstream?

Categorised by *why*, so you can decide which to keep and which to revert:

- **Workarounds for upstream bugs** — steps 20 (reaction cycle bug #46024),
  21 (Gemma 4 colon-namespace parser, see `vllm-llm/Dockerfile`). Keep these
  unless you're running unpatched upstream images.
- **UX improvements that suit slow LLMs** — step 24 (progressive streaming).
  Without it, a 6 tok/s backend produces ~80s of Discord silence per reply.
  Keep it on Gemma 4 / dense backends; consider `off` on cloud-fast backends.
- **Capability widening for the Discord-routed agent** — steps 22 + 25.
  Vanilla upstream gives the agent the `coding` profile, which excludes
  `browser`, `tts`, `canvas`. Disable both if you want a narrower bot.
- **Cheatsheet docs that compensate for Gemma 4's tool-discovery weakness** —
  steps 26 + 27. Doc-side patches, not behavioural overrides.
- **Policy choices that reflect single-operator homelab posture** — steps 28,
  29, 30. These open access in ways that wouldn't fit a shared / multi-tenant
  / public deploy. Revert with `OPENCLAW_DISCORD_AUTHZ=allowlist`,
  `OPENCLAW_DISCORD_REQUIRE_MENTION=on`, `OPENCLAW_DISCORD_VOICE=off`,
  `OPENCLAW_DISCORD_THREAD_BINDINGS=off`.

## Returning to a fully vanilla Discord deployment

If you want every Discord-related override disabled and OpenClaw's upstream
defaults to apply, set in `.env`:

```bash
OPENCLAW_DISCORD_ACK_REACTION_SCOPE=          # both stack and upstream default to off
OPENCLAW_DISCORD_ACTIONS_REACTIONS=           # upstream default applies
OPENCLAW_DISCORD_AGENT_ALSO_ALLOW=            # empty — disables step 22
OPENCLAW_DISCORD_STREAMING=off                # restore atomic delivery
OPENCLAW_DISCORD_DRAFTCHUNK_MIN_CHARS=
OPENCLAW_DISCORD_DRAFTCHUNK_MAX_CHARS=
OPENCLAW_DISCORD_DRAFTCHUNK_BREAK_PREFERENCE=
OPENCLAW_DISCORD_STREAMING_PREVIEW_TOOL_PROGRESS=
OPENCLAW_DISCORD_AGENT_TOOLS_PROFILE=         # empty — disables step 25
OPENCLAW_DISCORD_AGENT_THINKING=              # empty — disables step 25c
OPENCLAW_DISCORD_AUTHZ=allowlist              # skip step 28, keep upstream lockdown
OPENCLAW_DISCORD_REQUIRE_MENTION=on           # skip step 30, keep mention gate
OPENCLAW_DISCORD_VOICE=off
OPENCLAW_DISCORD_THREAD_BINDINGS=off
```

Then force-recreate:

```bash
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

You'll now have a Discord deployment that behaves exactly like an out-of-the-
box `openclaw channels add --channel discord …` install — except for the
cheatsheet blocks in `workspace-discord/AGENTS.md` (delete those by hand if
they bother you) and the `vllm-llm` reaction-parser fix (which sits in the
image layer, not in `openclaw.json`).

## Discord slash-command authorization (issue #19310 dual perm check)

OpenClaw's Discord channel runs slash commands through a **dual perm check**
that is hostile to the default config:

1. Global `channels.discord.allowFrom` allowlist, AND
2. Per-guild `channels.discord.guilds.<gid>.users` array.

Both must match. The default `dmPolicy: "pairing"` implicitly satisfies the
first for DM contexts after the user pairs once, but guild contexts have no
equivalent fallback — the `groupPolicy: "allowlist"` default plus an empty
`users` array silently blocks every slash invocation. Discord renders the
gateway's rejection as an ephemeral *"You are not authorized to use this
command"* visible only to the invoker, so the operator never sees a
server-side log line they can grep for.

Symptom: `/discord input: hello`, `/talkvoice input: hello`, `/activation
mode: always` work in DM, fail in guild. Confirmed in upstream [issue
#19310](https://github.com/openclaw/openclaw/issues/19310) (*"[Bug] Discord
Slash Commands Require Owner Configuration in Channels Despite Pairing"*);
upstream's stance is *"operator must hand-edit allowFrom + per-channel
users"*, no CLI shortcut.

The native slash UX is materially better than `@`-mention text on this stack
— Discord renders an immediate ack-dot *"thinking…"* indicator the moment the
interaction is received, so the user never sees the dead-air gap that text-
mention paths suffer from while the agent prefills (~1-5s) and generates
(6-50 tok/s depending on backend). Operators want slash on every channel,
not only DM.

**Patcher step 28** fixes this by writing open-guild defaults: `allowFrom =
["*"]`, `dmPolicy = "open"`, `groupPolicy = "open"`. Each field is
user-managed-protected (only written when undefined), so hand-set operator
values survive.

`OPENCLAW_DISCORD_AUTHZ` accepts three modes:

- **`open` (default)** — write the open-guild defaults above. Right for
  single-operator homelab where the guild member list is the trusted
  population.
- **`allowlist`** — skip the step entirely, preserve upstream defaults. Right
  for shared / multi-tenant / public guild deploys where you genuinely want
  per-user gating.
- **`owner-only`** — lock to `OPENCLAW_DISCORD_OWNER_IDS` snowflakes. Writes
  `allowFrom = [<ids>]` and both policies to `allowlist`.

When debugging a *"slash works in DM, fails in guild"* report, check the
live config:

```bash
cat $OPENCLAW_CONFIG_DIR/openclaw.json | \
  jq '.channels.discord | {allowFrom, dmPolicy, groupPolicy}'
```

If any of those fields is undefined, step 28 either didn't run
(`OPENCLAW_DISCORD_AUTHZ=allowlist`) or pre-existed in `openclaw.json`
(user-managed protection respected an explicit value).

## Discord slash-command matrix and the two feature gates

OpenClaw splits its Discord slash surface across three buckets, and a slash
command's availability depends on which bucket it falls in:

1. **Always-on** (gated by `commands.native` / `commands.text` globally —
   both default to enabled on this stack): `/help`, `/commands`, `/status`,
   `/whoami` (alias `/id`), `/tools`, `/tasks`, `/context`, `/usage`,
   `/model`, `/models`, `/think`, `/fast`, `/reasoning`, `/verbose`,
   `/queue`, `/steer`, `/skill <name>`, `/new`, `/reset`, `/stop`,
   `/compact`, `/export-session`, `/btw`, `/trace`, `/dock-discord`,
   `/dock-slack`, `/dock-telegram`, `/dock-mattermost`, `/subagents`,
   `/acp`, `/kill`, `/send`, `/approve`, `/activation mention|always`,
   `/tts`, `/voice`, `/talkvoice`, `/dreaming`, `/pair`, `/restart`. Plus
   the bundled-plugin natives `/discord input:` and `/codex …`.

2. **Owner-only** (gated by `commands.<feature>` flags and
   `commands.ownerAllowFrom` snowflake list): `/config show|get|set|unset`
   (needs `commands.config: true`), `/mcp ...` (`commands.mcp: true`),
   `/plugins inspect|enable|disable` (`commands.plugins: true`), `/debug
   show|set|unset` (`commands.debug: true`), `/bash <cmd>` plus `!cmd`
   shorthand (`commands.bash: true`), `/diagnostics`,
   `/export-trajectory`. On this stack `commands.bash: true` is on for the
   owner snowflake (the `!~/.openclaw/bin/img` bypass for image-gen — see
   [`img-bash-command.md`](./img-bash-command.md)); the others stay off
   until you explicitly enable them.

3. **Discord-feature-gated** (gated by `channels.discord.<feature>.enabled`):
   `/vc join|leave|status` (needs `channels.discord.voice.enabled: true`);
   `/focus`, `/unfocus`, `/agents`, `/session idle <duration|off>`,
   `/session max-age <duration|off>` (need
   `channels.discord.threadBindings.enabled: true`). **Patcher step 29
   enables both by default** — without it these slash commands don't even
   register in Discord's autocomplete.

### Voice modes

`OPENCLAW_DISCORD_VOICE` accepts `stt-tts` (default), `agent-proxy`, `bidi`,
or `off`. **`stt-tts` is the only mode that works on this stack out of the
box** because the realtime alternatives need an OpenAI Realtime or
equivalent provider that the bundle doesn't ship. `stt-tts` chains the
self-hosted faster-whisper (port 8093) for STT and the openclaw-tts-router
(port 8090) for TTS — both already configured by patcher steps 11 and 14.
When a user runs `/vc join` in a Discord voice channel, the bot connects
with Connect + Speak permissions and runs the loop:
hear → Whisper → agent → Kokoro/F5-TTS → speak. Higher latency than realtime
but fully offline. Detail in [`discord-voice-agent.md`](./discord-voice-agent.md).

### Thread bindings

Thread bindings are opt-in per-thread, not automatic per agent. After step 29
enables them, the operator creates a thread in a guild channel, types
`/focus <agent-or-target>`, and from that point follow-up messages in the
thread route to the bound session. `/unfocus` releases the binding;
`/session idle 30m` auto-releases after inactivity; `/session max-age 4h`
hard-expires regardless. `/agents` shows current bindings. Useful when you
want one Discord channel to host multiple parallel agent conversations
(e.g. a research session in thread A, a coding session in thread B).

## Discord mention gate (config) vs `/activation` slash (LLM behavior hint)

Two independent layers in the upstream Discord plugin that are easy to
conflate. They look like the same thing from the operator's point of view
but they're not, and getting them mixed up leads to misdiagnosing slash-
command behaviour as a bug.

### Layer 1 — the gate

Whether a guild message reaches the agent at all is decided by
`resolveDiscordShouldRequireMention` in the bundled
`extensions/discord/allow-list-CuKLSnAf.js`:

```js
function resolveDiscordShouldRequireMention(params) {
  if (!params.isGuildMessage) return false;
  if (params.isAutoThreadOwnedByBot ?? isDiscordAutoThreadOwnedByBot(params)) return false;
  return params.channelConfig?.requireMention ?? params.guildInfo?.requireMention ?? true;
}
```

Pure config-driven: `channels.discord.guilds.<id>.requireMention` (or the
wildcard `"*"` entry, see below). There is no session-store input, no
slash-command input. Whoever the operator is, this is the operator's gate.

### Layer 2 — the LLM behavior hint

`/activation mention|always` writes `sessionEntry.groupActivation = mode`
via `handleActivationCommand` in `commands-handlers.runtime-DfQhZZft.js`.
That value is consumed by `buildGroupIntro` in `get-reply-CwuPJWAe.js` as
an LLM-facing system-intro line:

- `always` → *"Activation: always-on (you receive every group message)."*
  plus a silent-token instruction so the LLM can opt out of replying to
  messages that aren't for it.
- `mention` → *"Activation: trigger-only (you are invoked only when
  explicitly mentioned; recent context may be included)."* No silent-token
  instruction because in `mention` mode the gate is supposed to be doing
  the filtering, so the LLM never sees non-mention messages in the first
  place.

So the slash assumes the operator has set the gate appropriately and tells
the LLM how to behave within that gate. **It is not a runtime gate toggle
and was never designed to be one** — upstream [issue
#22172](https://github.com/openclaw/openclaw/issues/22172) (*"/activation
ignored"*) was closed as "not planned" because the slash is working as
designed; the reporter assumed it controlled the gate.

### Wildcard `"*"` in `guilds` is the public-repo-safe gate setting

The bundled `resolveDiscordGuildEntry` tries id-match → slug-match →
`entries["*"]` fallback, so writing
`channels.discord.guilds["*"].requireMention = false` opens the gate for
every guild the bot joins without committing any snowflakes. Per-guild
overrides still win — an explicit
`channels.discord.guilds.<id>.requireMention = true` set by the operator
beats the wildcard, so you can keep the open default and selectively
silence a noisy guild.

**Patcher step 30** writes the wildcard by default.
`OPENCLAW_DISCORD_REQUIRE_MENTION`:

- **`off` (default)** — write `guilds["*"].requireMention = false`. Bot's
  gate is open in every guild. LLM gets the `always`-mode intro line
  whenever the operator invokes `/activation always` (or by default on the
  first turn after the system-intro flag is set), with the silent-token
  instruction so it can be selective. Matches the rest of this stack's
  wide-open homelab defaults (open-guild authz, voice + threadBindings on,
  slash UX everywhere).
- **`on`** — skip the step, preserve upstream mention-required default.
  Gate stays closed; only mention/reply messages reach the agent;
  `/activation mention` is then consistent with what the gate actually
  does. Use on shared / multi-tenant / public deploys.

The env value IS the desired `requireMention` posture. User-managed
protection: the patcher only writes when `guilds["*"].requireMention` is
undefined; if you hand-set the wildcard entry (or any specific guild
entry), the operator value survives.

### Debugging the most common confusion

When a user reports *"I set `/activation mention` but the bot still replies
to everything,"* that's not a bug. They have the gate open (step 30
default) and the slash is purely the LLM behavior hint. Either close the
gate at the config level:

```bash
# Persistent — survives gateway restart
echo 'OPENCLAW_DISCORD_REQUIRE_MENTION=on' >> .env
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli

# OR hot-reload
docker exec <PROJ>openclaw-cli openclaw config set \
  channels.discord.guilds.\"*\".requireMention true
```

Or accept that with the gate open the bot will see, and may decide to
reply to, any message the LLM finds worth replying to.

Don't try to "fix" this by patching the bundled plugin to make the slash
flip the gate — that fork would break on every image upgrade, and the
upstream design intentionally separates operator config from in-session
behavior hints.

## Related docs

- [`discord-text-agent.md`](./discord-text-agent.md) — text-channel agent
  design (mention pill, `tools.profile`, `message` tool, ackReaction cycle,
  progressive streaming, TTS opt-in).
- [`discord-voice-agent.md`](./discord-voice-agent.md) — voice-channel
  deployment, isolation, threat model.
- [`img-bash-command.md`](./img-bash-command.md) — the `!~/.openclaw/bin/img`
  Discord bash bypass for image-gen.
- [`chat-surface-capability-matrix.md`](./chat-surface-capability-matrix.md)
  — what renders where (Discord text vs voice vs web chat).
- `patch-config.mjs` (top docblock) — numbered rationale for every step,
  including the 11 Discord-related ones (20, 21, 22, 24, 25, 25c, 26, 27,
  28, 29, 30).
- Upstream OpenClaw Discord docs (authoritative):
  <https://docs.openclaw.ai/channels/discord>.
