# Media-bridge pre-flight checklist

> Run this before merging any new media-MCP-bridge (image / audio / video / file).

This 8-point checklist exists because every media integration on this stack so far surfaced ≥1 surprise limit during deploy (TTS ffmpeg gap, MCP image-content ignored by chat, Gemma colon-namespace tool-call regex, ComfyUI workflow `REPLACE_ME` placeholder, ackReaction stale-queue, …). The pattern is always the same: a feature works in isolation, but a surface or auth or sanitizer detail bites at runtime. Running this checklist catches each class of issue before the user does.

Cross-reference: `docs/reference/chat-surface-capability-matrix.md` — the surface × feature mátrix this checklist informs and updates.

## 1. Surface verification

**Question:** Which chat/voice surfaces will this bridge surface on (web chat / Discord text / Discord voice / agent skill API / control UI)?

**Why it matters:** Every surface has independent rendering and sanitizer behavior. A working agent-skill smoke does NOT imply web chat will render the result.

**Smoke test:**
```bash
# Test the new tool from CLI (agent skill API path):
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Call <new_server>__<new_tool> with sample params and describe the result." \
  --thinking off --json --timeout 600 \
  | jq '.result.payloads[0].text, .meta.toolSummary'
# Expected: tool ran, returned content, agent describes it.

# Then test on ALL listed surfaces:
# - Web chat: open https://<host>/chat, send a message that exercises the bridge
# - Discord text: @-mention the bot in a guild channel, watch it call the tool
# - Discord voice: voice-channel agents typically use the audio path; not relevant for image
# - Control UI: open the control plane on OPENCLAW_GATEWAY_CONTROL_PORT, repeat
```

Update `chat-surface-capability-matrix.md` with the verdict per surface.

## 2. Auth boundary crossing

**Question:** Does the bridge require auth from the browser? Same-origin or cross-origin? Bearer header, query token, Basic auth, or session cookie?

**Why it matters:** Browsers strip Basic auth credentials on cross-origin `<img>` fetches by design. Bearer headers don't work on `<img>` at all. Only `?token=<urlsafe>` URL params survive cross-origin image fetches. (See `image-comfyui-bridge.md:99-185` for the pattern that v0.9.8 commit landed.)

**Smoke test:**
```bash
# Bearer header probe — works for fetch() but not <img>:
curl -i -H "Authorization: Bearer $TOKEN" "https://your-bridge.example.com/health"

# Query-token probe — works on <img>:
curl -i "https://your-bridge.example.com/asset?token=$URLSAFE_TOKEN"

# Cross-origin <img> in DevTools console:
# > const img = new Image(); img.src = "https://your-bridge.example.com/asset?token=$T"; document.body.appendChild(img)
# Expected: image renders. Without ?token, expected: broken-image icon.
```

Document the auth strategy in the bridge README and the matrix.

## 3. Sanitizer pass

**Question:** Which markdown-output forms survive the chat web sanitizer? `![](url)` image syntax / `[text](url)` external link / inline `<audio>` / data URI / `<img>` HTML?

**Why it matters:** OpenClaw chat web (verified through 2026.4.25) strips ALL `![](url)` image syntax and ALL `[text](https://...)` external-origin link syntax — only `mailto:` survives. Plain auto-linkified URLs in text DO survive (clickable, opens new tab).

**Smoke test:**
```bash
# Send the agent a message that asks it to reply with each form, and inspect
# the rendered chat bubble in DevTools:
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message 'Reply with EXACTLY this markdown: ![](https://example.com/cat.jpg) [click](https://example.com) plain https://example.com mailto:test@example.com' \
  --thinking off --json --timeout 60 | jq -r '.result.payloads[0].text'

# Then in DevTools console on the chat tab:
# > document.querySelectorAll('[role="article"]').forEach(el => console.log(el.innerHTML.slice(0, 800)))
# Compare what reaches the DOM vs what the agent sent.
```

Document the survival pattern.

## 4. Context-prefill cost

**Question:** Will the tool-output JSON exceed ~5KB of text in the agent's next-step prefill?

**Why it matters:** Tool-output JSON gets serialized into the prefill of the next LLM call. Image base64 (typical 256×256 PNG ~30KB → ~10K tokens) and audio base64 (1s WAV ~50KB → ~17K tokens) blow up prefill, causing slow runs and idle-watchdog trips. Defaults must be metadata-only (URL + size + dims), with an opt-in `include_base64=true` parameter for callers who genuinely need inline bytes.

**Smoke test:**
```bash
# Run the tool, inspect input-token count:
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Call <new_server>__<new_tool>" \
  --thinking off --json --timeout 90 \
  | jq '.meta.agentMeta.usage.input, .meta.agentMeta.usage.output'

# Heuristic: input token count should rise by <500 tokens per tool call.
# If it spikes by >5K → there's base64 in the prefill, redesign to metadata-only.
```

See `image-comfyui-bridge.md:99-124` for the metadata-only response shape pattern.

## 5. MCP `_attachments` content blocks

**Question:** Does the bridge emit MCP-spec image/audio content blocks alongside the text output?

