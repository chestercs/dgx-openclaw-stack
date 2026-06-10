# `!~/.openclaw/bin/img` — operator-only Discord bash bypass for image-gen

## What this is

A wrapper script + OpenClaw `commands.bash` config that lets the operator
post `!~/.openclaw/bin/img <prompt>` in Discord and get a rendered image
back **without** Gemma's RLHF safety alignment evaluating the prompt.

The flow:
1. A trusted user types `!~/.openclaw/bin/img "<prompt>"` in any Discord
   channel the bot can read.
2. OpenClaw's gateway sees the `!` prefix, checks `commands.ownerAllowFrom`
   against the sender's Discord user id (the trusted snowflake list set via
   `OPENCLAW_DISCORD_COMMAND_OWNERS` — see "Configuration footprint" below).
3. The script runs inside the openclaw-gateway container as `node` (uid
   1000). It reads `$IMAGE_GEN_API_TOKEN` from the gateway env, builds a
   JSON-RPC `tools/call` payload (`include_base64: true`), and POSTs it
   directly to `http://openclaw-image-comfyui:9095/mcp`.
4. The bridge talks to ComfyUI, polls `/history`, fetches the rendered
   image bytes, and returns metadata + the base64 PNG + `display_markdown`.
5. The script delivers the result (see "Output" below): a TRUE Discord
   attachment when it can reach the channel id + bot token, else a webhook
   upload, else the auto-embedded public link. A one-line summary is printed
   to stdout, which OpenClaw posts back to the same channel.

No LLM in the loop. The script is the only entity that sees the
prompt; nothing in the path applies safety filtering or rewrites.

## Usage

The message **must start with `!`** (no `@mention` before it) and come from a
trusted user. A short and a long form both work:

```
!img [flags] <prompt>              # short — needs ~/.openclaw/bin on PATH
!~/.openclaw/bin/img [flags] <prompt>   # long — always works
```

⚠ **Do NOT @mention the bot.** `@Bot !img …` starts with the mention, so
OpenClaw routes it to the LLM (the `!`-directive only fires on a leading `!`),
and Gemma's RLHF then refuses adult prompts. Start the message with `!`.

The default workflow is set by `IMG_DEFAULT_WORKFLOW` (empty → `flux-krea-2k`
SFW). On an adult deploy set it to `flux-krea-2k-adult` so a bare `!img
<prompt>` is NSFW; `--sfw` forces SFW per call.

### Flags

| Flag         | Effect                                              |
|--------------|-----------------------------------------------------|
| `--nsfw`     | Force the `flux-krea-2k-adult` workflow (flux-uncensored-v2 LoRA at strength 1.5). |
| `--adult`    | Alias for `--nsfw`.                                 |
| `--sfw`      | Force the `flux-krea-2k` SFW workflow (overrides an adult `IMG_DEFAULT_WORKFLOW`). |
| `--safe`     | Alias for `--sfw`.                                  |
| `--2k`       | 2048×2048 square. Default is 1280×720.              |
| `--hd`       | 1280×720 (the default — included for explicitness). |
| `--portrait` | 768×1280.                                           |
| `--pano`     | 1920×1088.                                          |
| `--square`   | 1024×1024.                                          |
| `--w=N`      | Custom width (any FLUX-supported value).            |
| `--h=N`      | Custom height.                                      |
| `--seed=N`   | RNG seed (default random).                          |

### Examples

```
!~/.openclaw/bin/img "a misty forest at dawn, photorealistic"
!~/.openclaw/bin/img --2k "a single red apple, sharp focus"
!~/.openclaw/bin/img --portrait --seed=42 "a young woman in summer dress at a park"
!~/.openclaw/bin/img --nsfw "topless young blonde woman, bare breasts, sunlit park"
!~/.openclaw/bin/img --nsfw --2k "explicit prompt here..."
!~/.openclaw/bin/img --w=1024 --h=1024 "anything in 1024x1024"
```

### Render time on GB10 (warm cache, fp8 FLUX)

| Resolution      | Wall time |
|-----------------|-----------|
| 1280×720 (HD)   | ~50-90s   |
| 1024×1024       | ~50-80s   |
| 1920×1088 (pano)| ~1-2 min  |
| 2048×2048 (2K)  | ~2-3 min  |
| 768×1280 (portrait) | ~50-80s |

Cold first render after a stack restart adds ~60-120s for FLUX UNet
load (warmup happens once).

## Output

The script delivers the rendered image one of three ways, tried in order.
It always prints a one-line summary (`flux-krea-2k 1280x720 - 50.2s, seed
11`) to stdout, which OpenClaw posts as the bot's reply.

1. **True Discord attachment (preferred).** The bridge returns the PNG as
   base64 (`include_base64`); the script reads the bot token from
   `openclaw.json` (`channels.discord.token`) and the current channel id
   from the runtime env (`OPENCLAW_MCP_CURRENT_CHANNEL_ID`, or any
   `OPENCLAW_*CHANNEL*` env that looks like a snowflake), then uploads the
   file via `POST /channels/{id}/messages` (multipart). The image lands as a
   real uploaded attachment on Discord's CDN — it renders natively and does
   not depend on the external URL staying reachable. **Whether this path
   fires depends on the runtime exposing a channel id to the `!`-directive
   subprocess** — verify with `!printenv | grep -i channel` from a trusted
   account on your OpenClaw version.
