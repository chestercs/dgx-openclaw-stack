# Discord text-channel agent

How the bot works in a guild text channel. Settings and knobs:
[`discord-config.md`](./discord-config.md) and
[`configuration.md`](./configuration.md#discord-channel). Voice channels:
[`discord-voice-agent.md`](./discord-voice-agent.md).

On the reference host the bot runs Nemotron 3.5 Super 120B-A12B NVFP4 on
`vllm-llm` (medium-effort thinking, temperature 0.6 for clean Hungarian text);
see [`llm-stack.md`](./llm-stack.md). The agent is `discord-friend`, created by
the channel onboarding; config-init sets its tools, thinking and prompt text.

## Flow

```
User in a guild channel: "@Bot what's new in the patch notes?"
   │
   ▼ Discord gateway (WebSocket)
   │
openclaw-gateway, Discord plugin
   │  mention gate: guilds["*"].requireMention (an @mention or a reply to the bot)
   │  groupPolicy / dmPolicy; for DMs the dm-guild-gate plugin (server members only)
   │  queue: byChannel.discord = followup (one run per message, in order)
   │
   ▼ route: catch-all Discord binding → discord-friend
   │
discord-friend agent (workspace ~/.openclaw/workspace-discord/)
   │  bootstrap: AGENTS.md (marker blocks + skill router), people/MEMORY.md
   │  Active Memory: recall sub-agent before every reply (mode always, 30 s)
   │  before_prompt_build: people-context adds "@Name = <@ID>" for tagged people
   │    and the sender's people-file lines
   │  tool catalog: profile full + alsoAllow − deny (incl. media tools that are off)
   │
   ▼ tool calls (web_search, memory_search, message, browser, exec people-digest.mjs, …)
   │
vllm-llm (Nemotron) + tool runtime
   │
   ▼ final answer
   │  before_agent_finalize: claim-guard checks claimed actions against the
   │    run's tool calls and flags uncommon facts never looked up; one
   │    more pass on a mismatch
   │
openclaw-gateway → Discord plugin → partial streaming into one message
```

No plugin registers a reply hook (`message_sending` / `reply_payload_sending`),
so Discord preview streaming stays on. The memory layers and the honesty
checks are described in
[`discord-config.md` → Long-term memory](./discord-config.md#long-term-memory-in-guild-channels)
and [→ Honesty checks](./discord-config.md#honesty-checks-claim-guard).

## Mentions

With the gate closed (the default), a guild message reaches the agent only when
it mentions the bot or replies to one of its messages. Everything else is
dropped with `reason: "no-mention"` in the gateway log.

A mention means the message carries Discord's mention markup (`<@BOT_ID>`),
which only happens when the user picks the bot from Discord's autocomplete
picker. Plain typed text `@BotName` is just a string. This matters for browser
automation tests: a keyboard `type` of `@BotName` does not open the picker. Type
the first letters, press Tab, then type the rest.

The plugin turns `<@ID>` into `@Name` before the model sees the message, so the
model cannot tag a person back on its own. The `people-context` plugin reads
the message's mention list from the Discord API and adds a
`@Name = <@ID>` line to the turn; the format rules tell the bot to tag with the
`<@ID>` form. For outbound `@handle` text without an ID,
`OPENCLAW_DISCORD_MENTION_ALIASES` gives the plugin a fixed map.

## Tool catalog

OpenClaw filters an agent's tools in layers: the profile (`full`, `coding`,
`messaging`, `minimal`), then allow and deny lists, then provider policy.
Without an explicit profile the Discord agent inherits `coding`, which has no
`group:messaging` (the `message` tool), `browser`, `tts` or `canvas`. The bot
then answers a reaction request with "I can't use the tool `message` here".

`config-init/steps/agents.mjs` sets profile `full`, adds
`group:messaging,browser,tts,canvas,cron` to `alsoAllow` (on guild channels
`cron` drops out even under `full`) and denies the operator and desktop tools a
chat bot never needs. `full` plus the bundled plugins is over a hundred tools,
and a model picks badly from a catalog that size. Media groups listed in
`OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` (image, video and music by default, because
ComfyUI does not fit next to Nemotron) are denied too, and the skill router says
they are off, so the bot tells the user instead of calling a dead tool.

Measure the real catalog through the gateway's `tools.effective` call (with
`openclaw gateway call`) or on a real Discord turn, not with `openclaw agent`:
owner-gated tools are missing from CLI runs.

## Reactions: the `message` tool

The reaction tool is the `message` tool with an `action`, not a
`discord:add_reaction` tool:

```json
{ "tool": "message", "action": "react", "messageId": "<target-message-id>", "emoji": "✅" }
```

Other actions on the same tool include `send`, `read`, `edit`, `delete`, `pin`,
`unpin`, `threadReply` and `emojiList`. `emoji: ""` removes all of the bot's
reactions on a message; `remove: true` with an emoji removes just that one.
Source: [`docs.openclaw.ai/tools/reactions`](https://docs.openclaw.ai/tools/reactions).

Auto-ack reactions (`ackReactionScope`) stay off. A stale delivery queue
replays reaction events on session resume and the bot cycles emojis on a
message ([#46024](https://github.com/openclaw/openclaw/issues/46024)); and when
the agent decides not to reply, an auto-ack stays stuck on the message
([#30585](https://github.com/openclaw/openclaw/issues/30585)). Status reactions
(`messages.statusReactions`) show run state instead and are driven by the run
lifecycle, not by that queue. The agent still reacts on purpose when it wants
to.

## Progressive streaming

Upstream posts a reply only when it is complete. Nemotron decodes Hungarian at
~17 tok/s and English at ~26 tok/s on the GB10, after a prefill of the ~29 KB
AGENTS.md and the turn, so a long answer posted at once is a minute of silence
that reads as a frozen bot.

`OPENCLAW_DISCORD_STREAMING=partial` (the default) posts one preview message and
edits it as tokens arrive. Modes:

| Mode | Behaviour |
|---|---|
| `off` | Only the final reply is posted (upstream default). |
| `partial` | One preview message, edited in place. Stack default. |
| `block` | Paragraph-sized chunks as separate messages. |
| `progress` | Discord alias of `partial`. |

Discord allows 5 message edits per 5 s per channel. The upstream chunking
(about 200 characters per edit) at this decode speed stays under that. A much
faster backend, or several gateways on one bot token, can exhaust the edit
budget; use `block` or `off` there.

Caveats (from the [upstream Discord docs](https://docs.openclaw.ai/channels/discord)):

- Media, error and explicit-reply finals cancel the pending preview edit and
  arrive as a fresh post.
- Streaming is text only. Attachments use the normal delivery path.

`OPENCLAW_DISCORD_STREAMING_PREVIEW_TOOL_PROGRESS` controls the "Working... tool:
x" lines in the preview. Discord's markdown turns double underscores in tool
names (`comfyui_image__generate`) into italics; there is no upstream option to
escape them, only this on/off switch.

Long answers are split at 2000 characters or `maxLinesPerMessage`;
`OPENCLAW_DISCORD_MAX_LINES=60` lets the character limit decide, so a code
answer is not cut into six messages. The tool-policy block asks for anything
over ~40 lines of code as an attached file (`MEDIA:` line) instead.

Check the live setting:

```bash
cat $OPENCLAW_CONFIG_DIR/openclaw.json | jq '.channels.discord.streaming'
```

## Prompt text

`config-init/steps/workspaces.mjs` keeps marker blocks in
`workspace-discord/AGENTS.md` up to date (format rules, honesty, sender
identity, tool policy, thread tasks, sub-agent delegation, the skill router)
and writes the skills the router points to. Text outside the markers is the
operator's. The model sees only the router lines at decision time, so the core
of a rule belongs in the router line, not only in the skill body.

AGENTS.md is rendered when a session is activated, so a config-init change
usually reaches the bot without a reset. If an edit does not show up in a busy
channel, `/new` starts a fresh session.

## TTS replies (optional)

TTS is off on the reference host: Fish Audio S2 Pro (`compose/tts-fish.yml`)
does not fit next to Nemotron. With it added, `OPENCLAW_TTS_AUTO` decides when a
reply gets audio. On text channels use `tagged`: with `always` every reply gets
an attachment, and a failed transcode drops the text too.

With `tagged` the model opts in per reply with a directive at the start of the
final text. The parser matches `[[tts:...]]` (double brackets); `[tts]` is
just visible text. `[[tts:speak]]` is the simplest form: no voice override, the
directive is stripped, and the rest of the reply is shown and spoken. The
stack's prompt text has no TTS section, so add one outside the markers in
`workspace-discord/AGENTS.md`, for example: emit `[[tts:speak]]` only when the
user asks for a spoken answer.

Check: a plain question gets no audio; "say it out loud: 2+2" gets an audio
attachment with no directive visible in the text.

```bash
cat $OPENCLAW_CONFIG_DIR/openclaw.json | jq '.tts.auto'
```

TTS service details: `openclaw-tts-fish/README.md`.

## Checklist for a new deployment

```bash
# 1. Plugin status: running, connected.
docker exec ${PROJ}openclaw-cli openclaw channels status --deep | grep -i discord

# 2. Config-init log: what it wrote for Discord.
docker logs ${PROJ}openclaw-config-init 2>&1 | grep -i discord

# 3. Reaction test, with a real mention (picker, Tab):
#    "@Bot put a ✅ reaction on this message, no text reply"
#    Expected: ✅ under the message, no text.

# 4. Search test: "@Bot who won the last Formula 1 race?"
#    Expected: a web_search call (claim-guard log shows the tool) and a source.
docker exec ${PROJ}openclaw-gateway sh -c 'tail -n 5 /home/node/.openclaw/logs/claim-guard.jsonl'
```

## Related docs

- [`discord-config.md`](./discord-config.md): every Discord setting and its module.
- [`discord-voice-agent.md`](./discord-voice-agent.md): voice channels.
- [`agentic-coding.md`](./agentic-coding.md): exec, approvals, thread-per-task.
- `openclaw-image-comfyui/README.md`: image, video and music tools (optional,
  off on the reference host).
