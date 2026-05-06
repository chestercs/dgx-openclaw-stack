# Upstream feedback: cron tool unavailable on Discord guild text-channel routes

**Affected version:** openclaw 2026.4.22 (image digest `639f…` — pinned via `OPENCLAW_IMAGE_REF`).
**Discovered:** 2026-05-06.
**Reporter context:** GB10 / DGX Spark, Gemma 4 31B NVFP4 backend, dedicated `discord-friend` agent bound to a Discord text channel.

## Symptom

When a user pings the discord-routed agent on a **guild text channel** with a request that requires deferred execution (e.g. *"1 minute later, generate an image of …"*), the model:

1. Acknowledges the request ("Mehet, 1 perc múlva szólok!").
2. Attempts to call the `cron` tool — the tool-call validation fails repeatedly.
3. Eventually emits the literal string *"I can't use the tool 'cron' here because it isn't available."* and stops.

The same agent + same prompt over a **DM context** registers `cron` cleanly and the wake-up turn fires on schedule with `comfyui_image__generate` invocation and Discord delivery.

## Gateway log evidence

```
[tools] agents.discord-friend.tools.allow allowlist contains unknown entries (cron).
        These entries are shipped core tools but unavailable in the current
        runtime/provider/model/config.
```

The warning is emitted at every gateway boot AFTER the first inbound message on a guild text channel. On DM context the same `tools.allow` config does not produce the warning and `cron` is reachable.

## Configuration

- `agents.list[discord-friend].tools.profile = "full"`
- `agents.list[discord-friend].tools.alsoAllow = ["browser", "tts", "canvas", "cron"]` (cron added explicitly to defeat any list-level filter — does not help)
- `channels.discord.capabilities = ["cron"]` (attempt to opt-in via channel capabilities — does not help either)
- Provider: vLLM, model `nvidia/Gemma-4-31B-IT-NVFP4`, OpenAI-compat
- Runtime: `acpx` embedded backend (the standard runtime for this stack)

## Reproduction

1. Configure a `discord-friend` agent with `tools.profile=full` and add `cron` to alsoAllow.
2. Bind it to a guild text channel via `bindings[]`.
3. From the channel, mention the bot: `@bot 1 perc múlva csinálj egy képet egy lobsterről`.
4. Observe gateway log: `tools.allow allowlist contains unknown entries (cron). … unavailable in the current runtime/provider/model/config.`
5. Observe model output: text-only ack + `I can't use the tool "cron" here`.

## Workaround in this stack (v0.11.x patcher)

The discord-friend's `workspace-discord/AGENTS.md` documents a `sessions_send` delegation pattern: when on a guild route, the bot forwards the scheduling request to the `main` agent, which CAN call cron. The wake-up turn then runs in the discord-friend agent's context (so the action — image-gen, web-search, etc. — still happens on the right agent).

This is operator-facing prompt-engineering — not a runtime fix. Native support would let the discord-routed agent invoke cron directly on guild channels.

## Suggested upstream behavior

Either:

1. **Remove the runtime/provider/model/config gate on `cron`** for Discord-routed agents (it works in DM, the only difference is `is_group_chat: true` in the inbound metadata).
2. **Surface the gate as an explicit, settable config field** — currently `channels.discord.capabilities = ["cron"]` is accepted by the schema but does not unblock the tool. Either honor that array to opt the channel into cron, or document the actual config surface that controls it.
3. **Improve the warning text** — the current "unavailable in the current runtime/provider/model/config" doesn't tell the operator which of the four pivots is the deciding factor.

## Why this matters

Time-deferred actions on guild channels (reminders, scheduled image generation, scheduled posts) are core to a Discord bot UX. A user typing *"@bot reggel 9-kor írj rá hogy itt a kávé"* on a guild channel does not get a working flow today, even with `tools.profile=full`. The bot either ignores the timing (immediate execution — observed: 1-min requests get instant image-gen) or apologizes that it can't (observed: 5-min requests trip the cron path and get rejected).

The DM-vs-guild gap is the surprising part. Operators expect the agent's tool surface to be channel-route-uniform; the current behavior silently restricts one and not the other.
