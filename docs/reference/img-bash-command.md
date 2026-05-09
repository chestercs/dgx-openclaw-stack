# `!~/.openclaw/bin/img` — operator-only Discord bash bypass for image-gen

## What this is

A wrapper script + OpenClaw `commands.bash` config that lets the operator
post `!~/.openclaw/bin/img <prompt>` in Discord and get a rendered image
back **without** Gemma's RLHF safety alignment evaluating the prompt.

The flow:
1. Operator types `!~/.openclaw/bin/img "<prompt>"` in any Discord channel
   the bot can read.
2. OpenClaw's gateway sees the `!` prefix, checks
   `commands.ownerAllowFrom` against the sender's Discord user id
   (currently restricted to the bot owner's id, `244049593338167296`).
3. The shell command runs inside the openclaw-gateway container as
   `node` (uid 1000). The script reads `$IMAGE_GEN_API_TOKEN` from the
   gateway env, builds a JSON-RPC payload, and POSTs it directly to
   `http://openclaw-image-comfyui:9095/mcp`.
4. The bridge talks to ComfyUI, polls `/history`, fetches the rendered
   image bytes, returns metadata + `display_markdown` to the script.
5. The script prints a one-line summary + the image URL + an
   `[embed url=…]` shortcode to stdout, which OpenClaw posts back to
   the same Discord channel.

No LLM in the loop. The script is the only entity that sees the
prompt; nothing in the path applies safety filtering or rewrites.

## Usage

Top-level command (must start with `!`, must be from the operator):

```
!~/.openclaw/bin/img [flags] "<prompt>"
```

### Flags

| Flag         | Effect                                              |
|--------------|-----------------------------------------------------|
| `--nsfw`     | Use the `flux-krea-2k-adult` workflow (flux-uncensored-v2 LoRA at strength 1.5). Without this flag, runs `flux-krea-2k` (SFW). |
| `--adult`    | Alias for `--nsfw`.                                 |
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

The script's stdout is what Discord receives. Format:

```
🎨 flux-krea-2k @ 1280x720 — rendering...
**`flux-krea-2k`** 1280x720 — 50.2s, seed 11
https://vision.petyuspolisz.com/view?filename=flux-krea-2k_00005_.png&type=output&subfolder=openclaw-bridge&token=...

[embed url="/__openclaw__/canvas/comfyui-8d6e18a2-flux-krea-2k_00005_.html" /]
```

Discord auto-embeds the URL on its own line as an inline image
preview. The `[embed url=…]` shortcode is for the OpenClaw web chat
surface (Path A canvas inline render); Discord ignores it as text
noise but keeps the line for cross-surface compatibility.

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

- (A) A separate Discord bot with `/img` slash commands. Rejected by
  the operator: too much overhead, separate token, separate identity.
- (B) The existing OpenClaw bot's `commands.bash` text-command
  surface, gated to the operator's Discord user id only. Selected.

`commands.bash` was already a stable OpenClaw feature; it ran
arbitrary shell commands inside the gateway container as the node
user. By gating it to a single Discord id (`ownerAllowFrom`) and
restricting it via `tools.elevated.allowFrom.discord`, the bash
command surface is invisible to anyone else on the bot's servers.

The `img` script is purpose-built around `comfyui_image__generate` —
no general-purpose shell access required for image-gen. The script's
quoting, prompt extraction, and JSON parsing are all defensive.

## Configuration footprint

`openclaw.json` (already configured by ad-hoc `openclaw config set`
calls in this session):

```json
{
  "commands": {
    "bash": true,
    "ownerAllowFrom": ["244049593338167296"]
  },
  "tools": {
    "elevated": {
      "enabled": true,
      "allowFrom": {
        "discord": ["244049593338167296"]
      }
    }
  }
}
```

`docker-compose.yml` (committed): `IMAGE_GEN_API_TOKEN` env passthrough
to the openclaw-gateway service.

`/home/node/.openclaw/bin/img` (in the gateway container's
mounted `OPENCLAW_CONFIG_DIR/bin/`): the script itself; survives
gateway recreate because of the bind-mount.

## Limitations

- **Operator-only** — the bash command surface is locked down to one
  Discord user id. Adding more is a two-line edit to
  `commands.ownerAllowFrom` and `tools.elevated.allowFrom.discord`,
  then `openclaw config set` + gateway recreate.
- **Discord 2000-char limit** — the URL + embed line is ~280 chars,
  well under the limit. If you ever pipe long script output to
  Discord, expect truncation.
- **Render time vs. Discord interaction window** — the script blocks
  the OpenClaw runtime for the duration of the render (~50s-3min).
  Other operator commands have to wait. Concurrent renders aren't
  supported (`IMAGE_GEN_MAX_CONCURRENCY=1` on the bridge).
- **Operator's Discord ID is hardcoded** — if the operator switches
  Discord accounts, the allowlists need updating (see "Configuration
  footprint" above).
