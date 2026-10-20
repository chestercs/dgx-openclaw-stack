## Image-to-image — modify an attached image

When the user **attaches an image** AND asks to modify, restyle, edit, or transform it (key phrases: "alakítsd át", "csinálj belőle", "változtasd", "edit this", "stylize as…", "make it look like…", "tegyél rá…"), use **`comfyui_image__generate_i2i`** — NOT the plain `comfyui_image__generate` (that one is text-to-image only and ignores attachments).

**Call shape:**
- `init_image_url`: filesystem path of the attachment. Discord uploads land at `/home/node/.openclaw/media/inbound/<uuid>.<ext>` — pass that path verbatim. The bridge has the same path bind-mounted; no HTTP fetch needed.
- `prompt`: a text description of how to transform it. Be SPECIFIC about the change ("turn into anime style", "cyberpunk neon-lit reinterpretation", "remove the background, replace with beach"). Vague prompts give vague results.
- `denoise`: how much to change the source. Default 0.7. Use 0.3-0.5 for "tweak slightly" (keep face, change lighting); 0.6-0.75 for "restyle but keep structure"; 0.8-0.95 for "completely transform, source as anchor only".

**Examples:**
- User attaches photo + "tedd cyberpunk stílusúvá" → `denoise=0.8, prompt="cyberpunk neon-lit reinterpretation, futuristic city lights, vivid magenta and cyan, dramatic shadows"`
- User attaches photo + "kicsit változtass rajta színeken" → `denoise=0.35, prompt="<keep original subject and composition>, vibrant saturated colors, warm golden-hour lighting"`
- User attaches photo + "anime stílusra" → `denoise=0.75, prompt="anime art style, cel shading, detailed lineart, vivid colors, studio ghibli inspired"`

**Adult / NSFW variant:** pass `workflow="flux-krea-2k-i2i-adult"` to use the uncensored LoRA. Same call shape otherwise.

**Output:** one short sentence, then the tool's `display_markdown` value verbatim at the end (each `MEDIA:` line on its own line) — that attaches the modified image as a file. Never write a URL, link or `[embed …]` line.
