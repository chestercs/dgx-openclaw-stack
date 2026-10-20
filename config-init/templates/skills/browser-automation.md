## Browser tool — action API and chains

There is one `browser` tool with an `action` parameter (the old `browser__navigate/screenshot` names are retired). Actions: `open`{url,label} → returns a stable `targetId`; `snapshot`{targetId,refs:"aria"} → DOM + element refs for clicking; `screenshot`{targetId} → the tool result is a text vision description of the image + a `[Screenshot saved to "<path>"…]` line with the shareable PNG path — to attach it see the screenshot chain below; `screenshot`{targetId,ref:"eNN"} → ONLY that element (snapshot ref), NOT the whole page — use THIS for "csak ezt a dobozt/részt fotózd", not a crop afterwards; `act`{targetId,ref,…} click/type; `tabs` / `close`{targetId}. The default profile is `self-hosted` (openclaw-browser sidecar) — do NOT pass `target="sandbox/host"` (the gateway container has no browser binary).

**Chains:**
- **"csinálj screenshotot X-ről" (the user expects an ATTACHED image):** (1) `browser({action:"open", url:"X", label:"shot"})`; (2) `browser({action:"screenshot", targetId:"shot"})` → the tool result ends with a `[Screenshot saved to "<path>". …]` line; (3) the LAST line of your reply is a standalone `MEDIA:<path>` line, the path copied VERBATIM from the `[Screenshot saved to "…"]` line of the screenshot you want to attach (from between the quotes) — Discord turns that line into a real image attachment. 🚨 NEVER write the MEDIA path from memory: if the `[Screenshot saved to …]` line is missing, get it with `exec` `ls -t /home/node/.openclaw/media/browser/*.png | head -1` — an invented/old path = "Media failed" + the wrong image. 🚨 A screenshot attaches NOTHING by itself — without a MEDIA line the user gets no image; NEVER write "nézd meg a csatolt fájlt" without a MEDIA line. The MEDIA line holds a raw absolute path (no backticks, no spaces), with nothing after it; attach ONE image per turn (the best one), not more.
- **"csak ezt a dobozt/szekciót fotózd" (partial screenshot):** (1) `open`; (2) `snapshot`{targetId,refs:"aria"} → pick the CONTAINER ref of the requested section from the hierarchy (for a parent box, the ref that CONTAINS the requested element; do NOT try refs one by one); (3) `browser({action:"screenshot", targetId:"…", ref:"eNN"})` → the saved PNG IS ALREADY the cropped element, its path in the result's `[Screenshot saved to "…"]` line; (4) check from the vision description that the crop is right — if so, STOP and attach it with the `MEDIA:` line; max 2-3 ref attempts, then give the whole page. This is the PRIMARY route — faster and more precise than cropping afterwards. 🚨 `screenshot` is a TOP-LEVEL action — RIGHT: `{action:"screenshot", targetId, ref}`; WRONG: `{action:"act", kind:"screenshot", ref}` (`act` kinds are only click/type-like → "kind: must be equal to one of the allowed values" validation error).
- **Image post-processing (crop/resize/annotate) when the ref route is not enough:** 🚨 the gateway's python3 has NO PIL/Pillow — in `exec` `python3 -c "from PIL import …"` is ALWAYS ModuleNotFoundError. PIL is in python_sandbox (the canvas dir is shared, media/browser is NOT visible there): (1) `exec({command:"cp /home/node/.openclaw/media/browser/<uuid>.png /home/node/.openclaw/canvas/shot.png"})`; (2) `python_sandbox__python_exec` with PIL on `/home/node/.openclaw/canvas/shot.png`, saving to `/home/node/.openclaw/canvas/<out>.png`; (3) the last line of the reply is `MEDIA:/home/node/.openclaw/canvas/<out>.png`.
- "olvasd el ezt a cikket / mi van X oldalon" → `open` → `snapshot` → summarise.
- "találj nekem képet X-ről" → `web_search` (article URLs) → `browser open` → `snapshot urls:true` → image URL → `python_sandbox__python_exec` (`urllib.request.urlretrieve`) → save under `~/.openclaw/canvas/`.

**Workflow for a screenshot request:** (1) one short human sentence in the user's language ("Egy pillanat, csinálok egy képernyőképet.") — NOT raw tool syntax; (2) tool calls paired by `label=`/`targetId=`; (3) on failure an alternative (snapshot fallback on a screenshot timeout, python fallback on a browser 403); (4) if it still fails, the REAL error text.

## Browser tool usage — quick reference

`browser.act` routes by `kind`. Each kind has its own parameter shape.
Mixing them produces normalizer errors that look like the tool is broken
but are actually schema mismatches.

### `kind="fill"` — fill one or more form fields

Always nested under a `fields` array, even for a single field:

```json
{"action": "act", "kind": "fill", "profile": "<name>",
 "fields": [
   {"ref": "e14", "type": "text", "value": "MyUsername"},
   {"ref": "e15", "type": "text", "value": "secret"}
 ]}
```

**Wrong** (this is what produces `"fill requires fields"`):

```json
{"action": "act", "kind": "fill", "element": "e14", "text": "..."}
```

### `kind="click"` — click an element by ref

```json
{"action": "act", "kind": "click", "profile": "<name>", "ref": "e10"}
```

### `kind="type"` — type into the focused element

```json
{"action": "act", "kind": "type", "profile": "<name>", "text": "hello"}
```

### Recovery

If `browser.act` returns `"fill requires fields"`, you used the flat
shape on a fill action. Re-emit with `fields: [{ref, type, value}]`.
Do NOT abandon the task or apologize to the operator; one corrected
call resolves it.
