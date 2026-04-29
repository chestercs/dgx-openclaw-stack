# Discord text-channel agent — internals + design notes

Reference for deploying an OpenClaw agent in a Discord guild text channel. Sibling doc: [`discord-voice-agent.md`](./discord-voice-agent.md) for voice-channel deployments.

Most of the gotchas in this doc come from a real friend-group deployment on 2026-04-27 / 2026-04-28. The patcher steps that defend against each of them ship as a numbered list in `patch-config.mjs`'s top docblock — search for `// 20.`, `// 21.`, `// 22.` to find their inline rationale.

**Operator quick-start:** copy [`templates/discord-text-agent/AGENTS.md.example`](../../templates/discord-text-agent/AGENTS.md.example) to your `<workspace-discord>/AGENTS.md` (typically `~/.openclaw/workspace-discord/AGENTS.md` on the gateway host). The template contains the validated patterns: ack-react-then-text response form, image-gen prompt-only contract, `message`-tool reactions, web-search source-link convention.

## Flow

```
User in Discord channel: "@ImbulClaw generate a giraffe in a hat"
   │
   ▼ Discord gateway (WebSocket)
   │
openclaw-gateway (Discord plugin, channel listener)
   │  preflightDiscordMessage(channel, message)
   │  ├─ requireMention check (skips messages without proper bot mention)
   │  ├─ groupPolicy gate (open / allowlist)
   │  └─ ackReactionScope ack-emoji emit (defaults `off` — see step 20)
   │
   ▼  agent dispatch — route bound to channel: "discord"
   │
openclaw-gateway → discord-friend agent
   │  isolated workspace at ~/.openclaw/workspace-discord/
   │  reads AGENTS.md, IDENTITY.md, SOUL.md, USER.md, TOOLS.md once at session start
   │  tool catalog filtered by:
   │    1. tools.profile (default "coding" — set in patcher step 8)
   │    2. tools.deny (global)
   │    3. agents.list[].tools.alsoAllow (per-agent boost — patcher step 22 adds "group:messaging")
   │
   ▼ tool calls (message, image_generate, web_search, memory_*, react, …)
   │
vllm-llm (Gemma 4 31B NVFP4) + tool runtime
   │
   ▼ tool results → final assistant text + reactions
   │
openclaw-gateway → Discord plugin → channel post
```

## The four things that bit us

### 1. Mention-pill vs plain `@` text

The Discord plugin's `discord-auto-reply` module preflight-checks every inbound channel message with `preflightDiscordMessage()`. If the bot isn't properly mentioned, the message is dropped with `reason: "no-mention"`:

```json
{
  "module": "discord-auto-reply",
  "channelId": "1426994992248782920",
  "reason": "no-mention",
  "msg": "discord: skipping guild message"
}
```

A "proper mention" means the message contains a Discord mention markup token (`<@1498350417074196621>`), which only happens when the human user types `@<prefix>` and selects from Discord's autocomplete picker (Tab or click). Plain text `@ImbulClaw` written without picker confirmation is just a string — Discord doesn't convert it.

This bit us during Chrome MCP automated testing, where the keyboard `type` action wrote `@ImbulClaw` without invoking the picker. The fix is `type "@Imbu"` → `key "Tab"` → `type rest`. Real human users typing in Discord don't hit this because the picker pops up automatically.

### 2. `tools.profile: "coding"` excludes `group:messaging`

OpenClaw filters each agent's tool catalog through three layers (in order):

1. **Base profile** — `tools.profile` sets the allowlist. Documented profiles:
   - `"full"` — unrestricted (everything).
   - `"coding"` — `group:fs, group:runtime, group:web, group:sessions, group:memory, cron, image, image_generate, music_generate, video_generate`. **Does NOT include `group:messaging`.**
   - `"messaging"` — `group:messaging, sessions_list, sessions_history, sessions_send, session_status`.
   - `"minimal"` — `session_status` only.
2. **Allow/deny lists** — `tools.allow` / `tools.deny` narrow further.
3. **Provider-specific policy** — per-LLM-provider filtering on top.

