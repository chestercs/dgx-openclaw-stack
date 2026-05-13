# Workflow templates

Each `*.json` here is a literal ComfyUI **API-format** export with one
extra `_metadata` block at the top that the bridge reads at startup and
strips before submission.

The bridge ships four templates. Two FLUX-Krea-dev workflows are the
recommended path on this stack (require the model bundle — see
`docs/reference/image-comfyui-bridge.md` → "Recommended model bundle").
Two legacy templates (`flux-schnell`, `sdxl-base`) stay for any
non-FLUX-Krea checkpoints the operator may also keep around.

| Template | Purpose | Native res range | Models needed |
|--|--|--|--|
| `flux-krea-2k` | DEFAULT. Single-stage photorealism, any res up to 2K. | 256-2048 (any aspect) | bundle (FLUX-Krea + t5xxl + ae) |
| `flux-krea-2k-adult` | Adult content. Same pipeline + flux-uncensored-v2 LoRA. | 256-2048 (any aspect) | bundle + flux-uncensored-v2 |
| `flux-schnell` | FLUX.1 Schnell, 4-step distilled. Fastest universal. | 1024² | any FLUX-Schnell `*.safetensors` |
| `sdxl-base` | Generic SDXL 25-step. Works with any SDXL fine-tune. | 1024² | any SDXL `*.safetensors` |

Both `flux-krea-*` templates take width/height args and render at the
exact requested resolution — the targets cover both `EmptySD3LatentImage`
AND `ModelSamplingFlux` so the FLUX sigma schedule stays correct across
the resolution range. Defaults: 1280×720 widescreen. Pass
`width=2048,height=2048` for 2K square, `width=1920,height=1088` for
HD panorama, `width=768,height=1280` for portrait, and so on.

The legacy `flux-schnell` / `sdxl-base` templates ship with
`"ckpt_name": "REPLACE_ME.safetensors"` — the bridge refuses to
generate with that placeholder. Either pass `checkpoint=` per call
or edit the JSON once. The `flux-krea-*` templates load FLUX-Krea-dev
via `UNETLoader` (not `CheckpointLoaderSimple`) with the filename
baked in — they need no `checkpoint=` argument and `checkpoint_required`
is `false`.

### Why no 4K workflow

Earlier v0.11.0 attempts shipped four 4K workflows
(`flux-krea-4k-supir/tiled/adult/adult-realism`) that ran the
`flux-krea-2k` first stage and then SUPIR or UltimateSDUpscale at
2.5× to ~3840×3840. Verified end-to-end on GB10 on 2026-05-09:
SUPIR's `SUPIR_conditioner` raises `Cannot copy out of meta tensor`
under ComfyUI 0.12+ accelerate dispatch (upstream bug, persists across
fp8_unet and keep_model_loaded toggles), and the UltimateSDUpscale
fallback produces visible 1024-pixel tile-seam grids and ghost-face
artifacts on FLUX latents at any tested denoise (0.15-0.35) and
seam_fix mode. Operators who genuinely need 4K should render at
`flux-krea-2k` 2048×2048 native and upscale externally with
ESRGAN (no diffusion, no tile artifacts). The four 4K workflows are
removed in this revision.

## LTX-Video 2.3 templates (v0.12.0+) — operator-assembled

Two `.example` files ship with the bridge:

| Template | Purpose | Routed when |
|--|--|--|
| `ltx-2.3-t2v.json.example` | Text-to-video + native audio | `comfyui_image__generate_video` called without an `init_image_*` arg |
| `ltx-2.3-i2v.json.example` | Image-to-video + native audio | `comfyui_image__generate_video` called WITH `init_image_url` or `init_image_base64` |

These ship as `.example` because the LTX-2.3 node graph is large (~25-50
nodes per workflow) and the node input contracts are tied to the
operator's exact ComfyUI / LTXVideo-pack version. Shipping a pre-built
`.json` would bind brittle node IDs that may not match the operator's
install. **The bridge's `workflow_loader` skips `.example` files at
load time**, so this scaffolding is dormant until the operator opts in.

### Activation recipe

Operators who have run `scripts/install-ltx-video.sh` follow these
steps once per workflow (T2V and I2V are independent):

1. **Open ComfyUI's web UI** at your `COMFYUI_URL` (typically port 13036).
2. **Load Lightricks' reference workflow.** Their published t2v / i2v
   reference graphs live at
   <https://github.com/Comfy-Org/workflow_templates/tree/main/templates>
   (filenames `video_ltx2_3_t2v.json` and `video_ltx2_3_i2v.json`).
   Drag-and-drop into the UI or `File → Load`.
3. **Queue a test render** to confirm your install works end-to-end
   before wiring the bridge. The reference workflow takes 2-10 minutes
   on cold cache; a successful render writes an mp4 to `output/`.
