# Workflow templates

Each `*.json` here is a literal ComfyUI **API-format** export with one
extra `_metadata` block at the top that the bridge reads at startup and
strips before submission.

The bridge ships seven reference templates as of v0.11.0. Two are
generic universal-fallback templates; five are the max-quality 4K
bundle added in v0.11.0 (require the recommended model bundle —
see `docs/reference/image-comfyui-bridge.md` → "Recommended model
bundle for max-quality 4K").

| Template | Purpose | Native res | Final res | Models needed |
|--|--|--|--|--|
| `flux-schnell` | FLUX.1 Schnell, 4-step distilled. Fastest universal. | 1024² | 1024² | any FLUX-Schnell `*.safetensors` |
| `sdxl-base` | Generic SDXL 25-step. Works with any SDXL fine-tune. | 1024² | 1024² | any SDXL `*.safetensors` |
| `flux-krea-2k` | Single-stage FLUX-Krea-dev, 2048². Fast SFW iteration. | 2048² | 2048² | bundle (FLUX-Krea + t5xxl + ae) |
| `flux-krea-4k-supir` | Max SFW realism (DEFAULT for max-quality deploys). FLUX-Krea + realism LoRA stack → SUPIR → ~4K. | 1536² | ~4K | bundle + SUPIR + Juggernaut-XL-v9 |
| `flux-krea-4k-tiled` | SFW fallback when 4k-supir OOMs. Ultimate SD Upscale tile pass instead of SUPIR. | 1536² | ~4K | bundle + 4x-UltraSharp |
| `flux-krea-4k-adult` | Adult content, single LoRA → SUPIR → ~4K. | 1536² | ~4K | bundle + SUPIR + flux-uncensored-v2 |
| `flux-krea-4k-adult-realism` | Max adult realism: uncensored + realism LoRA stack → SUPIR → ~4K. | 1536² | ~4K | bundle + SUPIR + all LoRAs |

Both legacy templates (`flux-schnell` / `sdxl-base`) ship with
`"ckpt_name": "REPLACE_ME.safetensors"` — the bridge refuses to
generate with that placeholder. Either pass `checkpoint=` per call
or edit the JSON once. The five `flux-krea-*` templates load FLUX-Krea-dev
via `UNETLoader` instead of `CheckpointLoaderSimple`, with the filename
baked in — they need no `checkpoint=` argument and `checkpoint_required`
is `false`.

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
