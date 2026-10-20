# openclaw-image-comfyui

Status: optional; off on the reference host. Nemotron takes ~70 GiB of the
128 GB unified pool, which leaves no room for ComfyUI renders beside it, so
`OPENCLAW_DISCORD_MEDIA_TOOLS_OFF` defaults to `image,video,music`.

A thin **MCP bridge** that lets the OpenClaw agent drive image, video and
music generation on the operator's own ComfyUI install. It speaks MCP
Streamable-HTTP on `POST /mcp`, turns tool calls into ComfyUI workflow
submissions, polls for completion and returns metadata (or bytes on request).
The bridge owns no GPU and ships no model weights.

Config-init wires it in `config-init/steps/media.mjs`. Every knob it reads is
in [`docs/reference/configuration.md`](../docs/reference/configuration.md#media-comfyui-imagevideomusic-ltx);
the bridge's own knobs are in `docker-compose.yml` here, each with a comment.

## Tools

The gateway lists them as `comfyui_image__<tool>`:

| Tool | What it does |
|--|--|
| `generate` | Text-to-image from a workflow template. |
| `generate_i2i` | Image-to-image: an attached source image plus a prompt (`init_image_url` path or base64, `denoise`). |
| `generate_video` | LTX-Video 2.3, text-to-video or image-to-video, with native audio. |
| `generate_music` | ACE-Step 1.5: tags (required) plus optional lyrics, instrumental or sung. |
| `generate_music_repaint` | Restyles an attached track (audio-to-audio; `denoise` sets how much changes). |
| `list_workflows` | Templates with their tunable parameters and defaults. |
| `cancel` | Best-effort abort of an in-flight prompt. |
| `queue_status` | Running and pending jobs. |
| `free_memory` | Unloads ComfyUI models to free the pool. |

The [`/claw-img` bot](../openclaw-claw-img-bot/README.md) calls the same tools
directly, with no LLM in between.

## Design

- **MCP, not a plugin.** `mcp.servers.<name>` with `transport:
  streamable-http` is a stable OpenClaw surface; the gateway does the JSON-RPC
  handshake and the bridge owns only a hand-rolled wire handler (initialize,
  tools/list, tools/call, `Mcp-Session-Id` echo). Same layout as the Python
  sandbox.
- **Separate compose file.** The operator already runs ComfyUI with their own
  models and custom nodes; a second ComfyUI in the main stack would compete
  for the same GPU. The bridge joins the main stack's network as `external:
  true` (`${COMPOSE_PROJECT_NAME:-dgx-openclaw}_default`), so the main stack
  must have been `up` once before the bridge can start.
- **Host-gateway hop.** The bridge reaches ComfyUI at
  `http://host.docker.internal:13036` (`extra_hosts: host-gateway`), which
  needs no change to the operator's ComfyUI compose. For ComfyUI on another
  box, set `COMFYUI_URL` to its LAN address.
- **Single-flight.** `IMAGE_GEN_MAX_CONCURRENCY=1` serializes calls, because
  ComfyUI and vLLM share the GPU and concurrent renders stall token
  generation. `0` passes everything through (only for ComfyUI on another GPU).
- **Polling, not WebSocket.** `POST /prompt` returns a `prompt_id`; the bridge
  polls `/history/{id}` (start `IMAGE_GEN_POLL_INTERVAL_S`, ×1.5 backoff up
  to `IMAGE_GEN_POLL_BACKOFF_MAX_S`, total `IMAGE_GEN_TIMEOUT_S`). On timeout
  it deletes the queue entry and interrupts; a 404 after the prompt was seen
  means ComfyUI restarted mid-render and is reported as such.

## Enable

From the repo root:

1. Generate the bridge token: `./rotate-secrets.sh IMAGE_GEN_API_TOKEN`
   (an explicit key is generated even when it is empty).
2. In `.env`, take the groups you run off `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF`
   (`none` = all on), set `IMAGE_GEN_DEFAULT_WORKFLOW` (the image skill is
   written only once it is set) and, after the LTX download, `LTX_VIDEO_ENABLED=on`.
   Set `IMAGE_GEN_CANVAS_DIR=/canvas` so results arrive as attachments (see
   [Delivery](#delivery)).
3. Start the bridge:
   ```bash
   docker compose -f openclaw-image-comfyui/docker-compose.yml \
     --env-file .env --profile image-gen up -d --build
   ```
4. Re-run config-init so the gateway gets `mcp.servers.comfyui_image` and the
   skills:
   ```bash
   docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
   ```

What config-init then writes:

- `mcp.servers.comfyui_image` with `requestTimeoutMs`
  (`IMAGE_GEN_MCP_REQUEST_TIMEOUT_MS`, 960000). It must outlast the bridge's
  900 s render budget, so the bridge answers first with a clean error instead
  of the client dropping a render the bot then promises. With the token unset
  the entry is removed, so the gateway never dials a parked bridge.
- Skills in `workspace-discord/skills/`: `image-generation`, `image-to-image`,
  `video-generation` (also in the main workspace) and `music-generation`, one
  per group that is on.
- For each group in `OPENCLAW_DISCORD_MEDIA_TOOLS_OFF`: the tools are denied
  on the Discord agent, the skill is removed and the router gets an "off" line,
  so the bot says the feature is off instead of calling a dead tool.
- The built-in `video_generate` (OpenAI Sora, 401 without a key) stays denied
  (`OPENCLAW_DENY_OPENAI_VIDEO_TOOL`); video goes through this bridge.

Rotation: `./rotate-secrets.sh IMAGE_GEN_API_TOKEN` prints the services to
recreate; it spans both compose projects (the bridge and the config-init trio).

## Workflow templates

A template under `server/workflows/` is a ComfyUI **API-format** export
(`Save (API Format)`) plus a `_metadata` block the bridge strips before
submission: `name`, `description`, `checkpoint_required`, `defaults`, and
`targets` (bridge parameter → node id + input key). Parameters are bound by
node id and input key, never by string replacement on the JSON, so a prompt
containing `${…}` cannot corrupt the graph. The directory is bind-mounted;
restart the bridge to load a new file. Authoring guide:
[`server/workflows/README.md`](server/workflows/README.md).

| Template | Purpose |
|--|--|
| `flux-krea-2k` | FLUX.1-Krea-dev, single stage, any aspect up to 2K (default 1280×720). |
| `flux-krea-2k-adult` | Same plus the flux-uncensored-v2 LoRA. |
| `flux-krea-2k-i2i`, `flux-krea-2k-i2i-adult` | Image-to-image variants. |
| `realism-text-to-img` | SDXL (Juggernaut-XL v9) plus an SDXL realism LoRA. |
| `flux-schnell`, `sdxl-base` | Generic templates; pass `checkpoint=` or edit the `REPLACE_ME.safetensors` placeholder. |
| `ltx-2.3-t2v`, `ltx-2.3-i2v` | LTX-Video 2.3 distilled fp8, text- or image-to-video with audio. |

A LoRA must match its base family: an SDXL LoRA on a FLUX graph loads as a
no-op. There is no 4K template: SUPIR failed under ComfyUI 0.12+ and
UltimateSDUpscale left tile seams on FLUX latents. Render 2048×2048 and upscale
externally with ESRGAN.

## Response shape

Results carry metadata by default: per file `filename`, size, dimensions and
`fetch_url_path`, plus `display_markdown`. Bytes stay out because tool output
lands in the next prefill: one 1024×1024 PNG as base64 is 50-100K tokens, and
an agent run that embedded it timed out while the direct MCP call took 6 s.
`include_base64=true` exists for callers that need the bytes
(`IMAGE_GEN_MAX_OUTPUT_BYTES` caps them).

## Delivery

With `IMAGE_GEN_CANVAS_DIR=/canvas` the bridge mirrors each file into the
gateway's canvas dir (mounted by default from `OPENCLAW_CANVAS_DIR`) and:

- **Discord:** `display_markdown` becomes `MEDIA:<gateway path>` lines
  (`IMAGE_GEN_CANVAS_GATEWAY_DIR`, default `/home/node/.openclaw/canvas`).
  OpenClaw turns a `MEDIA:` line at the end of a reply into a file
  attachment, so images and mp4s arrive as files: no token in the text, no
  dead link after the view token rotates. Music returns a `canvas_path` the
  agent uploads with `message({action:"upload-file"})`; its `/view` link sits
  behind the proxy auth and Discord cannot fetch it. A clip over Discord's bot upload
  limit (~9.5 MB) falls back to its URL. Once a result is attachment-ready, the
  agent's view of it drops the URL fields, because the model otherwise
  rebuilt a link next to the attachment.
- **Web chat:** the `[embed url="/__openclaw__/canvas/<file>" /]` shortcode.
  The chat normalizer turns it into an iframe before the markdown sanitizer
  runs and rewrites the URL to a per-session capability path
  (`/__openclaw__/cap/<token>/…`), so it needs no other auth. Only
  `/__openclaw__/canvas/` and `/__openclaw__/a2ui/` URLs are accepted; leave
  `gateway.controlUi.allowExternalEmbedUrls` off. Plain `![](url)` images and
  external `[text](url)` links are stripped by the sanitizer, and a browser
  never sends cached Basic-auth credentials on a cross-origin `<img>`, so a
  `/view` URL only works when opened in a new tab.

Without the canvas dir, `display_markdown` holds the `/view` URL
(`COMFYUI_EXTERNAL_URL` + `fetch_url_path`).

The canvas dir is never pruned by the bridge. Its files carry a `comfyui-`
prefix; prune by that prefix only, so other canvas content stays:

```bash
docker exec openclaw-gateway find /home/node/.openclaw/canvas \
  -name 'comfyui-*' -type f -mtime +7 -delete
```

## View URL and reverse proxy

`COMFYUI_EXTERNAL_URL` is the browser-reachable ComfyUI address the bridge
puts into URLs (`host.docker.internal` does not resolve in a browser). Put an
HTTPS reverse proxy with auth in front of ComfyUI; it ships without auth, so
never publish its port on a routable interface. An HTTP image URL from an
HTTPS chat is dropped as mixed content.

`COMFYUI_VIEW_TOKEN` makes the bridge append `?token=<value>` to every URL and
serve `GET /auth-validate`, so the proxy can check the token without holding
the secret. The UI keeps Basic auth; only `/view` takes the token. The bridge
reads the token from the query string or, in an `auth_request` sub-request,
from `X-Original-URI`, and compares in constant time. Set `IMAGE_GEN_BIND`
so the proxy container can reach port 9095.

Two custom locations on the proxy host (Nginx Proxy Manager syntax; NPM emits
the `proxy_pass` from the form fields):

```nginx
# internal: only nginx's own auth_request sub-request can call it
location /auth-validate {
    internal;
    proxy_pass_request_body off;
    proxy_set_header Content-Length "";
    proxy_set_header X-Original-URI $request_uri;
    # proxy_pass http://<bridge-host>:9095
}

location /view {
    auth_request /auth-validate;
    # proxy_pass http://<comfyui-host>:13036
}
```

On the NPM access list: **Satisfy Any** (the `auth_request` 200 alone
passes), no `Allow all` rule (an IP match would then pass everything), and no
extra `auth_basic off;` (a second `auth_basic` directive is an `[emerg]`
config error).

```bash
TOKEN=$(grep '^COMFYUI_VIEW_TOKEN=' .env | cut -d= -f2-)
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://comfy.example.com/view?filename=foo.png&type=output&token=$TOKEN"
# valid token 200 (404 if the file does not exist); no or wrong token 401;
# /api/view and / without credentials 401; /auth-validate directly 404
```

The token is not in `rotate-secrets.sh`: set a new value
(`openssl rand -base64 48`) and recreate the bridge; the proxy needs no edit.
The token is visible in tool output and browser history; it only opens `/view`.

## Recommended model bundle

FLUX.1-Krea-dev for the `flux-krea-*` templates, ~35 GB. Accept the licence on
<https://huggingface.co/black-forest-labs/FLUX.1-Krea-dev> first. Only ComfyUI
core nodes are needed.

```bash
export HF_HUB_ENABLE_HF_TRANSFER=1
BASEDIR=/path/to/comfyui/basedir

huggingface-cli download black-forest-labs/FLUX.1-Krea-dev \
    flux1-krea-dev.safetensors --local-dir "$BASEDIR/models/diffusion_models/"
huggingface-cli download comfyanonymous/flux_text_encoders \
    t5xxl_fp16.safetensors clip_l.safetensors --local-dir "$BASEDIR/models/clip/"
huggingface-cli download black-forest-labs/FLUX.1-Krea-dev \
    ae.safetensors --local-dir "$BASEDIR/models/vae/"

# Adult LoRA (~700 MB), only for the -adult workflows; renamed to the
# filename the workflows expect.
huggingface-cli download enhanceaiteam/Flux-uncensored \
    lora.safetensors --local-dir "$BASEDIR/models/loras/" \
  && mv "$BASEDIR/models/loras/lora.safetensors" \
        "$BASEDIR/models/loras/flux-uncensored-v2.safetensors"
```

FLUX.1-Krea-dev is under the FLUX.1-dev licence (personal and research use;
commercial use needs an agreement with Black Forest Labs). flux-uncensored-v2
is non-commercial. Checkpoints for the other templates go into
`basedir/models/checkpoints/`.

## Video (LTX-Video 2.3)

`scripts/install-ltx-video.sh` downloads the model on the host that owns the
ComfyUI basedir and installs the `Lightricks/ComfyUI-LTXVideo` node pack. The
shipped workflows need ComfyUI core **0.17.0** or newer (`ComfyMathExpression`).

```bash
curl -fsS $COMFYUI_URL/system_stats | grep -o '"comfyui_version":"[^"]*"'
curl -fsS $COMFYUI_URL/object_info | grep -o '"EmptyLTXVLatentVideo"' | head -1
curl -fsS $COMFYUI_URL/object_info | grep -o '"ComfyMathExpression"' | head -1
```

- **Variants** (`--variant`): `distilled-1.1` (script default, bf16, ~46 GB),
  `distilled`, `dev` (bf16), `fp8-distilled`, `fp8-dev` (~30 GB). The shipped
  workflows load `ltx-2.3-22b-distilled-fp8.safetensors`, so install
  `fp8-distilled` or edit the workflow. The Gemma 3 12B text encoder (~25 GB)
  is gated: accept its licence on HuggingFace before running the script, or
  the download fails after the checkpoint. `--with-upscalers` adds ~1.3 GB of
  spatial/temporal upscalers that the shipped single-stage workflows do not use.
- **Audio** is generated in the same pass and muxed into the mp4; turn it off
  per call (`audio_enabled=false`) or by default (`LTX_VIDEO_DEFAULT_AUDIO=off`).
- **Resolution** is capped by a total pixel budget, `LTX_VIDEO_MAX_PIXELS`
  (default 589824 = 1024×576). Larger requests are scaled down keeping the
  aspect, never rejected. On the shared GB10 pool a clamped 1280×720 render
  was killed at 900 s mid-decode; raise the budget only on a box with
  headroom. The tool's `resolution` argument (`fullhd`, `720p`, …) survives
  the model rewriting the prompt.
- **Timeouts** are in lockstep: bridge `IMAGE_GEN_TIMEOUT_S` 900, gateway
  `requestTimeoutMs` 960000. A cold render (model load after an image→video
  switch) takes several minutes; `openclaw agent` tests need `--timeout 900`.
- **Memory headroom decides delivery.** On a saturated unified pool ComfyUI
  moves the VAE decode to the CPU and the render does not finish. `docker
  stats` showing ComfyUI pegged on CPU, plus `free -g`, is the diagnosis;
  restarting ComfyUI (or calling `free_memory`) returns the pool.

## Music (ACE-Step 1.5)

`generate_music` runs a fixed ACE-Step graph (not a workflow template). Model
files and sampling come from `ACE_MUSIC_UNET`, `ACE_MUSIC_CLIP1`,
`ACE_MUSIC_CLIP2`, `ACE_MUSIC_VAE`, `ACE_MUSIC_STEPS`, `ACE_MUSIC_CFG`
(turbo: CFG 1, ~8 steps; base/SFT: CFG 6, 50 steps); `ACE_MUSIC_MAX_SECONDS`
caps the duration. For a sung track the caller must pass lyrics; tags alone
give an instrumental.

## Smoke test

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)
TOKEN=$(grep '^IMAGE_GEN_API_TOKEN=' .env | cut -d= -f2-)

curl -fsS http://127.0.0.1:9095/healthz
docker exec ${PROJ}openclaw-image-comfyui curl -fsS http://host.docker.internal:13036/system_stats | head -c 200

curl -sS -X POST http://127.0.0.1:9095/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools[].name'

docker exec ${PROJ}openclaw-cli sh -c 'cat ~/.openclaw/openclaw.json' \
  | jq '.mcp.servers.comfyui_image.url'

docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message "Use comfyui_image__generate to render a 512x512 red cube on white. Reply with the prompt_id." \
  --thinking off --json --timeout 600 | jq '.toolSummary, .finalAssistantVisibleText'
```

With the canvas dir on, a Discord-agent image reply should carry
`mediaUrl: /home/node/.openclaw/canvas/…png` and no `/view` URL in its text.

## Checklist for media changes

Every media integration here hit at least one surprise at deploy time. Before
shipping a new tool or a changed response shape:

1. Test each surface it will appear on (CLI agent, Discord, web chat); one
   passing does not imply the others.
2. Know the auth path from the browser: Basic auth does not survive
   cross-origin `<img>`, Bearer headers never reach `<img>`; a query token
   does.
3. Check which markdown survives the web chat sanitizer.
4. Keep tool output small: input tokens should not jump by more than a few
   hundred per call.
5. Use the prefixed tool name (`comfyui_image__generate`) in prompts and docs.
6. Gate it: compose profile, empty token by default, config-init removes the
   entry when the token is unset.
7. Make sure `rotate-secrets.sh` knows the token and prints the recreate for
   both compose projects.

## Threat model

- **Operator-trusted prompts.** No content filter; the operator's model choice
  and the agent's prompt are the controls.
- **Bridge → ComfyUI is unauthenticated.** Keep ComfyUI's port loopback or LAN
  only.
- **Agent → bridge is Bearer-protected** (`IMAGE_GEN_API_TOKEN` on every
  `POST /mcp`; `/healthz` is open for the healthcheck). The port binds to
  `127.0.0.1` by default (`IMAGE_GEN_BIND`).
- **Hardened container:** `cap_drop: [ALL]`, `no-new-privileges`, UID 1000,
  1.5 GB memory, 1 CPU. No torch, no GPU, no model files.

## Licence posture

The bridge is content- and licence-agnostic: it submits whatever workflow and
checkpoint you configured. Model licences (FLUX.1-dev, adult fine-tunes such as
Pony or Illustrious XL, LTX-2.3, ACE-Step) are the operator's responsibility;
check each model card.

## Known limits

- No streaming progress: the agent waits in the tool call until the file is
  written.
- One ComfyUI per bridge (`COMFYUI_URL` is a single endpoint).
- Web-chat playback of video inside the `[embed]` iframe is untested.
- No audio-to-video mode and no two-stage upscaler path in the bridge.
