# Chat surface capability matrix

> Reference: which media features render on which OpenClaw surfaces, with reproducible smoke tests.

This document anchors the empirical answers to "if I send X over Y, will the user see it?" — the kind of question that bit us on every media integration so far (image-gen `display_markdown`, TTS attachment in Discord text, MCP image-content blocks, etc.). When you add a new media-bridge or surface, run the relevant cells against your deploy and update the verdict.

## Surfaces

- **Web chat** — the `https://<your-host>/chat` browser SPA. Markdown sanitizer + hard-wired `speechSynthesis` for "Read aloud".
- **Discord text** — guild text channels via the OpenClaw Discord plugin.
- **Discord voice** — `/vc join`-driven voice-channel agents (out of scope for most image/text features).
- **Agent skill API** — direct CLI / WS RPC (`openclaw agent --message ...`). Tool results return raw JSON; no surface-side rendering.
- **Control UI** — the gateway's separate control plane (port `OPENCLAW_GATEWAY_CONTROL_PORT`, default 18790). Not commonly used for chat; included for completeness.

## Matrix

Verdict legend: ✅ works · ❌ blocked · ⚠️ conditional (see verify cell) · n/a not applicable on this surface.

| Feature | Web chat | Discord text | Discord voice | Agent skill API | Control UI |
|---|:---:|:---:|:---:|:---:|:---:|
| Image markdown `![](url)` | ❌ sanitizer + cross-origin block (see notes) | ✅ Discord auto-embed | n/a | ✅ raw JSON passthrough | ❓ verify |
| Image markdown `[txt](url)` | ❌ sanitizer drop external | ✅ Discord embed-card | n/a | ✅ raw JSON | ❓ verify |
| MCP image-content block | ❌ 2026.4.25 still ignored | ❌ not surfaced | n/a | ✅ in tool result `content[]` | ❓ verify |
| Audio `<audio src=...>` HTML | ❌ sanitizer drop | ❌ no inline audio HTML | n/a | n/a | ❓ verify |
| Audio attachment (mp3/wav) | ❌ no fetch path | ⚠️ ffmpeg gap (`AUTO=tagged`) | ✅ voice channel stream | ✅ tool returns blob | ❓ verify |
| External link click | ✅ direct nav (new tab) | ✅ Discord embed-card | n/a | n/a | ✅ |
| Cross-origin Basic auth on `<img>` | ❌ browser strips creds | n/a | n/a | n/a | ❌ |
| `[embed url="/__openclaw__/canvas/..."]` shortcode | ✅ verified 2026-04-28 (renders .png + .html, capability-token auth) | n/a | n/a | n/a | ✅ |
| Same-origin static `/__openclaw__/canvas/<file>` | ✅ via `[embed]` (auth = `/__openclaw__/cap/<token>/...` rewrite) | n/a | n/a | n/a | ❓ verify |
| Speech synthesis (Read aloud) | ⚠️ browser default voice (poor for HU) | n/a | n/a | n/a | ❓ verify |

## Verify cells

Every `❌` / `⚠️` / `❓` here is reproducible. Run the snippet and the verdict either matches the cell (good — your deploy is consistent) or differs (worth investigating — upstream may have moved).

### Image markdown `![](url)` in web chat — sanitizer + cross-origin block

`![alt](url)` survives the DOMPurify allowlist as of upstream PR #15480
(`<img>`, `src`, `alt`, `ADD_DATA_URI_TAGS: ["img"]` permitted). What
actually blocks rendering on our deploy:

1. **Cross-origin Basic auth strip** — `<img src="https://vision.example.com/...">`
   triggers a fetch that browsers refuse to attach the cached Basic
   credential to (different origin from the chat tab). Result: 401, broken
   image icon, no inline render.
2. **Cross-origin CSP / referrer policy** — the chat host's CSP may
   `img-src 'self'` only, dropping any external host.

For same-origin images, see the `[embed]` shortcode row — that's the
working path on our deploy.

