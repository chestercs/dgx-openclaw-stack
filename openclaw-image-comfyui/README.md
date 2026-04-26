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
