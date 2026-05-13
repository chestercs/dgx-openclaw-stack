# `openclaw-image-comfyui` — video extension (LTX-Video 2.3)

The same MCP bridge that serves `comfyui_image__generate` also exposes
`comfyui_image__generate_video` once the operator has run
`scripts/install-ltx-video.sh` and flipped `LTX_VIDEO_ENABLED=1` in
`.env`. This document covers the video-specific bits — model bundle,
audio handling, quantization, embed limits, what the agent surface
actually shows for an mp4 reply.

For the bridge's shared architecture (MCP wire protocol, host-gateway
network hop, workflow `_metadata` schema, response envelope, capability-
token canvas mechanism) see [`image-comfyui-bridge.md`](image-comfyui-bridge.md).
This file does not repeat any of that material.

## Why extend the image bridge instead of a new bridge

Three considerations argued strongly for extending the
`openclaw-image-comfyui` service rather than shipping a sibling
`openclaw-video-comfyui`:

1. **Shared infrastructure already exists.** Bearer auth, single-flight
   semaphore, host-gateway hop, MCP wire protocol, workflow loader,
   ComfyUI `/prompt` submit + `/history` poll — every one of these
   has the same shape for video as for images. A sibling bridge would
   duplicate ~300 LOC of FastAPI / MCP plumbing.
2. **One ComfyUI per bridge contract.** The bridge talks to a single
   ComfyUI install via `COMFYUI_URL`. Splitting the bridge would force
   two MCP tool-namespaces (`comfyui_image__*` vs `comfyui_video__*`)
   plus two long-lived containers both pointing at the same ComfyUI.
   The agent has no way to express "use the same install for both"
   short of identical URLs.
3. **Workflow-loader template substitution is mode-agnostic.** The
   bridge mutates node-id+input-key pairs to inject parameters. A video
   workflow uses `EmptyLTXVLatentVideo` instead of `EmptyLatentImage`
   and emits to `SaveVideo` instead of `SaveImage`, but the loader's
   target-binding model handles both with the same code path. Only the
   output-extraction step has to know about `outputs[node_id]["videos"]`
   alongside `outputs[node_id]["images"]`.

The extension is the lowest-LOC path that also avoids a second bridge
container competing for the same GPU.

## ComfyUI version requirement

