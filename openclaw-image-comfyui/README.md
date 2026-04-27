# openclaw-image-comfyui

Thin **MCP bridge** that lets the OpenClaw agent drive image generation
on the operator's existing ComfyUI install. The bridge speaks MCP
Streamable-HTTP on `POST /mcp`, translates tool calls into ComfyUI
workflow submissions, polls for completion, and returns base64-encoded
PNGs back to the agent.

This service is **opt-in** and lives in a **separate compose file** —
not in the main stack `docker-compose.yml`. Three things must be true
before the agent sees the `comfyui_image__*` tools:

1. `IMAGE_GEN_API_TOKEN` is set in the main `.env`
   (bootstrap.sh prompt 3e or `./rotate-secrets.sh IMAGE_GEN_API_TOKEN`).
2. The bridge is running (`docker compose -f
   openclaw-image-comfyui/docker-compose.yml --profile image-gen up -d
   --build`).
3. The patcher has been re-run so the gateway picks up
   `mcp.servers.comfyui_image` (`docker compose up -d --force-recreate
   openclaw-config-init openclaw-gateway openclaw-cli` from the repo
   root).

If any of the three is missing the bridge stays parked — same posture
as F5-TTS HU and the Python sandbox.

## What's in the box

## Chat-side image rendering — known limit

**The OpenClaw chat surface (2026.4.22) cannot inline-render generated
images.** Two browser security layers conspire here:

1. The chat's markdown sanitizer drops `![alt](url)` image syntax
   entirely (verified in DOM: only the `alt` text becomes a `<p>`,
   no `<img>` tag is emitted). Plain markdown links survive — but
   only for trusted protocols like `mailto:`; arbitrary `https`
   external URLs are dropped too.
2. Even if you bypass the sanitizer (e.g. via a userscript that
   injects `<img>` tags from the rendered text), the browser will
   not send cached HTTP Basic auth credentials to a cross-origin
   `<img>` request. Verified in-browser: `new Image().src =
   'https://vision.example.com/...'` from the `claw.example.com`
   origin fires `onerror` immediately — the cached creds aren't
   exposed to image fetches across origins by design.

**Recommended workflow:** the `display_markdown` field of the tool
output JSON contains the full HTTPS URL. When the agent replies, the
tool-output bubble in the chat shows the JSON; copy the URL from
there and open it in a new tab. Your browser's cached vision-host
Basic auth credentials apply on direct navigation, so the image
opens transparently.

If you genuinely need inline-render (e.g. for a presentation), two
non-trivial paths:

- **Same-origin proxy**: serve the image off the same origin as the
  chat (e.g. via the openclaw gateway's `/__openclaw__/canvas/`
  endpoint or a custom reverse-proxy mount) so the chat's session
  cookie does the auth instead of Basic auth.
- **Workspace bind + agent `read` tool**: have the bridge save to
  `${OPENCLAW_WORKSPACE_DIR}` and use the agent's built-in `read`
  tool to surface the file as an attachment (depends on whether the
  chat surface renders attachments inline — verify before relying).

Neither path is wired in v0.9.x. Track upstream openclaw releases
for a native chat-side image-render mechanism.

## Token-protected proxy (alternative to Basic auth)

Set `COMFYUI_VIEW_TOKEN=<long-random-string>` in `.env`. The bridge
then appends `?token=<value>` to every URL it puts in `display_markdown`
(e.g. `https://vision.example.com/view?filename=...&token=<value>`),
AND exposes a `GET /auth-validate?token=...` endpoint your reverse-proxy
can call from `auth_request` to validate the token without ever holding
the secret itself.

The recommended setup is **per-location split** on your proxy host —
the ComfyUI UI keeps Basic auth (you log into it from a browser), and
only `/view` (chat-image fetches) is token-validated:

| Path | Auth | Used by |
|--|--|--|
| `/` (UI HTML/JS/CSS/WS) | Basic auth | You, in the browser |
| `/api/view?...` | Basic auth | The ComfyUI UI loading its own assets |
| `/view?...&token=...` | URL-param token | The bridge / chat-image direct-nav |

### NPM setup

1. **`.env`**: set `COMFYUI_VIEW_TOKEN=<openssl rand -base64 48>` and
   `IMAGE_GEN_BIND=0.0.0.0` so the bridge's `/auth-validate` is
   reachable from the NPM container's network namespace via the
   host LAN IP. The MCP endpoint (`POST /mcp`) stays Bearer-protected;
   `/auth-validate` uses a constant-time compare against the env var.
2. **NPM admin** → your ComfyUI proxy host → **Custom locations**:
   - **Add location** `/auth-validate`:
     - Scheme `http`, hostname `<your-host-LAN-IP>`, port `9095`
     - Save → Edit → "Edit Custom location" → **Advanced**:
       ```nginx
       internal;
       proxy_pass_request_body off;
       proxy_set_header Content-Length "";
       proxy_set_header X-Original-URI $request_uri;
       ```
       (the `internal;` directive prevents external clients from
       hitting `/auth-validate` directly — only NGINX's own
       `auth_request` sub-request can.)
   - **Add location** `/view`:
     - Scheme `http`, hostname `<your-comfyui-LAN-IP>`, port `13036`
     - Save → Edit → **Advanced**:
       ```nginx
       auth_request /auth-validate;
       auth_basic off;
       ```
