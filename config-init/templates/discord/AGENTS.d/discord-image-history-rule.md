## Image context discipline — HARD RULE

The vLLM backend enforces a **per-prompt image cap** (`LLM_LIMIT_MM_IMAGES`). Past it, the chat/completions request comes back HTTP 400 and the whole turn fails.

**Put ONLY these into the multimodal context:**
1. The images attached to the **current turn's user message** (the `/home/node/.openclaw/media/inbound/<uuid>.png` paths).
2. If the current turn **replies to** an earlier message, that **parent message's attached images** too.

**Refer to EVERY OTHER earlier image only IN TEXT** (e.g. "a korábbi macskás képen" / "az előbb generált cyberpunk verzió"). Do NOT re-encode them for vision. If the user explicitly points back at an older image ("emlékszel arra a képre?"), describe it from text or say that only its description is in memory.

Do not panic if vLLM returns 400 for a prompt with too many images: cut the context down to the last 2 images and retry.
