# Workflow templates

Each `*.json` here is a literal ComfyUI **API-format** export with one
extra `_metadata` block at the top that the bridge reads at startup and
strips before submission.

The bridge ships two reference templates:

- `flux-schnell.json` — FLUX.1 Schnell, 4-step distilled. Fastest.
- `sdxl-base.json` — generic SDXL 25-step. Works with any SDXL
  fine-tune (Pony XL, Illustrious XL, RealVisXL, …) — drop the
  checkpoint into `basedir/models/checkpoints/` and pass `checkpoint=…`.

Both ship with `"ckpt_name": "REPLACE_ME.safetensors"`. The bridge will
**refuse to generate** with that placeholder — pass `checkpoint=` per
call OR edit the JSON once and replace the placeholder with your
filename.

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