3. **Access** tab — keep the Basic auth on the parent proxy host (it
   covers everything except the two custom locations).
4. **Save**.

The proxy admin GUI now contains zero secrets — only the bridge
container's `.env` has `COMFYUI_VIEW_TOKEN`. Rotate the token via
`./rotate-secrets.sh COMFYUI_VIEW_TOKEN` and recreate the bridge;
no NPM edit needed.

### Why this is better than Basic auth for chat-image fetches

- **No browser auth dialog** when clicking a chat-image URL — the
  token rides in the URL itself.
- **Cross-origin `<img>` fetches work** (Basic auth headers don't
  survive cross-origin image requests). If a future chat surface
  bypasses the markdown sanitizer with a userscript, the
  `<img src="...&token=xyz">` tag will load the image transparently.
- **Direct navigation works** — clicking the URL out of the
  tool-output JSON opens the image in a new tab without a login
  dialog.
- **Token never appears in the proxy admin GUI** — the
  `auth_request` chain delegates validation to the bridge, which
  reads the secret from its container env. The proxy admin can be
  read-shared with operators who don't need the secret.

### Trade-offs to know

- The token is visible in the chat tool-output JSON (and in browser
  history when you copy a URL). It's a view-only credential for
  `/view`; the rest of the proxy host (incl. `/prompt`) is still
  Basic-auth-gated.
- The bridge's port must be reachable from the proxy container.
  When NPM and the bridge are in separate Docker compose projects
  (typical), bind the bridge to `0.0.0.0` and have NPM hit the host
  LAN IP. Loopback-only (`127.0.0.1`) only works if NPM and the
  bridge share a network namespace (uncommon).

## Setting `COMFYUI_EXTERNAL_URL`

The bridge embeds `COMFYUI_EXTERNAL_URL + fetch_url_path` into the
`display_markdown` field of every `generate` response. Whatever URL
sits in this env var is the URL the user will copy out of the
tool-output JSON to view the image in a new tab — so it MUST be a
URL that's reachable from the operator's browser.

Recommended: an HTTPS reverse-proxy in front of ComfyUI. Setup
sketch:

1. In your reverse-proxy stack (Nginx Proxy Manager, Cloudflare
   tunnel, etc.) add a proxy host like `comfy.your-domain.com` →
   `http://<host-lan-ip>:13036`.
2. Add HTTP Basic auth on that proxy host (ComfyUI ships without
   auth) so the URL isn't an unauthenticated public ComfyUI API.