**Practical minimum: ComfyUI core 0.17.0+.** LTX-2.3's primitive nodes
(`LTXAVTextEncoderLoader`, `EmptyLTXVLatentVideo`, etc.) landed in
0.16.x as day-0 support (per the [official blog
post](https://blog.comfy.org/p/ltx-23-day-0-supporte-in-comfyui)), but
the **official reference workflows** at
[`Comfy-Org/workflow_templates`](https://github.com/Comfy-Org/workflow_templates)
use `ComfyMathExpression` from 0.17.0 in their upscaler chain. Loading
those workflows on a 0.16.x install warns:

```
This workflow was created with a newer version of ComfyUI (0.12.3).
Some nodes may not work correctly.
Core nodes from version 0.17.0: ComfyMathExpression
```

Hand-built single-stage workflows that don't touch `ComfyMathExpression`
work on 0.16.x — but the realistic operator path is "load the published
reference workflow, export API format, paste under `_metadata`" (see
`workflows/README.md` recipe), so 0.17.0 is the de-facto requirement.

The `Lightricks/ComfyUI-LTXVideo` custom-node pack adds higher-level
helpers (`LTXVImgToVideoInplace`, `LTXVPreprocess`, …) used by the
two-stage reference workflows. Install it via the `scripts/install-ltx-video.sh`
script.

Verify the live install:

```bash
COMFYUI_URL=http://your-host:13036
curl -fsS $COMFYUI_URL/system_stats | grep -o '"comfyui_version":"[^"]*"'
# expect: "comfyui_version":"0.17.x" or higher
curl -fsS $COMFYUI_URL/object_info | grep -o '"EmptyLTXVLatentVideo"' | head -1
# expect: "EmptyLTXVLatentVideo"  (empty output ⇒ node pack failed to load)
curl -fsS $COMFYUI_URL/object_info | grep -o '"ComfyMathExpression"' | head -1
# expect: "ComfyMathExpression"  (empty ⇒ install is < 0.17.0; reference
#                                 workflows from Comfy-Org won't load
#                                 their upscaler chain)
```

## Recommended model bundle

Total disk: **~55 GB** with the fp8 checkpoint, **~71 GB** with bf16
(one main checkpoint + Gemma text encoder). With upscalers: +1.3 GB.
Run on the host that owns the ComfyUI basedir — `scripts/install-ltx-video.sh`
automates this. The fp8 variants are GB10-friendly when co-residing
with Gemma 4 dense + bge-m3 + a spatial upscaler — pick fp8 unless you
specifically need bf16 fidelity.

### What's downloaded

| Component                          | HuggingFace repo                                 | File                                                  | Size     | Variant flag             |
|------------------------------------|--------------------------------------------------|-------------------------------------------------------|----------|--------------------------|
| Main checkpoint — bf16 (one of three) | `Lightricks/LTX-2.3`                          | `ltx-2.3-22b-distilled-1.1.safetensors` (default)     | 46.1 GB  | `--variant distilled-1.1`|
|                                    |                                                  | `ltx-2.3-22b-distilled.safetensors`                   | 46.1 GB  | `--variant distilled`    |
|                                    |                                                  | `ltx-2.3-22b-dev.safetensors`                         | 46.1 GB  | `--variant dev`          |
| Main checkpoint — fp8 (one of two) | `Lightricks/LTX-2.3-fp8`                         | `ltx-2.3-22b-distilled-fp8.safetensors`               | 29.5 GB  | `--variant fp8-distilled`|
|                                    |                                                  | `ltx-2.3-22b-dev-fp8.safetensors`                     | 29.1 GB  | `--variant fp8-dev`      |
| Text encoder (Gemma 3 12B IT)      | `google/gemma-3-12b-it-qat-q4_0-unquantized`     | entire repo (mirrored)                                | ~25 GB   | (always downloaded)      |
| Spatial upscaler (optional)        | `Lightricks/LTX-2.3`                             | `ltx-2.3-spatial-upscaler-x2-1.1.safetensors`         | 996 MB   | `--with-upscalers`       |
| Temporal upscaler (optional)       | `Lightricks/LTX-2.3`                             | `ltx-2.3-temporal-upscaler-x2-1.0.safetensors`        | 262 MB   | `--with-upscalers`       |

The fp8 repo (`Lightricks/LTX-2.3-fp8`) is a separate HF repo from the
main `Lightricks/LTX-2.3`. As of 2026-05-13 it carries the **original
distilled** checkpoint in fp8 (not the `-1.1` refresh) and the dev
checkpoint. Operators who want the `-1.1` improvements stay on bf16
for now.

There is **no separate audio VAE** to download — video+audio VAE is
bundled inside the main checkpoint (both bf16 and fp8 variants).

The Gemma encoder is **gated** — accept the model card terms on the
HuggingFace page before running the installer or the download will 403
after the 46 GB checkpoint has already streamed in.

### Variant trade-off (the `--variant` flag)

- **`distilled-1.1`** (default, bf16): latest distilled checkpoint,
  fewer sampling steps required (typical: 16-24 steps vs dev's 30-50).
  Recommended when memory headroom is not a concern.
- **`fp8-distilled`** (bf16's space-saver): half-precision quantized
  version of the original distilled. ~30 GB on disk and resident vs
  46 GB for bf16, near-identical quality at matched steps. **Pick
  this on GB10 if Gemma 4 dense + bge-m3 + a spatial upscaler are
  all co-resident** — the unified 128 GB memory has ~50 GB headroom
  to spare once the LLM stack is up, fp8 fits comfortably.
- **`distilled`**: earlier distilled checkpoint (no `-1.1`) kept
  available for reproducibility comparisons.
- **`dev`** / **`fp8-dev`**: full 22B dev model in bf16 or fp8.
  Highest motion / detail quality, ~2x the inference time of
  distilled at matched output spec. Pick for offline / non-agent
  rendering where wall-clock doesn't matter.

### Audio is native, single-pass, on by default

LTX-2.3 generates the synchronized audio track inside the same
diffusion pass as the video frames — a unified diffusion transformer
treats video + audio as a joint temporal signal, with a HiFi-GAN
vocoder at the decode end. The bundled `ltx-2.3-t2v.json` and
`ltx-2.3-i2v.json` workflows use `CreateVideo` + `SaveVideo` to wrap
the decoded frames + audio into a single mp4 with `aac` or `opus` audio
(ComfyUI's default video encoder picks one based on what's available).

To disable audio for a specific call: `audio_enabled=false`. Stack
default: change `LTX_VIDEO_DEFAULT_AUDIO=off` in `.env`.

The bridge emits a one-second silence pad before the actual content in
the audio-on path so Discord / browser players don't clip the onset
(same posture as the F5-TTS HU router, fixed in v0.6.1 — see
[`tts-stack.md`](tts-stack.md)).

## Workflow templates included

`openclaw-image-comfyui/server/workflows/` ships two LTX-2.3 templates
as **`.example` files** after the bridge image bump to 0.12.0:

- `ltx-2.3-t2v.json.example` — text-to-video + audio scaffolding
- `ltx-2.3-i2v.json.example` — image-to-video + audio scaffolding

These are **scaffolding, not ready-to-run workflows**. The bridge's
`workflow_loader` skips `.example` files at load time. The operator
brings each one up by:

1. Running an LTX-2.3 reference workflow inside ComfyUI's UI (Lightricks
   publishes `video_ltx2_3_t2v.json` and `video_ltx2_3_i2v.json` at
   <https://github.com/Comfy-Org/workflow_templates>).
2. Exporting it via **Save (API Format)** in the queue panel.
3. Pasting the API-format graph into the `.example` skeleton (replacing
   the `REPLACE_ME_*` placeholders).
4. Tuning the `_metadata.targets` so each bridge parameter maps to the
   right node ID in the operator's specific export.
5. Renaming `.json.example` → `.json` so the loader picks it up.
6. Restarting the bridge.

See [`openclaw-image-comfyui/server/workflows/README.md`](../../openclaw-image-comfyui/server/workflows/README.md)
"LTX-Video 2.3 templates" for the step-by-step recipe.

**Why we don't ship pre-built `.json`:** the `_metadata.targets` table
binds specific node IDs (`"node": "29"`) that come from the operator's
ComfyUI export. Different versions of the LTX node pack number nodes
differently; a pre-bound `.json` would fire `workflow refers to missing
node id` on first call against any install whose IDs don't match.
Shipping as `.example` keeps the scaffolding visible (operator knows
what to assemble) but inert (bridge doesn't crash-loop on first start).

### Target table (used in both templates)

| Bridge parameter   | Node class              | Input key       | Notes                              |
|--------------------|-------------------------|-----------------|------------------------------------|
| `prompt`           | `CLIPTextEncode` (pos)  | `text`          | Required                           |
| `negative`         | `CLIPTextEncode` (neg)  | `text`          | Default: empty string              |
| `seed`             | `RandomNoise`           | `noise_seed`    |                                    |
| `width`            | `EmptyLTXVLatentVideo`  | `width`         |                                    |
| `height`           | `EmptyLTXVLatentVideo`  | `height`        |                                    |
| `length`           | `EmptyLTXVLatentVideo`  | `length`        | Frames; default 96                 |
| `fps`              | `LTXVConditioning`      | `fps` / `frame_rate` | Pack-version dependent — verify in your export |
| `init_image`       | `LoadImage`             | `image`         | I2V only — filename in input/ that the bridge uploads via /upload/image |

The bridge's `generate_video` tool accepts `init_image_base64` or
`init_image_url` for I2V. Both are decoded / fetched into bytes,
uploaded to ComfyUI's `/upload/image` endpoint (returns a filename in
the operator's ComfyUI `input/` dir), and that filename is bound into
the `LoadImage` node's `image` input via the `init_image` target above.

## Chat-side video rendering

### Discord (primary surface)

Discord auto-embeds raw mp4 URLs in messages, **as long as the file is
under the file-size cap of the destination channel** — the cap is 50 MB
for Nitro Basic / older guild boost levels, smaller on un-boosted free
guilds, larger on higher boost tiers. This is community-confirmed but
not officially documented in the Discord developer portal; treat 50 MB
as a defensive default and operators can raise it if their guild
allows.

At LTX-2.3's typical bitrate (~3-5 Mbps for 512×768 @ 24 fps with
audio), a 4-second clip is ~2-3 MB and a 10-second clip is ~5-8 MB —
comfortable margin below 50 MB. The bridge's `LTX_VIDEO_MAX_DURATION_S`
default (10s) keeps clips inside this envelope.

The `display_markdown` payload for a video has two lines:

1. **Naked mp4 URL** — Discord auto-embeds this inline, browser opens
   in a `<video>` element on click. This is the line the agent should
   surface verbatim in its Discord reply.
2. **`[embed url="/__openclaw__/canvas/<id>.html" /]`** — see web-chat
   section below.

### Web chat (degraded)

The OpenClaw web chat surface does NOT inline-render video as of
OpenClaw `2026.4.22`. The `[embed]` shortcode (added upstream in PR
#64104, see [`image-comfyui-bridge.md`](image-comfyui-bridge.md) for
the full mechanism explanation) accepts any MIME at
`/__openclaw__/canvas/<file>`, and the bridge writes a small `.html`
wrapper containing a `<video controls>` element pointing at the mp4
file in the same canvas dir. **This is unverified end-to-end for video
in web chat** — `image-comfyui-bridge.md` verified `.png` and `.html`
both load through the iframe, but a video inside an iframe-loaded HTML
wrapper is one additional layer that no GB10 test has exercised yet.

The recommended chat surface for video is therefore **Discord**, with
web chat as a degraded fallback (the agent reply contains the naked
mp4 URL; user copies it and opens in a new tab where the browser plays
it natively).

## GB10 bench numbers

Run on the deploy host with `scripts/bench-ltx-video.sh`. Numbers below
are placeholders; replace with measured values from your own bench
run.

| Mode                          | Resolution | Length | Audio | Cold (first run) | Warm (warm GPU) | Peak VRAM |
|-------------------------------|------------|--------|-------|------------------|-----------------|-----------|
| T2V, distilled-1.1            | 512 × 768  | 96 fr  | on    | TBD              | TBD             | TBD       |
| T2V, distilled-1.1            | 768 × 1024 | 192 fr | on    | TBD              | TBD             | TBD       |
| I2V, distilled-1.1, ref image | 512 × 768  | 96 fr  | on    | TBD              | TBD             | TBD       |
| T2V, dev                      | 512 × 768  | 96 fr  | on    | TBD              | TBD             | TBD       |

"Cold" includes the first-call load of the 46 GB checkpoint + 25 GB
Gemma encoder into VRAM — expect 3-10 minutes on GB10 unified memory.
Subsequent calls reuse the cache and are dominated by sampling
wall-clock.

GB10 has 128 GB unified memory; co-residency with Gemma 4 31B dense
(~48 GB) leaves ~50 GB headroom, enough for `distilled-1.1` at fp8 but
**not** for the `dev` variant at bf16. Stack-default models.json keeps
Gemma 4 MoE 26B-A4B (~28 GB) as the primary LLM, which gives more
headroom for video.

## Known limits

- **No two-stage upscaler workflow in the bridge.** The single-stage
  templates produce ~512×768 to ~1024×1024 native. To go higher,
  operators run a separate ComfyUI workflow with the spatial / temporal
  upscalers — outside the bridge surface today. Same posture as the
  abandoned 4K SUPIR / UltimateSDUpscale path in
  [`image-comfyui-bridge.md`](image-comfyui-bridge.md): single-stage
  shipped, multi-stage left to the operator.
- **No A2V (audio-to-video).** LTX-2.3's third mode (audio input drives
  video) needs an audio upload path that isn't wired in v0.12.0.
  Deferred until use-case demand exists.
- **No streaming progress.** The agent waits in the tool call until the
  mp4 is on disk. Cold-call wait can hit 5+ minutes — bound via
  `IMAGE_GEN_TIMEOUT_S` (default 600s; raise to 900s+ for video).
- **Web-chat inline video render unverified.** See chat-surface
  section.
- **Multi-step tool-call examples need `--timeout 900` minimum.** A
  cold-call video render plus the agent's prefill cycle blows past
  the default 60s `openclaw agent` timeout. The bundled tests use 900s.

## See also

- [`image-comfyui-bridge.md`](image-comfyui-bridge.md) — shared
  bridge architecture (host-gateway hop, MCP wire, capability-token
  canvas, workflow loader, response shape).
- [`chat-surface-capability-matrix.md`](chat-surface-capability-matrix.md)
  — surface × feature matrix, where video sits.
- [`media-bridge-checklist.md`](media-bridge-checklist.md) —
  the smoke checklist any media bridge change has to pass.
- `scripts/install-ltx-video.sh` — the one-shot installer this doc
  describes.
- `scripts/bench-ltx-video.sh` — the bench script for filling in the
  GB10 numbers table above.
