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
| Image markdown `![](url)` | ❌ sanitizer drop | ✅ Discord auto-embed | n/a | ✅ raw JSON passthrough | ❓ verify |
| Image markdown `[txt](url)` | ❌ sanitizer drop external | ✅ Discord embed-card | n/a | ✅ raw JSON | ❓ verify |
| MCP image-content block | ❌ 2026.4.25 still ignored | ❌ not surfaced | n/a | ✅ in tool result `content[]` | ❓ verify |
| Audio `<audio src=...>` HTML | ❌ sanitizer drop | ❌ no inline audio HTML | n/a | n/a | ❓ verify |
| Audio attachment (mp3/wav) | ❌ no fetch path | ⚠️ ffmpeg gap (`AUTO=tagged`) | ✅ voice channel stream | ✅ tool returns blob | ❓ verify |
| External link click | ✅ direct nav (new tab) | ✅ Discord embed-card | n/a | n/a | ✅ |
| Cross-origin Basic auth on `<img>` | ❌ browser strips creds | n/a | n/a | n/a | ❌ |
| Same-origin static (gateway canvas) | ❓ Path A research pending | n/a | n/a | n/a | ❓ verify |
| Speech synthesis (Read aloud) | ⚠️ browser default voice (poor for HU) | n/a | n/a | n/a | ❓ verify |

## Verify cells

Every `❌` / `⚠️` / `❓` here is reproducible. Run the snippet and the verdict either matches the cell (good — your deploy is consistent) or differs (worth investigating — upstream may have moved).

### Image markdown `![](url)` in web chat — sanitizer drop

```bash
# Send a message containing only `![alt](https://placekitten.com/200/200)` to the agent
# from a browser tab; observe the rendered DOM.
# Expected: only "alt" text remains, the <img> is stripped.
ssh -i KEY -l user host 'docker exec openclaw-cli openclaw agent --agent main \
  --message "Reply with literally `![cat](https://placekitten.com/200/200)` and nothing else." \
  --thinking off --json --timeout 60 2>&1 | jq -r ".result.payloads[0].text"'
# Then open the chat tab, find the bubble, devtools-inspect: should show <p>cat</p>, not <img>.
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

### Audio attachment in Discord text — ffmpeg gap

```bash
# Default messages.tts.auto=always triggers TTS-attachment on every Discord text reply.
# With the gateway image lacking ffmpeg, this crashes silently:
docker logs openclaw-gateway 2>&1 | grep -E "final reply failed.*ffmpeg" | tail -3
# Workaround: OPENCLAW_TTS_AUTO=tagged in .env; patcher step 11 honors the override.
# See docs/reference/tts-stack.md.
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

### Same-origin static (gateway canvas) — UNVERIFIED, research pending

```bash
# The gateway mounts /__openclaw__/canvas/ → /home/node/.openclaw/canvas/.
# Auth scheme unknown; sanitizer behavior on /__openclaw__/* paths unverified.
# Probes (run from a chat-tab DevTools console for cookie auth path):
ssh ... 'docker exec openclaw-gateway sh -c "curl -sS -o /dev/null -w \"naked %{http_code}\n\" http://127.0.0.1:18789/__openclaw__/canvas/test.png"'
ssh ... 'docker exec openclaw-gateway sh -c "curl -sS -o /dev/null -w \"bearer %{http_code}\n\" -H \"Authorization: Bearer \$OPENCLAW_GATEWAY_TOKEN\" http://127.0.0.1:18789/__openclaw__/canvas/test.png"'
# DOM-side: in the chat tab DevTools console:
# fetch("/__openclaw__/canvas/test.png", {credentials:"include"}).then(r => r.status)
# Status 200/401/403 dictates which auth strategy works.
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