4. **Save (API Format)** from the queue panel (NOT the regular Save —
   that emits the frontend graph, which `/prompt` does not accept).
   You get a JSON file with `{<node_id>: {class_type, inputs}}` shape.
5. **Copy the relevant `.example` file** to its active name:
   - `cp ltx-2.3-t2v.json.example ltx-2.3-t2v.json`
   - `cp ltx-2.3-i2v.json.example ltx-2.3-i2v.json`
6. **Paste your API-format export** below the `_metadata` block,
   replacing every `REPLACE_ME_*` placeholder.
7. **Tune the `targets` table** so each entry's `node` matches the
   right node ID in your export. The `//comment-*` keys inside
   `_metadata` of the `.example` file describe what to look for:
   - `prompt` → the positive `CLIPTextEncode` (the one whose CLIP
     input traces back to `LTXAVTextEncoderLoader`)
   - `negative` → the negative `CLIPTextEncode`
   - `width` / `height` / `length` → `EmptyLTXVLatentVideo`
   - `fps` → `LTXVConditioning.fps` (or `.frame_rate`, depending on
     pack version)
   - `seed` → `RandomNoise.noise_seed`
   - `init_image` (I2V only) → `LoadImage.image`
8. **Reload the bridge** so the new file gets picked up:
   ```bash
   docker compose -f openclaw-image-comfyui/docker-compose.yml \
                  --profile image-gen restart openclaw-image-comfyui
   ```
   Watch the startup log for the `loaded N workflows:` line —
   `ltx-2.3-t2v` and `ltx-2.3-i2v` should now be listed.
9. **Smoke test** via the bridge:
   ```bash
   TOKEN=$(grep '^IMAGE_GEN_API_TOKEN=' .env | cut -d= -f2-)
   curl -sS -X POST http://127.0.0.1:9095/mcp \
     -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
          "params":{"name":"generate_video",
                    "arguments":{"prompt":"a panda eating bamboo",
                                 "length":48,"fps":24,"timeout_s":900}}}' \
     | jq '.result.content[0].text' | jq -r . | jq '.workflow_used, .duration_s, .videos[0].fetch_url_path'
   ```
   Expect: `"ltx-2.3-t2v"`, `2.0`, `"/view?..."` after the render
   completes (~2-10 min cold, ~30-90s warm on GB10).

### Why we don't ship pre-built `.json` files

We can't promise compatibility against an LTX-Video node pack version
we haven't tested against. The `_metadata.targets` table binds node
IDs (`"node": "29"`) that come from the operator's specific export,
not from a canonical numbering. Shipping a pre-built workflow with
fixed IDs would mean every operator's first `generate_video` call
fails with `workflow refers to missing node id "29"` — and they'd
debug that mystery instead of the install.

The `.example` pattern keeps the scaffolding visible (so the operator
knows what to assemble) but inert (so the bridge doesn't crash-loop
on first start).

## Adding a custom workflow

1. In ComfyUI, build the graph you want, then click **Save (API
   Format)** in the queue panel. (NOT the regular "Save" — that emits
   the editor's UI graph, which the API does not accept.)
2. Drop the JSON into this directory.
3. Add a `_metadata` block at the top:

```jsonc
{
  "_metadata": {
    "name": "my-workflow",
    "description": "What this workflow does (shown in list_workflows)",
    "checkpoint_required": true,
    "defaults": { "steps": 30, "cfg": 7.5, "width": 1024, "height": 1024 },
    "targets": {
      "prompt":     { "node": "2",  "input": "text" },
      "negative":   { "node": "3",  "input": "text" },
      "checkpoint": { "node": "1",  "input": "ckpt_name" },
      "width":      { "node": "4",  "input": "width" },
      "height":     { "node": "4",  "input": "height" },
      "seed":       { "node": "5",  "input": "seed" },
      "steps":      { "node": "5",  "input": "steps" },
      "cfg":        { "node": "5",  "input": "cfg" },
      "sampler":    { "node": "5",  "input": "sampler_name" },
      "scheduler":  { "node": "5",  "input": "scheduler" }
    }
  },
  "1": { "class_type": "CheckpointLoaderSimple", "inputs": { ... } },
  ...
}
```

4. Restart the bridge (`docker compose -f
   openclaw-image-comfyui/docker-compose.yml up -d --force-recreate
   openclaw-image-comfyui`). The startup log lists every workflow it
   loaded. The bind-mount also makes `workflows/` live-readable
   without a rebuild.

### Why `targets` is required for `prompt` / `negative`

Most SDXL/FLUX workflows have **two** `CLIPTextEncode` nodes (positive
and negative). The bridge can't guess which is which by `class_type`
alone. For all other parameters (`checkpoint`, `width`, `seed`, ...)
the bridge falls back to the first node of the matching `class_type` if
you skip the explicit target.

## Conventions for filename_prefix

Shipped workflows write under `openclaw-bridge/<workflow>` so the
operator can find bridge-generated images separately from their own
ComfyUI sessions in `output/`.