Patcher step 8 sets the global default to `"coding"` (sandbox-safe for code-edit agents). The Discord-routed agent inherits this — and so it CANNOT call the `message` tool, which is the tool agents use for reactions, replies, edits, deletes, etc.

Symptom from a live test:

```
ChesTeR: @ImbulClaw tegyel egy ✅ reactiont erre az uzenetre
ImbulClaw: I can't use the tool "message" here because it isn't available.
           I need to stop retrying it and answer without that tool.
```

**Fix — patcher step 22:** locate the agent in `agents.list[]` whose route matches `channel: "discord"`, and ensure its `tools.alsoAllow` array contains `group:messaging`. Env override: `OPENCLAW_DISCORD_AGENT_ALSO_ALLOW` (comma-separated, default `group:messaging`). Set to empty string to disable the step.

This adds `group:messaging` WITHOUT switching the profile away from `coding` — so the agent keeps `image_generate`, `exec`, `read`/`write`, `sessions_*`, `cron`, etc.

### 3. The `message` tool is the reaction tool (NOT `discord:add_reaction`)

Older OpenClaw versions (and some operator-managed `AGENTS.md` files) refer to the reaction tool as `discord:add_reaction`. That namespace is deprecated. The current canonical is the `message` tool with an `action` parameter:

```json
{
  "tool": "message",
  "action": "react",
  "messageId": "<target-message-id>",
  "emoji": "✅"
}
```

Other actions on the same tool: `send`, `read`, `edit`, `delete`, `pin`, `unpin`, `threadReply`, `react`, `emojiList`. Cross-channel: works identically for Discord, Slack, Google Chat.

To remove a reaction: `emoji: ""` removes ALL of the bot's reactions on the message; `remove: true` + a specific emoji removes just that one.

