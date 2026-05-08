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

Status verified 2026-04-28 against openclaw `2026.4.25` (latest release at
that date). See `docs/reference/chat-surface-capability-matrix.md` for the
broader surface × feature mátrix.

- **Path A — Same-origin canvas via `[embed]` shortcode** (research-confirmed
  2026-04-28, **bridge POC shipped in v0.10.0 behind `IMAGE_GEN_CANVAS_DIR`
  env-gate**, default OFF, **end-to-end verified 2026-04-28 on GB10 against
  openclaw `2026.4.22`**): emit `[embed url="/__openclaw__/canvas/<file>" /]`
  in the agent reply. The shortcode (added in `2026.4.11` PR #64104) is
  parsed by the chat normalizer into a structured iframe directive — it
  bypasses the DOMPurify `<img>` sanitizer entirely. The URL is whitelisted
  to `/__openclaw__/canvas/...` and `/__openclaw__/a2ui/...` (parser-validated
  same-origin only); arbitrary http(s) URLs are gated by the dangerous
  `gateway.controlUi.allowExternalEmbedUrls=true` flag (default `false` —
  leave it that way). Iframe sandbox controlled by `gateway.controlUi.embedSandbox`
  (`"strict"` | `"scripts"` | `"trusted"`; default value if unset is `"scripts"`
  — verified live).
  
  **End-to-end mechanism (verified via Chrome DevTools 2026-04-28):**
  
  When the agent emits `[embed url="/__openclaw__/canvas/<file>" /]`,
  the chat normalizer rewrites the iframe `src` to
  `/__openclaw__/cap/<24-char-urlsafe-token>/__openclaw__/canvas/<file>`.
  The `cap/<token>/` prefix is a one-shot capability token the gateway
  issues per chat session — it gates the iframe's fetch without exposing
  the chat session's bearer or cookies to the iframe's content origin.
  No 401 on the iframe load: the token IS the auth.
  
  Both `.png` and `.html` files render through this path — verified
  by writing both file types into the canvas dir and observing two
  iframes in the chat DOM, both `visible: true`, `301×420 px`,
  `sandbox="allow-scripts"`, `title="Canvas"`. The shortcode is
  MIME-agnostic; the iframe loads whatever the gateway returns at
  the path.
  
  **Bridge implementation (shipped v0.10.0):**
  - `app.py` reads `IMAGE_GEN_CANVAS_DIR` env. Empty (default) → legacy
    cross-origin emission. Set → mirrors PNG bytes to that dir AND
    emits the `[embed]` shortcode in `display_markdown`. Write failures
    fall through to the legacy form gracefully (the bridge does not
    fail generation on a canvas-dir write error).
  - `docker-compose.yml` has the env passthrough wired and a commented
    bind-mount line `${OPENCLAW_CONFIG_DIR}/canvas:/canvas:rw` —
    operator uncomments to activate.
  - `_attachments` MCP block stays in place (zero cost, future-proofs
    for Path C).
  
  **Live deploy state (GB10, 2026-04-28):**
  
  - Canvas host path: `${OPENCLAW_CONFIG_DIR}/canvas` (matches gateway's
    `~/.openclaw/canvas/` mount, owner UID 1000:1000).
  - `embedSandbox` default in upstream `2026.4.22`: `"scripts"` (verified
    via DOM iframe attribute on the live deploy).
  - `[embed]` accepts `.png` URLs alongside `.html` — MIME-agnostic
    (DOM probe: two iframes rendered with both file types).
  - Capability-token rewrite `/__openclaw__/cap/<token>/...` is the
    auth mechanism (chat session issues per-iframe token, gateway
    accepts).
  
  Smoke-test recipe in `docs/reference/chat-surface-capability-matrix.md`
  → "`[embed url=...]` shortcode in web chat".

- **Path C — Native MCP image-content rendering**: the bridge already emits
  `_attachments` MCP `{type: "image", data: <base64>, mimeType: "image/png"}`
  blocks per `app.py:343-353, 480-488`. **Verified 2026-04-28: chat web UI
  still ignores these blocks** through `2026.4.25`. The 2026.4.23-25 release
  notes mention only `--background` flag for the CLI image-gen subcommand —
  no chat-side image renderer changelog. Track upstream releases (the bundle
  has `attachments` keyword in `dispatch-acp-*.js` / `protocol-*.js` for the
  bridge→gateway payload channel, NOT for chat-side render). When upstream
  ships, this path lights up with zero bridge code change.

- **Path B — Workspace bind + agent `read` tool**: save to
  `${OPENCLAW_WORKSPACE_DIR}/comfyui-bridge/<id>.png` and have the
  agent issue a follow-up `read` call. The `read` tool returns MCP
  content blocks — IF it recognizes PNG mime type and emits an image
  block (verify before relying), AND if the chat surface renders MCP
  image content (Path C). Path A is now strictly preferable, so Path B
  drops to fallback-of-fallback.

- **Full base64 inline** (`include_base64=true`): the response
  carries the bytes; the LLM prefill for the next call still chews
  through 50K+ tokens, so this is impractical for routine use.

Recommended priority for new POC sprints: Path A (verified viable, blocked
only on a single SSH probe to confirm `.png` MIME acceptance + canvas dir
location). Path B as fallback if `[embed]` rejects non-HTML.

### Canvas dir housekeeping

The bridge writes one PNG + one HTML wrapper per generation into
`${OPENCLAW_CONFIG_DIR}/canvas/`. **No automatic cleanup is implemented**
in the v0.10.5 bridge — files accumulate forever. Each generation is
~50-200 KB (PNG) + ~300 B (HTML wrapper). Sustained image-gen by an
active friend group could reach ~1 GB after a few thousand calls.

**Operator cleanup options:**

```bash
# One-shot: delete files older than 7 days from the canvas dir
ssh -i KEY -l user host \
  'docker exec openclaw-gateway find /home/node/.openclaw/canvas \
     -name "comfyui-*" -type f -mtime +7 -delete'

# As an opt-in cron on the host:
sudo tee /etc/cron.daily/openclaw-canvas-prune <<'EOF'
#!/bin/sh
# Daily prune of bridge-emitted canvas files older than 7 days.
docker exec openclaw-gateway find /home/node/.openclaw/canvas \
  -name "comfyui-*" -type f -mtime +7 -delete 2>/dev/null
EOF
sudo chmod +x /etc/cron.daily/openclaw-canvas-prune
```

The `comfyui-*` filename prefix scope-isolates the bridge-written files
from any operator-managed canvas content (canvas SKILL drops, A2UI
documents, etc.) — never delete by glob without that prefix.

Future bridge improvement: an `IMAGE_GEN_CANVAS_MAX_AGE_DAYS` env knob
would prune on each generate. Not implemented yet (deletion of files
is a destructive operation; the bridge defers to operator cron).

## Token-auth via `auth_request` (v0.9.8–v0.9.10, what actually shipped)

Default for v0.9.8+: the bridge's `/view` URL is gated by an NGINX
`auth_request` chain that delegates token validation to the bridge's
`GET /auth-validate` endpoint, so the secret never appears in the
proxy admin config. The chat-image URL the bridge embeds carries
`?token=<COMFYUI_VIEW_TOKEN>` and works on direct navigation
(operator clicks the URL out of the tool-output JSON, opens in a
new tab) — no Basic auth dialog, no per-origin credential cache.

The bridge's `app.py` reads the token from the request query string
first; when empty (the `auth_request` sub-request has a static URI
and parent `$args` don't propagate), it falls back to parsing the
`X-Original-URI` header that the proxy sets to `$request_uri`. The
constant-time compare uses `secrets.compare_digest`.

### NPM custom-location split

Two custom locations on the proxy host:

```nginx
# /auth-validate — internal, called only by NGINX's auth_request
location /auth-validate {
    internal;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header X-Original-URI $request_uri;
    # NPM auto-emits proxy_pass http://<host>:9095 from the form fields
}

# /view — chat-image fetch endpoint, gated by auth_request
location /view {
    auth_request /auth-validate;
    # NPM auto-emits proxy_pass http://<comfyui-host>:13036
}
```

Plus three Access-List settings that have to line up:

1. **Satisfy Any** on Details — so the auth_request 200 alone
   satisfies the request (otherwise `Satisfy All` requires Basic
   creds AND IP-allow AND auth_request all together).
2. **No `Allow all` on Rules** — drop it. Just leave the
   auto-fallback `deny all`. With `Satisfy Any` + `Allow all` an IP
   match alone passes everything, leaking `/view` to anyone who
   knows the URL.
3. **Don't add `auth_basic off;`** to either custom location's
   Advanced. NPM already emits `auth_basic "Authorization required";`
   from the Access List, and a second `auth_basic` directive is an
   `[emerg]` config error. The Satisfy Any setting is the lever.

### Verify recipe

```bash
TOKEN=$(grep '^COMFYUI_VIEW_TOKEN=' .env | cut -d= -f2-)
# /view + valid token  -> 200 (chat-image works)
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://vision.example.com/view?filename=foo.png&type=output&subfolder=openclaw-bridge&token=$TOKEN"
# /view no token       -> 401 (token gate)
# /view wrong token    -> 401 (constant-time compare)
# /api/view no creds   -> 401 (Basic auth challenge for the UI assets)
# /                    -> 401 (Basic auth challenge for the UI HTML)
# /auth-validate direct -> 404 (`internal;` blocks external access)
```

### Token rotation

```bash
./rotate-secrets.sh COMFYUI_VIEW_TOKEN
docker compose -f openclaw-image-comfyui/docker-compose.yml \
               up -d --force-recreate openclaw-image-comfyui
```

The NPM admin GUI doesn't need editing — the proxy just proxies the
token over to the bridge for validation, and the bridge picks up the
new env on recreate.

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

## Recommended model bundle for max-quality 4K

The bridge ships no model weights. The five `flux-krea-*.json`
workflow templates added in v0.11.0 expect a specific bundle of
open-weight models on the operator's ComfyUI install. Running this
download is opt-in — without it, the bundle's templates fail at
ComfyUI's prompt-validate step ("model not found"), but the legacy
`flux-schnell` / `sdxl-base` templates keep working unchanged.

Total disk ≈ 60-70 GB. Targets are paths inside the user's ComfyUI
basedir (`models/diffusion_models/`, `models/clip/`, etc.).

```bash
# Run on the host that owns the ComfyUI basedir. Pre-req: HF_TOKEN env
# or `huggingface-cli login` for FLUX-Krea-dev (gated behind the
# FLUX.1-dev license click-through on huggingface.co — accept once,
# token works thereafter).
export HF_HUB_ENABLE_HF_TRANSFER=1
BASEDIR=/path/to/comfyui/basedir   # adjust to your install

# Primary FLUX stack (~34 GB)
huggingface-cli download black-forest-labs/FLUX.1-Krea-dev \
    flux1-krea-dev.safetensors --local-dir "$BASEDIR/models/diffusion_models/"
huggingface-cli download comfyanonymous/flux_text_encoders \
    t5xxl_fp16.safetensors clip_l.safetensors --local-dir "$BASEDIR/models/clip/"
huggingface-cli download black-forest-labs/FLUX.1-Krea-dev \
    ae.safetensors --local-dir "$BASEDIR/models/vae/"

# SUPIR backbone (~12 GB) — required for the 4k-supir / 4k-adult /
# 4k-adult-realism templates. Note both SUPIR weights AND the SDXL
# refiner backbone (Juggernaut) live under models/checkpoints/ because
# SUPIR_model_loader (legacy v1) reads its `sdxl_model` AND
# `supir_model` COMBOs from the checkpoints/ directory.
huggingface-cli download Kijai/SUPIR_pruned \
    SUPIR-v0Q_fp16.safetensors --local-dir "$BASEDIR/models/checkpoints/"
huggingface-cli download stabilityai/sdxl-vae \
    sdxl_vae.safetensors --local-dir "$BASEDIR/models/vae/"
huggingface-cli download RunDiffusion/Juggernaut-XL-v9 \
    Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors --local-dir "$BASEDIR/models/checkpoints/"

# Upscaler (~70 MB) — used by the 4k-tiled fallback path
huggingface-cli download Kim2091/UltraSharp \
    4x-UltraSharp.pth --local-dir "$BASEDIR/models/upscale_models/"

# LoRAs (~1.3 GB total) — realism stack used by 4k-supir; flux-uncensored
# adds the adult variants. Files are renamed to match the workflow
# templates' baked-in filenames.
huggingface-cli download strangerzonehf/Flux-Super-Realism-LoRA \
    super-realism.safetensors --local-dir "$BASEDIR/models/loras/"
huggingface-cli download XLabs-AI/flux-RealismLora \
    lora.safetensors --local-dir "$BASEDIR/models/loras/" \
    && mv "$BASEDIR/models/loras/lora.safetensors" \
          "$BASEDIR/models/loras/flux-realism-xlabs.safetensors"
huggingface-cli download enhanceaiteam/Flux-uncensored \
    lora.safetensors --local-dir "$BASEDIR/models/loras/" \
    && mv "$BASEDIR/models/loras/lora.safetensors" \
          "$BASEDIR/models/loras/flux-uncensored-v2.safetensors"
```

License notes (operator-owned, same posture as F5-TTS HU's CC-BY-NC
weights):

- **FLUX.1-Krea-dev / FLUX.1-dev**: free for personal / research use under
  the FLUX.1-dev license; commercial deployment requires a separate
  agreement with Black Forest Labs. Click-through on the HF model card
  acts as license acceptance.
- **flux-uncensored-v2**: non-commercial; included only in the `4k-adult*`
  templates. Ship with caution — verify the upstream model card before
  routing user-facing traffic through it.
- **Juggernaut-XL-v9**: SDXL fine-tune under the SDXL community license;
  used here only as the SUPIR refiner backbone, not as a generation model.
- **super-realism / flux-realism-xlabs**: model-card-stated open-weights
  for personal use; verify the upstream model card if planning commercial
  deployment.
- **4x-UltraSharp**: public-domain ESRGAN model.
- **SUPIR-v0Q (pruned by kijai)**: research weights; non-commercial.

## Required custom nodes for the bundle

The bundle templates depend on three custom-node packs that aren't in
the upstream ComfyUI core. Install them inside the user's ComfyUI compose
project's `basedir/custom_nodes/` directory (each `git clone` plus its
own `pip install -r requirements.txt` if present), then restart the
ComfyUI container.

```bash
cd "$BASEDIR/custom_nodes"

# SUPIR diffusion-restoration upscaler — used by the 4k-supir / 4k-adult
# / 4k-adult-realism templates. The kijai fork is the maintained one.
git clone https://github.com/kijai/ComfyUI-SUPIR.git
( cd ComfyUI-SUPIR && pip install -r requirements.txt )

# Ultimate SD Upscale — used by the 4k-tiled fallback. ssitu's fork is
# the most actively maintained ComfyUI port.
git clone https://github.com/ssitu/ComfyUI_UltimateSDUpscale.git
# (no requirements.txt — pure-python on top of ComfyUI's own deps)

# rgthree-comfy — workflow-quality-of-life nodes (mute/bypass groups,
# conditional skip-on-mute). Optional but recommended; the shipped
# templates work without it.
git clone https://github.com/rgthree/rgthree-comfy.git

# ComfyUI-Manager — UI-based add-on installer for any future expansion.
# Optional but recommended; lets the operator install additional nodes
# from the ComfyUI web UI without further SSH copy-paste.
git clone https://github.com/ltdrdata/ComfyUI-Manager.git
( cd ComfyUI-Manager && pip install -r requirements.txt )
```

After install + ComfyUI restart, the new node types
(`SUPIR_model_loader_v2`, `UltimateSDUpscale`, …) appear under the
right-click "Add Node" menu and the workflow templates parse without
"unknown node type" errors.

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
