#!/usr/bin/env python3
"""claw-img — a tiny standalone Discord bot that exposes a real `/claw-img`
slash command and generates images by calling the comfyui_image bridge
DIRECTLY (the same MCP endpoint the OpenClaw gateway uses).

Why a separate bot instead of OpenClaw's own command surface: OpenClaw routes
every slash/mention message through the agent LLM (whose RLHF refuses adult
prompts), and its only LLM-bypass path (`commands.bash`) is arbitrary-shell,
approval-gated, and not a real Discord slash command. This bot sidesteps all of
that: the slash command runs deterministic code, never touches the LLM, never
asks for approval, and uploads the result as a true Discord attachment.

It needs only outbound access (to Discord's gateway + the bridge on the compose
network) — no inbound ports. One env var holds its own bot token; it shares the
bridge's IMAGE_GEN_API_TOKEN to authenticate to the MCP endpoint.
"""

import asyncio
import base64
import io
import json
import logging
import os

import aiohttp
import discord
from discord import app_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("claw-img")

TOKEN = os.environ.get("CLAW_IMG_DISCORD_TOKEN", "").strip()
GUILD_ID = os.environ.get("CLAW_IMG_GUILD_ID", "").strip()
BRIDGE_URL = os.environ.get("IMAGE_GEN_URL", "http://openclaw-image-comfyui:9095/mcp")
BRIDGE_TOKEN = os.environ.get("IMAGE_GEN_API_TOKEN", "").strip()
# Default workflow for a bare prompt. Set CLAW_IMG_DEFAULT_WORKFLOW to an adult
# workflow (e.g. flux-krea-2k-adult) so /claw-img is NSFW by default; the `safe`
# option then forces the SFW workflow per call.
DEFAULT_WORKFLOW = os.environ.get("CLAW_IMG_DEFAULT_WORKFLOW", "flux-krea-2k").strip()
SFW_WORKFLOW = os.environ.get("CLAW_IMG_SFW_WORKFLOW", "flux-krea-2k").strip()
# ~9 MiB keeps us under Discord's 10 MiB non-boosted upload cap; over this the
# bot posts the public link instead of an attachment.
MAX_BYTES = int(os.environ.get("CLAW_IMG_MAX_BYTES", "9437184"))
TIMEOUT_S = float(os.environ.get("CLAW_IMG_TIMEOUT_S", "600"))
# Video renders are slower (LTX cold cache 3-10 min) and the mp4 is bigger.
VIDEO_TIMEOUT_S = float(os.environ.get("CLAW_VIDEO_TIMEOUT_S", "900"))
# Max seconds of video (the bridge also caps via LTX_VIDEO_MAX_DURATION_S).
VIDEO_MAX_SECONDS = float(os.environ.get("CLAW_VIDEO_MAX_SECONDS", "10"))

# Resolution presets (explicit dims). 4K is deliberately omitted — a 3840px
# FLUX render peaks ~100+ GB on this box and has livelocked the host; set
# width/height by hand if you really need it.
PRESETS = {
    "square": (1024, 1024),
    "portrait": (768, 1280),
    "landscape": (1280, 768),
    "hd": (1280, 720),
    "fullhd": (1920, 1088),
    "2k": (2048, 2048),
}

# Video presets — LTX dims must be divisible by 32 and stay within the bridge's
# max (1920×1088). No 2K/4K for video.
VIDEO_PRESETS = {
    "square": (1024, 1024),
    "portrait": (704, 1280),
    "landscape": (1280, 704),
    "fullhd": (1920, 1088),
}


def round32(n: int) -> int:
    """LTX requires width/height divisible by 32; round custom dims to the
    nearest multiple so a hand-typed value doesn't get silently dropped."""
    return max(32, int(round(n / 32)) * 32)

if not TOKEN:
    raise SystemExit("CLAW_IMG_DISCORD_TOKEN is not set — nothing to run.")
if not BRIDGE_TOKEN:
    raise SystemExit("IMAGE_GEN_API_TOKEN is not set — the bridge would reject every call.")

