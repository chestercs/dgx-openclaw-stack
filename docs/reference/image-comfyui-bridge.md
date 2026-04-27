# `openclaw-image-comfyui` — image-generation bridge

A thin MCP bridge that exposes `comfyui_image__*` tools to the OpenClaw
agent and proxies generation to the operator's existing ComfyUI install.
Lives in a separate compose file
(`openclaw-image-comfyui/docker-compose.yml`), opt-in via the
`image-gen` profile and a non-empty `IMAGE_GEN_API_TOKEN`.

This document explains the design choices, threat model, and the bits
that bit somebody once and ended up shaping the architecture.

## Why a bridge — and why MCP

OpenClaw exposes three integration paths an external image generator
could plug into:

1. **OpenAI-compat `/v1/images/generations`** — natural fit, but
   `tools.media.image` doesn't exist in the OpenClaw schema as of
   2026-04-26 (verified against `docs.openclaw.ai`). Any wiring at
   this surface would be speculative.
2. **A bespoke HTTP tool** — write a one-off plugin schema, register
   it as a `plugins.entries.<name>` block. Maintenance-iszony: any
   schema change upstream breaks our wiring.
3. **MCP (Model Context Protocol) server** — `mcp.servers.<name>`
   with `transport: streamable-http` is the path the v0.8.0 Python
   sandbox already uses; OpenClaw's gateway does the JSON-RPC
   handshake, surfaces tools by name, and we own only the wire
   handler.

The MCP path is the verified one. We mirror the python-sandbox layout
exactly: ~250 LOC FastAPI handler, no SDK dependency, hand-rolled MCP
wire (initialize / tools/list / tools/call / Mcp-Session-Id echo).

## Why not run ComfyUI in the main stack

Two reasons:

1. **The operator already runs one.** A full ComfyUI install with the
   user's chosen models, custom nodes, and base directory layout takes
   meaningful disk and configuration. Duplicating it inside the main
   compose would mean two ComfyUI processes competing for the same
   GPU.
2. **License isolation.** The bridge ships **no model weights**.
   Operators pick checkpoints (FLUX, Pony XL, Illustrious XL, RealVisXL,
   adult fine-tunes, …) under whichever upstream license they accept;
   the bridge stays content- and license-agnostic.

