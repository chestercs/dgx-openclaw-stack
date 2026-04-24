# Discord bot setup — from zero to voice-ready

End-to-end walkthrough for operators who have never created a Discord bot. Covers the Discord Developer Portal, the bot permission model, and the server invite flow. When the bot is visible (offline) in your guild and the token is in `.env`, continue in [`docs/CUSTOMIZATION.md` → Voice-controlled agent over Discord](./CUSTOMIZATION.md#voice-controlled-agent-over-discord) for the OpenClaw-side wiring.

Skip this guide if you already have a bot application in the Developer Portal and it's authorized in your guild — the OpenClaw runbook picks up from there.

## 1. Discord's permission model (the bits that matter)

Discord layers three independent "who can do what" systems on top of each other. You'll touch all three while setting up the bot, and it helps to see the whole shape before picking boxes.

### Layer 1 — OAuth2 Scopes (*what your bot is allowed to be*)

Scopes are chosen when you build the invite URL. They grant the bot an identity category:

- `bot` — the application registers as a bot user. Without this scope the invite URL does nothing useful.
- `applications.commands` — the bot can register and respond to slash commands (`/vc join` and friends). OpenClaw uses slash commands heavily, so you'll always tick both.

Scopes are frozen at invite time. To **add** a scope later (say, you later want guild integrations access), you have to re-invite the bot with a new URL. You cannot grant a new scope through server settings alone.

### Layer 2 — Bot Permissions (*what your bot can do in a channel*)

Permissions are the familiar Discord grants: Send Messages, Connect, Speak, etc. You tick the boxes in the Developer Portal's URL Generator; Discord packs them into a `permissions=<integer>` param on the invite URL.

Two things to know:

- Permissions set at invite time are a **starting baseline**, not a hard cap. A guild admin can override them per-channel (revoke Connect in a specific voice channel, tighten Send Messages in a sensitive text channel, etc.). Re-invite with a new URL is the only way to grant permissions you forgot at invite time.
- The integer is a bitmask Discord computes from your ticks — **don't hand-roll it**. Use the URL Generator and copy the full URL Discord gives you.

### Layer 3 — Privileged Gateway Intents (*what events Discord sends your bot*)

Gateway events are the realtime websocket feed: messages posted, users joining, voices starting, etc. Most events are freely available to every bot. Three are **privileged** because they're sensitive:

- **Presence Intent** — user online/idle/dnd status. Not needed for this voice integration.
- **Server Members Intent** — member join/leave/update events and the full member list. **Optional** for voice: only needed if you want the agent to say "Alice just said X, reply to her" instead of "speaker 2 said X" (i.e. map Discord user IDs to display names).
- **Message Content Intent** — the text content of messages the bot didn't interact with. **Not needed** for this integration: OpenClaw drives the bot through slash commands, which flow through the interactions gateway and don't require this intent. If you later add legacy prefix commands (`!vc join`) this would change.

Privileged intents are gated by a verification process: bots on 100+ guilds need Discord to approve each privileged intent manually. Your self-hosted OpenClaw bot typically stays on one guild — well under the gate — but over-requesting intents is still an antipattern, so default all three to off.

## 2. Create the Discord application

1. Open <https://discord.com/developers/applications> and sign in with the Discord account that'll own the bot. Click **New Application** top-right.

2. Name it something recognizable (`openclaw-gb10`, `my-homelab-assistant`, etc.). You can change the display name later.

3. Sidebar → **Bot**.

   - Click **Reset Token** to generate the bot token. Discord shows it once; copy it immediately and paste into your `.env` under `DISCORD_BOT_TOKEN=<paste here>`. If you lose it, reset again — the old token is invalidated on reset.
   - Scroll to **Privileged Gateway Intents**: leave all three off for the default voice setup. Enable **Server Members Intent** only if you need per-speaker attribution (see Layer 3 above). Skip **Presence Intent** and **Message Content Intent** entirely.

4. (Recommended while you're experimenting) Under **Bot** → **Authorization Flow**, untick **Public Bot** so random people can't invite your bot elsewhere. You can flip it back later once you're sure of the setup.

## 3. Generate the invite URL

1. Sidebar → **OAuth2** → **URL Generator**.

2. In the **Scopes** grid, tick:
   - `bot`
   - `applications.commands`

   A **Bot Permissions** panel appears below once `bot` is ticked.

3. In the **Bot Permissions** panel, tick these six boxes and no others:

   | Permission | What it does | If missing |
   |---|---|---|
   | View Channels | Bot sees the channel list | Bot can't see the voice channel you want it to join |
   | Send Messages | Bot can post text replies and command acknowledgements | Slash commands still invoke but the bot can't post error text or status replies |
   | Read Message History | Bot can fetch prior messages for context | Minor; helps if the agent references what someone said earlier in text |
   | Connect | Bot can join voice channels | Voice integration fully broken |
   | Speak | Bot can transmit audio into voice channels | Agent can hear you but can't reply — no TTS playback |
   | Use Application Commands | Role-level parity flag for slash commands (the `applications.commands` scope above is what actually unlocks them) | Nothing material; keep it for convention |

   Do **not** tick Administrator, Use Voice Activity, Priority Speaker, Manage Channels, or anything else. Over-permissioning a bot is a security smell and fixes nothing for this use case.

4. Scroll to the bottom. Discord builds the URL with the correct `permissions=<integer>` bitmask — **copy the full URL**. Example shape:

   ```
   https://discord.com/api/oauth2/authorize?client_id=123456789012345678&permissions=3214336&scope=bot+applications.commands
   ```

   The `permissions` integer is computed by Discord from your ticks. Don't hand-edit it; re-tick in the generator if you need to change the permission set.

## 4. Invite the bot to your server

Who can authorize a bot into a guild:

- The **server owner** (crown icon next to their name in the member list).
- Any member with the **Manage Server** permission in the target guild.

You can only authorize into guilds the Discord account you're currently signed in as is a member of. If the bot is going into someone else's guild, they have to open the URL themselves — the token stays on your side, they never see it.

Steps:

1. Paste the invite URL into a browser. Discord asks which server to add the bot to.
2. Pick the target guild from the dropdown and click **Authorize** (solve the captcha if prompted).
3. Go back to Discord and open that guild. The bot appears in the member list, **greyed out / offline** — this is correct. It has no process running yet; OpenClaw will connect it later.

If the bot didn't appear:

- Check **Server Settings → Audit Log** — a successful invite writes a "Bot added" entry.
- Make sure the URL carries both `scope=bot` and `scope=applications.commands` (URL-encoded as `bot+applications.commands` or `bot%20applications.commands`).
- If you got a 401 on the Authorize page, the signed-in Discord account isn't a member of that guild — join it first or have the owner open the URL.

## 5. Post-invite Discord-side setup

### 5.1 Role hierarchy

Inviting the bot created a dedicated role with the application's name. Discord enforces roles top-down: a role's allow/deny only applies if no role above it denies the same permission.

- For the bot to work in **public voice channels**, the default role position is fine — no adjustment needed.
- For the bot to work in a **private voice channel** gated by other roles, drag the bot's role up in **Server Settings → Roles** until it sits above those gate roles.

### 5.2 Channel-level overrides (optional)

If you want the bot limited to one specific voice channel:

1. Right-click the voice channel → **Edit Channel** → **Permissions**.
2. On the `@everyone` role: remove Connect and Speak if you want the channel bot-only; leave them if you want a shared channel.
3. Click **+ Add members or roles**, pick the bot's role, and grant Connect + Speak explicitly.

That restricts the bot to this channel while still allowing future expansion. The OpenClaw side can also pin the bot to a specific guild+channel via `autoJoin` — covered in the runbook.

### 5.3 Changing the bot's permissions later

If you later realize the bot needs a permission you didn't invite it with, **re-invite** with a new URL that includes it:

1. Go back to the URL Generator, tick the additional permission, copy the new URL.
2. Open it in your browser; Discord shows the permission delta and asks you to reauthorize.
3. Accept. The bot's role is updated with the new baseline.

You cannot add a new permission through Server Settings → Roles alone — Discord routes permission bumps through the OAuth flow so they land in the audit log.

## 6. Checkpoint: bot visible offline

Before continuing to the OpenClaw side, confirm:

- The bot is in your guild's member list, greyed out (offline).
- `.env` in this repo has `DISCORD_BOT_TOKEN=<the value you copied in §2>`.
- `.env` has `DISCORD_AGENT_NAME=discord-voice` (or a name of your choice — whatever you set here becomes the OpenClaw agent identifier).
- `.env` has `DISCORD_WORKSPACE_DIR=/opt/openclaw/workspace-discord` (or your preferred host path — must not overlap with `OPENCLAW_WORKSPACE_DIR`, since the isolation depends on disjoint directories).

Continue in [`docs/CUSTOMIZATION.md` → Voice-controlled agent over Discord](./CUSTOMIZATION.md#voice-controlled-agent-over-discord) from **Step 2** (the schema probe). It registers the channel in OpenClaw, creates the isolated agent, tightens the exec-policy, and walks you through an end-to-end voice test.

## 7. Troubleshooting (Discord-side)

- **Bot stays offline after `docker compose up -d` and OpenClaw onboarding.** Token mismatch is the usual cause — you probably reset the token in the Developer Portal without updating `.env`. Paste the current token into `DISCORD_BOT_TOKEN`, then `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli` and re-run `openclaw channels add` per the runbook.

- **Slash commands don't appear in Discord's autocomplete.** Global slash-command propagation takes up to 1 hour on first registration. OpenClaw registers them automatically when the bot connects; wait 15-60 minutes, or kick + re-invite to the same guild to force a guild-scoped re-register (instant propagation for that guild only).

- **Bot is online in text but won't join voice.** Three common causes, in order of likelihood: (a) role hierarchy — the bot's role sits below a role that denies Connect; drag it up per §5.1; (b) channel-level override — the voice channel denies Connect for `@everyone` and you haven't whitelisted the bot role per §5.2; (c) Connect permission was never ticked in the invite — re-invite per §5.3.

- **Can't decide which Privileged Gateway Intents to enable.** For the default voice use case: **none**. Enable Server Members Intent only if the agent needs to name specific speakers (rare); enable Message Content Intent only if you later add legacy prefix commands (this integration uses slash commands exclusively).

- **Someone else needs to invite the bot to their guild.** Send them the invite URL. Only a member of that guild with Manage Server (or the owner) can authorize. Do not share the bot token — they never need it for the invite step.

## Related

- OpenClaw-side runbook (continues from §6): [`docs/CUSTOMIZATION.md` → Voice-controlled agent over Discord](./CUSTOMIZATION.md#voice-controlled-agent-over-discord)
- Internals, isolation design, threat model, DAVE encryption details: [`docs/reference/discord-voice-agent.md`](./reference/discord-voice-agent.md)
- Upstream Discord developer documentation: <https://discord.com/developers/docs>
