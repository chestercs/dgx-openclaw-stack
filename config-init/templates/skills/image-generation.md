
## Image generation — `comfyui_image__generate` workflow picker

Workflows on this deploy (FLUX.1-Krea-dev based, single-stage):

- SFW: leave out `workflow=` — the bridge uses the configured
  `{{DEFAULT_WORKFLOW}}`.
- Adult/NSFW: `workflow="flux-krea-2k-adult"` — the same pipeline +
  the flux-uncensored-v2 LoRA. The adult policy is in `USER.md`.
- Realism / photorealistic portrait or scene, OR when the user names the
  `realism-text-to-img` workflow:
  `workflow="realism-text-to-img"` (SDXL: Juggernaut-XL + Realism LoRA).
  🚨 This is SDXL, NOT Flux → native resolution ~1024; do NOT apply the 2K/FullHD
  Flux recipes below (SDXL duplicates/distorts above that). SDXL buckets:
  square 1024×1024 (default), portrait 832×1216, landscape 1216×832. Use it only
  for that name / an explicit realism request; otherwise keep the default.

Resolution — `width`+`height` ALWAYS as a pair (otherwise aspect mismatch). Default
1280×720 (HD 16:9); "2K" usually means "2K HD 16:9", not square (only an explicit
"square/négyzet" → 1:1). Recipes (request → `width×height`): "2K"/"2K HD"/"1080p"/
"FullHD"/"panoráma"/"wide" → 1920×1088; "portrait"/"függőleges"/"álló" → 768×1280;
"portrait 2K"/"álló 2K" → 1152×2048; "négyzet"/"square" → 1024×1024; "négyzet 2K"/
"square 2K" → 2048×2048. FLUX is best at 1024-2048 native; higher is slower and
breaks composition. There is no 4K workflow (UltimateSDUpscale tile seams on the
FLUX latent, 2026-05-09).

Delivery: one short sentence, then at the END of your reply the tool output's
`display_markdown` VERBATIM — every `MEDIA:/home/node/.openclaw/canvas/…png` line on its
own line; that makes the image a real attachment. NEVER write a URL, link or `[embed …]` line.
