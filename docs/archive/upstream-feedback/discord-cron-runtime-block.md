# Upstream feedback: cron tool unavailable on Discord agent routes (DM and guild)

**Affected version:** openclaw 2026.4.22 (image digest `639f…` — pinned via `OPENCLAW_IMAGE_REF`).
**Discovered:** 2026-05-06 (initial guild-only report).
**Re-verified:** 2026-05-07 (now confirmed DM route is affected too).
**Reporter context:** GB10 / DGX Spark, Gemma 4 31B NVFP4 backend, dedicated `discord-friend` agent bound to a Discord text channel.

## Symptom

When a user pings the discord-routed agent on **either route — DM or guild text channel** — with a request that requires deferred execution (e.g. *"1 minute later, generate an image of …"*), the model:

1. Acknowledges the request ("Mehet, 1 perc múlva szólok!").
2. Attempts to call the `cron` tool — the tool-call returns `Tool cron not found` from the runtime resolver.
3. Eventually emits the literal string *"I can't use the tool 'cron' here because it isn't available."* and stops.

**Initial 2026-05-06 report claimed DM worked**: that was a transient cache state (two successful runs that day, `hawaii-paradicsom` and `teszt6`, both with `to: "channel:<dm-channel-id>"`). 2026-05-07 re-test under identical conditions: same `Tool cron not found` on DM. The DM-vs-guild distinction is not real — both Discord agent routes are equally broken.

## Two-layer evidence

### 1. `tools.allow` warning (until per-guild policy is configured)

```
[tools] agents.discord-friend.tools.allow allowlist contains unknown entries (cron).
        These entries are shipped core tools but unavailable in the current
        runtime/provider/model/config.
[tools] group tools.allow allowlist contains unknown entries (cron). These
        entries are shipped core tools but unavailable in the current
        runtime/provider/model/config.
```

Setting `channels.discord.guilds.<guild-id>.tools.alsoAllow = ["cron"]` (the
per-guild tool policy override) silences these warnings — `cron` then
appears in the agent's textual tool catalog AND the `enable-auto-tool-choice`
path becomes active.

### 2. `toolResult: "Tool cron not found"` (the actual runtime block)

But even with per-guild `alsoAllow=cron` and the warnings gone, the model's
correctly-shaped tool-call still fails at the runtime tool-resolver layer.
Verified 2026-05-07 from the trajectory `messagesSnapshot`:

```
role: assistant
content: [{type: toolCall, id: chatcmpl-tool-a57fbf1cee3a3f58, name: "cron",
           arguments: {action: "add", agent: "discord-friend", at: "+90s",
                       channel: "discord", message: "<…>",
                       to: "channel:1426994992248782920"}}]

role: toolResult
content: [{type: text, text: "Tool cron not found"}]

role: assistant
content: [{type: text, text: "I can't use the tool \"cron\" here because it
           isn't available. I need to stop retrying it and answer without
           that tool."}]
```

The model emits a perfectly-shaped tool-call (correct name, correct args
schema). The runtime tool-resolver then returns `"Tool cron not found"` —
NOT a tool-policy denial, NOT a schema violation, but a runtime registration
gap on this route. The model retries a few times, gets the same error, and
gives up with the apologetic text seen in the chat.

The DM context with the same agent + same payload registers cron cleanly
and the wake-up turn fires on schedule.

## Configuration

- `agents.list[discord-friend].tools.profile = "full"`
- `agents.list[discord-friend].tools.alsoAllow = ["browser", "tts", "canvas", "cron"]` (cron added explicitly to defeat any list-level filter — does not help)
- `channels.discord.capabilities = ["cron"]` (attempt to opt-in via channel capabilities — does not help either)
- Provider: vLLM, model `nvidia/Gemma-4-31B-IT-NVFP4`, OpenAI-compat
- Runtime: `acpx` embedded backend (the standard runtime for this stack)

## Reproduction

1. Configure a `discord-friend` agent with `tools.profile=full` and add `cron` to alsoAllow.
2. Bind it to a Discord channel via `bindings[]` (DM or guild text — both reproduce).
3. Optional but recommended (silences config-layer warnings, isolates the runtime block): set `channels.discord.guilds.<guild-id>.tools.alsoAllow=["cron"]`.
4. From the channel, mention the bot: `@bot 1 perc múlva csinálj egy képet egy lobsterről`.
5. Observe gateway trajectory `messagesSnapshot`:
   ```
   role: assistant   toolCall name=cron args={action:add, at:+1m, …}
   role: toolResult  text: "Tool cron not found"
   role: assistant   text: "I can't use the tool 'cron' here because it isn't available."
   ```

## Workaround for operators

Operator-driven scheduling via the CLI works:
```
docker exec openclaw-cli openclaw cron add --name "X" --at "1m" \
  --agent discord-friend --message "do Y" \
  --channel discord --to "channel:<dm-or-guild-channel-id>" \
  --delete-after-run
```
This registers correctly and the wake-up turn fires + delivers normally — confirming the cron handler is alive in the runtime, just not reachable from the in-channel agent tool-call path.

## Suggested upstream behavior

Either:

1. **Register the `cron` tool handler on the Discord agent runtime** so structured tool-calls from the model resolve, OR
2. **Surface a settable config field** that toggles cron on the Discord route — `channels.discord.capabilities = ["cron"]` is accepted by the schema but has no observable effect; either honor that or document the actual surface.
3. **Improve the error message**: `Tool cron not found` is misleading when the tool obviously exists (CLI cron add succeeds) — something like `Tool cron not registered for runtime=discord:agent-route` would point operators in the right direction.

## Why this matters

Time-deferred actions (reminders, scheduled image generation, scheduled posts) are core to a Discord bot UX. *"@bot reggel 9-kor írj rá hogy itt a kávé"* doesn't work today on either DM or guild routes. The model either ignores the timing and executes immediately (1-min requests turn into instant image-gen, observed) or apologizes that it can't (5-min requests trip the cron path and get rejected with the misleading "not available" text).

Initial 2026-05-06 report described this as a guild-only block. 2026-05-07 testing showed the DM route is equally affected — the earlier "DM works" observations were transient cache state, not stable behavior.