Source: [`docs.openclaw.ai/tools/reactions`](https://docs.openclaw.ai/tools/reactions).

### 4. Auto-ack reactions and the cycle bug (issue #46024)

OpenClaw has an upstream auto-ack feature: `messages.ackReaction` (emoji) + `messages.ackReactionScope` (`off`/`own`/`all`/`group-mentions`/`allowlist`) automatically reacts to inbound messages BEFORE the agent generates its text reply. Configurable per-channel via `channels.discord.ackReaction*`.

But: there's a [known bug (issue #46024)](https://github.com/openclaw/openclaw/issues/46024) where the delivery queue replays stale reaction events on session resume — bot rapidly cycles 👀🤔👍🔥 across the user's mention without the agent having any tool-call awareness. Closed upstream but the fix-version is not documented in the public release notes (we checked 2026.4.15 through 2026.4.25). Default `off` defends against this.

Plus: [issue #30585](https://github.com/openclaw/openclaw/issues/30585) — when the agent decides NOT to reply (NO_REPLY status), the auto-ack stays stuck on the message permanently because `removeAckAfterReply` doesn't fire. The cleanup path is missing for this edge case.

**Stack default:** `channels.discord.ackReactionScope: "off"` (patcher step 20). Defends against both bugs.

**Operator-driven option (recommended):** instruct the agent in `AGENTS.md` to call `message` with `action: "react"` and `emoji: "✅"` BEFORE it generates the text reply. This is agent-driven, predictable, only fires when the agent decides to (so no cycle from stale-queue replay). The agent emits one extra tool call per turn, which costs ~1-3s of LLM latency.

```markdown
## ELSŐDLEGES VÁLASZFORMA

Minden mention-re ebben a sorrendben:
1. ELŐSZÖR ack-reactiont teszel ✅-vel a user mention-üzenetére.
2. AZTÁN szövegesen válaszolsz.
```

The agent-driven path doesn't hit the cycle bug (the cycle is in the upstream queue replay logic, not in agent-emitted tool calls) and doesn't hit the NO_REPLY-stuck bug (the agent just doesn't react in that case).

If you want to enable the upstream auto-ack instead, set `OPENCLAW_DISCORD_ACK_REACTION_SCOPE` env to a non-off value (`group-mentions` is the most narrow / lowest-risk; `all` would auto-react to every channel message the bot is allowed to see). Pair with `messages.ackReaction = "<emoji>"` and `messages.removeAckAfterReply = true`. Watch the gateway log for `[discord-auto-reply]` rapid-fire events and revert to `off` if the cycle returns.

## groupPolicy and dmPolicy

`channels.discord.groupPolicy` — per-channel access for messages in guild channels:
- `"open"` — bot processes mentions in any channel it has read access to. Default for our deploy.
- `"allowlist"` — only specific channels. List in `channels.discord.allowedChannelIds[]`.

`channels.discord.dmPolicy` — DM access:
- `"open"` — accept DMs from anyone.
- `"allowlist"` — only specific user IDs.

For a friend-group guild, `groupPolicy: "open"` + the channel restricted by Discord channel-permissions is usually fine. For a public-server bot, `allowlist` with explicit channel IDs is the better posture.

## Progressive streaming (`channels.discord.streaming`)

The OpenClaw upstream default `channels.discord.streaming = "off"` posts replies atomically — the channel sees nothing until the agent emits its final assistant text. With this stack's reference backend (Gemma 4 31B NVFP4 on a single GB10 at ~6 tok/s), a typical 500-token answer takes ~80 seconds to materialise; that's ~80 seconds of dead silence in Discord, and users invariably ask "is the bot stuck?".

**Patcher step 24** writes `channels.discord.streaming = "partial"` by default (env-tunable via `OPENCLAW_DISCORD_STREAMING`). `"partial"` mode posts a single placeholder message and edit-in-place as tokens arrive. The four documented modes:

| mode | behaviour |
|---|---|
| `off` | Atomic delivery — only the final reply is posted. Upstream default. |
| `partial` | Single preview message, edit-in-place as tokens arrive. **Stack default.** |
| `block` | Paragraph-sized chunks posted as separate messages. |
| `progress` | Discord-side alias of `partial`. |

**Why `"partial"` works on this stack:**

- Discord rate limit is **5 message edits / 5 s per channel**.
- `draftChunk.minChars` defaults to 200 (~33 tokens).
- 6 tok/s × 33 tokens = ~5.5 s per edit ⇒ comfortably under the limit.
- Single dedicated bot application token (no rate-budget contention with sibling bots).

A faster backend (operator points `OPENAI_BASE_URL` at a cloud Sonnet/Haiku endpoint generating at 80+ tok/s, or swaps Gemma for a local sm_120-tuned NVFP4 build that hits 30+ tok/s) would chew through the edit budget. Drop to `"block"` (chunked posts, fewer edits) or `"off"` (atomic) in those cases.

**Caveats** (from [`docs.openclaw.ai/channels/discord.md`](https://docs.openclaw.ai/channels/discord.md)):

- **Media, error, and explicit-reply finals cancel the pending preview edit.** The final then arrives as a fresh atomic post. This is correct behaviour: image-gen replies (Path A `[embed]` shortcodes), tool errors, and explicit-reply finals should land as standalone events, not as overwrites of an in-progress preview.
- **Streaming is text-only.** Image attachments and file uploads use the atomic delivery path regardless of the streaming mode. Voice-channel TTS is independent.
- **Multiple bots / gateways sharing one Discord application token** will collide on the per-channel edit budget. Set `OPENCLAW_DISCORD_STREAMING=off` in that case.

**`draftChunk` env-knobbed in step 24** (each independently optional, default unset → OpenClaw docs default applies):

- `OPENCLAW_DISCORD_DRAFTCHUNK_MIN_CHARS` — minimum chars per edit. Default 200 (~33 tokens at 6 tok/s ≈ 5.5s/edit). Lower → more frequent edits; the soft floor is ~80 (~2s/edit) before the Discord 5-edits/5s rate limit becomes problematic.
- `OPENCLAW_DISCORD_DRAFTCHUNK_MAX_CHARS` — maximum chars per edit. Default 800 (clamped to `textChunkLimit=2000`).
- `OPENCLAW_DISCORD_DRAFTCHUNK_BREAK_PREFERENCE` — break heuristic. Default `"paragraph"`. Validated enum: `{paragraph, newline, sentence}`. `"newline"` gives line-grain edits ideal for short replies, much closer to typing UX; `"sentence"` is mid-grain, good for long-form. The upstream docs only show `"paragraph"`; the full enum was discovered on 2026-04-29 from the openclaw 2026.4.22 runtime validator (`Config invalid - channels.discord.streaming.preview.chunk.breakPreference: Invalid input (allowed: 'paragraph', 'newline', 'sentence')`). Common wrong guess: `"line"` — REJECTED, use `"newline"`. The patcher refuses any out-of-enum value with a `[patch-config]` warning to avoid crashing the gateway with an invalid config.

**Other tunables not env-knobbed by default:**

- `channels.discord.streaming.preview.toolProgress` (default `true`) — whether tool-execution progress reuses the preview message.
- `channels.discord.textChunkLimit` (default `2000`) — Discord's hard 2000-char per-message limit; replies above this are auto-split into sequential posts.
- `channels.discord.maxLinesPerMessage` (default `17`) — splits tall messages even when under the char limit.

If a live deploy proves any of these needs tuning, add a focused env knob following the same pattern as `OPENCLAW_DISCORD_STREAMING` (env-gated, user-managed protection, one `[patch-config]` log line).

**Recipe — line-grain typing UX:**

```bash
# In .env, append:
OPENCLAW_DISCORD_DRAFTCHUNK_MIN_CHARS=100
OPENCLAW_DISCORD_DRAFTCHUNK_BREAK_PREFERENCE=newline

# Then force-recreate:
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli

# Verify:
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}
docker exec ${PROJ}openclaw-cli node -e \
  "const j=require('fs').readFileSync('/home/node/.openclaw/openclaw.json','utf8'); \
   console.log('draftChunk =', JSON.stringify(JSON.parse(j).channels.discord.draftChunk))"
# Expect: draftChunk = {"minChars":100,"breakPreference":"newline"}
```

**Override examples:**

```bash
# Disable streaming for shared-bot deploys.
OPENCLAW_DISCORD_STREAMING=off

# Switch to chunked-block mode if `partial` edits are too chatty for you.
OPENCLAW_DISCORD_STREAMING=block

# Skip the patcher step entirely (operator manages the field by hand in openclaw.json).
OPENCLAW_DISCORD_STREAMING=
```

After changing the env, force-recreate the patcher chain so the new value lands in `openclaw.json`:

```bash
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

Verify the live config:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}
docker exec ${PROJ}openclaw-cli node -e \
  "const j=require('fs').readFileSync('/home/node/.openclaw/openclaw.json','utf8'); \
   console.log('channels.discord.streaming =', JSON.parse(j).channels.discord.streaming)"
# Expect: channels.discord.streaming = partial
```

Then mention the bot in Discord with a medium prompt (something that takes 30+ s to generate, e.g. "magyarázd el 200 szóban hogyan működik a TLS handshake"). The expected UX: a placeholder appears within ~1-2 s, the message body grows ~1 paragraph every ~5-6 s, and at the end you have a single coherent message — the bot does NOT post a fresh final and delete the preview.

## Verifying everything

Quick checklist when deploying a new Discord text-channel agent:

```bash
# 1. Plugin status — should show running + connected.
docker exec <PROJ>openclaw-cli openclaw channels status --deep | grep -i discord
# Expected: "running, connected, bot:@<botname>, intents:content=limited"

# 2. Live tool catalog for the discord-routed agent — should include `message`.
docker exec <PROJ>openclaw-cli openclaw agent --agent <discord-agent-id> \
  --message "List your tool names that contain 'message' or 'react'. One per line." \
  --thinking off --json --timeout 600 | jq -r '.result.payloads[0].text'
# Expected: "message" listed.

# 3. End-to-end react test — proper mention required.
# In Discord, type @<bot-prefix>, Tab to confirm the picker, then type:
#   "tegyel egy ✅ reactiont erre az uzenetre, semmi szoveges valasz nem kell"
# Expected: ✅ reaction appears below your message within 30-60s. No text reply.

# 4. Image-gen test (if image-comfyui bridge is up).
# In Discord: "@<bot> generálj egy zsiráfot kalapban"
# Expected: bot pastes a vision URL on its own line, Discord auto-embeds the
# image inline below. See docs/reference/image-comfyui-bridge.md for the full
# end-to-end flow.

# 5. AGENTS.md re-read on session restart.
# AGENTS.md is read once per agent session. Mid-session edits don't propagate
# until the agent session restarts. Workarounds:
# - Tell the agent explicitly: "olvasd ujra a workspace AGENTS.md-d"
# - Or restart the gateway (heavyweight, drops all sessions).
# - Or wait for the session to time out (varies by gateway config).
```

## TTS opt-in behavior (`OPENCLAW_TTS_AUTO=tagged`)

The Discord text-channel default is currently `OPENCLAW_TTS_AUTO=always` — the gateway attaches a TTS audio file to every final reply. For text-channel deploys this is often too aggressive (Discord text is a text-first surface, voice is a per-request opt-in). The recommended posture for text channels is `tagged`: the agent decides per-reply, by emitting a `[[tts:...]]` directive at the start of the final assistant text.

The marker syntax is **NOT** `[tts]` (single brackets) — that's just visible text-noise. The gateway parser (`provider-error-utils-yQpR7tSK.js` → `parseTtsDirectives`) matches the regex `/\[\[tts:([^\]]+)\]\]/gi` for inline directives and `/\[\[tts:text\]\]([\s\S]*?)\[\[\/tts:text\]\]/gi` for spoken-text blocks. Anything else is ignored. The simplest opt-in marker is `[[tts:speak]]` — no provider/voice override, parser strips it from the visible text, the rest of the assistant reply gets both rendered AND voiced.

Wire it in three steps:

1. **`.env`:** set `OPENCLAW_TTS_AUTO=tagged`.
2. **Force-recreate** the patcher chain: `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli`.
3. **AGENTS.md (workspace-side):** add the `## TTS — opt-in voice attach` section from [`templates/discord-text-agent/AGENTS.md.example`](../../templates/discord-text-agent/AGENTS.md.example). It tells the agent: never emit a directive by default, only on explicit user request ("mondd el voice-ban", "olvasd fel", "tts-eld", …).

Verify with two prompts in Discord:

1. Plain question: `@<bot> mennyi 2+2` → text-only reply, NO audio attachment.
2. TTS-trigger question: `@<bot> mondd el voice-ban hogy mennyi 2+2` → text reply WITH audio (.mp3/.opus) attached, AND no `[[tts:...]]` directive visible in the text (the parser strips it).

If the plain question returns audio anyway, either AGENTS.md is missing the opt-in section, or the env override didn't propagate. Check the live config:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}
docker exec ${PROJ}openclaw-cli node -e \
  "const j=require('fs').readFileSync('/home/node/.openclaw/openclaw.json','utf8'); \
   console.log('messages.tts.auto =', JSON.parse(j).messages.tts.auto)"
# Expect: messages.tts.auto = tagged
```

If the output is `always`, fix `.env` and force-recreate. The agent reads AGENTS.md only at session start — for AGENTS.md edits to take effect, either ask the agent to re-read it (`olvasd újra a workspace AGENTS.md-d`), restart the gateway, or wait for the session to time out.

Voice-channel agents (separate `/vc join`-driven deploy) typically stay on `always` because the voice-stream path always speaks regardless of `[tts]` tagging — set their per-deploy `.env` accordingly. See [`tts-stack.md`](./tts-stack.md) → "OPENCLAW_TTS_AUTO env knob" for the per-surface decision matrix.

## Related docs

- [`discord-voice-agent.md`](./discord-voice-agent.md) — voice-channel deployment
- [`image-comfyui-bridge.md`](./image-comfyui-bridge.md) — `comfyui_image__generate` tool surface
- [`chat-surface-capability-matrix.md`](./chat-surface-capability-matrix.md) — what renders where (Discord text vs web chat)
- `patch-config.mjs` (top docblock) — numbered list of every patcher step, including 20 (ackReactionScope), 21 (actions.reactions), 22 (tools.alsoAllow)