```bash
# Send a message containing only `![alt](https://placekitten.com/200/200)` to the agent
# and observe the rendered DOM:
ssh -i KEY -l user host 'docker exec openclaw-cli openclaw agent --agent main \
  --message "Reply with literally `![cat](https://placekitten.com/200/200)` and nothing else." \
  --thinking off --json --timeout 60 2>&1 | jq -r ".result.payloads[0].text"'
# In the chat tab DevTools, the <img> tag IS in the DOM. Whether it renders
# depends on whether placekitten.com is reachable cross-origin from the
# chat origin (no auth here, so should — useful for isolating sanitizer
# vs auth as the cause when debugging a real broken image).
```

### Image markdown `[text](url)` in web chat — sanitizer drop external

```bash
# Send `[click](https://example.com)` in chat. Expected: text "click" survives but
# the click target is stripped (or the link rendered as plain text).
# Mailto: links are the only documented surviving link type (per CHANGELOG.md v0.9.5-0.9.7).
```

### MCP image-content block — verified 2026-04-28 still ignored

```bash
# Bridge already emits image MCP content blocks in tool results, but the chat
# surface ignores them. Verify with:
ssh -i KEY -l user host 'docker exec openclaw-cli openclaw agent --agent main \
  --message "Use comfyui_image__generate with prompt=red square width=256 height=256" \
  --thinking off --json --timeout 600' | jq '.result.payloads[0].text' \
  | grep -E "image|attachment|inline"
# Expected: only `display_markdown` URL is mentioned in the assistant reply text.
# The MCP content block IS in the tool-result `content[]` but no surface renders it.

# Cross-check upstream release notes for the chat-side fix:
# WebFetch https://github.com/openclaw/openclaw/releases — last verified 2026-04-28
# on 2026.4.25: no image-content rendering changelog entry. Path C still pending.
```

### Audio attachment in Discord text — gap closed in v0.11.0

```bash
# Pre-v0.11.0 (the upstream openclaw image shipped without ffmpeg):
# default messages.tts.auto=always triggered TTS-attachment on every
# Discord text reply, and the attachment pipeline crashed silently
# with `final reply failed: Error: ffmpeg not found in trusted system
# directories` — the bot's text payload never lands. Workaround:
# OPENCLAW_TTS_AUTO=tagged in .env; patcher step 11 honors the override.
#
# v0.11.0+ ships openclaw-base-ext/Dockerfile that wraps the upstream
# image and apt-installs ffmpeg, so auto=always works end-to-end.
# Verify on a current deploy:
docker exec ${PROJ}openclaw-gateway ffmpeg -version | head -1
# Expected: `ffmpeg version 5.1.x-...`. If `command not found`, the
# operator hasn't rebuilt the openclaw-base-ext image yet — see
# CHANGELOG [0.11.0] migration.
docker logs ${PROJ}openclaw-gateway --since 5m 2>&1 \
  | grep -E "final reply failed.*ffmpeg" | tail -3
# Expected: no hits after v0.11.0 deploy.
```

### External link in web chat — direct nav works

```bash
# `[click](https://example.com)` opens example.com in a new tab when clicked.
# This is the canonical workaround for image-gen until Path A/B/C lands.
# No automation needed; manual click test.
```

### Cross-origin Basic auth on `<img>` — browser strips

```bash
# Browsers DO NOT send Basic auth credentials to cross-origin <img> fetches by design.
# Trying to embed `<img src="https://user:pass@vision.example.com/foo.png">` fails:
# Chrome strips the userinfo; Firefox may prompt or fail silently; Safari refuses entirely.
# Mitigation: ?token=<urlsafe> URL-param auth (the only style that works on cross-origin
# <img>). The image-comfyui bridge added COMFYUI_VIEW_TOKEN exactly for this in v0.9.8.
```

### `[embed url=...]` shortcode in web chat — verified 2026-04-28

Upstream openclaw added `[embed ...]` in `2026.4.11` (PR #64104). The chat
normalizer extracts the directive into structured iframe metadata BEFORE the
DOMPurify pass, so the shortcode bypasses the `<img>` sanitizer.

URL whitelist (parser-validated, same-origin only):

- `/__openclaw__/canvas/...`
- `/__openclaw__/a2ui/...`

Absolute http(s) URLs are gated by `gateway.controlUi.allowExternalEmbedUrls`
(default `false`, marked **dangerous** in the schema — leave it off; the
whole point of the same-origin path is dodging the cross-origin auth /
sanitizer mess).

**Auth = capability-token URL rewrite** (verified live on `2026.4.22`).
The chat session normalizer rewrites the iframe `src` from
`/__openclaw__/canvas/<file>` to
`/__openclaw__/cap/<24-char-urlsafe-token>/__openclaw__/canvas/<file>`.
The `cap/<token>/` prefix is a one-shot capability the gateway issues
per chat session; the iframe doesn't carry cookies or bearers, the URL
itself is the auth.

Sandbox controlled by `gateway.controlUi.embedSandbox`:

- `"strict"` — minimal sandbox
- `"scripts"` — `allow-scripts` (**default in `2026.4.22`** — verified)
- `"trusted"` — `allow-scripts allow-same-origin` (lets the chat session's
  bearer/cookie flow through to the iframe content fetch — needed only if
  the iframe content does cross-origin XHR)

For inline image render (`.png` / `.jpg` / `.webp`), `"scripts"` is
sufficient — no JS or same-origin fetch needed inside the iframe.

Three-step verify (steps 1-2 read-only diagnostic, step 3 needs a one-time
write authorization for the test PNG):

```bash
# 1. Read-only: find the host filesystem path the gateway serves at
#    /__openclaw__/canvas/ and the current embedSandbox config.
ssh -i KEY -l user host 'docker exec openclaw-gateway sh -c "
  ls -la /home/node/.openclaw/ 2>/dev/null;
  echo ---;
  for d in canvas attachments media a2ui; do
    test -d /home/node/.openclaw/\$d && {
      echo \"~/.openclaw/\$d/:\";
      ls -la /home/node/.openclaw/\$d/ 2>/dev/null | head -10;
    };
  done"'

# 2. Read-only: probe the gateway's /__openclaw__/canvas/ route (route
#    presence + auth requirement on a file that exists from step 1).
ssh -i KEY -l user host 'docker exec openclaw-gateway sh -c "
  curl -sS -o /dev/null -w \"%{http_code} %{content_type}\n\" \
    http://127.0.0.1:18789/__openclaw__/canvas/<file-from-step-1>"'

# 3. Write (one-time, pre-authorized by the operator): drop a real 1×1
#    PNG into the canvas dir, send an embed-shortcode reply, observe
#    chat render.
docker exec openclaw-gateway sh -c \
  'printf "\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x78\x9c\x62\x00\x01\x00\x00\x05\x00\x01\x0d\x0a\x2d\xb4\x00\x00\x00\x00IEND\xaeB\x60\x82" > /home/node/.openclaw/canvas/embed-probe.png'
docker exec openclaw-cli openclaw agent --agent main \
  --message 'Reply with literally [embed url="/__openclaw__/canvas/embed-probe.png" /] and nothing else.' \
  --thinking off --json --timeout 90
# Then in the chat tab: confirm a 1×1 image bubble renders (not the literal markup).
```

### Speech synthesis (Read aloud) in web chat — browser-default voice

```bash
# The chat bundle's "Read aloud" button calls window.speechSynthesis.speak() with the
# OS-default voice. Hungarian is poor on most OSes. Mitigation: userscript that
# monkey-patches speechSynthesis to fetch from the openclaw-tts-router instead.
# See docs/reference/tts-stack.md → "Web chat workaround".
```

## How this matrix evolves

When upstream OpenClaw or this stack ships a change that flips a cell:

1. Update the verdict (✅/❌/⚠️) in the matrix above.
2. Update the verify cell with the date and the smoke command output.
3. Cross-link from the relevant `docs/reference/` doc (e.g. `image-comfyui-bridge.md`'s "Future paths" section).
4. If the cell changes from ❌ to ✅ on a major surface (web chat especially), add a `CHANGELOG.md` entry.

## Related references

- `docs/reference/media-bridge-checklist.md` — pre-flight checklist for any new media-feature integration
- `docs/reference/image-comfyui-bridge.md` — bridge architecture and the three candidate fix paths for image rendering
- `docs/reference/tts-stack.md` — TTS router design, web-chat speechSynthesis workaround
- `docs/TROUBLESHOOTING.md` — quick fixes for the most common surface mismatches users hit first