The price is one cross-compose hop: the bridge runs in its own compose
and joins the main stack's bridge network as `external: true`. The
main stack must be `up` at least once before the bridge can start
(otherwise the named network doesn't exist yet).

## Why host-gateway, not a shared external network

We considered three ways for the bridge to reach the operator's
ComfyUI:

| Option | Pros | Cons |
|--|--|--|
| `extra_hosts: host.docker.internal:host-gateway` (chosen) | Zero changes to the user's existing ComfyUI compose. Works on any Linux 20.10+ host out of the box. | Only works when ComfyUI is on the same physical host. |
| Shared external Docker network | Bridge DNS by service name, no port-publish needed. | Requires editing the user's `petyus-gpt` compose to attach the network. Cross-compose-project breaks the user's existing layout. |
| LAN IP (e.g. `http://192.168.1.50:13036`) | Works for ComfyUI on a different box. | Operator has to set the IP manually, port-publish on the LAN side. |

The default targets `http://host.docker.internal:13036` and the user
overrides via `COMFYUI_URL` for the LAN path. Same flexibility as the
remote-vLLM pattern documented in `docs/CUSTOMIZATION.md`.

## Workflow template architecture

A workflow file under `server/workflows/` is a literal ComfyUI
**API-format** export (the `Save (API Format)` button in the queue
panel) plus an extra `_metadata` block at the top that the bridge
strips before submission. The metadata declares:

- `name`, `description` — surfaced via `comfyui_image__list_workflows`.
- `checkpoint_required: bool` — when true, the bridge refuses to run
  if the workflow's `CheckpointLoaderSimple` node still has the
  `REPLACE_ME.safetensors` placeholder AND the caller didn't pass
  `checkpoint=`.
- `defaults: { ... }` — fallback values for `steps`, `cfg`, `sampler`,
  etc. when the caller doesn't override.
- `targets: { <param>: { node, input } }` — explicit mapping from
  bridge parameter names to node ids and `inputs.*` keys. Required
  for `prompt` and `negative` (positive vs negative
  `CLIPTextEncode` is otherwise ambiguous). Optional for the rest —
  the bridge falls back to first-`class_type` lookup
  (`CheckpointLoaderSimple`, `EmptyLatentImage`, `KSampler`,
  `SaveImage`).

**Substitution is by node-id + input-key, never by string-replace on
the serialized JSON.** A user prompt may legitimately contain `${…}`
patterns (LoRA stack syntax, embedding refs); a regex over the JSON
text would corrupt them.

The `client_id` field is regenerated per request (`uuid.uuid4().hex`)
so the WebSocket feed (which we don't currently consume) would route
cleanly if we ever needed it.

## Response shape: metadata-only by default (v0.9.3+)

The `generate` tool result no longer embeds the PNG bytes by default.
Each `images[]` entry contains `format`, `filename`, `subfolder`, `type`,
`node_id`, `width`, `height`, `byte_size`, and `fetch_url_path` — the
relative URL on ComfyUI's HTTP API to retrieve the actual file. The
top-level result also carries `comfyui_base_url`, so the agent (or a
chat surface) can reconstruct a full URL: `{comfyui_base_url}{fetch_url_path}`.

Why default-off: a single 512×512 PNG is ~30-100 KB, base64-encoded
~40-130 KB chars ≈ 10-30K tokens. A 1024×1024 image gets to 50-100K
tokens easily. Embedding that in the agent's chat history forces the
next LLM call's prefill to chew through every uncached token; on
Gemma 4 NVFP4 (~16 tok/s prefill for new content) a single 1024×1024
generate balloons the run from ~10s to >50 minutes — far past any
realistic `--timeout`. The v0.9.0/v0.9.2 GB10 smoke tests reproduced
this exactly: direct MCP `tools/call generate` returned in 6.25s, the
agent-wrapped run timed out at 600s while the LLM was still prefilling
the response.

Pass `include_base64=true` only when you need the bytes inside the
agent reply (e.g., a follow-up tool call that hashes them, or a
specialty chat surface that consumes data URIs from tool results).
The bridge itself doesn't change — `IMAGE_GEN_MAX_OUTPUT_BYTES` still
caps the total base64 payload when it IS requested.

## Chat-side image rendering: known browser-security limit

**As of OpenClaw 2026.4.22 + Chrome/Firefox 2026.04, the chat surface
cannot inline-render generated images.** Two independent browser
security layers block it, verified empirically against
`vision.petyuspolisz.com` end-to-end on 2026-04-27:

1. **Markdown sanitizer.** The chat's renderer keeps `mailto:` links
   and bold/italic/code/list, but drops `![alt](url)` image syntax
   entirely (only the `alt` becomes a `<p>` in the DOM, no `<img>`)
   and drops `[text](https://...)` link syntax to arbitrary external
   origins (only the link text remains). This is internal to
   openclaw and not configurable from the bridge side.
2. **Cross-origin Basic auth.** Even if you bypass the sanitizer
   (e.g. with a Tampermonkey userscript that re-injects `<img>` tags
   from the rendered text), the browser refuses to send cached HTTP
   Basic auth credentials on a cross-origin `<img>` fetch. Verified:
   `new Image().src = 'https://vision.example.com/...'` from the
   `claw.example.com` origin fires `onerror` immediately. Same with
   `fetch(url, {credentials: 'include'})` → `Failed to fetch`.

The `display_markdown` field is therefore best-effort: useful for a
future chat surface that adds image-content support, and useful as a
literal "copy this URL" payload that lands in the tool-output JSON.

### Recommended workflow

When the agent calls `comfyui_image__generate`, the tool-output
bubble shows the response JSON in the chat. The user copies the
`display_markdown` URL (or `comfyui_external_url + fetch_url_path`)
from there and opens it in a new tab. The vision-host's Basic auth
credentials cache applies on direct navigation, so the image opens
transparently.

### Future paths (not wired in v0.9.x)

- **Same-origin proxy via gateway canvas**: save to
  `${OPENCLAW_CONFIG_DIR}/canvas/` (host-bound) and serve via
  `/__openclaw__/canvas/<name>` on the openclaw gateway. The chat's
  session cookie auths the request, no Basic auth issue. The
  endpoint exists (returns 401 unauthenticated as of 2026-04-27);
  nailing down the auth flow is the work.
- **Workspace bind + agent `read` tool**: save to
  `${OPENCLAW_WORKSPACE_DIR}/comfyui-bridge/<id>.png` and have the
  agent issue a follow-up `read` call. If the chat surface renders
  the resulting attachment inline (verify before relying), this
  works without bridge code changes.
- **Full base64 inline** (`include_base64=true`): the response
  carries the bytes; the LLM prefill for the next call still chews
  through 50K+ tokens, so this is impractical for routine use.

Track upstream openclaw releases for native MCP image-content
rendering or a server-side proxy.

## Concurrency: single-flight by default

`IMAGE_GEN_MAX_CONCURRENCY=1` is the default. ComfyUI runs on the same
GB10 GPU as vLLM; concurrent generation pauses LLM token generation
and is observable as multi-second user-facing stalls. The bridge
serializes calls via `asyncio.Semaphore(1)` so two `generate` requests
don't pile up on the GPU at once.

Set to `0` for pass-through (let ComfyUI's internal queue handle
ordering) only if your ComfyUI runs on a different GPU than vLLM.
Higher integers (`2`, `3`, …) increase the bridge's parallelism but
the GPU still bottlenecks at one render at a time.

## Async render flow

ComfyUI's `POST /prompt` returns `{prompt_id}` immediately and the
render proceeds asynchronously. We poll `/history/{prompt_id}`:

- Initial interval: `IMAGE_GEN_POLL_INTERVAL_S` (default 0.5s)
- Backoff multiplier: 1.5
- Cap: `IMAGE_GEN_POLL_BACKOFF_MAX_S` (default 2.0s)
- Total budget: `IMAGE_GEN_TIMEOUT_S` (default 600s)

On success: walk the history entry's `outputs` for every `SaveImage`
node, fetch each filename via `GET /view?filename=…&type=…&subfolder=…`,
base64-encode, return in the MCP `tool_result.content[0].text`
envelope as a structured JSON blob.

On timeout: best-effort `POST /queue {delete:[id]}` + `POST /interrupt`,
then surface a structured MCP error.

On `404 Not Found` from `/history` after we've seen the prompt at
least once: the ComfyUI process appears to have restarted
mid-generation. Surface as `ComfyUIRestartedError` rather than
hanging until the timeout.

## Threat model

- **Operator-trusted prompt only.** The bridge has no content filter;
  it submits whatever the agent prompts it with. The operator's
  choice of upstream models + the agent's prompt hygiene are the
  controls.
- **Bridge → ComfyUI hop is unauth'd.** ComfyUI ships without auth
  by default. Mitigation: ComfyUI's port (`13036` in the user's
  reference setup) should remain loopback-bound on the host. If the
  user publishes it on a routable interface, an unauth'd remote
  attacker can drive image generation directly — that's a known
  ComfyUI risk, orthogonal to this bridge.
- **Bridge → agent surface is Bearer-protected.** `IMAGE_GEN_API_TOKEN`
  on every `POST /mcp`. Token rotation via `./rotate-secrets.sh
  IMAGE_GEN_API_TOKEN`; rotation requires a cross-compose
  force-recreate (the bridge is in a separate compose file from the
  gateway).
- **Bridge container is hardened**: `cap_drop: [ALL]`,
  `no-new-privileges`, `mem_limit: 1024m`, `cpus: 1`, non-root user
  `1000:1000`. The bridge is a thin HTTP wrapper — no torch, no
  GPU, no model files — so the attack surface is limited to httpx /
  pillow / FastAPI.

## Verification recipes

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}

