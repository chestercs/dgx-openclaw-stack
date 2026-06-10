"""Image-generation MCP bridge — MCP Streamable-HTTP server.

Speaks the Model Context Protocol wire format (JSON-RPC 2.0 over HTTP)
on POST /mcp, exposing three tools that the OpenClaw agent can call:

  - comfyui_image__generate(prompt, workflow="flux-schnell", width, height,
                            steps, cfg, seed, negative, checkpoint, sampler,
                            scheduler, batch_size, timeout_s)
        Submit a workflow to the operator's ComfyUI install, poll until the
        image renders, and return one or more base64-encoded PNGs.
  - comfyui_image__list_workflows()
        Return the workflows shipped under workflows/ with their tunable
        parameter list — saves the agent a round-trip when it needs to
        pick one.
  - comfyui_image__cancel(prompt_id)
        Best-effort cancel of an in-flight prompt by id.

Auth: Authorization: Bearer ${IMAGE_GEN_API_TOKEN} on every POST. The
/healthz endpoint is intentionally unauth'd so docker healthchecks don't
need the token.

Why we hand-roll the MCP wire instead of pulling the `mcp` SDK: same
rationale as the python-sandbox sibling — one Streamable-HTTP POST
handler plus a JSON-RPC dispatch is ~250 LOC; the SDK has churned
across 1.x; freezing the protocol shape inside this file is more honest
about the contract OpenClaw will actually see.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import secrets
import time
import uuid
from io import BytesIO
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image

from comfy_client import (
    ComfyClient,
    ComfyUIError,
    ComfyUIRestartedError,
    ComfyUITimeout,
    extract_media_outputs,
)
from workflow_loader import WorkflowError, WorkflowLoader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("image-bridge")


# ── Config ────────────────────────────────────────────────────────────
TOKEN = os.environ.get("IMAGE_GEN_API_TOKEN", "").strip()
if not TOKEN:
    # Fail fast: the bridge talks to a no-auth ComfyUI on the host gateway,
    # so a token-less /mcp would let any container on the bridge network
    # drive arbitrary generation. The opt-in posture is token-gated.
    raise RuntimeError("IMAGE_GEN_API_TOKEN must be set (bridge refuses to start without auth).")

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:13036").rstrip("/")
# COMFYUI_EXTERNAL_URL is the host-browser-reachable URL — used to build the
# `display_markdown` field that the agent can paste into its reply so the
# chat surface renders the image inline. `host.docker.internal` is a
# container-only hostname and does NOT resolve from the operator's browser,
# so the operator typically sets this to the host's LAN IP, e.g.
# `http://192.168.x.x:13036`. Defaults to COMFYUI_URL if unset, which
# means the markdown will only work from inside the docker bridge — set
# the env var explicitly for chat rendering to work.
COMFYUI_EXTERNAL_URL = os.environ.get("COMFYUI_EXTERNAL_URL", COMFYUI_URL).rstrip("/")
# COMFYUI_VIEW_TOKEN — when set, the bridge appends `?token=<value>` to the
# fetch URL it embeds in `display_markdown`. Pair with a token-validation
# block on your reverse-proxy (NGINX `if ($arg_token != "...")`) and you can
# drop HTTP Basic auth there: the `?token=` URL param works on direct
# navigation AND on cross-origin `<img>` tags (browsers always send query
# strings; HTTP Basic auth headers don't survive cross-origin <img> fetches).
# See the bridge README "Token-protected proxy (alternative to Basic auth)"
# section for the exact NGINX config.
COMFYUI_VIEW_TOKEN = os.environ.get("COMFYUI_VIEW_TOKEN", "").strip()
DEFAULT_TIMEOUT_S = float(os.environ.get("IMAGE_GEN_TIMEOUT_S", "600"))
MAX_OUTPUT_BYTES = int(os.environ.get("IMAGE_GEN_MAX_OUTPUT_BYTES", str(50 * 1024 * 1024)))
MAX_CONCURRENCY = int(os.environ.get("IMAGE_GEN_MAX_CONCURRENCY", "1"))
POLL_INTERVAL_S = float(os.environ.get("IMAGE_GEN_POLL_INTERVAL_S", "0.5"))
POLL_BACKOFF_MAX_S = float(os.environ.get("IMAGE_GEN_POLL_BACKOFF_MAX_S", "2.0"))

WORKFLOWS_DIR = os.environ.get("IMAGE_GEN_WORKFLOWS_DIR", "/app/workflows")

# Path A: same-origin chat-side image rendering via the [embed] shortcode.
# When set, the bridge mirrors each generated image into this directory
# and emits `[embed url="/__openclaw__/canvas/<file>" /]` in
# display_markdown — letting the OpenClaw web chat render inline via the
# parser-validated same-origin canvas route. The chat normalizer extracts
# the embed directive BEFORE DOMPurify runs, so the shortcode bypasses
# the <img> sanitizer entirely.
#
# The path is the IN-CONTAINER path; bind it to the gateway's host-side
# canvas dir (typically `${OPENCLAW_CONFIG_DIR}/canvas` — verify against
# your deploy via the SSH probe in docs/reference/image-comfyui-bridge.md).
# Leave empty to keep the legacy display_markdown emission (cross-origin
# <img> markdown + autolinked plain URL — works on Discord and on direct
# navigation with cached Basic auth, but not inline in webchat).
IMAGE_GEN_CANVAS_DIR = os.environ.get("IMAGE_GEN_CANVAS_DIR", "").strip().rstrip("/")

# Sensible-defaults env knobs (v0.10.5) — let the agent succeed with just
# `comfyui_image__generate(prompt="...")` instead of having to remember
# the workflow + checkpoint name on every call. When the caller omits one
# of these args, the bridge falls back to these env values; if the env
# value is also empty, the bridge surfaces the original parameter-required
# error so the operator sees what's missing.
IMAGE_GEN_DEFAULT_WORKFLOW = os.environ.get("IMAGE_GEN_DEFAULT_WORKFLOW", "").strip()
IMAGE_GEN_DEFAULT_CHECKPOINT = os.environ.get("IMAGE_GEN_DEFAULT_CHECKPOINT", "").strip()

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "openclaw-image-comfyui", "version": "0.12.4"}

# LTX-Video 2.3 knobs — only used when the operator has enabled the
# video tool surface. Defaults survive a fresh install where the
# operator hasn't yet run scripts/install-ltx-video.sh. The actual
# tool registration is unconditional (the bridge always advertises
# generate_video), but a workflow-not-found error surfaces cleanly
# the first time a caller tries to use it without the workflows in
# place.
LTX_VIDEO_DEFAULT_LENGTH_FRAMES = int(os.environ.get("LTX_VIDEO_DEFAULT_LENGTH_FRAMES", "145"))
LTX_VIDEO_DEFAULT_FPS = int(os.environ.get("LTX_VIDEO_DEFAULT_FPS", "24"))
# `on` / `off` — controls the default audio state when the caller doesn't
# pass audio_enabled. Workflows that don't support disabling audio (no
# audio-disable target declared) ignore this knob.
LTX_VIDEO_DEFAULT_AUDIO = os.environ.get("LTX_VIDEO_DEFAULT_AUDIO", "on").strip().lower()
# Hard ceiling on video duration — guards against an agent asking for
# 60-second clips that take 30+ minutes and blow past Discord's
# auto-embed size cap. Bridge enforces seconds = length / fps <= cap.
LTX_VIDEO_MAX_DURATION_S = float(os.environ.get("LTX_VIDEO_MAX_DURATION_S", "10"))
# Hard ceiling on video resolution. The LTX-Video 2.3 22B distilled
# checkpoint is trained around 1280×720 / 1216×704; pushing past FullHD
# (1920×1088) the sampler does not converge and the job runs indefinitely
# at 96% GPU until manually killed — verified in production 2026-06-06,
# where a `4k`/`uhd` keyword in the prompt resolved to 3840×2176 via
# RESOLUTION_ALIASES and locked the discord-friend session queue. The
# default cap matches LTX's documented native ceiling. Operators on
# fine-tuned higher-res checkpoints can raise it via env.
LTX_VIDEO_MAX_WIDTH = int(os.environ.get("LTX_VIDEO_MAX_WIDTH", "1920"))
LTX_VIDEO_MAX_HEIGHT = int(os.environ.get("LTX_VIDEO_MAX_HEIGHT", "1088"))


# ── Proxy-side resolution resolver ────────────────────────────────────
#
# Gemma 4 has a persistent failure mode: when the user asks for a non-
# default resolution by name ("FullHD", "1080p", "négyzet"), the agent
# transcribes the keyword into the prompt text correctly but emits the
# tool call with only one of `width` / `height` set (verified live
# 2026-05-14: "fullhd változatot" → tool args `{width: 1920}` with
# `height` unset, producing 1920×576 ultra-wide instead of 1920×1088).
#
# Earlier attempts to fix this in the agent layer (cheatsheet rules,
# stronger inputSchema descriptions, MCP-error reject) all failed:
#   - v0.12.0: cheatsheet table — Gemma ignored it
#   - v0.12.1: hard reject (MCP -32602) — openclaw retry loop blew up
#   - v0.12.2: silent default fallback — user got 1024×576 when asking
#              for FullHD (semantically wrong, just non-broken)
#
# v0.12.3 path: proxy-side resolution from the prompt text itself. The
# prompt is the ONE field the agent reliably gets right — it's the
# user's natural-language ask copied verbatim. We parse the prompt for
# either explicit AxB notation (e.g. "1920x1088") or a known resolution
# keyword and use the resulting (width, height) pair when the agent
# didn't send both dims explicitly. This is NOT auto-derive (we don't
# infer the missing dim from the supplied one — which depends on an
# unknown target aspect ratio); it's a deterministic, documented
# keyword→(w,h) lookup table that translates user intent to dims.
#
# Tuple order matters: most-specific patterns FIRST so e.g. "full hd"
# wins over the substring "hd". Regex word boundaries (`\b`) prevent
# false positives ("read" matching against "hd").
RESOLUTION_AXB_RE = re.compile(r'\b(\d{3,4})\s*[x×*]\s*(\d{3,4})\b', re.IGNORECASE)
RESOLUTION_ALIASES: list[tuple[str, tuple[int, int]]] = [
    # (regex pattern, (width, height))
    # FullHD — pixel count fits on GB10 with ~270s render.
    (r"\bfull[\s\-]?hd\b",                  (1920, 1088)),
    (r"\bfhd\b",                            (1920, 1088)),
    (r"\b1080p\b",                          (1920, 1088)),
    # 4K — ~115 GB peak, may OOM on busy stacks. Documented limit.
    (r"\b4k\b",                             (3840, 2176)),
    (r"\bultra[\s\-]?hd\b",                 (3840, 2176)),
    (r"\buhd\b",                            (3840, 2176)),
    (r"\b2160p\b",                          (3840, 2176)),
    # 1440p
    (r"\bqhd\b",                            (2560, 1440)),
    (r"\b1440p\b",                          (2560, 1440)),
    # 720p HD
    (r"\b720p\b",                           (1280, 704)),
    (r"\bhd\b",                             (1280, 704)),   # last among hd-bearing
    # MiniHD (the default, but matches if a user explicitly asks for it)
    (r"\bmini[\s\-]?hd\b",                  (1024, 576)),
    # Aspect/shape keywords (Hungarian + English).
    (r"\b(négyzet|square|kocka)\b",         (1024, 1024)),
    (r"\b(portrait|függőleges|álló)\b",     (768, 1024)),
    (r"\b(landscape|fekvő|szélesvásznú)\b", (1280, 704)),
]


def _resolve_dims_from_prompt(prompt: str) -> Optional[tuple[int, int]]:
    """Parse `prompt` for an explicit AxB token or a known resolution
    keyword. Returns the matching (width, height) pair or None if
    nothing matched. AxB is tried first because it's the most specific
    signal: a prompt containing "1920x1088" is unambiguous regardless
    of any other keywords also present (e.g. "FullHD (1920x1088)").
    """
    if not prompt:
        return None
    m = RESOLUTION_AXB_RE.search(prompt)
    if m:
        try:
            w, h = int(m.group(1)), int(m.group(2))
        except ValueError:
            w = h = 0
        # Sanity-check the AxB hit — single-digit-range numbers like
        # "10x10" come from frame counts, FPS, etc. and must not
        # become render dims. Require both >= 256 (smallest sane LTX
        # latent) and <= 4096 (above this VRAM blows up regardless).
        if 256 <= w <= 4096 and 256 <= h <= 4096:
            return (w, h)
    p = prompt.lower()
    for pattern, dims in RESOLUTION_ALIASES:
        if re.search(pattern, p, re.IGNORECASE):
            return dims
    return None


TOOLS = [
    {
        # Bare tool names — OpenClaw gateway prefixes them with the server name
        # (`comfyui_image`) before surfacing in the agent catalog. The
        # python-sandbox sibling does the same (`python_exec`, not
        # `python_sandbox__python_exec`); pre-prefixing here would result in
        # double-prefixed names like `comfyui_image__comfyui_image__generate`
        # in the agent's tool catalog.
        "name": "generate",
        "description": (
            "Generate one or more images via the operator's ComfyUI install. "
            "Returns metadata (prompt_id, workflow, seed, elapsed_s, per-image "
            "filename + size + width/height) PLUS a `display_markdown` field "
            "containing the chat-renderable image markup.\n\n"
            "MANDATORY OUTPUT CONTRACT — your reply MUST start with the EXACT "
            "verbatim contents of the `display_markdown` field, INCLUDING ALL "
            "LINES of it (it usually contains a public image URL line AND a "
            "[embed] shortcode line — paste BOTH, separated by the blank line "
            "as in the field value). Copy character-for-character: do not "
            "edit, do not rewrap, do not wrap in code fences, do not translate, "
            "do not summarize, do not skip lines. Add your Hungarian or English "
            "commentary AFTER the entire paste on a new line. If you skip any "
            "part of the paste the user will see ZERO image on at least one "
            "surface (Discord needs the URL line, web-chat needs the [embed] "
            "line). Describing the image in your own words is NOT a "
            "substitute for the paste; the user already asked for an IMAGE."
            "\n\n"
            "The PNG bytes themselves are NOT in the response by default — "
            "embedding ~200 KB of base64 per image would balloon the agent's "
            "context (50K+ tokens) and 5-10× the wall clock for the next "
            "LLM call's prefill on Gemma 4 NVFP4. The chat surface fetches "
            "the actual PNG from the operator's ComfyUI install via the URL "
            "the markdown points at. Pass `include_base64=true` only when "
            "you genuinely need the bytes inside the agent reply (rare)."
            "\n\n"
            "`workflow` selects a JSON template under workflows/; the bridge "
            "binds your params (prompt, dimensions, sampler, seed, ...) to "
            "the template's tunable nodes and submits to ComfyUI's queue. "
            "Pass `checkpoint` to override the model name (must exist under "
            "ComfyUI's basedir/models/checkpoints/). Use comfyui_image__list_"
            "workflows first if you don't know which workflow to pick."
            "\n\n"
            "Workflow picker quick-guide for the v0.11.0 FLUX-Krea-dev bundle "
            "(skip if your deploy hasn't installed the recommended models):\n"
            "  - SFW DEFAULT: `flux-krea-2k` at 1280x720 (16:9, ~30s warm). "
            "Pass `width=2048, height=2048` for 2K square (~1 min warm). Any "
            "res 256-2048 works (the targets cover EmptyLatent + "
            "ModelSamplingFlux so sigma scheduling stays correct).\n"
            "  - Adult/NSFW: `flux-krea-2k-adult` — same pipeline + "
            "flux-uncensored-v2 LoRA. Use when the user requests explicit "
            "content; the deploy is private and the LoRA bundle is in scope.\n"
            "  - Anything else / non-FLUX checkpoint: `sdxl-base` or "
            "`flux-schnell`. (The earlier 4K-via-UltimateSDUpscale variants "
            "were removed — they produced tile artifacts on FLUX latents; "
            "use 2K + external ESRGAN if 4K is needed.)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt":     {"type": "string", "description": "Positive prompt — what the image should depict."},
                "workflow":   {"type": "string", "description": "Workflow template name (default 'flux-schnell').", "default": "flux-schnell"},
                "width":      {"type": "integer", "description": "Output width in pixels.", "default": 1024},
                "height":     {"type": "integer", "description": "Output height in pixels.", "default": 1024},
                "steps":      {"type": "integer", "description": "Sampler steps (workflow default if omitted)."},
                "cfg":        {"type": "number",  "description": "Classifier-free guidance scale (workflow default if omitted)."},
                "seed":       {"type": "integer", "description": "RNG seed. -1 (default) mints a random one."},
                "negative":   {"type": "string",  "description": "Negative prompt (skipped silently if the workflow has no negative slot)."},
                "checkpoint": {"type": "string",  "description": "Checkpoint filename inside basedir/models/checkpoints/. Required if the workflow has REPLACE_ME placeholder."},
                "sampler":    {"type": "string",  "description": "KSampler `sampler_name` override."},
                "scheduler":  {"type": "string",  "description": "KSampler `scheduler` override."},
                "batch_size": {"type": "integer", "description": "Number of images per call (default 1)."},
                "timeout_s":  {"type": "number",  "description": f"Max wall-clock seconds to wait for the render. Default {DEFAULT_TIMEOUT_S:.0f}."},
                "include_base64": {"type": "boolean", "description": "Embed the PNG bytes as base64 in the response's text content. Default false (returns metadata only — keeps the agent's context light). Set true only when you need the bytes inside the agent reply.", "default": False},
                "attach_image_content": {"type": "boolean", "description": "Add the PNG bytes as MCP `image` content items alongside the metadata text content. Default true — chat surfaces that honor the MCP image content type (OpenClaw web/control UI) render the image inline; clients that ignore it lose nothing. Disable if your chat surface mistakenly prefills image content into the LLM context.", "default": True},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_workflows",
        "description": (
            "List the workflow templates the bridge ships, with their "
            "tunable parameters and defaults. Use this to discover which "
            "workflow fits the request before calling generate."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "generate_video",
        "description": (
            "Generate a short video clip (4-10 seconds, with native audio) "
            "via the operator's ComfyUI install running LTX-Video 2.3. "
            "Returns metadata (prompt_id, workflow, seed, elapsed_s, "
            "per-video filename + size + width/height/duration) PLUS a "
            "`display_markdown` field containing the chat-renderable URL "
            "(Discord auto-embeds mp4 URLs inline; web chat surfaces the "
            "URL as a plain link).\n\n"
            "MANDATORY OUTPUT CONTRACT — your reply MUST start with the "
            "EXACT verbatim contents of the `display_markdown` field, "
            "INCLUDING ALL LINES of it. Copy character-for-character: do "
            "not rewrap, do not wrap in code fences, do not summarize. "
            "Add your Hungarian or English commentary AFTER the entire "
            "paste on a new line. If you skip the URL line the user "
            "sees ZERO video — describing what you generated in prose "
            "is NOT a substitute.\n\n"
            "Modes — pick by what the caller supplied:\n"
            "  - TEXT-TO-VIDEO (T2V): pass only `prompt` (plus optional "
            "knobs). Workflow defaults to `ltx-2.3-t2v` (set via "
            "LTX_VIDEO_DEFAULT_WORKFLOW or per-call workflow=).\n"
            "  - IMAGE-TO-VIDEO (I2V): pass `prompt` AND ONE of "
            "`init_image_url` (an http(s) URL the bridge fetches) or "
            "`init_image_base64` (raw base64 of the source frame). "
            "Workflow auto-switches to `ltx-2.3-i2v` if either is "
            "present and the caller didn't override `workflow=`.\n\n"
            "Length is measured in frames; at the default 24 fps a 96-"
            "frame clip is 4 seconds. The hard ceiling "
            "(LTX_VIDEO_MAX_DURATION_S) protects against 60+ second "
            "renders that blow past Discord's auto-embed cap (~50 MB) "
            "and the agent's per-tool timeout. Bump timeout_s for any "
            "call past the default — cold-cache renders take 3-10 "
            "minutes on first call after a stack restart."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt":     {"type": "string", "description": "Positive prompt — what the video should depict and (if audio enabled) what should be audible."},
                "workflow":   {"type": "string", "description": "Workflow template name. Default auto-picks ltx-2.3-i2v when init_image_* is set, otherwise ltx-2.3-t2v."},
                "negative":   {"type": "string", "description": "Negative prompt (skipped silently if the workflow has no negative slot)."},
                "resolution": {"type": "string", "description": "RECOMMENDED for resolution intent. User-friendly resolution name OR explicit AxB. Examples: 'fullhd' / '1080p' / 'fhd' → 1920×1088; '4k' / 'uhd' / '2160p' → 3840×2176; 'hd' / '720p' → 1280×704; 'mini-hd' → 1024×576 (default 16:9); 'square' / 'négyzet' → 1024×1024; 'portrait' / 'függőleges' → 768×1024; 'landscape' / 'fekvő' → 1280×704; OR explicit dims like '1024x1024', '1920x1088'. When the USER mentions a resolution by name (e.g. asks for fullhd / 1080p / square / portrait), pass that EXACT keyword here — this is the most reliable way to convey resolution intent and is preserved even if you rewrite the prompt text. Explicit width+height args still win over this if both are set."},
                "width":      {"type": "integer", "description": "Output width in pixels. Optional — prefer `resolution` arg when the user names a known resolution. Default 1024 (paired with height 576 for 16:9 MiniHD). MUST be divisible by 32. If you set width, ALSO set height — single-dim calls fall back to defaults."},
                "height":     {"type": "integer", "description": "Output height in pixels. Optional — see `resolution` and `width` args. Default 576. MUST be divisible by 32. Always paired with width."},
                "length":     {"type": "integer", "description": f"Number of frames. Default {LTX_VIDEO_DEFAULT_LENGTH_FRAMES} (= 4 s @ {LTX_VIDEO_DEFAULT_FPS} fps)."},
                "fps":        {"type": "integer", "description": f"Frames per second. Default {LTX_VIDEO_DEFAULT_FPS}."},
                "audio_enabled": {"type": "boolean", "description": f"Mux LTX-2.3's generated audio track into the mp4. Default {LTX_VIDEO_DEFAULT_AUDIO} (set via LTX_VIDEO_DEFAULT_AUDIO env)."},
                "seed":       {"type": "integer", "description": "RNG seed. -1 (default) mints a random one."},
                "steps":      {"type": "integer", "description": "Sampler steps (workflow default if omitted; distilled checkpoints need fewer)."},
                "cfg":        {"type": "number",  "description": "Classifier-free guidance scale (workflow default if omitted)."},
                "checkpoint": {"type": "string",  "description": "Checkpoint filename inside basedir/models/checkpoints/."},
                "init_image_url":    {"type": "string", "description": "I2V: source frame for image-to-video. Accepts THREE shapes: (a) an `http(s)://` URL the bridge fetches; (b) a `file://` URL pointing at a path the bridge can see; (c) a raw filesystem PATH starting with `/` — used by the OpenClaw Discord agent flow, where Discord attachments live at `/home/node/.openclaw/media/inbound/<uuid>.png` (bind-mounted into the bridge). Use the path as-is from the agent's media reference; the bridge reads it directly. Do NOT try to construct a `vision.<domain>/view?type=inbound&subfolder=media&...` URL — that's a ComfyUI /view endpoint that only knows about type=output|input|temp and will 400 on inbound media paths."},
                "init_image_base64": {"type": "string", "description": "I2V: raw base64 of the source frame. Alternative to init_image_url. Most Discord-routed I2V flows should prefer the filesystem path via init_image_url since the agent already has the path from the attachment reference."},
                "timeout_s":  {"type": "number",  "description": f"Max wall-clock seconds. Default {DEFAULT_TIMEOUT_S:.0f}. Use 900+ on cold cache."},
                "include_base64": {"type": "boolean", "description": "Embed mp4 bytes as base64 in the response. Default false — mp4s are usually MB-scale, embedding would balloon the agent context.", "default": False},
                "attach_image_content": {"type": "boolean", "description": "Add the mp4 bytes as MCP `image` content alongside the metadata text. Default FALSE for video — mp4 is not a valid image content type, and downstream LLMs that try PIL.Image.open() on it surface a `cannot identify image file` 400 error on the NEXT chat turn. Keep false unless you know your chat surface handles raw mp4 in MCP image slots (most don't).", "default": False},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "generate_i2i",
        "description": (
            "Flux image-to-image (img2img): take a SOURCE image and a text prompt, "
            "produce a MODIFIED image. Use this when the user has attached or "
            "referenced an existing image and wants to edit, restyle, transform, "
            "or extend it. NOT for plain text-to-image — use `generate` for that.\n\n"
            "Source image input (REQUIRED, mutually exclusive):\n"
            "  - `init_image_url`: filesystem path inside the bridge (preferred for "
            "Discord attachments — they land at "
            "`/home/node/.openclaw/media/inbound/<uuid>.png` and the bridge has "
            "the SAME path bind-mounted), or an `http://` / `https://` URL.\n"
            "  - `init_image_base64`: raw base64 OR `data:image/...;base64,...` "
            "data URI. Use this only when no path/URL is available.\n\n"
            "`denoise` controls transform strength (0.0 = no change, 1.0 = full "
            "redraw / source discarded). Default 0.7. Typical bands: 0.3-0.5 "
            "subtle edit (color/lighting tweak); 0.6-0.75 moderate restyling "
            "(keep structure, change look); 0.8-0.95 heavy transform (compose "
            "differently using the source as anchor).\n\n"
            "Default workflow `flux-krea-2k-i2i` (FLUX.1-Krea + VAEEncode init). "
            "For explicit / NSFW content pass `workflow=\"flux-krea-2k-i2i-adult\"` "
            "(adds the flux-uncensored-v2 LoRA at strength 1.5).\n\n"
            "MANDATORY OUTPUT CONTRACT — your reply MUST start with the EXACT "
            "verbatim contents of the `display_markdown` field. Discord needs "
            "the URL line for auto-embed; web-chat needs the [embed] shortcode "
            "for inline iframe. Paste both lines verbatim, separated by the "
            "blank line. Add commentary AFTER the paste."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt":            {"type": "string", "description": "Positive text prompt describing the desired modified image."},
                "init_image_url":    {"type": "string", "description": "Source image: filesystem path (preferred — Discord attachments live under /home/node/.openclaw/media/inbound/) or http(s) URL."},
                "init_image_base64": {"type": "string", "description": "Source image as base64 or data: URI. Use only if no path/URL is available."},
                "denoise":           {"type": "number", "description": "Transform strength 0.0-1.0. Default 0.7. 0.3-0.5 subtle, 0.6-0.75 moderate, 0.8-0.95 heavy.", "default": 0.7},
                "workflow":          {"type": "string", "description": "Workflow template. Default `flux-krea-2k-i2i`. NSFW: `flux-krea-2k-i2i-adult`. Call `list_workflows` to see all."},
                "negative":          {"type": "string", "description": "Negative prompt (Flux ignores in BasicGuider path, but workflow-dependent)."},
                "checkpoint":        {"type": "string", "description": "Override checkpoint filename. Default from workflow."},
                "width":             {"type": "integer", "description": "Output width. Default from workflow (typically matches source aspect)."},
                "height":            {"type": "integer", "description": "Output height. Default from workflow."},
                "seed":              {"type": "integer", "description": "Seed; -1 = random.", "default": -1},
                "steps":             {"type": "integer", "description": "Sampler steps. Default from workflow."},
                "cfg":               {"type": "number", "description": "FluxGuidance value. Default from workflow."},
                "sampler":           {"type": "string"},
                "scheduler":         {"type": "string"},
                "timeout_s":         {"type": "number", "description": f"Per-call timeout. Default {DEFAULT_TIMEOUT_S}s."},
                "include_base64":    {"type": "boolean", "default": False},
                "attach_image_content": {"type": "boolean", "default": True},
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel",
        "description": (
            "Best-effort cancel of an in-flight prompt by id. Useful if a "
            "long render needs to be aborted (e.g. wrong prompt). Returns "
            "the HTTP status of the queue-delete + interrupt attempts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt_id": {"type": "string", "description": "ComfyUI prompt id returned by an earlier generate call."},
            },
            "required": ["prompt_id"],
            "additionalProperties": False,
        },
    },
]


# ── App + module-level state ──────────────────────────────────────────
loader = WorkflowLoader(WORKFLOWS_DIR)
loader.load_all()

# Single-flight lock by default (MAX_CONCURRENCY=1). 0 = pass-through and
# let ComfyUI's internal queue handle ordering. The semaphore is built
# once at startup; changing MAX_CONCURRENCY needs a service restart.
_gen_sem: Optional[asyncio.Semaphore] = (
    asyncio.Semaphore(MAX_CONCURRENCY) if MAX_CONCURRENCY > 0 else None
)

comfy = ComfyClient(
    base_url=COMFYUI_URL,
    poll_interval_s=POLL_INTERVAL_S,
    poll_backoff_max_s=POLL_BACKOFF_MAX_S,
)

app = FastAPI(title="openclaw-image-comfyui", version="0.1.0")
bearer = HTTPBearer(auto_error=False)


def require_token(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
    if creds is None or creds.scheme.lower() != "bearer" or creds.credentials != TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")


# ── Shared helper: fetch + upload init image ──────────────────────────
async def _fetch_and_upload_init_image(
    init_image_url: Optional[str],
    init_image_b64: Optional[str],
) -> str:
    """Resolve an init image from one of three input shapes and upload it to
    ComfyUI's input/ dir, returning the uploaded filename for a LoadImage
    workflow node to consume.

    Accepted shapes (mutually exclusive; XOR checked by the caller):
      1. `init_image_url` starting with `/` or `file://` — filesystem path.
         Read from disk directly. Used by the Discord agent flow on OpenClaw:
         inbound attachments land at `/home/node/.openclaw/media/inbound/
         <uuid>.png`. The compose file binds the gateway's inbound dir into
         the bridge container at the SAME path so the agent's reference works
         verbatim (verified live 2026-05-14).
      2. `init_image_url` starting with `http://` or `https://` — httpx-fetched.
      3. `init_image_base64` — raw base64 or a `data:image/...;base64,` data URI.

    All three end at the same place: ref_bytes + base filename →
    `comfy.upload_image()` → ComfyUI input/.

    Returns the final filename ComfyUI assigned (the bridge prefixes with a
    uuid-hex prefix to avoid collisions on concurrent calls, but ComfyUI may
    rename on collision — we trust the response).

    Reused by both `_tool_generate_video` (I2V) and `_tool_generate_i2i`
    (Flux img2img). Extracted 2026-06-06 when img2img landed; before that
    the body was inline in `_tool_generate_video`.
    """
    if not (init_image_url or init_image_b64):
        raise ValueError(
            "init image required: pass init_image_url (filesystem path / "
            "http(s) URL) OR init_image_base64"
        )
    if init_image_url and init_image_b64:
        raise ValueError("pass either init_image_url OR init_image_base64, not both")

    try:
        if init_image_url:
            src = init_image_url
            if src.startswith("file://"):
                src = src[len("file://"):]
            if src.startswith("/"):
                if not os.path.isfile(src):
                    raise ValueError(
                        f"init_image_url path doesn't exist inside the bridge "
                        f"container: {src} (mount the source dir into the "
                        "openclaw-image-comfyui compose volumes if you're "
                        "passing a path the gateway can see)"
                    )
                with open(src, "rb") as f:
                    ref_bytes = f.read()
                base = os.path.basename(src) or "init.png"
            else:
                async with httpx.AsyncClient(timeout=30.0) as ic:
                    r = await ic.get(init_image_url)
                if r.status_code != 200:
                    raise ValueError(f"init_image_url returned HTTP {r.status_code}")
                ref_bytes = r.content
                from urllib.parse import urlsplit
                url_path = urlsplit(init_image_url).path
                base = os.path.basename(url_path) or "init.png"
        else:
            # init_image_base64 may include a data: URI prefix or be raw base64.
            b64_text = init_image_b64
            if "," in b64_text and b64_text.startswith("data:"):
                b64_text = b64_text.split(",", 1)[1]
            try:
                ref_bytes = base64.b64decode(b64_text)
            except Exception as e:
                raise ValueError(f"init_image_base64 not valid base64: {e}") from e
            base = "init.png"
    except httpx.RequestError as e:
        raise ValueError(f"init_image_url fetch failed: {type(e).__name__}: {e}") from e

    # uuid-hex prefix so concurrent in-flight calls don't collide on
    # ComfyUI's input/ filenames.
    uniq = uuid.uuid4().hex[:8]
    upload_name = f"openclaw-{uniq}-{base}"
    upload_resp = await comfy.upload_image(ref_bytes, upload_name, image_type="input", overwrite=True)
    # ComfyUI may rename on collision; use whatever name it actually wrote.
    return upload_resp.get("name", upload_name)


# ── Tool implementations ──────────────────────────────────────────────
async def _tool_list_workflows(_args: dict) -> dict:
    return {"workflows": loader.list()}


async def _tool_cancel(args: dict) -> dict:
    prompt_id = args.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise ValueError("`prompt_id` is required and must be a non-empty string")
    return await comfy.cancel(prompt_id)


async def _tool_generate(args: dict) -> dict:
    # Default-cascade order for the workflow:
    #   1. explicit `workflow=` arg from the caller
    #   2. operator's IMAGE_GEN_DEFAULT_WORKFLOW env var
    #   3. hardcoded "flux-schnell" historical fallback
    workflow_name = (
        args.get("workflow")
        or IMAGE_GEN_DEFAULT_WORKFLOW
        or "flux-schnell"
    )
    try:
        workflow = loader.get(workflow_name)
    except WorkflowError as e:
        raise ValueError(str(e)) from e

    # Seed handling: -1 (or unset) → mint random; else honor caller's value.
    raw_seed = args.get("seed", -1)
    try:
        seed_val = int(raw_seed)
    except (TypeError, ValueError):
        seed_val = -1
    if seed_val < 0:
        seed_val = random.randint(0, 2**32 - 1)

    bind_args = {
        "prompt":     args.get("prompt"),
        "negative":   args.get("negative") or workflow.defaults.get("negative"),
        # Default-cascade for checkpoint:
        #   1. explicit `checkpoint=` arg from the caller
        #   2. workflow's own `defaults.checkpoint` (per-template default)
        #   3. operator's IMAGE_GEN_DEFAULT_CHECKPOINT env var (global default)
        # Without #3 the agent would have to remember a checkpoint name on
        # every call when the workflow uses the REPLACE_ME placeholder.
        "checkpoint": (
            args.get("checkpoint")
            or workflow.defaults.get("checkpoint")
            or (IMAGE_GEN_DEFAULT_CHECKPOINT or None)
        ),
        "width":      args.get("width") or workflow.defaults.get("width") or 1024,
        "height":     args.get("height") or workflow.defaults.get("height") or 1024,
        "batch_size": args.get("batch_size") or workflow.defaults.get("batch_size") or 1,
        "seed":       seed_val,
        "steps":      args.get("steps") or workflow.defaults.get("steps"),
        "cfg":        args.get("cfg") or workflow.defaults.get("cfg"),
        "sampler":    args.get("sampler") or workflow.defaults.get("sampler"),
        "scheduler":  args.get("scheduler") or workflow.defaults.get("scheduler"),
    }
    try:
        prompt_dict = workflow.bind(bind_args)
    except WorkflowError as e:
        raise ValueError(str(e)) from e

    try:
        timeout_s = float(args.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S

    # `include_base64` (default false) puts the PNG bytes into the
    # response's TEXT content (forces the LLM to prefill them — slow).
    # `attach_image_content` (default true) puts the PNG bytes into a
    # SEPARATE MCP `image` content item alongside the text — chat
    # surfaces that honor MCP image content render it as an attachment;
    # clients that ignore it lose nothing. The agent's text-context
    # prefill stays light either way.
    include_base64 = bool(args.get("include_base64", False))
    attach_image_content = bool(args.get("attach_image_content", True))

    client_id = uuid.uuid4().hex
    started = time.monotonic()

    async def _run() -> dict:
        prompt_id = await comfy.submit_prompt(prompt_dict, client_id)
        try:
            entry = await comfy.wait_for_completion(prompt_id, timeout_s)
        except ComfyUITimeout:
            await comfy.cancel(prompt_id)
            raise
        # Image workflows only emit `images[]`; the filter is defensive
        # against a future workflow accidentally chaining a video output.
        outputs = [o for o in extract_media_outputs(entry) if o.get("media_kind") == "image"]
        if not outputs:
            raise ComfyUIError(
                f"prompt {prompt_id} completed but produced no images "
                "(check the workflow has a SaveImage node)"
            )

        images: list[dict] = []
        total_bytes = 0
        for out in outputs:
            data = await comfy.fetch_image(
                out["filename"], image_type=out["type"], subfolder=out["subfolder"]
            )
            total_bytes += len(data)
            if total_bytes > MAX_OUTPUT_BYTES:
                raise ComfyUIError(
                    f"image batch exceeded IMAGE_GEN_MAX_OUTPUT_BYTES "
                    f"({MAX_OUTPUT_BYTES} B) — increase the cap or reduce batch_size"
                )
            try:
                with Image.open(BytesIO(data)) as im:
                    width, height, fmt = im.width, im.height, (im.format or "PNG").lower()
            except Exception:
                # Don't fail generation on a metadata read; fall back to defaults.
                width, height, fmt = 0, 0, "png"
            b64 = base64.b64encode(data).decode("ascii")
            from urllib.parse import quote
            view_qs = (
                f"filename={quote(out['filename'], safe='')}"
                f"&type={quote(out['type'], safe='')}"
                f"&subfolder={quote(out['subfolder'], safe='')}"
            )
            if COMFYUI_VIEW_TOKEN:
                view_qs += f"&token={quote(COMFYUI_VIEW_TOKEN, safe='')}"
            # Path A: mirror bytes into the gateway's same-origin canvas
            # dir so display_markdown can use the [embed] shortcode.
            # We write TWO files — the PNG itself plus a thin HTML wrapper.
            # Why: the chat-tool-card CSS gives the embed iframe a fixed
            # size (~301-371 × 420 px) and an iframe loading a raw PNG
            # renders the image at native size, so a 1024×1024 generation
            # gets cropped inside the small frame. The HTML wrapper uses
            # `object-fit: contain` so the PNG always fits the iframe
            # whatever the iframe's dimensions are, with letterbox bars
            # instead of cropping.
            #
            # Failure to write does NOT fail generation — fall through
            # to the legacy URL path (the entry's canvas_url_path stays
            # None and display_markdown emits the cross-origin form).
            canvas_url_path: Optional[str] = None
            if IMAGE_GEN_CANVAS_DIR:
                # Prefix avoids collision with canvas-documents.ts files
                # that the agent's canvas SKILL may emit. Short prompt-id
                # suffix gives traceability across logs without a long
                # filename.
                base_name = f"comfyui-{prompt_id[:8]}-{out['filename']}"
                png_target = os.path.join(IMAGE_GEN_CANVAS_DIR, base_name)
                # Strip the .png extension before appending .html so we
                # don't double up: foo.png → foo.html (not foo.png.html).
                html_basename = (
                    base_name[:-4] + ".html"
                    if base_name.lower().endswith((".png", ".jpg", ".webp"))
                    else base_name + ".html"
                )
                html_target = os.path.join(IMAGE_GEN_CANVAS_DIR, html_basename)
                # Same-dir relative URL — the iframe fetches the PNG via
                # the same `cap/<token>/` capability path the chat session
                # issued for the HTML, so no extra auth dance.
                wrapper_html = (
                    "<!DOCTYPE html><meta charset=\"utf-8\">"
                    "<style>"
                    "html,body{margin:0;height:100%;background:#0a0a0a;}"
                    "body{display:flex;align-items:center;justify-content:center;}"
                    "img{max-width:100%;max-height:100%;object-fit:contain;display:block;}"
                    "</style>"
                    f"<img src=\"{base_name}\" alt=\"generated image\">"
                )
                try:
                    with open(png_target, "wb") as f:
                        f.write(data)
                    with open(html_target, "w", encoding="utf-8") as f:
                        f.write(wrapper_html)
                    canvas_url_path = f"/__openclaw__/canvas/{html_basename}"
                except OSError as e:
                    log.warning(
                        "[path-a] canvas dir write failed (png=%s html=%s): %s — "
                        "falling back to legacy display_markdown emission",
                        png_target, html_target, e,
                    )
            entry = {
                "format": fmt,
                "filename": out["filename"],
                "subfolder": out["subfolder"],
                "type": out["type"],
                "node_id": out["node_id"],
                "width": width,
                "height": height,
                "byte_size": len(data),
                "fetch_url_path": f"/view?{view_qs}",
                "canvas_url_path": canvas_url_path,
            }
            if include_base64:
                entry["base64"] = b64
            entry["_b64_blob"] = b64  # internal — extracted by MCP wire
            images.append(entry)

        # Strip the per-image internal _b64_blob into a top-level
        # _attachments list before returning. The MCP `tools/call`
        # handler reads `_attachments` and emits one MCP `image`
        # content item per entry — separate from the text content
        # the LLM sees. Set attach_image_content=false to skip this
        # step and return only the text metadata.
        attachments = []
        if attach_image_content:
            for img in images:
                attachments.append({
                    "mime_type": (
                        f"image/{img['format']}"
                        if img['format'] in ("png", "jpeg", "webp")
                        else "image/png"
                    ),
                    "data": img.pop("_b64_blob"),
                    "filename": img["filename"],
                })
        else:
            for img in images:
                img.pop("_b64_blob", None)

        # `display_markdown` is the chat-side rendering hint. Two surface
        # patterns are supported simultaneously when canvas dir is set:
        #
        # 1. A naked image URL on its own line — Discord auto-embeds the
        #    URL preview as a chat-attached image. Discord's regular text
        #    messages do NOT render `[text](URL)` masked-link markdown
        #    (only embed objects do), so a plain URL is the cleanest
        #    presentation Discord supports.
        # 2. The `[embed url="/__openclaw__/canvas/<file>" /]` shortcode —
        #    OpenClaw web chat normalizer extracts this BEFORE DOMPurify
        #    and renders an inline iframe (same-origin, capability-token
        #    auth, parser-whitelisted). Added in upstream 2026.4.11
        #    (PR #64104). See docs/reference/image-comfyui-bridge.md.
        #
        # ORDER MATTERS: the naked URL goes FIRST so that even if the
        # agent cherrypicks only the first line, Discord still auto-embeds
        # the image. The [embed] shortcode comes second, additive on chat.
        #
        # Per-agent preference: AGENTS.md in each workspace can instruct
        # the agent to paste only the relevant subset (e.g. discord-friend
        # may skip [embed] since it renders as text noise on Discord;
        # main:main pastes both for chat-side iframe inline render).
        #
        # Legacy (no canvas dir): only the naked URL — Discord auto-embed
        # works without canvas dir setup.
        display_lines = []
        for img in images:
            url = f"{COMFYUI_EXTERNAL_URL}{img['fetch_url_path']}"
            # Discord auto-embed trick: append "#<filename>.ext" fragment so
            # the Discord crawler recognizes the URL as an image and triggers
            # auto-embed. The fragment is NOT sent in the actual GET request
            # — ComfyUI receives the same query-only URL as before.
            fname = img.get("filename", "")
            if fname and "." in fname:
                url = f"{url}#{fname}"
                # Pretty markdown-link FIRST: clickable text with the URL hidden
                # in the link target. Discord renders this as a clean blue
                # hyperlink — the long auth token doesn't appear as visible text.
                label = "📷"
                display_lines.append(f"[{label} {fname}]({url})")
            # Raw URL on its own line — required for Discord auto-embed
            # (markdown-links don't trigger auto-embed; only raw URLs do).
            # The preview attaches below the link in Discord.
            display_lines.append(url)
            if img.get("canvas_url_path"):
                display_lines.append(f'[embed url="{img["canvas_url_path"]}" /]')
        display_markdown = "\n\n".join(display_lines)

        return {
            "prompt_id": prompt_id,
            "workflow_used": workflow_name,
            "seed_used": seed_val,
            "elapsed_s": round(time.monotonic() - started, 3),
            "include_base64": include_base64,
            "attach_image_content": attach_image_content,
            "comfyui_base_url": COMFYUI_URL,
            "comfyui_external_url": COMFYUI_EXTERNAL_URL,
            "display_markdown": display_markdown,
            "agent_hint": (
                "MANDATORY: include the EXACT `display_markdown` value at "
                "the START of your reply. It contains a public image URL line "
                "(Discord auto-embeds it as inline image preview; web-chat "
                "autolinks it) AND optionally a `[embed url=...]` shortcode "
                "line (web-chat extracts as inline iframe; Discord renders "
                "as text noise so AGENTS.md may instruct you to skip it). "
                "Default: paste both lines verbatim, separated by the blank "
                "line. Per-agent override: your workspace AGENTS.md may say "
                "skip the [embed] on Discord — follow that. Add your own "
                "commentary AFTER the paste. Describing the image in prose "
                "is NOT a substitute."
            ),
            "images": images,
            "_attachments": attachments,
        }

    if _gen_sem is None:
        return await _run()
    async with _gen_sem:
        return await _run()


async def _tool_generate_i2i(args: dict) -> dict:
    """Flux img2img: source image + text prompt → modified image. Parallel
    to `_tool_generate` (text-to-image) but takes an init image and a
    denoise strength, runs an image-to-image workflow (default
    `flux-krea-2k-i2i`).

    Why a separate tool rather than an `init_image=` flag on `_tool_generate`:
    the LLM-facing tool catalog surfaces this as its own entry with explicit
    args (init_image_url / init_image_base64 / denoise), so the agent picks
    the right tool from intent ("modify this attached image", "restyle it")
    instead of having to remember a hidden flag on the regular generate.
    Also: the workflow templates DIFFER (i2i has LoadImage + VAEEncode nodes
    where t2i has EmptyLatentImage); same-tool overloading would need to
    detect which template was loaded and bind conditionally, which is more
    error-prone than two thin tools sharing helpers.

    Reuses `_fetch_and_upload_init_image()` for the source-image upload —
    same 3-shape input (file path / http(s) URL / base64) as the I2V flow.
    """
    # 1. Validate init image presence (helper does the XOR check too, but
    #    we want a clean error before workflow load if both are unset).
    init_image_url = args.get("init_image_url")
    init_image_b64 = args.get("init_image_base64")
    if not (init_image_url or init_image_b64):
        raise ValueError(
            "img2img requires either init_image_url (filesystem path / http(s) URL) "
            "or init_image_base64 to be set"
        )

    # 2. Workflow resolve. Default `flux-krea-2k-i2i` ships in workflows/
    #    and binds `init_image` (LoadImage filename) + `denoise` targets.
    workflow_name = args.get("workflow") or "flux-krea-2k-i2i"
    try:
        workflow = loader.get(workflow_name)
    except WorkflowError as e:
        raise ValueError(str(e)) from e

    # 3. Init image fetch + upload. The helper validates XOR (both unset OR
    #    both set both raise) and uploads to ComfyUI's input/ dir, returns
    #    the uploaded filename for LoadImage to consume.
    init_image_filename = await _fetch_and_upload_init_image(
        init_image_url, init_image_b64
    )

    # 4. Seed handling — same as _tool_generate.
    raw_seed = args.get("seed", -1)
    try:
        seed_val = int(raw_seed)
    except (TypeError, ValueError):
        seed_val = -1
    if seed_val < 0:
        seed_val = random.randint(0, 2**32 - 1)

    # 5. Denoise strength. 0.0 = no change (pointless), 1.0 = full noise
    #    (~equivalent to t2i, source image discarded). Sweet spot 0.5-0.8.
    try:
        denoise = float(args.get("denoise", workflow.defaults.get("denoise", 0.7)))
    except (TypeError, ValueError):
        denoise = 0.7
    if not (0.0 <= denoise <= 1.0):
        raise ValueError(f"denoise must be in [0.0, 1.0], got {denoise}")

    # 6. Bind args — same as _tool_generate plus init_image + denoise.
    #    No batch_size (img2img typically renders 1 variant per call;
    #    if the user wants multiple variants, they re-call with a new seed).
    bind_args = {
        "prompt":     args.get("prompt"),
        "negative":   args.get("negative") or workflow.defaults.get("negative"),
        "checkpoint": (
            args.get("checkpoint")
            or workflow.defaults.get("checkpoint")
            or (IMAGE_GEN_DEFAULT_CHECKPOINT or None)
        ),
        "width":      args.get("width") or workflow.defaults.get("width") or 1280,
        "height":     args.get("height") or workflow.defaults.get("height") or 720,
        "seed":       seed_val,
        "steps":      args.get("steps") or workflow.defaults.get("steps"),
        "cfg":        args.get("cfg") or workflow.defaults.get("cfg"),
        "sampler":    args.get("sampler") or workflow.defaults.get("sampler"),
        "scheduler":  args.get("scheduler") or workflow.defaults.get("scheduler"),
        "init_image": init_image_filename,
        "denoise":    denoise,
    }
    try:
        prompt_dict = workflow.bind(bind_args)
    except WorkflowError as e:
        raise ValueError(str(e)) from e

    try:
        timeout_s = float(args.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S

    include_base64 = bool(args.get("include_base64", False))
    attach_image_content = bool(args.get("attach_image_content", True))

    client_id = uuid.uuid4().hex
    started = time.monotonic()

    async def _run() -> dict:
        prompt_id = await comfy.submit_prompt(prompt_dict, client_id)
        try:
            entry = await comfy.wait_for_completion(prompt_id, timeout_s)
        except ComfyUITimeout:
            await comfy.cancel(prompt_id)
            raise
        outputs = [o for o in extract_media_outputs(entry) if o.get("media_kind") == "image"]
        if not outputs:
            raise ComfyUIError(
                f"prompt {prompt_id} completed but produced no images "
                "(check the workflow has a SaveImage node)"
            )

        images: list[dict] = []
        total_bytes = 0
        for out in outputs:
            data = await comfy.fetch_image(
                out["filename"], image_type=out["type"], subfolder=out["subfolder"]
            )
            total_bytes += len(data)
            if total_bytes > MAX_OUTPUT_BYTES:
                raise ComfyUIError(
                    f"image batch exceeded IMAGE_GEN_MAX_OUTPUT_BYTES "
                    f"({MAX_OUTPUT_BYTES} B) — increase the cap"
                )
            try:
                with Image.open(BytesIO(data)) as im:
                    width, height, fmt = im.width, im.height, (im.format or "PNG").lower()
            except Exception:
                width, height, fmt = 0, 0, "png"
            b64 = base64.b64encode(data).decode("ascii")
            from urllib.parse import quote
            view_qs = (
                f"filename={quote(out['filename'], safe='')}"
                f"&type={quote(out['type'], safe='')}"
                f"&subfolder={quote(out['subfolder'], safe='')}"
            )
            if COMFYUI_VIEW_TOKEN:
                view_qs += f"&token={quote(COMFYUI_VIEW_TOKEN, safe='')}"
            canvas_url_path: Optional[str] = None
            if IMAGE_GEN_CANVAS_DIR:
                base_name = f"comfyui-{prompt_id[:8]}-{out['filename']}"
                png_target = os.path.join(IMAGE_GEN_CANVAS_DIR, base_name)
                html_basename = (
                    base_name[:-4] + ".html"
                    if base_name.lower().endswith((".png", ".jpg", ".webp"))
                    else base_name + ".html"
                )
                html_target = os.path.join(IMAGE_GEN_CANVAS_DIR, html_basename)
                wrapper_html = (
                    "<!DOCTYPE html><meta charset=\"utf-8\">"
                    "<style>"
                    "html,body{margin:0;height:100%;background:#0a0a0a;}"
                    "body{display:flex;align-items:center;justify-content:center;}"
                    "img{max-width:100%;max-height:100%;object-fit:contain;display:block;}"
                    "</style>"
                    f"<img src=\"{base_name}\" alt=\"generated image\">"
                )
                try:
                    with open(png_target, "wb") as f:
                        f.write(data)
                    with open(html_target, "w", encoding="utf-8") as f:
                        f.write(wrapper_html)
                    canvas_url_path = f"/__openclaw__/canvas/{html_basename}"
                except OSError as e:
                    log.warning(
                        "[path-a] canvas dir write failed (i2i, png=%s html=%s): %s",
                        png_target, html_target, e,
                    )
            entry = {
                "format": fmt,
                "filename": out["filename"],
                "subfolder": out["subfolder"],
                "type": out["type"],
                "node_id": out["node_id"],
                "width": width,
                "height": height,
                "byte_size": len(data),
                "fetch_url_path": f"/view?{view_qs}",
                "canvas_url_path": canvas_url_path,
            }
            if include_base64:
                entry["base64"] = b64
            entry["_b64_blob"] = b64
            images.append(entry)

        attachments = []
        if attach_image_content:
            for img in images:
                attachments.append({
                    "mime_type": (
                        f"image/{img['format']}"
                        if img['format'] in ("png", "jpeg", "webp")
                        else "image/png"
                    ),
                    "data": img.pop("_b64_blob"),
                    "filename": img["filename"],
                })
        else:
            for img in images:
                img.pop("_b64_blob", None)

        display_lines = []
        for img in images:
            url = f"{COMFYUI_EXTERNAL_URL}{img['fetch_url_path']}"
            # Discord auto-embed trick: append "#<filename>.ext" fragment so
            # the Discord crawler recognizes the URL as an image and triggers
            # auto-embed. The fragment is NOT sent in the actual GET request
            # — ComfyUI receives the same query-only URL as before.
            fname = img.get("filename", "")
            if fname and "." in fname:
                url = f"{url}#{fname}"
                # Pretty markdown-link FIRST: clickable text with the URL hidden
                # in the link target. Discord renders this as a clean blue
                # hyperlink — the long auth token doesn't appear as visible text.
                label = "📷"
                display_lines.append(f"[{label} {fname}]({url})")
            # Raw URL on its own line — required for Discord auto-embed
            # (markdown-links don't trigger auto-embed; only raw URLs do).
            # The preview attaches below the link in Discord.
            display_lines.append(url)
            if img.get("canvas_url_path"):
                display_lines.append(f'[embed url="{img["canvas_url_path"]}" /]')
        display_markdown = "\n\n".join(display_lines)

        return {
            "prompt_id": prompt_id,
            "workflow_used": workflow_name,
            "seed_used": seed_val,
            "denoise_used": denoise,
            "init_image_source": init_image_filename,
            "elapsed_s": round(time.monotonic() - started, 3),
            "include_base64": include_base64,
            "attach_image_content": attach_image_content,
            "comfyui_base_url": COMFYUI_URL,
            "comfyui_external_url": COMFYUI_EXTERNAL_URL,
            "display_markdown": display_markdown,
            "agent_hint": (
                "MANDATORY: include the EXACT `display_markdown` value at "
                "the START of your reply. It contains the modified-image URL "
                "(Discord auto-embeds; web-chat autolinks) and optionally an "
                "`[embed url=...]` shortcode line. Paste verbatim. Add your "
                "own commentary AFTER. Describing the image in prose is NOT "
                "a substitute. Per-agent AGENTS.md may instruct you to skip "
                "the [embed] on Discord — follow that."
            ),
            "images": images,
            "_attachments": attachments,
        }

    if _gen_sem is None:
        return await _run()
    async with _gen_sem:
        return await _run()


async def _tool_generate_video(args: dict) -> dict:
    """LTX-Video 2.3 T2V or I2V renderer. Parallel to _tool_generate but
    talks to the `ltx-2.3-*` workflows, supports an optional init image
    upload for I2V, and emits video-aware metadata in the response.

    The split into two top-level tools (rather than threading a `kind=
    video` flag through generate) buys two things:

    1. Distinct MCP tool schemas — the agent's tool catalog surfaces
       `length`/`fps`/`audio_enabled` only where they matter, instead of
       polluting the image tool's parameter list with knobs that quietly
       no-op there.
    2. Distinct timeout / safety envelopes — video calls take 10x longer
       and emit larger output bytes; this handler enforces the duration
       cap (LTX_VIDEO_MAX_DURATION_S) before submitting the workflow,
       saving the agent a 5-minute wait on an obviously-too-long ask."""
    # Auto-pick workflow when caller didn't override:
    #   - either init_image_* is set → I2V
    #   - else → T2V
    init_image_url = args.get("init_image_url")
    init_image_b64 = args.get("init_image_base64")
    is_i2v = bool(init_image_url or init_image_b64)
    default_workflow = "ltx-2.3-i2v" if is_i2v else "ltx-2.3-t2v"
    workflow_name = args.get("workflow") or default_workflow
    try:
        workflow = loader.get(workflow_name)
    except WorkflowError as e:
        raise ValueError(str(e)) from e

    # Seed handling matches _tool_generate.
    raw_seed = args.get("seed", -1)
    try:
        seed_val = int(raw_seed)
    except (TypeError, ValueError):
        seed_val = -1
    if seed_val < 0:
        seed_val = random.randint(0, 2**32 - 1)

    # Length / fps with env defaults, then duration sanity check before
    # the operator pays the 3-10 minute cold-cache wait. The error
    # message names the actual cap so the agent can self-correct in a
    # follow-up call.
    try:
        length_frames = int(args.get("length") or LTX_VIDEO_DEFAULT_LENGTH_FRAMES)
    except (TypeError, ValueError):
        length_frames = LTX_VIDEO_DEFAULT_LENGTH_FRAMES
    try:
        fps_val = int(args.get("fps") or LTX_VIDEO_DEFAULT_FPS)
    except (TypeError, ValueError):
        fps_val = LTX_VIDEO_DEFAULT_FPS
    if length_frames < 8 or fps_val < 1:
        raise ValueError(f"invalid length={length_frames} or fps={fps_val} (length >= 8, fps >= 1)")
    duration_s = length_frames / fps_val
    if duration_s > LTX_VIDEO_MAX_DURATION_S:
        raise ValueError(
            f"requested duration {duration_s:.1f}s exceeds LTX_VIDEO_MAX_DURATION_S "
            f"({LTX_VIDEO_MAX_DURATION_S:.1f}s). Lower `length` or raise the env cap. "
            f"At fps={fps_val} the max length is {int(LTX_VIDEO_MAX_DURATION_S * fps_val)} frames."
        )

    # Audio default — boolean, taken from env unless caller overrides.
    if "audio_enabled" in args and args["audio_enabled"] is not None:
        audio_on = bool(args["audio_enabled"])
    else:
        audio_on = LTX_VIDEO_DEFAULT_AUDIO != "off"

    # I2V: fetch / decode the init image, upload to ComfyUI, get back
    # a filename in input/ that the LoadImage node can use.
    #
    # Three accepted shapes for the caller (in priority order):
    #
    #   1. `init_image_url` starting with `/` or `file://` — a filesystem
    #      path. Read directly from disk. Used by the Discord agent
    #      flow on OpenClaw: inbound attachments land at paths like
    #      `/home/node/.openclaw/media/inbound/<uuid>.png` (verified
    #      live 2026-05-14: agent tried this path, bridge previously
    #      rejected with UnsupportedProtocol). The compose file binds
    #      the gateway's inbound dir into the bridge container at the
    #      SAME path so the agent's reference works verbatim.
    #
    #   2. `init_image_url` starting with `http://` or `https://` —
    #      a real URL. httpx-fetched.
    #
    #   3. `init_image_base64` — raw base64 or a `data:image/...;base64,`
    #      data URI.
    #
    # All three end at the same place: ref_bytes + base filename →
    # upload to ComfyUI's input/ dir via /upload/image.
    init_image_filename: Optional[str] = None
    if is_i2v:
        # Body extracted to _fetch_and_upload_init_image() 2026-06-06 so the
        # img2img tool (_tool_generate_i2i) can reuse the same 3-shape input
        # logic (file path / http(s) URL / base64) without copy-paste drift.
        init_image_filename = await _fetch_and_upload_init_image(
            init_image_url, init_image_b64
        )

        # Width/height resolution is the AGENT's responsibility — pass
        # what the caller gave us, fall through to workflow defaults
        # otherwise. The bridge does not silently auto-orient or derive
        # missing dims; predictability over magic. See the patcher
        # cheatsheet for the named-resolution → explicit (width, height)
        # pairs the agent is supposed to send.

    # Proxy-side resolution resolver. Four precedence tiers (most
    # explicit first):
    #
    #   1. Explicit pair: BOTH `width` and `height` set → use verbatim.
    #   2. `resolution` arg: a user-friendly keyword or AxB string
    #      (added in v0.12.4 after the agent-rewrite failure mode —
    #      the agent often translates the user's Hungarian prompt to
    #      a polished English image-gen prompt and drops keywords
    #      like "fullhd" in the process; the `resolution` arg is a
    #      dedicated channel for resolution intent that survives any
    #      prompt rewriting). Parsed via the same RESOLUTION_ALIASES
    #      table as the prompt text.
    #   3. Prompt-text parse: scan `prompt` for AxB or a keyword.
    #      Useful when the user typed the resolution into the prompt
    #      AND the agent preserved it.
    #   4. Workflow defaults (1024×576 MiniHD).
    #
    # The parser is deterministic and explicit (a documented keyword
    # table, not LLM inference), so this is NOT auto-derive. It's a
    # translation layer from user-typed text → documented dim pair.
    caller_w = args.get("width")
    caller_h = args.get("height")
    resolution_arg = (args.get("resolution") or "").strip()
    both_explicit = caller_w is not None and caller_h is not None
    if not both_explicit:
        parsed = None
        src = None
        if resolution_arg:
            parsed = _resolve_dims_from_prompt(resolution_arg)
            if parsed:
                src = f"resolution arg {resolution_arg!r}"
        if not parsed:
            parsed = _resolve_dims_from_prompt(args.get("prompt") or "")
            if parsed:
                src = "prompt text"
        if parsed:
            log.info(
                "generate_video: resolved %dx%d from %s "
                "(caller passed width=%s, height=%s, resolution=%s)",
                parsed[0], parsed[1], src, caller_w, caller_h, resolution_arg or None,
            )
            args = {**args, "width": parsed[0], "height": parsed[1]}
        elif caller_w is not None or caller_h is not None:
            # Single-dim call with no recoverable resolution signal.
            log.warning(
                "generate_video: single-dim call (width=%s, height=%s) "
                "with no resolution arg or prompt-text signal — falling "
                "back to workflow defaults.",
                caller_w, caller_h,
            )
            args = {**args, "width": None, "height": None}

    # Hard reject >FullHD before workflow bind. See LTX_VIDEO_MAX_WIDTH /
    # _HEIGHT for the why; the agent receives an actionable error and
    # can self-correct in a follow-up call instead of locking the queue.
    final_w = args.get("width") or 0
    final_h = args.get("height") or 0
    if final_w > LTX_VIDEO_MAX_WIDTH or final_h > LTX_VIDEO_MAX_HEIGHT:
        raise ValueError(
            f"requested resolution {final_w}x{final_h} exceeds the LTX-Video cap "
            f"{LTX_VIDEO_MAX_WIDTH}x{LTX_VIDEO_MAX_HEIGHT}. The 22B distilled checkpoint "
            f"is not trained for >FullHD and will not converge on 4K/UHD. "
            f"Use resolution=fullhd or lower (or raise LTX_VIDEO_MAX_WIDTH/_HEIGHT env if "
            f"you've swapped in a higher-res checkpoint)."
        )

    bind_args = {
        "prompt":       args.get("prompt"),
        "negative":     args.get("negative") or workflow.defaults.get("negative"),
        "checkpoint":   args.get("checkpoint") or workflow.defaults.get("checkpoint"),
        "width":        args.get("width") or workflow.defaults.get("width") or 1024,
        "height":       args.get("height") or workflow.defaults.get("height") or 576,
        "seed":         seed_val,
        "steps":        args.get("steps") or workflow.defaults.get("steps"),
        "cfg":          args.get("cfg") or workflow.defaults.get("cfg"),
        # Video-specific bind keys. workflow_loader.bind() picks them
        # up from this dict and applies them via the workflow's
        # `_metadata.targets` declarations. A workflow that doesn't
        # declare a target for a key silently no-ops the override.
        "length":       length_frames,
        "fps":          fps_val,
        "audio_enabled": audio_on,
        "init_image":   init_image_filename,
    }
    try:
        prompt_dict = workflow.bind(bind_args)
    except WorkflowError as e:
        raise ValueError(str(e)) from e

    try:
        timeout_s = float(args.get("timeout_s") or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S

    include_base64 = bool(args.get("include_base64", False))
    # For video, attach_image_content defaults to FALSE — mp4 bytes are
    # NOT a valid MCP `image` content type. Downstream LLMs that try
    # PIL.Image.open() on the attached bytes (vllm + Gemma 4 verified
    # 2026-05-14) surface a 400 "cannot identify image file" on the
    # NEXT turn, killing the Discord reply path. Image-gen keeps the
    # true default because PNG/JPG bytes DO open as PIL images cleanly.
    attach_image_content = bool(args.get("attach_image_content", False))

    client_id = uuid.uuid4().hex
    started = time.monotonic()

    async def _run() -> dict:
        prompt_id = await comfy.submit_prompt(prompt_dict, client_id)
        try:
            entry = await comfy.wait_for_completion(prompt_id, timeout_s)
        except ComfyUITimeout:
            await comfy.cancel(prompt_id)
            raise
        # Filter to video outputs only. Image-side outputs from auxiliary
        # nodes (e.g., a PreviewImage debugging the spatial decode) are
        # ignored here — the tool's contract is "deliver an mp4".
        outputs = [o for o in extract_media_outputs(entry) if o.get("media_kind") == "video"]
        if not outputs:
            raise ComfyUIError(
                f"prompt {prompt_id} completed but produced no video "
                "(check the workflow has a SaveVideo or VHS_VideoCombine node)"
            )

        videos: list[dict] = []
        total_bytes = 0
        from urllib.parse import quote
        for out in outputs:
            data = await comfy.fetch_image(
                out["filename"], image_type=out["type"], subfolder=out["subfolder"]
            )
            total_bytes += len(data)
            if total_bytes > MAX_OUTPUT_BYTES:
                raise ComfyUIError(
                    f"video batch exceeded IMAGE_GEN_MAX_OUTPUT_BYTES "
                    f"({MAX_OUTPUT_BYTES} B) — raise the cap or shorten the clip"
                )
            # Dimensions come from the bind args. Probing the mp4 with
            # ffprobe would give exact post-encode numbers but adds a
            # subprocess hop per call; the bind values are accurate
            # enough for the agent's metadata reply.
            b64 = base64.b64encode(data).decode("ascii") if (include_base64 or attach_image_content) else ""
            view_qs = (
                f"filename={quote(out['filename'], safe='')}"
                f"&type={quote(out['type'], safe='')}"
                f"&subfolder={quote(out['subfolder'], safe='')}"
            )
            if COMFYUI_VIEW_TOKEN:
                view_qs += f"&token={quote(COMFYUI_VIEW_TOKEN, safe='')}"
            # Path A: write an HTML wrapper into the canvas dir. The
            # iframe's src loads the wrapper; the wrapper's <video> tag
            # references the mp4 via the same relative URL the chat
            # session's capability token already gates. Unverified
            # end-to-end (see docs/reference/video-comfyui-bridge.md
            # "web chat (degraded)" section).
            canvas_url_path: Optional[str] = None
            if IMAGE_GEN_CANVAS_DIR:
                base_name = f"ltxvideo-{prompt_id[:8]}-{out['filename']}"
                mp4_target = os.path.join(IMAGE_GEN_CANVAS_DIR, base_name)
                html_basename = (
                    base_name[:-4] + ".html"
                    if base_name.lower().endswith((".mp4", ".webm", ".mov"))
                    else base_name + ".html"
                )
                html_target = os.path.join(IMAGE_GEN_CANVAS_DIR, html_basename)
                wrapper_html = (
                    "<!DOCTYPE html><meta charset=\"utf-8\">"
                    "<style>"
                    "html,body{margin:0;height:100%;background:#0a0a0a;}"
                    "body{display:flex;align-items:center;justify-content:center;}"
                    "video{max-width:100%;max-height:100%;display:block;}"
                    "</style>"
                    f"<video controls autoplay muted playsinline src=\"{base_name}\"></video>"
                )
                try:
                    with open(mp4_target, "wb") as f:
                        f.write(data)
                    with open(html_target, "w", encoding="utf-8") as f:
                        f.write(wrapper_html)
                    canvas_url_path = f"/__openclaw__/canvas/{html_basename}"
                except OSError as e:
                    log.warning(
                        "[path-a] canvas dir write failed for video (mp4=%s html=%s): %s — "
                        "falling back to legacy display_markdown emission",
                        mp4_target, html_target, e,
                    )
            entry_out = {
                "format": (out["filename"].rsplit(".", 1)[-1] or "mp4").lower(),
                "filename": out["filename"],
                "subfolder": out["subfolder"],
                "type": out["type"],
                "node_id": out["node_id"],
                "media_kind": "video",
                "width": bind_args["width"],
                "height": bind_args["height"],
                "length_frames": length_frames,
                "fps": fps_val,
                "duration_s": round(duration_s, 3),
                "audio_enabled": audio_on,
                "byte_size": len(data),
                "fetch_url_path": f"/view?{view_qs}",
                "canvas_url_path": canvas_url_path,
            }
            if include_base64:
                entry_out["base64"] = b64
            if attach_image_content and b64:
                entry_out["_b64_blob"] = b64
            videos.append(entry_out)

        attachments = []
        if attach_image_content:
            for v in videos:
                blob = v.pop("_b64_blob", None)
                if not blob:
                    continue
                # mime-type heuristic from filename extension. Most
                # SaveVideo outputs are mp4 but VHS_VideoCombine can
                # also emit webm; ComfyUI doesn't surface mime in the
                # /history payload so we sniff the extension.
                ext = v["format"]
                mime = {
                    "mp4":  "video/mp4",
                    "webm": "video/webm",
                    "mov":  "video/quicktime",
                    "gif":  "image/gif",
                }.get(ext, "video/mp4")
                attachments.append({"mime_type": mime, "data": blob, "filename": v["filename"]})
        else:
            for v in videos:
                v.pop("_b64_blob", None)

        # display_markdown shape: line 1 the public mp4 URL (Discord
        # auto-embeds), line 2 the [embed] shortcode if canvas dir is
        # set (web-chat iframe — unverified for video, see doc). The
        # agent's reply pastes both verbatim.
        display_lines = []
        for v in videos:
            url = f"{COMFYUI_EXTERNAL_URL}{v['fetch_url_path']}"
            # Discord auto-embed trick (same as image path above): append
            # "#<filename>.mp4" fragment so the crawler recognizes the URL
            # as video. Fragment is not sent in actual GET request.
            fname = v.get("filename", "")
            if fname and "." in fname:
                url = f"{url}#{fname}"
                # Pretty markdown-link FIRST: clickable text, token hidden.
                display_lines.append(f"[🎬 {fname}]({url})")
            # Raw URL second line — Discord auto-embed trigger for video preview.
            display_lines.append(url)
            if v.get("canvas_url_path"):
                display_lines.append(f'[embed url="{v["canvas_url_path"]}" /]')
        display_markdown = "\n\n".join(display_lines)

        return {
            "prompt_id": prompt_id,
            "workflow_used": workflow_name,
            "mode": "i2v" if is_i2v else "t2v",
            "seed_used": seed_val,
            "length_frames": length_frames,
            "fps": fps_val,
            "duration_s": round(duration_s, 3),
            "audio_enabled": audio_on,
            "init_image_used": init_image_filename,
            "elapsed_s": round(time.monotonic() - started, 3),
            "include_base64": include_base64,
            "attach_image_content": attach_image_content,
            "comfyui_base_url": COMFYUI_URL,
            "comfyui_external_url": COMFYUI_EXTERNAL_URL,
            "display_markdown": display_markdown,
            "agent_hint": (
                "MANDATORY: include the EXACT `display_markdown` value at "
                "the START of your reply. The first line is a public mp4 "
                "URL — Discord auto-embeds it inline so the user actually "
                "sees the video. The second line (if present) is the "
                "`[embed]` shortcode for the web-chat iframe surface. "
                "Paste both verbatim, separated by the blank line. Add "
                "your own commentary AFTER the paste."
            ),
            "videos": videos,
            "_attachments": attachments,
        }

    if _gen_sem is None:
        return await _run()
    async with _gen_sem:
        return await _run()


TOOL_HANDLERS = {
    "generate":        _tool_generate,
    "generate_video":  _tool_generate_video,
    "generate_i2i":    _tool_generate_i2i,
    "list_workflows":  _tool_list_workflows,
    "cancel":          _tool_cancel,
}


# ── MCP wire protocol (mirrors openclaw-python-sandbox/server/app.py) ─
def _jsonrpc_result(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _jsonrpc_error(id_: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


async def _handle_message(msg: dict) -> Optional[dict]:
    method = msg.get("method")
    params = msg.get("params") or {}
    id_ = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
        return None if is_notification else _jsonrpc_result(id_, result)

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return None if is_notification else _jsonrpc_result(id_, {})

    if method == "tools/list":
        return None if is_notification else _jsonrpc_result(id_, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _jsonrpc_error(id_, -32601, f"tool not found: {name}")
        try:
            tool_result = await handler(args)
            is_error = False
        except ValueError as e:
            return _jsonrpc_error(id_, -32602, str(e))
        except ComfyUITimeout as e:
            tool_result = {"error": "timeout", "message": str(e)}
            is_error = True
        except ComfyUIRestartedError as e:
            tool_result = {"error": "comfyui_restarted", "message": str(e)}
            is_error = True
        except ComfyUIError as e:
            tool_result = {"error": "comfyui_error", "message": str(e)}
            is_error = True
        except Exception as e:  # noqa: BLE001
            log.exception("tool call %s raised", name)
            return _jsonrpc_error(id_, -32603, f"{type(e).__name__}: {e}")
        # Internal `_attachments` is the bridge's signal to add MCP
        # `image` content items alongside the text. Pop it BEFORE
        # serializing the text content so the base64 doesn't end up
        # in the JSON the LLM prefills.
        attachments = []
        if isinstance(tool_result, dict):
            attachments = tool_result.pop("_attachments", []) or []
        content: list[dict] = [
            {"type": "text", "text": json.dumps(tool_result, default=str)}
        ]
        for att in attachments:
            data = att.get("data")
            if not isinstance(data, str) or not data:
                continue
            content.append({
                "type": "image",
                "data": data,
                "mimeType": att.get("mime_type") or "image/png",
            })
        return _jsonrpc_result(
            id_,
            {"content": content, "isError": is_error},
        )

    if is_notification:
        return None
    return _jsonrpc_error(id_, -32601, f"method not found: {method}")


# ── HTTP endpoints ────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz() -> PlainTextResponse:
    """Liveness probe — unauth'd so docker's HEALTHCHECK can hit it."""
    return PlainTextResponse(f"ok workflows={len(loader.list())}\n")


@app.get("/auth-validate")
async def auth_validate(request: Request) -> PlainTextResponse:
    """Token-validation endpoint for NGINX `auth_request`.

    Compares `?token=...` against `COMFYUI_VIEW_TOKEN`. Returns 200
    on match, 401 otherwise. No body — `auth_request` only inspects
    the status code. Designed to be called by an NGINX reverse-proxy
    (typically Nginx Proxy Manager) that fronts ComfyUI:

        location = /auth-validate {
            internal;
            proxy_pass http://<host>:9095/auth-validate$is_args$args;
            proxy_pass_request_body off;
            proxy_set_header Content-Length "";
        }
        location /view {
            auth_request /auth-validate;
            auth_basic off;
            proxy_pass http://<host>:13036;
        }

    The token stays in this service's `.env` (COMFYUI_VIEW_TOKEN);
    the proxy admin GUI no longer needs to hold the secret. If
    COMFYUI_VIEW_TOKEN is empty, the endpoint refuses everything
    (401) — fail-closed.
    """
    if not COMFYUI_VIEW_TOKEN:
        return PlainTextResponse("auth-validate disabled: COMFYUI_VIEW_TOKEN unset", status_code=401)
    # Primary: query string on this request. Works when the proxy uses
    # `proxy_pass http://.../auth-validate?token=$arg_token` style and
    # forwards the parent's args.
    supplied = request.query_params.get("token", "")
    # Fallback: NGINX `auth_request /auth-validate;` is a sub-request
    # with a STATIC URI — the parent request's `?token=...` query
    # string does NOT propagate to the sub-request's $args, so the
    # bridge sees no `token=` query param. Recover it from the
    # `X-Original-URI` header that the proxy sets to `$request_uri`
    # (the parent request URI, including query string). NPM's default
    # /auth-validate custom-location Advanced includes
    # `proxy_set_header X-Original-URI $request_uri;` for exactly this.
    if not supplied:
        from urllib.parse import urlsplit, parse_qs
        original_uri = request.headers.get("X-Original-URI", "")
        if "?" in original_uri:
            qs = parse_qs(urlsplit(original_uri).query)
            supplied = (qs.get("token") or [""])[0]
    # Constant-time compare to avoid timing-side-channel leakage of the
    # secret prefix. secrets.compare_digest needs equal-length bytes.
    if len(supplied) == len(COMFYUI_VIEW_TOKEN) and secrets.compare_digest(supplied, COMFYUI_VIEW_TOKEN):
        return PlainTextResponse("ok", status_code=200)
    return PlainTextResponse("invalid token", status_code=401)


def _resolve_session_header(request: Request, payload: Any) -> Optional[str]:
    """Echo or mint Mcp-Session-Id (mirrors python-sandbox)."""
    incoming = request.headers.get("Mcp-Session-Id")
    if incoming:
        return incoming
    is_initialize = False
    if isinstance(payload, dict):
        is_initialize = payload.get("method") == "initialize"
    elif isinstance(payload, list):
        is_initialize = any(isinstance(m, dict) and m.get("method") == "initialize" for m in payload)
    return secrets.token_urlsafe(16) if is_initialize else None


@app.post("/mcp", dependencies=[Depends(require_token)])
async def mcp_endpoint(request: Request) -> JSONResponse:
    try:
        body = await request.body()
        if not body:
            raise ValueError("empty request body")
        payload = json.loads(body)
    except Exception as e:
        return JSONResponse(
            status_code=400, content=_jsonrpc_error(None, -32700, f"parse error: {e}")
        )

    session_id = _resolve_session_header(request, payload)
    response_headers = {"Mcp-Session-Id": session_id} if session_id else None

    if isinstance(payload, list):
        responses = await asyncio.gather(*(_handle_message(m) for m in payload))
        responses = [r for r in responses if r is not None]
        if not responses:
            return JSONResponse(status_code=202, content=None, headers=response_headers)
        return JSONResponse(content=responses, headers=response_headers)

    if isinstance(payload, dict):
        response = await _handle_message(payload)
        if response is None:
            return JSONResponse(status_code=202, content=None, headers=response_headers)
        return JSONResponse(content=response, headers=response_headers)

    return JSONResponse(
        status_code=400,
        content=_jsonrpc_error(None, -32600, "request must be an object or array"),
    )


# ── Lifecycle ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def _on_start() -> None:
    log.info(
        "image-bridge starting; comfyui_url=%s default_timeout=%.0fs max_concurrency=%d",
        COMFYUI_URL, DEFAULT_TIMEOUT_S, MAX_CONCURRENCY,
    )


@app.on_event("shutdown")
async def _on_stop() -> None:
    log.info("image-bridge shutting down; closing comfyui client")
    await comfy.aclose()
