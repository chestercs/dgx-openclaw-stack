# `openclaw-claw-img-bot` — standalone `/claw-img` Discord slash command

A ~150-line Discord bot that exposes a real **`/claw-img`** slash command and
generates images by calling the `comfyui_image` bridge **directly** — the LLM is
never in the loop.

## Why this exists

OpenClaw's own command surface can't do this on the current build:

- Every slash/`@mention` message routes through the agent LLM, whose RLHF
  refuses adult prompts.
- The only LLM-bypass path (`commands.bash` → `!`/`​/bash`) is arbitrary-shell,
  approval-gated, and not a real Discord slash command (it doesn't appear in
  Discord's `/` autocomplete and triggers DM approval prompts).

This bot sidesteps all of it: the slash command runs deterministic code, calls
the bridge MCP endpoint, and uploads the rendered PNG as a **true Discord
attachment**. No LLM, no approval, no shell.

It is **opt-in** behind the `claw-img` compose profile and uses its **own**
Discord application/token (a second bot user), so it never conflicts with the
OpenClaw bot's gateway connection.

## One-time setup (operator)

1. **Create a Discord application + bot** at
   <https://discord.com/developers/applications> → *New Application* → *Bot* →
   *Reset Token* → copy the token into `CLAW_IMG_DISCORD_TOKEN` in `.env`.
   No privileged intents are required (leave Message Content Intent **off**).
2. **Invite it** to your server. *OAuth2 → URL Generator* → scopes
   **`bot`** + **`applications.commands`** → no special bot permissions needed
   (it only sends messages/attachments via slash-command responses) → open the
   generated URL and add it to your guild.
3. Put your server id in `CLAW_IMG_GUILD_ID` (right-click the server →
   *Copy Server ID*, with Developer Mode on) so the command registers
   **instantly** (global registration takes ~1 h).
4. Make sure `IMAGE_GEN_API_TOKEN` is set (same token the bridge uses) and the
   `openclaw-image-comfyui` bridge is up on the stack network.
5. Start it:
   ```bash
   docker compose --profile claw-img up -d --build openclaw-claw-img-bot
   ```

## Usage

`/claw-help` (no arguments) shows an ephemeral cheatsheet of every option for
both commands.

```
/claw-img prompt:<text> [negative:<text>]
          [resolution:square|portrait|landscape|hd|fullhd|2k]
          [width:<px>] [height:<px>] [steps:<int>] [cfg:<float>]
          [seed:<int>] [safe:true]
```

- `negative` — what to keep out of the image.
- `resolution` — size preset; `width`/`height` override it (4K omitted on
  purpose — it can OOM the box; set width/height by hand if you must).
- `steps` / `cfg` — quality knobs; omit to use the workflow defaults.

### `/claw-video` (LTX-Video 2.3)

```
/claw-video prompt:<text> [image:<attachment>] [negative:<text>]
            [resolution:square|portrait|landscape|fullhd]
            [width:<px>] [height:<px>] [seconds:<float>] [fps:<int>]
            [audio:true|false] [seed:<int>]
```

- **`image`** — attach a source image to **animate it** (image-to-video, the
  bridge auto-picks `ltx-2.3-i2v`). Without it, it's text-to-video.
- `seconds` — clip length (capped at `CLAW_VIDEO_MAX_SECONDS`, default 10).
- `audio` — LTX-2.3 generates a synced audio track and muxes it into the mp4
  (default on).
- width/height are rounded to a multiple of 32 (LTX requirement); 2K/4K are not
  offered for video. Returns the mp4 as an attachment (link fallback if it's
  over the Discord upload cap). Cold-cache renders take a few minutes.

- The default workflow is `CLAW_IMG_DEFAULT_WORKFLOW`. Set it to
  `flux-krea-2k-adult` for an NSFW-by-default deploy; `safe:true` forces the SFW
  workflow per call.
- `width`/`height` override the `resolution` preset.
- Anyone who can use slash commands in the guild can run it. Because the bot
  only generates images (no shell, no LLM), opening it to everyone is safe —
  unlike `commands.bash`. Restrict per-command in *Server Settings →
  Integrations* if you want.

## Env vars

| Var | Default | Purpose |
|-----|---------|---------|
| `CLAW_IMG_DISCORD_TOKEN` | (required) | This bot's Discord token. |
| `CLAW_IMG_GUILD_ID` | (empty) | Guild id for instant command registration. |
| `IMAGE_GEN_URL` | `http://openclaw-image-comfyui:9095/mcp` | Bridge MCP endpoint. |
| `IMAGE_GEN_API_TOKEN` | (required) | Bridge bearer token. |
| `CLAW_IMG_DEFAULT_WORKFLOW` | `flux-krea-2k` | Workflow for a bare prompt. |
| `CLAW_IMG_SFW_WORKFLOW` | `flux-krea-2k` | Workflow when `safe:true`. |
| `CLAW_IMG_MAX_BYTES` | `9437184` | Max attachment size before link fallback. |
| `CLAW_IMG_TIMEOUT_S` | `600` | Per-render bridge timeout. |