# 1. Static sanity
docker compose -f openclaw-image-comfyui/docker-compose.yml --env-file .env --profile image-gen config
node --check patch-config.mjs

# 2. Bridge → ComfyUI reachability (after both composes are up)
docker exec ${PROJ}openclaw-image-comfyui curl -fsS http://host.docker.internal:13036/system_stats | head -c 200

# 3. MCP tools/list
TOKEN=$(grep '^IMAGE_GEN_API_TOKEN=' .env | cut -d= -f2-)
curl -sS -X POST http://127.0.0.1:9095/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | jq '.result.tools[].name'

# 4. End-to-end generate via the agent (after the patcher has wired step 19)
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Use comfyui_image__generate to render a 512x512 image of a red cube on a white background. Reply with the image's prompt_id." \
  --thinking off --json --timeout 600 \
  | jq '.toolSummary, .finalAssistantVisibleText'

# 5. Cleanup (token unset → entry removed)
sed -i.bak 's/^IMAGE_GEN_API_TOKEN=.*/IMAGE_GEN_API_TOKEN=/' .env
docker exec ${PROJ}openclaw-config-init node /opt/patch-config.mjs
docker exec ${PROJ}openclaw-cli sh -c 'cat ~/.openclaw/openclaw.json' \
  | jq '.mcp.servers.comfyui_image // "removed"'
```

## Known limits

- **No img2img yet.** The shipped workflows are text-to-image only.
  Adding img2img needs (a) a workflow with a `LoadImage` node, (b) a
  bridge code path to ferry uploaded base bytes into ComfyUI's input
  directory, and (c) a tool argument for the source image. Pending a
  use case that asks for it.
- **No streaming progress.** A 25-step SDXL render takes 20-40s; the
  agent waits in the tool call until the PNG arrives. Bound via
  `IMAGE_GEN_TIMEOUT_S`.
- **One ComfyUI per bridge.** `COMFYUI_URL` is a single endpoint;
  the bridge does not pool across multiple ComfyUI instances.
- **Workflow files at startup.** Adding a new template under
  `server/workflows/` requires a bridge restart for it to be picked
  up (the bind mount makes the directory live, but the loader runs
  once at `app.on_event("startup")`).

## See also

- `docs/CUSTOMIZATION.md` → "Image generation bridge" section for the
  operator-facing walkthrough.
- `docs/ARCHITECTURE.md` → "Image-gen bridge subsystem" subsection
  for the cross-compose network architecture in context.
- `docs/TROUBLESHOOTING.md` → "Image-gen bridge" section for the
  three most common failure modes.
- `openclaw-image-comfyui/README.md` for the activation steps and
  smoke tests.
- `openclaw-image-comfyui/server/workflows/README.md` for the workflow
  authoring guide.