3. Get a cert for it (Let's Encrypt via NPM is one click).
4. Set `COMFYUI_EXTERNAL_URL=https://comfy.your-domain.com` in the
   main `.env` and recreate the bridge.
5. The first time you click a generated image URL out of the chat,
   the browser pops up the Basic auth dialog — log in once. The
   browser caches the credentials per origin, so every subsequent
   click on a URL from the same origin opens the image
   transparently.

LAN-only setup (no HTTPS / no proxy): set
`COMFYUI_EXTERNAL_URL=http://<host-lan-ip>:13036`. Works only when
the chat UI is also served over HTTP on the same LAN — HTTPS chat
+ HTTP image URL = mixed-content silently dropped (and even at the
direct-navigation level, modern Chrome/Firefox flag the HTTP URL).

> Note: see "Chat-side image rendering — known limit" above. Even
> with a perfectly configured HTTPS proxy, the URL still has to be
> opened in a new tab — the chat surface itself does not render
> the image inline. Verified empirically on 2026-04-27.

## Tools

- **Three MCP tools** surfaced through OpenClaw's tool catalog:
  - `comfyui_image__generate(prompt, workflow, ..., include_base64=false)` —
    submit a workflow, poll, return image **metadata** (filename,
    width/height, byte size, fetch URL path). PNG bytes are NOT in the
    response by default — they would balloon the agent's context to
    50K+ tokens per image and 5-10× the next LLM call's wall clock.
    Operators or chat surfaces fetch the actual PNG via ComfyUI's
    `GET /view?filename=…&type=output&subfolder=…` endpoint with the
    metadata returned. Pass `include_base64=true` only when you need
    the bytes inside the agent reply.
  - `comfyui_image__list_workflows()` — list shipped templates with
    their tunable params and defaults.
  - `comfyui_image__cancel(prompt_id)` — best-effort abort of an
    in-flight prompt.
- **Two reference workflows** (`workflows/flux-schnell.json`,
  `workflows/sdxl-base.json`) you can drop your own checkpoints into.
  See `server/workflows/README.md` for how to add custom templates.
- **Bearer auth** on `POST /mcp`; `/healthz` is unauth'd so the docker
  HEALTHCHECK doesn't need the token.
- **Single-flight by default** (`IMAGE_GEN_MAX_CONCURRENCY=1`) so
  concurrent generation can't pile up on the GB10 GPU while vLLM is
  also serving tokens. Flip to `0` for pass-through.

## Architecture

```
┌──────────────────────┐  POST /mcp   ┌────────────────────────────┐
│  OpenClaw gateway    │ ───────────▶ │  openclaw-image-comfyui    │
│  (main stack bridge) │              │  (this compose)            │
└──────────────────────┘              │                            │
                                      │  ┌──────────────────────┐  │
                                      │  │ workflow_loader.py   │  │
                                      │  │ comfy_client.py      │  │
                                      │  │ FastAPI MCP wire     │  │
                                      │  └──────────────────────┘  │
                                      └────────────┬───────────────┘
                                                   │ HTTP
                                                   │ host.docker.internal:13036
                                                   ▼
                                      ┌────────────────────────────┐
                                      │  YOUR ComfyUI install      │
                                      │  (separate compose)        │
                                      │                            │
                                      │  /prompt /history /view    │
                                      └────────────────────────────┘
```

The bridge does NOT own a GPU — it's a pure Python wrapper around the
ComfyUI HTTP API. The actual generation runs in the operator's
separate ComfyUI container (which IS GPU-attached).

## Adding image models

The bridge ships **no model weights**. You pick the checkpoint, you
own its license. Drop a `*.safetensors` file into your ComfyUI
install's `basedir/models/checkpoints/` directory; reference it
either:

- per-call: `comfyui_image__generate(prompt="...", checkpoint="my-model.safetensors")`
- or once in the workflow JSON: edit
  `server/workflows/<name>.json` and replace `"REPLACE_ME.safetensors"`
  with your filename, then restart the bridge.

## Content & license posture

The bridge is **content-agnostic**. It submits whatever workflow you
configured to whatever checkpoint you loaded. Model + prompt + license
posture is the operator's responsibility — same as F5-TTS HU's CC-BY-NC
weights. Common adult-fine-tune families (Pony Diffusion XL,
Illustrious XL, certain FLUX Dev derivatives) ship under their own
licenses; check the model card before shipping anything based on them.

## Smoke test (after the three activation steps)

```bash
# health
curl -fsS http://127.0.0.1:9095/healthz
# → ok workflows=2

# tools/list
TOKEN=$(grep '^IMAGE_GEN_API_TOKEN=' .env | cut -d= -f2-)
curl -sS -X POST http://127.0.0.1:9095/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'

# generate (substitute a checkpoint that actually exists)
curl -sS -X POST http://127.0.0.1:9095/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"comfyui_image__generate","arguments":{"prompt":"a red cube on white background","workflow":"flux-schnell","checkpoint":"flux1-schnell-fp8.safetensors","width":512,"height":512}}}' \
  | jq '.result.content[0].text' | jq -r . | jq '{prompt_id, workflow_used, seed_used, count: (.images | length), first_format: .images[0].format}'
```

## Threat model

- **Operator-trusted prompt only.** The bridge has no content filter;
  it submits whatever the agent calls it with. The agent's own prompt
  hygiene + the operator's choice of upstream models are the controls.
- **Bridge → ComfyUI hop is unauth'd.** ComfyUI ships without auth and
  the bridge reaches it over the host-gateway interface. Mitigated by
  ComfyUI's port-publish being loopback-only on the user's existing
  setup. **Do not** publish ComfyUI's port on a routable interface —
  the bridge already provides a Bearer-protected MCP surface.
- **Bridge → agent surface is Bearer-token-protected.** Token rotation
  via `./rotate-secrets.sh IMAGE_GEN_API_TOKEN` (cross-compose
  force-recreate documented under `docs/CUSTOMIZATION.md`).
- **Single-flight default** keeps the GPU usable for vLLM during
  rendering. Flip via `IMAGE_GEN_MAX_CONCURRENCY=0` only if the
  contention is acceptable.

## Known limits

- **WebSocket progress feed not used.** The bridge polls
  `/history/{prompt_id}` instead. Rationale: WS adds reconnect /
  out-of-order-frame handling that polling avoids in ~30 LOC.
- **No image upload yet.** This v0.1.0 surfaces text-to-image only;
  img2img (uploading a base image) needs a workflow that includes a
  `LoadImage` node and a bridge code path to ferry bytes — pending a
  use case that asks for it.
- **No streaming progress to the agent.** A 25-step SDXL render can
  take 20-40 seconds; the agent waits in the tool call until the PNG
  arrives. Use `IMAGE_GEN_TIMEOUT_S` to bound it.
- **One ComfyUI per bridge.** `COMFYUI_URL` is a single endpoint; the
  bridge does not pool across multiple ComfyUI instances.
