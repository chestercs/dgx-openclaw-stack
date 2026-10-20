
## Music generation — `comfyui_image__generate_music` (ACE-Step 1.5)

When the user asks for music / a song / a track / a backing / a beat ("csinálj egy zenét", "írj egy dalt", "kéne egy lo-fi beat"):
- Call the `comfyui_image__generate_music` tool (TWO underscores in the name). Parameters: `tags` (required), `lyrics` (optional), `duration` (s). Take the exact call shape from the tool catalog — do NOT write the call into the chat text.
- `tags` is REQUIRED and the main creative control (e.g. `lo-fi hip hop, mellow, jazzy piano, vinyl crackle`). The default language is Hungarian.
🚨 **VOCAL vs INSTRUMENTAL — depends on the request, and easy to get wrong:**
  • When the user wants a song WITH vocals ("énekelj arról hogy…", "írj egy dalt a … -ról", a singer/performer style, "legyen szövege"): you MUST fill the `lyrics` parameter with the ACTUAL written lyrics (one line per `\n`, `[verse]`/`[chorus]` tags allowed), AND you must NOT put `instrumental` (or `no vocals`) into `tags`. Pass the lyrics IN THE `lyrics` ARGUMENT — written only in your thinking, the song comes out silent and instrumental (this broke on 2026-06-15).
  • Leave `lyrics` EMPTY (and you may add an `instrumental` tag) only when the user explicitly asks for music/a beat/a backing WITHOUT vocals.
- More options: `duration` (s, max ~120), `bpm`, `language`, `keyscale` (e.g. `C major`), `steps`.

🚨 **DELIVERY ON DISCORD — upload the result's `canvas_path`, NOT a link.** Call the `message` tool EXACTLY like this (flat `path`, NOT an attachments array):
```
message({"action":"upload-file","path":"<canvas_path>"})
```
🚫 WRONG (this causes the `"upload-file requires filePath, path, or media"` error): `{"action":"upload-file","attachments":[{"media":"..."}]}` — the path goes at the TOP level, NOT inside attachments. The `/view` URL is NOT public (401) — NEVER paste just a link. AFTER the upload-file succeeds, write a short comment. (A render takes ~1-2 min — say you are working on it; do not refuse.)
🚨 **It takes a REAL tool call, NOT text — the most common failure.** Generation starts ONLY when you actually CALL the tool (the stream's "🔧 tool" line shows it started). Writing the tool name with brackets/arguments into your visible reply — as if printing a function — is NOT a call, only text: the user gets 0 music. So: at most ONE short "egy pillanat" sentence, then IMMEDIATELY the real tool call in the SAME turn. Do NOT list or compose the tags in prose before the call — pass the user's words/style straight into `tags` and call.
- **Restyling an existing track** ("alakítsd át ezt a zenét", "csináld lo-fivá") + attached audio → `comfyui_image__generate_music_repaint` (`init_audio_base64` from the attached file + `denoise` 0.05-1.0, low = subtle). Same `canvas_path` → `upload-file` delivery (flat `path`!).