2. **Fixed-channel webhook.** If no channel id is in the env but
   `IMG_DISCORD_WEBHOOK_URL` is set, the script uploads the PNG to that
   webhook (a real attachment, but always in the webhook's channel).
3. **Public link (fallback).** If neither attachment path is available — or
   the PNG exceeds `IMG_DISCORD_MAX_BYTES` (~9 MiB, under Discord's 10 MiB
   non-boosted cap) — the script prints the bridge's `display_markdown`
   (image URL + `[embed]` shortcode). Discord auto-embeds the URL on its own
   line as an inline preview; the `[embed]` shortcode is web-chat-only and
   Discord ignores it as text noise.

## Why this exists (Gemma RLHF bypass)

The Discord-friend agent runs Gemma 4 26B-A4B NVFP4 on this stack.
Even with explicit AGENTS.md / SOUL.md operator-permission instructions
that tell the agent the deploy is private, adult, and the
flux-uncensored-v2 LoRA is in scope, Gemma's safety alignment refuses
to construct adult prompts (verified on `vision.petyuspolisz.com` /
GB10 on 2026-05-09 with multiple framings). Soft prompt-engineering
overrides aren't reliable on Gemma's RLHF.

The only true bypass is to keep the LLM out of the path entirely. Two
realistic options were available:

- (A) A separate Discord bot with `/img` slash commands. The only design
  that supports a real slash command, typed options, AND an "image-only"
  permission tier (a user who can generate images but get nothing else) —
  because it doesn't ride on `commands.bash`. Heavier (separate token,
  separate identity, a small always-on service).
- (B) The existing OpenClaw bot's `commands.bash` text-command surface,
  gated to a trusted Discord user-id list. Selected — no extra service.

`commands.bash` is a stable OpenClaw feature, but it is **all-or-nothing
arbitrary shell** inside the gateway container as the node user: there is no
per-command allowlist and no role gate. The only gate is the user-id lists
`commands.ownerAllowFrom` + `tools.elevated.allowFrom.discord`. So every user
on that list can run ANY command (`!rm -rf …`, read the config volume's
secrets) and the owner-only slash commands (`/config`, `/mcp`, …) — NOT just
image-gen. Keep the list short and fully trusted; this is option (B)'s
inherent trade-off, and the reason an "image-only" tier needs option (A).

The `img` script itself is purpose-built around `comfyui_image__generate` —
its quoting, prompt extraction, JSON parsing, and Discord upload are all
defensive — but the surface it rides on (`commands.bash`) is not narrowed.

## Configuration footprint

All of this is **patcher-managed** (`patch-config.mjs`) and driven from
`.env` — no ad-hoc `openclaw config set`. The relevant knobs:

```bash
# Enable the !-bash directive (patcher step 8f₂) + write ~/.openclaw/bin/img.
OPENCLAW_COMMANDS_BASH=on
# Gate it to a trusted snowflake list (NOT the `*` default!). These users get
# full container shell + owner-only slash commands, not just image-gen.
OPENCLAW_DISCORD_COMMAND_OWNERS=244049593338167296,OTHER_TRUSTED_SNOWFLAKE
OPENCLAW_TOOLS_ELEVATED_DISCORD_ALLOW=244049593338167296,OTHER_TRUSTED_SNOWFLAKE
# Bridge token (already wired) — the script reaches the bridge with this.
IMAGE_GEN_API_TOKEN=…
# Optional: guarantee attachments via a fixed-channel webhook + size cap.
IMG_DISCORD_WEBHOOK_URL=
IMG_DISCORD_MAX_BYTES=9437184
```

The patcher writes `commands.bash` (step 8f₂), `commands.ownerAllowFrom` +
`tools.elevated.allowFrom.discord` (step 8f), and — when `OPENCLAW_COMMANDS_BASH`
is `on` and `IMAGE_GEN_API_TOKEN` is set — the `img` script itself to
`/home/node/.openclaw/bin/img` (mode 0755, in the config volume so it survives
recreate). **The script is patcher-owned**: it is rewritten on every patcher
run, so edit the `IMG_BASH_SCRIPT` constant in `patch-config.mjs`, never the
deployed file. `docker-compose.yml` passes `IMAGE_GEN_URL`,
`IMG_DISCORD_WEBHOOK_URL`, and `IMG_DISCORD_MAX_BYTES` to the gateway (where
the script runs) and `OPENCLAW_COMMANDS_BASH` to config-init (the patcher).

## Limitations

- **Trusted users get full shell, not just image-gen** — `commands.bash` is
  all-or-nothing (see "Why this exists"). Everyone on
  `OPENCLAW_DISCORD_COMMAND_OWNERS` can run arbitrary container commands and
  owner-only slash commands. There is no image-only tier without option (A).
  Keep the list short. Leaving it at the `*` default while bash is on =
  guild-wide RCE.
- **Attachment delivery depends on the runtime** — the true-attachment path
  needs a channel id in the `!`-directive subprocess env. If your OpenClaw
  version doesn't expose one (`!printenv | grep -i channel` is empty), set
  `IMG_DISCORD_WEBHOOK_URL` for attachments, or accept the link fallback.
- **Discord 2000-char limit** — the summary + link line is well under it.
- **Render time vs. concurrency** — the script blocks for the render
  (~50s-3min); other directives wait. Concurrent renders aren't supported
  (`IMAGE_GEN_MAX_CONCURRENCY=1` on the bridge).
- **Snowflakes as STRINGS** — list ids as quoted strings; a bare 19-digit
  integer is rounded by JS Number precision and the equality check then
  fails. The patcher stringifies defensively, but set them as strings in
  `.env` regardless.