# Slash commands need no PRIVILEGED intents, but discord.py warns (and can hit
# guild-state issues) with Intents.none(); the non-privileged default set
# includes the guilds intent and is the standard for a slash-only bot.
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def call_bridge(arguments: dict, tool: str = "generate", timeout_s: float = TIMEOUT_S) -> dict:
    """POST a single JSON-RPC tools/call to the bridge and return the parsed
    JSON-RPC envelope. The bridge's /mcp endpoint answers a lone request with
    plain JSON (no SSE), so a normal POST + .json() is enough."""
    headers = {
        "Authorization": "Bearer " + BRIDGE_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(BRIDGE_URL, json=body, headers=headers) as resp:
            return await resp.json()


def pick_media(data: dict) -> dict:
    """Return the first generated media entry — videos take precedence over
    images so /claw-video grabs the mp4."""
    for key in ("videos", "images"):
        arr = data.get(key) or []
        if arr:
            return arr[0]
    return {}


def extract_result(envelope: dict) -> dict:
    """Pull the bridge's tool-result dict out of the JSON-RPC content array."""
    if not isinstance(envelope, dict):
        return {"error": "bad-response", "message": "non-JSON bridge response"}
    if envelope.get("error"):
        err = envelope["error"]
        return {"error": "rpc", "message": err.get("message") or json.dumps(err)}
    content = ((envelope.get("result") or {}).get("content")) or []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except Exception:  # noqa: BLE001
                pass
    return {"error": "no-text", "message": "no text content in bridge result"}


@tree.command(name="claw-img", description="Generate an image (direct, no chat LLM in the loop).")
@app_commands.describe(
    prompt="What the image should depict.",
    negative="What to avoid / keep out of the image.",
    resolution="Output size preset (width/height below override it).",
    width="Custom width in px (overrides the preset).",
    height="Custom height in px (overrides the preset).",
    steps="Sampler steps — higher = more detail/slower (omit = workflow default).",
    cfg="Guidance scale — how strictly to follow the prompt (omit = workflow default).",
    seed="RNG seed for reproducibility (omit = random).",
    safe="Force the SFW workflow for this one call.",
)
@app_commands.choices(
    resolution=[app_commands.Choice(name=k, value=k) for k in PRESETS]
)
async def claw_img(
    interaction: discord.Interaction,
    prompt: str,
    negative: str | None = None,
    resolution: app_commands.Choice[str] | None = None,
    width: int | None = None,
    height: int | None = None,
    steps: int | None = None,
    cfg: float | None = None,
    seed: int | None = None,
    safe: bool = False,
):
    # Ack within Discord's 3 s window; the render takes ~50-100 s and we have
    # up to 15 min to send the follow-up.
    await interaction.response.defer(thinking=True)

    workflow = SFW_WORKFLOW if safe else DEFAULT_WORKFLOW
    args = {
        "prompt": prompt,
        "workflow": workflow,
        "include_base64": True,
        "attach_image_content": False,
    }
    if negative:
        args["negative"] = negative
    if resolution is not None:
        args["width"], args["height"] = PRESETS[resolution.value]
    if width:
        args["width"] = width
    if height:
        args["height"] = height
    if steps:
        args["steps"] = steps
    if cfg is not None:
        args["cfg"] = cfg
    if seed is not None:
        args["seed"] = seed

    try:
        envelope = await call_bridge(args)
    except asyncio.TimeoutError:
        await interaction.followup.send("⌛ image-gen timed out — try again or a smaller size.")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("bridge call failed")
        await interaction.followup.send(f"image-gen request failed: {e}")
        return

    data = extract_result(envelope)
    if data.get("error"):
        await interaction.followup.send(f"image-gen error: {data.get('message', data['error'])}")
        return

    images = data.get("images") or []
    img = images[0] if images else {}
    summary = (
        f"`{data.get('workflow_used', workflow)}` "
        f"{img.get('width', '?')}x{img.get('height', '?')} — "
        f"{data.get('elapsed_s', '?')}s, seed {data.get('seed_used', '?')}"
    )

    b64 = img.get("base64")
    if not b64:
        # No bytes came back — fall back to whatever link the bridge built.
        link = data.get("display_markdown") or "(no image returned)"
        await interaction.followup.send(f"{summary}\n{link}")
        return

    raw = base64.b64decode(b64)
    filename = img.get("filename") or "image.png"
    if len(raw) <= MAX_BYTES:
        await interaction.followup.send(
            content=summary,
            file=discord.File(io.BytesIO(raw), filename=filename),
        )
    else:
        link = data.get("display_markdown") or ""
        await interaction.followup.send(
            f"{summary}\n(image is {len(raw) // 1024} KiB — over the attachment cap, linking instead)\n{link}"
        )


@claw_img.error
async def claw_img_error(interaction: discord.Interaction, error: Exception):
    log.exception("claw-img command error: %s", error)
    msg = f"⚠️ command failed: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:  # noqa: BLE001
        pass


@tree.command(name="claw-video", description="Generate a short video (LTX-Video 2.3, direct — no chat LLM).")
@app_commands.describe(
    prompt="What the video should depict (and, if audio on, sound like).",
    image="Optional source image to animate (image-to-video).",
    negative="What to avoid / keep out of the video.",
    resolution="Output size preset (width/height below override it).",
    width="Custom width px — rounded to a multiple of 32 (pair with height).",
    height="Custom height px — rounded to a multiple of 32 (pair with width).",
    seconds=f"Clip length in seconds (max {int(VIDEO_MAX_SECONDS)}; omit = workflow default).",
    fps="Frames per second (default 24).",
    audio="Generate + mux an audio track (default on).",
    seed="RNG seed (omit = random).",
)
@app_commands.choices(
    resolution=[app_commands.Choice(name=k, value=k) for k in VIDEO_PRESETS]
)
async def claw_video(
    interaction: discord.Interaction,
    prompt: str,
    image: discord.Attachment | None = None,
    negative: str | None = None,
    resolution: app_commands.Choice[str] | None = None,
    width: int | None = None,
    height: int | None = None,
    seconds: float | None = None,
    fps: int | None = None,
    audio: bool | None = None,
    seed: int | None = None,
):
    await interaction.response.defer(thinking=True)

    args = {
        "prompt": prompt,
        "include_base64": True,
        "attach_image_content": False,
        # Keep the bridge's own timeout just under ours so it errors first
        # with a clean message rather than us aborting the socket.
        "timeout_s": max(60.0, VIDEO_TIMEOUT_S - 30),
    }
    if negative:
        args["negative"] = negative
    if resolution is not None:
        args["width"], args["height"] = VIDEO_PRESETS[resolution.value]
    if width:
        args["width"] = round32(width)
    if height:
        args["height"] = round32(height)
    fps_eff = fps or 24
    if fps:
        args["fps"] = fps
    if seconds is not None:
        clip = max(0.5, min(seconds, VIDEO_MAX_SECONDS))
        args["length"] = max(1, round(clip * fps_eff))
    if audio is not None:
        args["audio_enabled"] = audio
    if seed is not None:
        args["seed"] = seed

    # Image-to-video: download the attachment and pass it as base64. KozelBot's
    # uploads don't land in the bridge's media dir, so the filesystem-path route
    # isn't available — base64 is the portable input. The bridge then auto-picks
    # the ltx-2.3-i2v workflow.
    if image is not None:
        if not (image.content_type or "").startswith("image/"):
            await interaction.followup.send("the `image` attachment must be an image (png/jpg/webp).")
            return
        raw_img = await image.read()
        args["init_image_base64"] = base64.b64encode(raw_img).decode("ascii")

    try:
        envelope = await call_bridge(args, tool="generate_video", timeout_s=VIDEO_TIMEOUT_S)
    except asyncio.TimeoutError:
        await interaction.followup.send("⌛ video render timed out — try fewer seconds or a smaller size.")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("video bridge call failed")
        await interaction.followup.send(f"video request failed: {e}")
        return

    data = extract_result(envelope)
    if data.get("error"):
        await interaction.followup.send(f"video error: {data.get('message', data['error'])}")
        return

    media = pick_media(data)
    summary = (
        f"`{data.get('workflow_used', 'ltx-2.3')}` "
        f"{media.get('width', '?')}x{media.get('height', '?')} — "
        f"{media.get('duration_s', '?')}s @ {media.get('fps', '?')}fps, "
        f"audio {'on' if media.get('audio_enabled') else 'off'}, "
        f"seed {data.get('seed_used', '?')} (rendered {data.get('elapsed_s', '?')}s)"
    )

    b64 = media.get("base64")
    if not b64:
        await interaction.followup.send(f"{summary}\n{data.get('display_markdown', '(no video returned)')}")
        return

    raw = base64.b64decode(b64)
    filename = media.get("filename") or "video.mp4"
    if len(raw) <= MAX_BYTES:
        await interaction.followup.send(content=summary, file=discord.File(io.BytesIO(raw), filename=filename))
    else:
        await interaction.followup.send(
            f"{summary}\n(video is {len(raw) // 1024} KiB — over the attachment cap, linking instead)\n"
            f"{data.get('display_markdown', '')}"
        )


@claw_video.error
async def claw_video_error(interaction: discord.Interaction, error: Exception):
    log.exception("claw-video command error: %s", error)
    msg = f"⚠️ command failed: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:  # noqa: BLE001
        pass


@tree.command(name="claw-help", description="How /claw-img and /claw-video work — what each option does.")
async def claw_help(interaction: discord.Interaction):
    # Instant, no bridge call — just an ephemeral formatted cheatsheet. Preset
    # lists are built from the constants so they never drift from the commands.
    embed = discord.Embed(
        title="🎨 KozelBot — image & video generation",
        description="Two slash commands, no chat LLM in the loop. Fill **prompt** and go — everything else is optional.",
        color=0x5865F2,
    )
    embed.add_field(
        name="🖼 /claw-img",
        value=(
            "• **prompt** — what to draw *(required)*\n"
            "• **negative** — what to keep out\n"
            f"• **resolution** — `{' / '.join(PRESETS)}`\n"
            "• **width / height** — custom px (overrides resolution)\n"
            "• **steps** — more detail vs faster *(default: workflow)*\n"
            "• **cfg** — how strictly to follow the prompt\n"
            "• **seed** — reproduce a result\n"
            "• **safe** — `true` = SFW for this one call"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎬 /claw-video",
        value=(
            "• **prompt** — what the clip shows *(required)*\n"
            "• **image** — attach a picture to **animate it** (image→video)\n"
            "• **negative** — what to keep out\n"
            f"• **resolution** — `{' / '.join(VIDEO_PRESETS)}`\n"
            "• **width / height** — custom px (rounded to ÷32)\n"
            f"• **seconds** — clip length (max {int(VIDEO_MAX_SECONDS)})\n"
            "• **fps** — frames per second *(default 24)*\n"
            "• **audio** — generate a sound track *(default on)*\n"
            "• **seed** — reproduce a result"
        ),
        inline=False,
    )
    embed.set_footer(text="Renders take ~1 min (image) to a few min (video) — just wait for the result.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def sync_to_guild(guild: discord.abc.Snowflake):
    """Register the slash commands on one guild — instant, unlike global sync."""
    tree.copy_global_to(guild=guild)
    synced = await tree.sync(guild=guild)
    log.info("synced %d command(s) to guild %s", len(synced), getattr(guild, "id", guild))


@client.event
async def on_ready():
    # No CLAW_IMG_GUILD_ID → "works on any server it's invited to": register on
    # every guild the bot is currently in (instant), and on_guild_join covers
    # servers invited later. A single CLAW_IMG_GUILD_ID still works if set.
    try:
        if GUILD_ID:
            await sync_to_guild(discord.Object(id=int(GUILD_ID)))
        else:
            for g in client.guilds:
                await sync_to_guild(g)
            log.info("registered /claw-img on %d current guild(s)", len(client.guilds))
    except Exception:  # noqa: BLE001
        log.exception("command sync failed")
    log.info("claw-img bot ready as %s (bridge=%s)", client.user, BRIDGE_URL)


@client.event
async def on_guild_join(guild: discord.Guild):
    # Auto-register the command when the bot is invited to a new server, so the
    # operator doesn't have to restart the bot or wait for global propagation.
    try:
        await sync_to_guild(guild)
    except Exception:  # noqa: BLE001
        log.exception("guild-join sync failed for %s", guild.id)


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