**Why it matters:** The MCP spec lets tool results return `{type: "image", data: "<base64>", mimeType: "image/png"}` or `{type: "audio", ...}`. Even if the current chat surface ignores them (verified through 2026.4.25), emitting them is jövő-proof — when upstream wires the chat-side renderer, no bridge change is needed. Cost: zero (already part of the MCP response shape).

**Smoke test:**
```bash
# Inspect the raw MCP response shape from the bridge:
TOKEN=$(grep '^IMAGE_GEN_API_TOKEN=' .env | cut -d= -f2)
curl -sS -X POST http://127.0.0.1:9095/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"<your_tool>","arguments":{}}}' \
  | jq '.result.content'
# Expect: at least one element with `"type": "image"` or `"type": "audio"` if
# the bridge produces media. Plus a `"type": "text"` element with metadata.
```

## 6. Tool-prefix verification

**Question:** Does the agent use `<server>__<tool>` (double underscore) in tool calls, NOT bare `<tool>`?

**Why it matters:** Gemma 4 NVFP4 silently fails on unprefixed tool names — the tool-call envelope renders correctly but vLLM's parser drops it because the regex `[\w\-\.:]+` doesn't see the prefix. Documented in `CLAUDE.md:288-291` (issue resolved by `vllm-llm/Dockerfile` patch on 2026-04-28 for colon namespaces; underscore prefixes always worked).

**Smoke test:**
```bash
# WRONG (Gemma 4 won't reliably call):
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message 'use the "generate" tool with prompt=foo' --thinking off --json --timeout 60

# RIGHT (always works):
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message 'use the "comfyui_image__generate" tool with prompt=foo' --thinking off --json --timeout 60

# Inspect tool-call extraction in agent output:
# > .meta.toolSummary should list the prefixed tool name; if missing, the parser dropped it.
```

Always document the prefixed name in the bridge README and any prompt examples.

## 7. License gate (opt-in profile + token-gated patcher step)

**Question:** Is the bridge gated behind an opt-in compose profile AND a patcher step that env-checks before writing config?

**Why it matters:** Bridges that pull non-permissively-licensed weights (CC-BY-NC, Apache 2.0 with attribution, model-license-required) must be opt-in. The triad established by F5-TTS HU (`docs/reference/tts-stack.md`) and the Python sandbox (`docs/reference/python-sandbox.md`):
1. Compose profile (`profiles: ["<name>"]`) parks the service unless `COMPOSE_PROFILES=<name>` includes it.
2. Token env (`OPENCLAW_<NAME>_API_TOKEN`) defaults empty.
3. Patcher step is env-gated — when the token is empty, it cleanly removes the config block (no zombie references).

**Smoke test:**
```bash
# With token unset:
grep "$NEW_TOOL_TOKEN" .env || echo "(unset)"
docker compose up -d --force-recreate openclaw-config-init
docker logs ${PROJ}openclaw-config-init 2>&1 | grep -E "$NEW_TOOL|skipped|removed"
# Expect: '[patch-config] $NEW_TOOL_TOKEN unset — removed mcp.servers.<new_server>.'
# OR a clean skip (no config write, no error).
```

Refer to patcher step 18-21 (in `patch-config.mjs`) for the canonical idempotent pattern.

## 8. Rotate-secrets recipe

**Question:** Does `./rotate-secrets.sh <NEW_TOKEN_KEY>` correctly tell the operator which compose-projects to recreate?

**Why it matters:** Cross-compose bridges (image-comfyui is its own `docker-compose.yml` joined to the main stack via external network) can require recreating two compose projects — easy to miss. The rotate-secrets script must print explicit `up -d --force-recreate` commands for both.

**Smoke test:**
```bash
# Dry-run the rotation:
./rotate-secrets.sh $NEW_TOKEN_KEY --dry-run 2>&1 | tail -10
# Expected output: explicit recreate commands for the main compose AND any
# external-compose bridge that uses the rotated token.

# Real rotation + full recreate cycle:
./rotate-secrets.sh $NEW_TOKEN_KEY
# Then run the printed commands. Verify the bridge is healthy:
docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E "$NEW_TOOL|openclaw"
```

If the rotate-secrets script doesn't know about your new bridge, add it to the script's known-keys list.

## When to run this checklist

- **Every new media-MCP bridge** (or major rev that changes the response shape)
- **Every gateway image upgrade** (especially major version bumps where chat sanitizer or speechSynthesis path may move)
- **After any `rotate-secrets.sh` run** that touched a media-bridge token
- **As part of release-readiness** for any commit that adds a new tool to the agent's context

## When NOT to run

- For pure agent-skill API consumers (CLI scripts, batch processing) — the `--json` output is canonical there; no surface concerns.
- For voice-channel-only bridges (rare; the voice STT/TTS pipeline is the only surface).

## Tracking

When a checkpoint reveals a new limit:
1. Add it to `chat-surface-capability-matrix.md` as a new row.
2. Add a `memory/project_<name>.md` note (operator-side, not committed) with the diagnostic chain.
3. If the limit needs an env-knob workaround, follow the pattern in `patch-config.mjs` steps 11/20/21.
4. Add a `docs/TROUBLESHOOTING.md` entry for the symptom.
