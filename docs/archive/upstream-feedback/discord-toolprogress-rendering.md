# Upstream feature request — Discord tool-progress rendering

This is a draft feature-request body intended for filing on
`https://github.com/openclaw/openclaw/issues`. It documents a real-world
issue we hit on `openclaw 2026.4.22` and proposes a focused fix. Operator
of this stack: file the issue and link this file as the source.

---

## Title

`channels.discord.streaming.preview.toolProgress`: tool names with `__` separator get mangled by Discord italic markdown

## Summary

When `channels.discord.streaming` is set to `"partial"` (or `"block"` /
`"progress"`) and `streaming.preview.toolProgress` is `true` (the default),
the Discord plugin emits a "Working...\n- tool: \<name\>" line into the
streaming preview as the agent invokes tools.

The tool name is rendered **as raw text**, with no escaping or code-block
wrapping. Tool names that follow OpenClaw's documented MCP convention
(`<server>__<tool>`, e.g. `comfyui_image__generate`) contain underscores
that Discord's markdown parser interprets as italic markup:

- `_image_` (single-underscore pair) → italic
- `_generate` (single underscore at the end of one segment, no closing
  pair) → italic-mode triggered, propagates into surrounding text

Result: the line is visually mangled. The operator sees a broken-looking
display and concludes the bot is buggy.

## Reproduction

1. Configure `channels.discord.streaming = "partial"` (or `"block"`).
2. Wire up an MCP server whose tool name uses the `__` separator
   (`comfyui_image__generate`, `python_sandbox__python_exec`, …).
3. Mention the bot with a prompt that triggers the tool
   (`@bot generálj egy zsiráfot kalapban`).
4. While the streaming preview is mid-flight, observe the
   "Working...\n- tool: comfyui_image__generate" line — the `_image_`
   middle of the tool name is italicised, and the rest of the line is
   visually broken.

## Expected

The tool name is rendered verbatim, without markdown interpretation. The
operator can clearly see which tool is executing.

## Proposed fixes (pick any one — they don't conflict)

1. **Wrap tool names in inline code blocks** (`` `comfyui_image__generate` ``).
   Discord renders inline code as monospace text and explicitly does NOT
   apply markdown rules inside code spans. Zero behavioural change for the
   operator, fixes 100% of underscore/asterisk/tilde cases.

2. **Escape markdown special chars** in tool names before they're emitted
   into Discord text (`comfyui\_image\_\_generate`). Same visual effect as
   #1 (no italic), but doesn't change the typeface — closer to the
   current look-and-feel.

3. **Add a `format` sub-key** to `streaming.preview.toolProgress`:
   - `streaming.preview.toolProgress: true | false | "raw" | "code" | "icon"`
   - `"code"` wraps in inline code (#1).
   - `"raw"` is the current behaviour (kept for backwards compat).
   - `"icon"` shows a single emoji (e.g. 🛠️) instead of the tool name —
     less detail but always visually clean.
   - `false` keeps the existing on/off off-state.

4. **Document the knob explicitly**. The current docs only describe
   `streaming.preview.toolProgress` as a boolean. If the upstream
   intends to keep the raw text rendering, at minimum document the
   markdown-collision risk and recommend that MCP server authors avoid
   `__` in their server names.

#1 is the smallest possible fix and would close the issue without any
new config surface.

## Why this matters

The `__` separator is OpenClaw's own documented convention for
namespacing MCP tools. Operators who wire up MCP servers
(`comfyui_image`, `python_sandbox`, `browser`, …) cannot change the
tool-name shape without forking OpenClaw. The collision with Discord
markdown is therefore not the operator's fault and not avoidable through
configuration — only through a fix in the Discord plugin's preview
rendering.

## Versions

- OpenClaw `2026.4.22` (published bundle, image
  `ghcr.io/openclaw/openclaw:2026.4.22`).
- Discord client: web 2026.04 (also reproduces on desktop 2026.04).
- vLLM `0.19.1.dev6+g6d4a8e6d2` (post-#38946 fix).
- Verified end-to-end on a GB10 (DGX Spark) deploy of the
  `dgx-openclaw-stack` repo, 2026-04-29.

## Workaround in `dgx-openclaw-stack`

A `OPENCLAW_DISCORD_STREAMING_PREVIEW_TOOL_PROGRESS=false` env knob is
exposed in patcher step 24, which writes
`channels.discord.streaming.preview.toolProgress = false` to the live
config. This suppresses the lines entirely and avoids the mangle, but
also loses the mid-stream visibility — operators who genuinely want to
see what the agent is doing have no clean option until upstream ships
one of the proposed fixes.

## Visual evidence

A 12-frame GIF of the bug is available at
`discord-toolcall-render-bug-2026-04-29.gif` (taken from the
operator's Chrome session). It shows the streaming preview with
"Working...\n- comfyui_image__generate\n- tool: comfyui_image__generate"
where the middle `_image_` segment renders italic and the trailing
`_generate` segment cascades the italic into surrounding text.

(Attach the GIF to the GitHub issue when filing.)
