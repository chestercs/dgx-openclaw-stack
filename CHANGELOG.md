# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — Discord progressive streaming
- **Patcher step 24** writes `channels.discord.streaming = "partial"`
  by default. The OpenClaw upstream default `"off"` posts replies
  atomically; with Gemma 4 NVFP4 at ~6 tok/s a 500-token reply means
  ~80 s of dead silence in the channel before anything appears, and
  users perceive the bot as frozen. `"partial"` posts a single
  preview message and edit-in-place as tokens arrive; Discord's edit
  rate limit (5 / 5 s per channel) is comfortably above the
  ~5.5 s/edit cadence at 6 tok/s × `draftChunk.minChars=200`
  (~33 tokens) on a single dedicated bot account. Env override:
  `OPENCLAW_DISCORD_STREAMING=off|partial|block|progress` (or empty
  to skip the step entirely). Same user-managed protection as steps
  20-22: only writes when `channels.discord` is configured AND the
  operator hasn't set the field themselves. `draftChunk` and
  `streaming.preview.toolProgress` are intentionally left at the
  docs defaults — knob them later only if a live deploy proves it
  necessary.
- **`docs/reference/discord-text-agent.md`** gains a "Progressive
  streaming" section with the four documented modes, the rate-limit
  math, the cancel-on-media/error/explicit-final caveats, and a
  copy-paste verification recipe.
- **`CLAUDE.md`** "Implementation details" gets a matching paragraph
  next to the `--timeout 600` note, since both are consequences of
  the same ~6 tok/s LLM throughput.

### Migration
- `git pull && docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli`
  picks up step 24 and writes `channels.discord.streaming = "partial"`
  into the live `openclaw.json` (assuming `channels.discord.enabled =
  true` and the operator hasn't set `streaming` already).
- Operators on a faster backend (cloud LLM endpoint, sm_120-tuned
  NVFP4 build hitting 30+ tok/s) should set
  `OPENCLAW_DISCORD_STREAMING=block` or `=off` to avoid burning
  through Discord's edit rate budget.

## [0.11.0] - 2026-04-28

Big release that batches every accumulated commit since v0.9.10 — the
v0.10.x image-gen polish series, the chat-side `[embed]` shortcode
(Path A) verified end-to-end, the Discord text-channel ack/parser
patches, the Gemma 4 colon-namespace vLLM patch, and finally an
ffmpeg-augmented gateway image so `messages.tts.auto=always` works
on every voice surface (including Discord text channels) with no
operator workarounds.

### Added — Path A: chat-side inline image render
- **`[embed url="/__openclaw__/canvas/<file>"]` shortcode emission**
  (v0.10.0, commit `257f73f`). The bridge mirrors each generated
  PNG into `${OPENCLAW_CONFIG_DIR}/canvas/comfyui-<id>.png` and
  emits the embed shortcode in `display_markdown`. The chat
  normalizer extracts `[embed]` directives into structured iframe
  metadata BEFORE DOMPurify runs, bypassing the `<img>` sanitizer
  that drops cross-origin image markdown. Verified end-to-end on
  GB10 (commit `82b7f17`): renders inline in webchat with the
  `cap/<token>/` rewrite + `sandbox="scripts"` default.
- **`IMAGE_GEN_CANVAS_DIR` env opt-in** + corresponding compose
  bind-mount (`${OPENCLAW_CONFIG_DIR}/canvas:/canvas:rw`,
  commented by default). Set to `/canvas` and uncomment the volume
  to flip from legacy cross-origin URL to inline shortcode.
- **HTML wrapper `<openclaw-embed>` with sandbox="scripts"** for
  fit-to-iframe rendering (v0.10.1). Iframe gets the chat's auth
  via the cap-token rewrite, no Basic auth dialog, no mixed
  content, no markdown sanitizer to negotiate.
- **Documented in `docs/reference/image-comfyui-bridge.md`** —
  Path A is the architectural answer; Path B (cross-origin URL +
  Basic auth) demoted to fallback-of-fallback. The chat-render
  story is finally clean.

### Added — Image-gen polish (v0.10.0 → v0.10.5)
- **URL-first ordering + emphatic agent_hint** (v0.10.2-v0.10.4).
  The `display_markdown` block now leads with the public image URL
  (Discord auto-embeds it), followed by `[embed]` shortcode (web
  chat renders inline), with explicit "MUST paste verbatim, every
  line" instruction in both the tool description AND a structured
  `agent_hint` field so the LLM doesn't summarize the URL away.
- **Sensible-defaults env knobs** (v0.10.5):
  `IMAGE_GEN_DEFAULT_WORKFLOW`, `IMAGE_GEN_DEFAULT_CHECKPOINT`.
  Lets `comfyui_image__generate(prompt="...")` succeed without the
  caller remembering the workflow + checkpoint name on every call.
  Empty default falls back to the original parameter-required
  error so misconfiguration is loud.
- **Masked markdown link instead of bare URL** (v0.10.4) — the
  emitted markdown link uses `[<filename>](<url>)` so the chat
  surface renders it as a clickable link with the filename as
  display text instead of a long ugly URL.

### Added — Discord text-channel ack/reaction support
- **Patcher step 20** (commit `1d7f093`): `ackReactionScope=off`
  defends against OpenClaw issue #46024 (stale reaction-event queue
  replays emoji ack-reactions on session resume; bot rapidly cycles
  👀🤔👍🔥 without agent awareness). Env-tunable via
  `OPENCLAW_DISCORD_ACK_REACTION_SCOPE` (default `off`). Only
  written when `channels.discord` is already configured AND the
  user hasn't set `ackReactionScope` themselves — preserves
  user-managed values.
- **Patcher step 21** (commit `b1b329e`): `actions.reactions=true`
  enables `discord:add_reaction` for agents. Default `true` because
  the bundled vllm-llm image now ships the gemma4 parser patch (see
  next entry). Env-tunable via `OPENCLAW_DISCORD_ACTIONS_REACTIONS`.
- **Patcher step 22** (commit `4d9bd14`): `tools.alsoAllow +=
  group:messaging` on Discord-routed agents. The default
  `tools.profile: "coding"` (set in step 8) does NOT include
  `group:messaging`, so the `message` tool was catalog-filtered out
  even though `actions.reactions=true`. Symptom: discord-friend
  agent responding with "I can't use the tool 'message' here
  because it isn't available." Step 22 walks `agents.routes[]`,
  finds Discord-routed agents (`match.channel === "discord"`), and
  appends the missing entries to their `alsoAllow` array
  (preserving any operator-added entries). Env-tunable via
  `OPENCLAW_DISCORD_AGENT_ALSO_ALLOW` (default `group:messaging`).

### Added — vLLM gemma4 colon-namespace tool-call parser patch
- **`vllm-llm/Dockerfile` + `patch_parser.py`** (commit `2c7e9e4`)
  extends the upstream gemma4 parser regex from `[\w\-\.]+` to
  `[\w\-\.:]+`, accepting colons in tool names. Gemma 4 NVFP4 calls
  `discord:add_reaction` correctly, but unpatched vLLM dropped the
  call (regex stops at the second colon) — the literal envelope
  leaked into Discord chat as garbage text. Now built into the
  bundled image; works transparently with patcher step 21.

### Added — Media-stack reference docs
- **`docs/reference/chat-surface-capability-matrix.md`** —
  systematic matrix of which media features render on which surface
  (web chat / Discord text / Discord voice / agent skill API /
  control UI), with reproducible verify cells per cell.
- **`docs/reference/media-bridge-checklist.md`** — 8-point
  pre-flight checklist for any new media-MCP-bridge (image / audio
  / video / file). Surface verification, auth boundary crossing,
  sanitizer pass, content-size limits, error surface, workflow
  selection, MIME types, idempotency.
- **`docs/reference/discord-text-agent.md`** — Discord text-channel
  agent specifics: mention pill, tools.profile gating, message
  tool, ackReactionScope cycle bug + agent-driven workaround,
  verify checklist.

### Added — ffmpeg-augmented gateway image (this release)
- **`openclaw-base-ext/Dockerfile`** wraps
  `ghcr.io/openclaw/openclaw:${OPENCLAW_IMAGE_REF}` and apt-installs
  ffmpeg. The upstream image does NOT ship ffmpeg, so the gateway's
  Discord text-channel TTS-attachment path (`messages.tts.auto =
  always` shells out to ffmpeg) crashed with `ffmpeg not found in
  trusted system directories`. The workaround so far has been
  `OPENCLAW_TTS_AUTO=tagged` (only TTS-attach when the LLM
  explicitly tags a reply) — that's a real feature regression. The
  ffmpeg-bundled image fixes it cleanly; `OPENCLAW_TTS_AUTO=always`
  now works on every surface.
- **`docker-compose.yml` switch**: the three openclaw services
  (`openclaw-config-init`, `openclaw-gateway`, `openclaw-cli`)
  reference `openclaw-base-ext:${OPENCLAW_BASE_EXT_VERSION:-0.11.0}`
  instead of the upstream image directly. `openclaw-config-init`
  owns the build context (`./openclaw-base-ext`); the other two
  reuse the cached image. Build trigger: `docker compose build
  openclaw-config-init` (or `up -d --build` on first deploy).

### Migration
- `git pull && docker compose build openclaw-config-init`
  (rebuilds the local extension on top of the upstream image
  the `OPENCLAW_IMAGE_REF` env points at).
- `docker compose up -d --force-recreate openclaw-config-init
  openclaw-gateway openclaw-cli` (rolls all three onto the new
  image, picks up patcher steps 20-22 if not already applied).
- Operators who set `OPENCLAW_TTS_AUTO=tagged` as a workaround can
  flip it back to `always` (or unset; `always` is the patcher
  default again). Force-recreate the same three services to apply.
- Verify: `docker exec openclaw-gateway ffmpeg -version` should
  print `ffmpeg version 5.1.8-...` (Debian 12's package).

### GB10 deploy + smoke (final, 2026-04-28 night)
- Build: `openclaw-base-ext:0.11.0` produced in ~15s (slim apt
  layer, no node rebuild).
- `openclaw-gateway` post-recreate: ffmpeg present
  (`ffmpeg version 5.1.8-0+deb12u1`).
- `openclaw.json` post-recreate: `messages.tts.auto = always` (no
  workaround needed).
- Patcher steps 20-22: `[patch-config] no-op` on second run
  (idempotent, openclaw.json already in desired state).
- Image-gen Path A: `[embed]` shortcode renders inline (verified
  separately on 2026-04-28, commit `82b7f17`).
- Discord text-channel TTS-attach: end-to-end smoke deferred to
  operator (requires sending an `@mention` message in the bound
  guild; the ffmpeg presence is the structural unblock and was the
  only missing piece).

## [Unreleased]

(empty — slot reserved for upcoming v0.11.x patches)

## [0.9.10] - 2026-04-27

`auth_request` works end-to-end. Fixes the `?token=...` propagation
gap that made every valid token return 401, plus documents the NPM
custom-location gotchas that surfaced during deploy.

### Fixed
- **`/auth-validate` reads the token from `X-Original-URI` header**
  as a fallback when the query string is empty. NGINX's
  `auth_request /auth-validate;` directive uses a sub-request with a
  static URI — the parent request's `?token=...` does NOT propagate
  to the sub-request's `$args`, so the bridge was seeing an empty
  token. The proxy already sets
  `proxy_set_header X-Original-URI $request_uri;` (NPM default for
  custom auth-validate locations), the fix is a 5-line urlsplit
  parse on the bridge side. Constant-time compare preserved.

### Documented
- **`auth_basic` duplicate emission** during NPM custom-location
  setup. NPM auto-emits `auth_basic "Authorization required";` from
  the host-level Access List into every custom location; if the
  operator also adds `auth_basic off;` in the location's Advanced,
  NGINX `[emerg]` rejects the config (`"auth_basic" directive is
  duplicate`). Solution: drop `auth_basic off;` from the location
  Advanced — instead use `Satisfy Any` on the Access List Details
  tab so the auth_request 200 result is enough on its own.
- **`Satisfy Any` + `Allow all` IP rule = wide-open**. NPM's
  Access List Rules tab `Allow all` means "every IP passes the
  IP-allow check"; combined with `Satisfy Any` the IP-allow alone
  satisfies the request, no auth needed. Drop the `Allow all` rule
  to leave only the `deny all` fallback, then `Satisfy Any` falls
  through to the auth checks (Basic OR auth_request → 200).

### GB10 deploy + smoke (final, 2026-04-27 night)
- `/view + valid token` → HTTP 200 ✅
- `/view no token` → HTTP 401 ✅
- `/view wrong token` → HTTP 401 ✅
- `/api/view no creds` → HTTP 401 (Basic challenge) ✅
- `/auth-validate direct external` → HTTP 404 (`internal;` blocks) ✅
- `/` no creds → HTTP 401 (Basic challenge) ✅
- ComfyUI UI in browser: Basic auth dialog once, cached, all UI
  paths (HTML / `/api/view` / `/api/prompt` / WebSocket) work
  transparently.

The token now lives ONLY in the bridge container's `.env`. The NPM
admin GUI contains zero secrets. Token rotation:
`./rotate-secrets.sh COMFYUI_VIEW_TOKEN` → bridge recreate; no NPM
edit required.

## [0.9.9] - 2026-04-27

`auth_request` endpoint: keep the token in the bridge container's
`.env` only. The reverse-proxy admin GUI no longer needs to hold the
secret in plain text; it just calls `/auth-validate` on the bridge
via NGINX's `auth_request` sub-request mechanism.

### Added
- **`GET /auth-validate?token=...` endpoint** on the bridge.
  Constant-time compare (`secrets.compare_digest`) against
  `COMFYUI_VIEW_TOKEN`. Returns 200 on match, 401 otherwise.
  Fail-closed: 401 if `COMFYUI_VIEW_TOKEN` is empty. No body —
  `auth_request` only inspects the status code. Unauth'd in the
  HTTP sense (no Bearer required to reach `/auth-validate`); the
  token-validation IS the auth.
- **`README.md` "Token-protected proxy" section rewritten** around
  the per-location split + `auth_request` chain. New three-row
  routing table (`/` Basic auth, `/api/view` Basic auth, `/view`
  token via `auth_request`). NPM Custom locations walkthrough,
  including the `internal;` directive on `/auth-validate` so
  external clients can't hit it directly — only the `auth_request`
  sub-request can.
- **`.env.example` `IMAGE_GEN_BIND` doc** notes the
  `IMAGE_GEN_BIND=0.0.0.0` requirement when the proxy lives in a
  separate compose project (typical for NPM + this stack).

### Changed
- **Reverse-proxy config no longer stores `COMFYUI_VIEW_TOKEN`** in
  plain text. The proxy admin GUI is now safe to share with
  operators who shouldn't see the secret.

### Migration
- Bridge image rebuild + recreate (new endpoint).
- Set `IMAGE_GEN_BIND=0.0.0.0` in `.env` if NPM and the bridge are
  in separate compose projects.
- In NPM: switch from "Advanced" `if ($arg_token != ...)` to two
  Custom locations (`/auth-validate` + `/view` with `auth_request`).
  See `openclaw-image-comfyui/README.md` for the exact recipe.
- Token stays in the bridge `.env`; no token edits on NPM after
  rotation — just `./rotate-secrets.sh COMFYUI_VIEW_TOKEN` +
  recreate the bridge.

## [0.9.8] - 2026-04-27

URL-param token auth as an alternative to HTTP Basic auth on the
ComfyUI proxy host. Lets the operator drop Basic auth on
`vision.example.com` (or whatever proxy front their ComfyUI) — the
`?token=<value>` query parameter survives cross-origin `<img>` tag
fetches that Basic auth headers can't.

### Added
- **`COMFYUI_VIEW_TOKEN` env**. When set, the bridge appends
  `?token=<urlencoded value>` to every fetch URL it embeds in
  `display_markdown` and to each `images[].fetch_url_path`. Filename /
  type / subfolder are now also URL-encoded (they weren't before; was
  a latent bug for filenames with spaces or special chars).
- **`openclaw-image-comfyui/README.md` "Token-protected proxy"
  section** with the exact NGINX `if ($arg_token != $required_token)
  { return 401; }` block to paste into NPM's Advanced tab. Notes the
  trade-off (token visible in tool-output JSON, view-and-generate
  scope unless you split per-location), and a per-location sketch
  for stricter scoping (token-only on `/view`, Basic auth on
  `/prompt`).
- **`.env.example` `COMFYUI_VIEW_TOKEN` block** with the
  `openssl rand -base64 48` mint command.
- **`docker-compose.yml` (bridge compose) env passthrough**
  `COMFYUI_VIEW_TOKEN: ${COMFYUI_VIEW_TOKEN:-}`.

### Migration
- Generate a token: `openssl rand -base64 48 | tr -d '\n'`.
- Set `COMFYUI_VIEW_TOKEN=<value>` in main `.env`.
- Recreate the bridge (`docker compose -f
  openclaw-image-comfyui/docker-compose.yml up -d --force-recreate
  openclaw-image-comfyui`).
- On the NPM proxy host (`vision.example.com`): paste the NGINX
  validation block into Advanced, drop the Basic auth.
- Verify: open a generated URL in a browser tab. Should load
  immediately with no auth dialog. Without `?token=...` in the URL,
  the host returns 401.

### Compatibility
- `COMFYUI_VIEW_TOKEN` is optional. Empty (default) → bridge behaves
  exactly as v0.9.7 (no token in URLs, Basic auth on the proxy
  remains the auth surface).

## [0.9.7] - 2026-04-27

Empirical chat-render diagnosis. After v0.9.4-v0.9.6 attempts to
get the generated PNG inline-rendered in the OpenClaw chat surface,
verified end-to-end via Chrome devtools + DOM inspection that the
chat surface (openclaw 2026.4.22) cannot inline-render the image,
and that the limit is at the **browser security layer**, not in
anything the bridge can fix from its side:

1. The chat's markdown sanitizer drops `![alt](url)` image syntax
   AND drops `[text](https://...)` external-origin link syntax.
   Only `mailto:` and trusted-protocol links survive. The DOM
   shows only the `alt` text in a `<p>`, no `<img>` tag is
   produced.
2. Cross-origin Basic auth credentials are not sent on `<img>`
   fetches, even when the user has cached the credentials by
   logging in to the host directly. Verified: `new Image().src =
   'https://vision.example.com/...'` from `claw.example.com` fires
   `onerror` immediately; `fetch` with `credentials: 'include'`
   returns `Failed to fetch`.

### Documented (no code change in this release)
- **`openclaw-image-comfyui/README.md` "Chat-side image rendering —
  known limit" section** with the empirical findings and the
  recommended copy-URL-from-tool-output workflow.
- **`docs/reference/image-comfyui-bridge.md` new "Chat-side image
  rendering: known browser-security limit" section** with the same
  diagnostic trail plus three not-wired future paths
  (same-origin canvas proxy, workspace + read tool, base64 inline).
- **`CLAUDE.md` (root) Image-gen bridge nugget gets a 5th point**
  warning future contributors not to chase markdown-syntax
  workarounds — only same-origin paths or upstream openclaw native
  support will move the needle.

### Bridge response shape (preserved from v0.9.5/0.9.6)
- `display_markdown` still emits `![<filename>](<url>)` followed by
  `🖼️ <filename>: <url>` so a future renderer that supports either
  shape gets the data, but the user's primary path is to copy the
  URL out of the tool-output JSON.

## [0.9.6] - 2026-04-27

Chat-side image rendering, take 2. The v0.9.5 `display_markdown` field
used markdown image syntax (`![alt](url)`); on the OpenClaw chat UI
(2026.4.22) this renders as **plain text** of the alt name with no
clickable link and no image — image syntax is silently dropped by the
chat's markdown sanitizer. Plain markdown links (`[text](url)`) DO
render as clickable links, so we now emit BOTH per image:

```
![<filename>](<url>)
[🖼️ Open: <filename>](<url>)
```

The user clicks the link, the image opens in a new tab on the
HTTPS-reachable URL (already-cached Basic auth credentials send
automatically). If a future chat surface starts honoring image syntax,
the inline render kicks in transparently.

### Changed
- **`comfyui_image__generate` `display_markdown` field** now contains
  both the image syntax and a clickable markdown link per generated
  PNG, separated by a blank line.

### Migration
- Bridge image rebuild + recreate.
- No env / patcher / compose-services / network changes.

## [0.9.5] - 2026-04-27

Chat-side image rendering. The OpenClaw web/control UI (2026.4.22)
ignores MCP `image` content blocks in tool results — the v0.9.4
attempt produced the bytes but the chat showed only the metadata
JSON. Fix: emit a `display_markdown` field in the generate response
with markdown image syntax pointing at the host-browser-reachable
URL; the agent pastes it into its reply, and the chat surface
renders markdown inside agent text replies just fine.

### Added
- **`COMFYUI_EXTERNAL_URL` env var** (defaults to `COMFYUI_URL`).
  The browser-reachable URL embedded in `display_markdown`. Operators
  typically set this to the host LAN IP (e.g.
  `http://192.168.x.x:13036`) or a tunnel/proxy URL —
  `host.docker.internal` doesn't resolve from the operator's browser,
  so the default only works inside the docker bridge.
- **`display_markdown` field** in the `comfyui_image__generate`
  response. One markdown image per generated PNG using the external
  URL: `![<filename>](<external_url><fetch_url_path>)`. Emitted
  unconditionally — light enough (~150 chars per image) that even
  multi-image batches don't push the agent context.
- **`agent_hint` field** in the same response: a one-sentence
  instruction telling the agent to paste `display_markdown` verbatim
  into the reply. Surfaced because tool description prose is easy
  for the LLM to skim past — repeating it in the structured result
  improves compliance.

### Changed
- **`comfyui_image__generate` description** now opens with the rule:
  ALWAYS paste `display_markdown` into the final reply. Without that
  paste the user sees only the JSON metadata.
- **The bridge response also returns `comfyui_external_url`**
  alongside `comfyui_base_url` so chat surfaces / userscripts that
  want to construct their own URLs have both.

### Migration
- Set `COMFYUI_EXTERNAL_URL` in `.env`.
- Bridge image rebuild + recreate.
- No patcher / compose-services / network changes.

### Documented (deploy follow-up, same day)
- **HTTPS-proxy + browser-cached HTTP Basic auth pattern** is the
  recommended setup for `COMFYUI_EXTERNAL_URL` when the chat UI is
  served over HTTPS (Cloudflare tunnel / reverse-proxy domain). Mixed
  content blocking silently drops HTTP `<img>` requests on HTTPS pages
  — host-LAN-IP only works when the chat itself is also HTTP.
  Verified end-to-end on GB10 with `vision.petyuspolisz.com` fronting
  `192.168.111.100:13036`: NPM Basic auth + browser per-origin
  credential cache, the chat surface renders the image after a one-time
  login dialog.
- **`.env.example` `COMFYUI_EXTERNAL_URL` block** rewritten with the
  3-setup decision matrix (HTTPS reverse-proxy / HTTP LAN IP / blank).
- **`openclaw-image-comfyui/README.md`** new "Chat-side image
  rendering" section with the NPM walkthrough.

## [0.9.4] - 2026-04-27

UX fix on top of v0.9.3 — generated images now render in the chat
surface without putting the base64 into the agent's prefill context.
The bridge emits an MCP `image` content item alongside the metadata
text content; chat surfaces that honor the MCP image content type
(OpenClaw web/control UI per the MCP spec) render it inline; clients
that ignore unknown content types lose nothing. The text content
the LLM actually prefills stays metadata-only.

### Added
- **MCP `image` content emission** in `comfyui_image__generate`'s
  `tools/call` response. Each generated image becomes one
  `{type: "image", data: <base64>, mimeType: "image/png"}` content
  item in the response's `content` array, alongside the existing text
  metadata content.
- **`attach_image_content: bool` parameter** (default `true`).
  Controls whether the bridge emits MCP image content items. Set
  `false` if your chat surface mistakenly prefills image content
  blocks into the LLM's text context (verify-first; not observed on
  OpenClaw 2026.4.22 with Gemma 4 NVFP4 — the spec says image content
  is host-renderable, not text-prefilled).

### Changed
- **`_tool_generate` builds an internal `_attachments` list** that the
  MCP wire dispatch extracts before serializing the tool result text.
  The text content the LLM prefills remains the same metadata-only
  payload v0.9.3 introduced — the image bytes never appear in the
  text JSON. `include_base64=true` still forces the bytes into the
  text content for clients that want them inline (rare).

### Verify
- `comfyui_image__generate` agent E2E with default `attach_image_content`:
  - Tool catalog still shows `comfyui_image__generate`.
  - Agent reply includes the metadata + the image renders in the
    OpenClaw chat surface.
  - Run wall clock should match the v0.9.3 metadata-only timing
    (~10-20s for SD1.5 512×512), NOT the v0.9.0 50K-token-prefill
    minutes — confirming the MCP image content is NOT being text-
    prefilled by the runtime.

## [0.9.3] - 2026-04-27

Bridge response-shape change. `comfyui_image__generate` no longer
embeds the PNG bytes in the tool result by default — agent runs that
called the tool with the v0.9.0/v0.9.2 default were timing out for a
different reason than the multi-step issue v0.9.2 covered: the
~50K-token base64 payload was forcing the next LLM-call's prefill to
chew through tens of thousands of uncached tokens at Gemma 4 NVFP4's
~16 tok/s prefill speed. Direct MCP `tools/call generate` returned in
6.25s (verified on GB10); the agent-wrapped run timed out at 600s
while the LLM was still prefilling the response.

### Changed
- **`comfyui_image__generate` returns metadata only by default.**
  Each `images[]` entry now contains `format`, `filename`, `subfolder`,
  `type`, `node_id`, `width`, `height`, `byte_size`, and a new
  `fetch_url_path` (relative URL on ComfyUI's HTTP API). The top-level
  result also carries `comfyui_base_url` so the agent can reconstruct a
  full fetchable URL: `{comfyui_base_url}{fetch_url_path}`. Operators
  and chat surfaces fetch the actual PNG via ComfyUI's `GET /view`
  endpoint with the metadata.
- **New `include_base64: bool` parameter** (default `false`). Set
  `true` to opt in to the old behavior — the PNG bytes appear under
  `images[].base64` and the result also has `include_base64: true` at
  the top level. Use only when you genuinely need the bytes inside the
  agent reply (e.g., a follow-up tool call that hashes them).
- **`comfyui_image__generate` description** rewritten to lead with the
  rationale and the `/view` fallback so the agent picks the right
  default unprompted.

### Documented
- **`docs/reference/image-comfyui-bridge.md` new "Response shape"
  section** with the prefill-throughput math and the v0.9.0/v0.9.2
  reproduction trail.
- **`openclaw-image-comfyui/README.md` "What's in the box"** rewritten
  to describe the metadata-only default and the `/view` fetch pattern.

### Migration
- No `.env` changes; no patcher changes; no breaking compose changes.
- Bridge image rebuild + recreate required to pick up the new
  response-shape default:
  `docker compose -f openclaw-image-comfyui/docker-compose.yml --profile image-gen up -d --build openclaw-image-comfyui`

## [0.9.2] - 2026-04-27

Documentation-only release. Captures the multi-step tool-call timeout
finding from the v0.9.0 GB10 smoke and bumps every documented agent
invocation to `--timeout 600`, the safe floor for any tool-using run on
Gemma 4 NVFP4 with the current MCP catalog.

### Documented
- **`docs/TROUBLESHOOTING.md` "Agent runs (multi-step tool calls)"
  section** — diagnostic chain for `Request was aborted` /
  `embedded run timeout` errors on tool-using agent runs. Spelled out:
  Gemma 4 NVFP4 generates ~6 tok/s on GB10; a multi-step tool-call run
  does 2-3 LLM calls of ~200 tokens each → 90-120s wall-clock; the
  default `--timeout 60` aborts mid-run; bump to `300` (single tool) or
  `600` (multi-step). Also covers `--thinking off` for routine tool
  calls and trimming the catalog by dropping unused profiles.
- **`CLAUDE.md` (root) new "Multi-step tool-call agent runs need a
  generous --timeout" implementation-detail nugget.** Captures the
  generation-throughput math, the failure mode (gateway logs
  `rawErrorPreview: "Request was aborted." failoverReason: "timeout"`,
  empty agent reply), the fix, and the rule that any documented
  tool-using prompt in this repo must use `--timeout 600`.
- **`docs/CUSTOMIZATION.md` Python sandbox + Image-gen smoke tests
  bumped to `--timeout 600`** with an inline comment cross-linking to
  the troubleshooting section.
- **`docs/reference/image-comfyui-bridge.md` verification recipe**
  bumped to `--timeout 600`.

### Changed
- **CLAUDE.md "Useful one-liners" multi-tool agent example** uses
  `--timeout 600` (was `--timeout 240`).
- **CLAUDE.md "Verification recipes" web_search example** uses
  `--timeout 600` (was `--timeout 180`).

### GB10 deploy notes (2026-04-27 hajnal)
- Direct MCP `tools/call generate` end-to-end: DreamShaper SD1.5,
  512×512, 15 steps, 6.25s wall-clock on GB10.
- Agent E2E `comfyui_image__list_workflows` with `--timeout 600`:
  `toolSummary {calls:1, failures:0}, stopReason: stop`. Reply:
  `NAMES: flux-schnell, sdxl-base`.
- Same diagnostic was reproducing on `python_sandbox__python_exec` —
  not a v0.9.0 regression, broader Gemma-4-NVFP4 + multi-step
  tool-call timing footprint that the documentation now reflects.

## [0.9.1] - 2026-04-27

Patch release. Fixes a tool-name double-prefix bug discovered on the
v0.9.0 GB10 smoke.

### Fixed
- **Bridge tool names are now bare** (`generate`, `list_workflows`,
  `cancel`) instead of pre-prefixed (`comfyui_image__generate`, …).
  The OpenClaw gateway prefixes MCP tool names with `<server>__` at
  surface time; pre-prefixing produced
  `comfyui_image__comfyui_image__generate` in the agent catalog,
  which the agent's prompt couldn't match. Mirrors the python-sandbox
  sibling, which always used bare `python_exec`.

## [0.9.0] - 2026-04-26

Image-generation MCP bridge — the agent can now drive image generation
on the operator's existing ComfyUI install via three new tools
(`comfyui_image__generate`, `comfyui_image__list_workflows`,
`comfyui_image__cancel`). The bridge is content- and model-agnostic;
the repo ships no model weights. First service in the stack to live in
its own Compose file (`openclaw-image-comfyui/docker-compose.yml`) —
deliberate separation from the main stack so the bridge can be brought
up independently and to keep the main `docker compose up -d` invariant
("9 default services") unchanged.

### Added
- **`openclaw-image-comfyui` service** — opt-in
  (`COMPOSE_PROFILES=image-gen` + non-empty `IMAGE_GEN_API_TOKEN`).
  Lives in a separate compose file
  (`openclaw-image-comfyui/docker-compose.yml`) joined to the main
  stack's bridge via `external: true` network reference. One container,
  one uvicorn process. No GPU, no torch, no model weights — pure HTTP
  wrapper. MCP Streamable-HTTP wire on `POST /mcp`, hand-rolled in
  `server/app.py` (~250 LOC, no SDK dependency, mirrors the v0.8.0
  python-sandbox pattern). Three tools surface: `comfyui_image__generate
  (prompt, workflow, width, height, steps, cfg, seed, negative,
  checkpoint, sampler, scheduler, batch_size, timeout_s)`,
  `comfyui_image__list_workflows()`, `comfyui_image__cancel(prompt_id)`.
- **Patcher step 19** — `mcp.servers.comfyui_image` deep-merge,
  env-gated by `IMAGE_GEN_API_TOKEN`. Same shape as step 18
  (`transport: streamable-http`, `url`, `connectionTimeoutMs`,
  `headers.Authorization`). When the token is unset, the entry is
  *removed* (and empty parent objects cleaned up) so the gateway
  doesn't try to dial a parked bridge.
- **Workflow template engine** (`server/workflow_loader.py`) — parses
  ComfyUI API-format JSON exports, strips a bridge-only `_metadata`
  block, substitutes user params (prompt, seed, dimensions, sampler,
  ...) into nodes by `class_type` lookup or explicit `targets` mapping.
  Substitution is by node-id + input-key, never by string-replace —
  prompts that legitimately contain `${…}` patterns (LoRA syntax,
  embedding refs) survive intact.
- **Two reference workflows** — `flux-schnell.json` (4-step distilled,
  fastest), `sdxl-base.json` (25-step, generic SDXL fine-tune carrier).
  Both ship with `"REPLACE_ME.safetensors"` checkpoint placeholder; the
  bridge refuses to generate without either an explicit `checkpoint=`
  arg or an operator-edited workflow JSON. License posture: this repo
  ships no model weights — operators pick FLUX Dev / Schnell, SDXL
  fine-tunes (Pony XL, Illustrious XL, RealVisXL, adult fine-tunes,
  ...) under whichever upstream license they accept.
- **`server/comfy_client.py`** — async httpx wrapper around ComfyUI's
  `/prompt`, `/history/{id}`, `/view`, `/queue`, `/interrupt`. Polling
  loop with 0.5s start, ×1.5 backoff, 2s cap, total budget
  `IMAGE_GEN_TIMEOUT_S`. Detects mid-render ComfyUI restart
  (`/history/{id}` 404 after a confirmed submission) and surfaces
  `ComfyUIRestartedError` rather than hanging until timeout.
- **Single-flight by default** — `IMAGE_GEN_MAX_CONCURRENCY=1` via
  `asyncio.Semaphore(1)`. ComfyUI runs on the same GB10 GPU as vLLM;
  concurrent generation pauses LLM token gen and is observable as
  multi-second user stalls. Set to `0` for pass-through if your ComfyUI
  is on a different GPU.
- **`bootstrap.sh` opt-in prompt 3e** — after the python sandbox
  prompt. Token-presence guard so re-runs don't re-ask. `openssl rand
  -base64 48` to mint. Optional `COMFYUI_URL` follow-up prompt with
  `http://host.docker.internal:13036` default. Best-effort
  `COMPOSE_PROFILES` toggle (advisory: the bridge lives in a separate
  compose file so the `image-gen` profile gate is informational, not
  load-bearing).
- **`rotate-secrets.sh` registers `IMAGE_GEN_API_TOKEN`** as a
  conditional secret — auto-included in `--all` only when already set
  (mirrors `F5HUN_API_TOKEN` / `PYTHON_SANDBOX_API_TOKEN`). Restart
  matrix maps the key to `openclaw-image-comfyui openclaw-config-init
  openclaw-gateway openclaw-cli`. Post-rotation print emits TWO
  `up -d --force-recreate` commands (one per compose file) — the
  bridge's `force-recreate openclaw-image-comfyui` cannot be merged
  into the main stack's command because the bridge is in a different
  compose project.
- **`.env.example` block** for the new tunables: `IMAGE_GEN_API_TOKEN`,
  `COMFYUI_URL`, `IMAGE_GEN_BIND/PORT`, `IMAGE_GEN_TIMEOUT_S`,
  `IMAGE_GEN_MAX_CONCURRENCY`, `IMAGE_GEN_MAX_OUTPUT_BYTES`,
  `IMAGE_GEN_POLL_INTERVAL_S`, `IMAGE_GEN_POLL_BACKOFF_MAX_S`. Section
  documents the model-agnostic posture and links to the bridge's
  `workflows/` README for the workflow authoring guide.
- **`docs/reference/image-comfyui-bridge.md`** — design rationale (why
  MCP not OpenAI-compat shim, why host-gateway not shared external
  network, why separate compose), threat model, workflow template
  architecture, verification recipes, known limits.
- **`docs/CUSTOMIZATION.md` "Image generation bridge (ComfyUI MCP)"
  section** with activation walkthrough, smoke tests, model-add
  workflow, custom-workflow authoring pointer, tuning, disable steps.
  Single explicit paragraph on license posture: model + workflow
  choice + their respective licenses (FLUX Dev research-use terms,
  Pony XL CC-BY-NC, vendor clauses, ...) are operator's
  responsibility.
- **`docs/ARCHITECTURE.md` "Image-gen bridge subsystem" subsection** +
  patcher-step list expansion (18 → 19) + networking-trust exposure
  list update + cross-compose join section explaining the
  `external: true` network reference + why-not-run-ComfyUI-here
  rationale.
- **`docs/TROUBLESHOOTING.md` four new entries** under "Image-gen
  bridge (openclaw-image-comfyui)": agent-can't-find-tool diagnostic
  chain (mirroring the python-sandbox tool-prefix gotcha), bridge →
  ComfyUI host-gateway hop reachability test, missing-checkpoint /
  REPLACE_ME placeholder error, mid-render ComfyUI restart recovery,
  GPU contention with vLLM token gen.
- **`docs/reference/README.md`** indexes the new
  `image-comfyui-bridge.md` reference doc.
- **`CLAUDE.md` (root) "Image-gen bridge: separate compose,
  host-gateway hop, model-agnostic"** implementation-detail nugget.
  Captures: separate-compose precedent, host-gateway hop choice,
  tool-prefix gotcha re-applies (`comfyui_image__*`), deliberate
  model-agnosticism.
- **`private/docs/todos.md`** — wishlist #4 ✅ DONE.

### Changed
- **Patcher header comment in `docker-compose.yml` and inline
  `patch-config.mjs` doc-block** updated from "18 steps" to "19
  steps". Top-level step 19 entry added in both places.
- **`docker-compose.yml` `openclaw-config-init` env block** gets
  `IMAGE_GEN_API_TOKEN` and `IMAGE_GEN_URL` so step 19 can deep-merge.
- **`bootstrap.sh` final summary block** mentions the bridge's
  separate-compose `up -d --build` invocation.
- **`rotate-secrets.sh` usage()** documents `IMAGE_GEN_API_TOKEN` in
  the conditional-key set + as an explicit positional arg example.

### Documented
- **Honest limit: no img2img yet.** Shipped workflows are
  text-to-image only. Adding img2img needs a `LoadImage` node in the
  workflow + a bridge code path to ferry uploaded base bytes into
  ComfyUI's input directory. Pending a use case that asks for it.
- **Honest limit: no streaming progress.** A 25-step SDXL render takes
  20-40s; the agent waits in the tool call until the PNG arrives.
  Bound by `IMAGE_GEN_TIMEOUT_S` (default 600s).
- **Honest limit: bridge → ComfyUI hop is unauth'd.** Mitigated by
  ComfyUI's port being loopback-only on the host. Documented loudly
  that publishing ComfyUI's port on a routable interface is a
  separate ComfyUI-default risk, orthogonal to this bridge.

### Pending GB10 deploy + smoke (2026-04-26 evening)
Code-level stable; not yet deployed end-to-end. Remaining
verification recipes (per `docs/reference/image-comfyui-bridge.md`):
build + bring up the bridge, healthz + tools/list direct curl, agent
end-to-end via `openclaw agent --message "Use comfyui_image__generate
..."`, token rotation E2E (cross-compose force-recreate), cleanup
branch (token unset → entry removed), restart-mid-gen recovery
(`ComfyUIRestartedError` path), single-flight serialization vs
pass-through behavior under concurrent agent calls.

## [0.8.0] - 2026-04-26

Self-hosted Python code-execution sandbox via MCP — the agent can now
write Python in tool calls, get stdout / stderr / inline plots back,
and keep variables alive across calls within a session. Plus a
documentation freshness pass for the MCP-related comments that became
stale when OpenClaw added native MCP client support.

### Added
- **`openclaw-python-sandbox` service** — opt-in
  (`COMPOSE_PROFILES=python` + non-empty `PYTHON_SANDBOX_API_TOKEN`).
  One container, one uvicorn process. Persistent ipykernel per
  `session_id` (lazy spawn, idle reaper at 30 min default), data-
  science stack baked in (pandas, numpy, matplotlib `Agg` backend,
  scikit-learn, scipy). `MCP Streamable-HTTP` wire protocol on
  `POST /mcp`, hand-rolled in `server/app.py` (~250 LOC, no SDK
  dependency). Two tools surface to the agent: `python_exec(code,
  session_id, timeout_s)` and `python_session_reset(session_id)`.
- **Patcher step 18** — `mcp.servers.python_sandbox` deep-merge,
  env-gated by `PYTHON_SANDBOX_API_TOKEN`. Schema verified against
  `docs.openclaw.ai/cli/mcp` on 2026-04-26 (`url`, `transport:
  streamable-http`, `connectionTimeoutMs`, `headers`). When the
  token is unset, the entry is *removed* (and empty parent objects
  cleaned up) so the gateway doesn't try to dial a parked service.
- **`bootstrap.sh` opt-in prompt** — section 3d, after the browser
  prompt. Token-presence guard so re-runs don't re-ask. Best-effort
  `COMPOSE_PROFILES` toggle so the activation triad lights up in
  one step.
- **`rotate-secrets.sh` registers `PYTHON_SANDBOX_API_TOKEN`** as a
  conditional secret — auto-included in `--all` only when already
  set (same posture as `F5HUN_API_TOKEN`). Empty token = the user
  declined opt-in; `--all` does not silently re-enable. Restart
  matrix maps the key to `openclaw-python-sandbox`,
  `openclaw-config-init`, `openclaw-gateway`, `openclaw-cli`.
- **`.env.example` block** for the new tunables: `*_API_TOKEN`,
  `*_PORT`, `*_BIND`, `*_KERNEL_TIMEOUT_S`, `*_MAX_OUTPUT_BYTES`,
  `*_IDLE_TTL_S`, `*_REAP_INTERVAL_S`, `*_MEMORY_MB`, `*_CPUS`.
  Two documented placeholders (`*_NETWORK`, `*_GPU`) reserved for
  future v0.8.x patches.
- **`docs/reference/python-sandbox.md`** — design rationale (why
  MCP and not native `code_execution` / `agents.defaults.sandbox`),
  threat model, kernel pool architecture, MCP wire protocol shapes,
  tunable reference, verification recipes, known limits.
- **CUSTOMIZATION.md** "Python code execution sandbox" section
  with activation walkthrough, tuning guide, hard-egress hardening
  recipe (docker `--internal` network override), library-add
  workflow, disable steps.
- **ARCHITECTURE.md** "Python sandbox subsystem" subsection +
  patcher-step list expansion (15 → 18) + networking-trust
  exposure list update.
- **TROUBLESHOOTING.md** entries for the three common failure
  modes: tool-not-registered (token / profile / patcher chain),
  kernel timeout (genuine long compute vs accidental sleep), OOM
  kill mid-call.

### Documented
- **MCP-stale comment fixups across 3 files**. Verify-first
  WebFetch on 2026-04-26 confirmed `docs.openclaw.ai/cli/mcp.md`
  lists native MCP client support — `openclaw mcp serve|list|show
  |set|unset`, config schema `mcp.servers.<name>`, transports
  stdio / SSE-HTTP / Streamable-HTTP. The repo's own design notes
  in `CLAUDE.md` (line ~270), `docs/ARCHITECTURE.md` (line ~355),
  and `docs/reference/browser-automation.md` (line ~33) all
  asserted "no MCP slot" — true at v0.7.0 design time
  (2026-04-25), stale by v0.8.0. Each comment now reads as
  history (the CDP-attach decision was correct *then*; the
  browser stack stays on CDP-attach because port-per-profile +
  cdpUrl token routing is the actual constraint, not transport
  shape) and points new MCP-based tool wiring at
  `mcp.servers.<name>`.
- **Honest limit: no hard egress block in v0.8.0.** The container
  has no curl / wget / requests-by-default and runs without root,
  so the kernel can't open raw sockets or apt-install — but
  `urllib` / `http.client` / `socket` work. Hard isolation is a
  documented `--internal` docker network override; a future
  v0.8.x patch will fold the wiring in via `PYTHON_SANDBOX_NETWORK`.
- **Honest limit: trusted-prompt only.** Container namespaces +
  non-root user defend the host filesystem. Python introspection
  could in principle drive a kernel exploit chain to escape the
  container — gVisor / Kata Containers documented as the upgrade
  path for multi-tenant deployments.

### Changed
- **Patcher header comment in `docker-compose.yml` and inline
  `patch-config.mjs` doc-block** updated from "15 steps" to "18
  steps". The repo had been at 17 steps in code (steps 16 and 17
  landed in v0.7.0 / v0.7.1) but the comment hadn't followed; this
  release brings them back in sync and adds step 18.

### GB10 deploy notes (2026-04-26 evening)
- `docker compose --profile python build openclaw-python-sandbox`
  succeeds in ~18s (slim base + cached pandas/numpy/scipy ARM64
  wheels). Image: `openclaw-python-sandbox:0.1.0`,
  `sha256:5d4b0b33c4c4...`.
- Patcher step 18 logs four lines on first run after the token is
  set: `mcp.servers.python_sandbox.{transport,url,connectionTimeoutMs,headers}`,
  with the headers value redacted as `<set>` so the bearer doesn't
  hit the audit log.
- `/healthz` returns `ok kernels=0` immediately (no kernel is
  spawned at startup; first `python_exec` lazy-spawns one).
- Direct MCP `tools/list` returns `python_exec` and
  `python_session_reset`.
- Direct `python_exec` call: `import pandas as pd, numpy as np;
  print(2**128)` returns `pandas 2.3.3 numpy 2.4.4` and the integer
  in 543ms (cold first call).
- Persistence smoke (set `x=42, y=[1,2,3]` in session=demo, then
  `print(x, y, sum(y))` in the same session) returns `42 [1, 2, 3] 6`.
- **Tool prefix gotcha**: OpenClaw surfaces external MCP tools under
  `<server>__<tool>`, so the catalog name is
  `python_sandbox__python_exec`. Gemma 4 31B NVFP4 silently fails
  to call the tool when the prompt refers to `python_exec` without
  the prefix (no failure logged, just an unrelated reply). With
  the prefixed name + `--thinking medium`, the agent calls the tool
  first try (1 call, 0 failures, correct result). All
  documentation now uses the prefixed form.
- A CLI plugin-loader hiccup hit during the post-patch
  `--force-recreate openclaw-cli` step: stale `node_modules` for the
  bundled `kimi-coding` and `openai` extensions caused
  `npm error ENOTEMPTY` on `rmdir`. Recreating the CLI container
  one more time (`docker compose up -d --force-recreate openclaw-cli`)
  resolved it cleanly — the second recreate gets a fresh image
  layer extraction with no stale `node_modules`. This is an
  upstream OpenClaw / npm interaction, unrelated to the sandbox
  changes; documented here so future deploys know it's benign.

### GB10 second-pass smoke (2026-04-26 22:00, post-correctness fixes)

After the per-session lock race fix, the Mcp-Session-Id echo, and
the MPLBACKEND drop, ran a wider smoke matrix. All green:

- **Mcp-Session-Id**: client-supplied header round-trips on
  `tools/list`. A fresh `initialize` request with no header gets a
  16-byte `token_urlsafe` minted in the response header.
- **Error handling**: `raise ValueError("test boom")` returns
  `isError: true` and a structured `error: { type: "ValueError",
  message: "test boom", traceback: <non-empty> }`. Kernel state
  survives — same `session_id` keeps working on the next call.
- **Timeout / interrupt_kernel**: `time.sleep(12)` with `timeout_s=3`
  returns `error.type="TimeoutError"`, `duration_ms ≈ 3001`. The
  kernel is interrupted, not killed; subsequent calls work.
- **Plot output**: `fig, ax = plt.subplots(); ax.plot(...); fig`
  returns `plots: [<base64 PNG>]` (PNG header `iVBORw0KGgoAAAA...`),
  with `result: "<Figure size 640x480 with 1 Axes>"` carrying the
  matplotlib repr. The kernel's matplotlib_inline backend publishes
  via iopub `display_data`. Verified that the previous
  `MPLBACKEND=Agg` env var killed this — Agg never publishes.
- **`python_session_reset`**: returns `{ session_id, existed: true }`,
  follow-up `python_exec` on the same `session_id` correctly raises
  `NameError` for previously-defined names. Lock and entry are popped
  together; no leak.
- **Multi-session concurrency**: two parallel `python_exec` calls on
  different `session_id`s with `time.sleep(3)` finish in ~3.3 s wall
  (parallel), not ~6 s (serialized). Per-session lock isolates
  correctly across concurrent kernels.
- **Output truncation**: `print("x" * 15 MB)` with default 10 MB cap
  returns `truncated: true`, `stdout` length `10485779` (10 MB +
  19-byte truncation marker). 5 MB stdout (under cap) returns
  `truncated: false`.
- **Patcher cleanup branch**: setting `PYTHON_SANDBOX_API_TOKEN=` and
  `--force-recreate openclaw-config-init` produces
  `[patch-config] PYTHON_SANDBOX_API_TOKEN unset — removed
  mcp.servers.python_sandbox.` and the entire `mcp` parent object is
  deleted from `openclaw.json` (no orphan `mcp.servers: {}` shell).
  Restoring the token + recreate writes the four fields back.
- **Token rotation E2E**: `./rotate-secrets.sh -y
  PYTHON_SANDBOX_API_TOKEN` rotates with `--all`-style audit
  output (sha256 fingerprints, planned restart command, validated
  via `docker compose config`). After running the printed
  `--force-recreate openclaw-python-sandbox openclaw-config-init
  openclaw-gateway openclaw-cli`, the OLD token returns 401 against
  `/mcp`, the NEW token returns 200 with the tools list, and the
  bearer in `openclaw.json` matches the container's
  `PYTHON_SANDBOX_API_TOKEN` env. Agent end-to-end via the gateway
  with the new token is green (`VAL: 63` from `print(7*9)`).
- **Permission gotcha during deploy**: `bootstrap.sh`,
  `rotate-secrets.sh`, and `bootstrap-browser-login.sh` lost their
  executable bit through the Windows-side commit. Fixed with `git
  update-index --chmod=+x`; one-time `chmod +x` was needed on
  GB10 before this commit landed. After this commit, fresh clones
  inherit the right mode.

## [0.7.3] - 2026-04-26

Tooling persistence — the runtime tools we leaned on heavily during
the v0.7.2 session investigation (xdotool, ImageMagick, playwright-
stealth) are now baked into the `openclaw-browser` image so a
`docker compose build --no-cache` brings them back automatically.

### Added
- **`xdotool`** in the `openclaw-browser` image (apt). OS-level mouse
  driver for `DISPLAY=:99`. Required when an agent-driven flow needs
  the *physical* X cursor to move (some site CAPTCHAs and animation-
  heavy UIs fingerprint the real X11 cursor, not just the JS event
  surface). Playwright's `mouse.move/down/up` dispatches CDP Input
  events that update the JS layer but DON'T move the X cursor;
  `xdotool` fills that gap so headful flows look closer to a human
  session. Coord note: viewport y + ~90 px (Chrome's chrome bar) =
  X11 y, when running against a 1920x1080 Chromium window.
- **`imagemagick`** in the same image. `import -window root` captures
  the X11 root window *including* the cursor — the CDP-side
  `Page.captureScreenshot` does not. Useful for debugging where a
  click actually landed visually.
- **`playwright-stealth>=2.0,<3`** in
  `openclaw-browser/server/requirements.txt`. Soft anti-bot
  fingerprint masking (navigator.webdriver=false, window.chrome mock,
  plugin spoof, language + WebGL/Canvas neutralization) applied via
  per-page `add_init_script`. Enough to pass `bot.sannysoft.com` and
  Cloudflare's gentler challenges; **NOT** enough to pass modern
  hCaptcha behavioral biometrics.

### Documented (no code change, but captured for future reference)
- Modern hCaptcha (sitekey rotation, multi-modal challenge engine,
  behavioral biometrics) is **not bypassable** with the layered
  open-source stack we ship: stealth flags + playwright-stealth +
  xdotool + visible cursor. The mechanical click works; the
  fingerprint wins. Patchright Chromium fork + paid solver service
  are the documented next-step options. See
  `workspace/memory/klanhaboru.md` for the full investigation log.
- Stealth Chromium with `--disable-blink-features=AutomationControlled`
  surfaces a *"You are using an unsupported command line flag"*
  warning in the Chrome address bar. Visible in any X11 root-window
  screenshot. May contribute to anti-bot fingerprinting; suppression
  would need a Chromium build flag patch (Patchright territory).

## [0.7.2] - 2026-04-26

Browser-automation hardening — bundled response to a real-world failure
mode (the v0.7.0 stack tried to do agent-driven registration on a
Hungarian browser game, the agent doom-looped on a `browser.act`
schema mismatch, then on closer inspection the underlying vLLM
streaming tool-call parser was leaking pipe-quote tokens into the
JSON arguments). Three independent fixes ride together:

### Added
- **`openclaw-vllm-proxy` service.** Drop-in OpenAI-compatible proxy
  in front of `vllm-llm`, in the default profile (no extra opt-in).
  Forces `stream=false` on every chat-completions request to dodge
  vllm-project/vllm#38946 (the gemma4 streaming tool-call parser leaks
  `<|"|>` string-delimiter literals into `tool_calls.arguments`), then
  re-fragments the bug-free non-streaming response back into a single
  SSE chunk so the gateway's stream reader is happy. Also runs a JSON-
  level normalizer over `browser.act` arguments to repair the recurring
  Gemma 4 mistake of emitting `request: {...}` without `request.kind`,
  by mirroring top-level `kind` / `ref` / `text` / `fields` into the
  wrapper. ~250 LOC FastAPI + httpx, no GPU. `LLM_BASE_URL` and
  `OPENAI_BASE_URL` defaults updated to point at `vllm-llm-proxy:8004`.
  When the upstream vLLM streaming-bug fix lands, drop the service and
  point the URLs back at `vllm-llm:8004` directly.
- **Stealth Chromium launch flags** (`openclaw-browser`).
  `--disable-blink-features=AutomationControlled`, `--disable-infobars`,
  `--exclude-switches=enable-automation`. Soft layer — hides the most
  common automation fingerprints (navigator.webdriver, "Chrome is being
  controlled" infobar) so light bot-detection systems don't auto-reject
  on first page load. Does NOT spoof the full Patchright fingerprint
  set; deeper anti-bot frameworks (hCaptcha behavioral biometrics,
  DataDome) still detect.
- **Viewport size is now fixed and configurable.**
  `BROWSER_VIEWPORT_WIDTH` / `BROWSER_VIEWPORT_HEIGHT` env vars (default
  `1920` / `1080`) drive both the Xvfb screen dimensions and the
  Chromium `--window-size` launch flag, kept in lock-step so iframe-
  rect math, full-page screenshots, and pixel-coord automation all
  agree on the same coordinate space. Earlier revisions hard-coded
  Xvfb to 1280x800 and let Chromium pick its own (small) default,
  which made screenshot-driven workflows hit-and-miss.

### Changed
- **Patcher step 15 `cdpHost` URL is unchanged**, but the OpenClaw
  config now writes `models.providers.vllm.baseUrl` pointing at
  `vllm-llm-proxy:8004/v1/`. Memory-search embeddings keep going
  directly to `vllm-embedding:8005` (no streaming, no tool calls,
  no need to proxy).

### Honest limitations (carried forward as docs)
- **hCaptcha behavioral biometrics still detect us.** The combined
  stealth flags + playwright-stealth init scripts get past
  `bot.sannysoft.com` checks, but real-world hCaptcha challenges
  (drag-puzzle, click-on-odd, image-grid) reset on the first
  CDP-driven mouse event. This is a known limit of vanilla Playwright
  Chromium against modern anti-bot infrastructure — a Patchright /
  full Chromium fork is the documented Phase 2 swap. Operator manual
  via noVNC remains the supported path for CAPTCHA-protected flows.
  Documented in `docs/reference/browser-automation.md`.

## [0.7.1] - 2026-04-25

Patcher step 17 — `browser.act` cheatsheet block in workspace
`AGENTS.md`. Smaller open models (Gemma 4 in particular) routinely emit
the flat `{element, text}` shape on `kind="fill"` actions that need the
nested `{fields: [{ref, type, value}]}` shape; the normalizer rejects
the call with `"fill requires fields"`, the model retries the same
broken shape, context fills with errors, and the agent eventually gives
up with an apology. The cheatsheet sits next to the existing browser-
profile policy block in the file every session reads at startup,
showing the right shape for `fill` / `click` / `type` plus a labelled
wrong shape and a one-line recovery hint. Idempotent (HTML-comment
markers `patch-config:browser-tools:start / :end`).

No upstream OpenClaw or compose changes — same workspace mount the
v0.7.0 patcher already uses for step 16.

## [0.7.0] - 2026-04-25

Browser-automation hardening release. Focuses on three things that bit
in real use of v0.6.x:

- **noVNC is always-on.** No more OTP-per-session UX; the bridge runs
  with a persistent password and the operator can attach any time the
  container is up.
- **The patcher registers `openclaw-browser` in
  `browser.ssrfPolicy.allowedHostnames`.** Without this, every CDP
  attach failed with `BrowserCdpEndpointBlockedError ("browser endpoint
  blocked by policy")` because the gateway's default SSRF guard rejects
  RFC1918 addresses (and the docker-bridge DNS name resolves into that
  range). The first stack to use the v0.6.x browser feature on a fresh
  install hit this immediately.
- **Workspace `AGENTS.md` carries a soft policy block** about
  credentialed browser profiles, so the agent has a written reminder
  that non-default profiles are opt-in. Soft layer only — see threat
  model below.

### Added
- **Always-on noVNC bridge — `BROWSER_VNC_PASSWORD`.** New required env
  generated by `bootstrap.sh` on first run, rotated by `rotate-secrets.sh
  --all`. Xvfb + x11vnc + websockify start with the container and stay
  up for its lifetime. Outside an active login-helper window the screen
  is blank (no headful Chromium attached); pop a profile into headful
  mode via `./bootstrap-browser-login.sh <name>` to peek at the agent's
  view. New `GET /v1/vnc` endpoint on the management API returns the
  current bridge URL so any caller can fetch it without reconstructing.
  RFB Type-2 truncates the password to 8 chars on the wire — real
  defense is the loopback host bind on `BROWSER_VNC_BIND` (default
  `127.0.0.1`).
- **Patcher step 16 — soft policy block in workspace `AGENTS.md`.**
  Idempotent (HTML-comment markers `patch-config:browser-policy:start /
  :end`). Reminds the agent to use `profile="self-hosted"` for general
  browsing and treat credentialed profiles as opt-in only, mentioning
  prompt-injection as the threat model. Documented as a SOFT layer in
  the patcher comment: a sufficiently aggressive injection can talk
  past it. The hard layer (per-agent tool/profile capability isolation)
  is a tracked follow-up — needs upstream openclaw schema research.

### Changed
- **`openclaw-browser` v0.7.0 — split `VncBridge` from `LoginHelper`.**
  The bridge is one module-level singleton with `start()` / `stop()` on
  the FastAPI startup / shutdown hooks. `LoginHelper` simplifies to a
  Chromium headful/headless toggle on the bridge's existing display.
  `LoginHelperRequest` no longer takes an `otp` field — the request body
  is empty. `bootstrap-browser-login.sh` no longer generates an OTP and
  reads the bridge URL from the API response directly. Breaking change
  for anyone who scripted around the old OTP-in-request body.
- **Patcher step 15 also writes `browser.ssrfPolicy.allowedHostnames`.**
  Whenever the patcher emits a `browser.profiles.<n>.cdpUrl` pointing at
  a docker-bridge host, it also adds that hostname to the SSRF allow
  list. Targeted exemption — `dangerouslyAllowPrivateNetwork=true` is
  *not* used; only the configured CDP host gets a private-network pass,
  every other private-IP nav target stays blocked.

### Original v0.6.x→0.7.0 browser-automation feature set (carried
forward in the public-API docs, summarised here for the release notes
audience):

- **`openclaw-browser` service — self-hosted Playwright Chromium over CDP
  (OPT-IN, `--profile browser`).** OpenClaw's built-in `browser` tool
  attaches via `browser.profiles.<name>.cdpUrl`; one warm Chromium per
  onboarded credential, persistent user-data-dir on the new
  `browser-storage` Docker volume so 1x manual login holds for the full
  upstream session lifetime (~14d GitHub, ~30d Notion). Port-per-profile
  routing (default 9222, named profiles 9223-9241) because OpenClaw
  doesn't forward `?profile=<name>` query params on cdpUrl attaches
  (issues #4841 / #9723 / #11926). FastAPI management API on port 9220
  (Bearer-auth, distinct from the CDP query-token path). Markdown
  extraction via trafilatura + readability-lxml fallback exposed at
  `/v1/extract` for agents that grab HTML via `browser.evaluate` and want
  cleaner text. Apache 2.0 wrapper code; vanilla Playwright Chromium —
  no stealth shipped, hostile-CDN sites are out of scope. Detailed
  rationale (CDP-attach vs MCP, port-per-profile, query-string token
  trade-off, session expiry matrix, WebAuthn/passkey limitation) in
  `docs/reference/browser-automation.md`.
- **`bootstrap-browser-login.sh <profile-name>` — 1x OAuth onboarding
  helper.** Toggles the named profile's Chromium into headful on the
  always-on noVNC bridge (since v0.7.0 — earlier revisions span the VNC
  stack up per-session). Operator drives the auth flow on their laptop
  browser (password + TOTP / SMS OTP / magic link — passkeys do NOT
  work over noVNC by W3C origin-bound spec), hits Enter; service
  flushes Chromium cleanly so cookies persist, re-launches Chromium
  headless on the same `--user-data-dir`, appends the profile to
  `BROWSER_PROFILE_NAMES` in `.env`, and re-runs `openclaw-config-init`
  so the patcher writes the new `browser.profiles.<n>.cdpUrl` entry.
- **Patcher step 15 — browser provider wiring.** Env-gated on
  `BROWSER_API_TOKEN`. Writes `browser.enabled=true` plus one
  `browser.profiles.<name>.cdpUrl` per registered profile (default
  `self-hosted` + each name in `BROWSER_PROFILE_NAMES`, comma-separated).
  Auth is `?token=<...>` in the URL — the only auth surface OpenClaw's
  cdpUrl config field supports (query token or HTTP Basic only, not
  Authorization headers).
- **`bootstrap.sh` browser opt-in prompt.** New interactive block after
  the HU TTS prompt; generates `BROWSER_API_TOKEN` (48-byte random)
  alongside the other secrets, asks the operator whether to add
  `browser` to `COMPOSE_PROFILES`. Idempotent — re-running preserves
  prior choices.
- **`rotate-secrets.sh` covers `BROWSER_API_TOKEN`.** Added to
  `DEFAULT_KEYS`; `--all` rotates it on the same cadence as every other
  secret. The `--profile browser` flag is appended to the printed
  recreate command when `BROWSER_API_TOKEN` is in the rotation set
  (matches the existing `--profile hu` handling for the F5HUN token).
- **Public docs rewritten / extended for the new service.** README
  service table row, SETUP.md section 8 ("Enable browser automation"
  with re-numbered subsequent sections), ARCHITECTURE.md "Browser
  automation subsystem" + step 15 description, CUSTOMIZATION.md tuning
  subsections (rate limiter, blocklist, port range, LAN exposure
  reverse-proxy pattern, Patchright stealth swap, Firecrawl sidecar),
  TROUBLESHOOTING.md eight new entries (session_expired, noVNC black
  screen, WebAuthn/passkey limitation, Chromium SIGKILL, Cloudflare
  block, port range exhaustion, Bearer vs query-token confusion),
  CLAUDE.md two new "Implementation details worth knowing" entries
  (CDP-attach + port-per-profile + query-token rationale; WebAuthn
  doesn't work over noVNC).

## [0.6.1] - 2026-04-24

Point release with two TTS-router polishes surfaced by the closed-loop
benchmark (`scripts/bench_tts_stt_roundtrip.py`) — both in the router only,
no backend or model changes.

### Added
- **Leading silence pad in the TTS router (`ROUTER_LEADING_SILENCE_MS`,
  default 300 ms).** F5-TTS emits audio with near-zero onset silence — the
  first phoneme starts at t=0 — which collides with Whisper's ~50-100 ms
  STT warm-up window and drops the first phoneme. Benchmark surface was
  "Szia Petya, ez egy rövid teszt" → "Zia Petya, ez egy rövid teszt"
  (16.7% WER on a 6-word clip). The router now pipes every backend output
  through an ffmpeg `adelay` filter that prepends `ROUTER_LEADING_SILENCE_MS`
  of silence before converting to the client-requested format. Overhead is
  sub-millisecond; set to 0 to revert (brings back the onset-clip bug).
  Kokoro EN was not the primary motivation (its training audio has natural
  leading silence) but the pad applies uniformly and is harmless for
  English too.
- **TTS→STT closed-loop benchmark (`scripts/bench_tts_stt_roundtrip.py`).**
  Already landed in 0.6.0; calling it out here because the 0.6.1 fix was
  discovered by running it. Measures per-size WER + latency for backend-direct
  and router paths across EN (Kokoro) + HU (F5-TTS).

### Changed
- **Router ffmpeg path now mandatory for every response_format.** Previously
  wav/pcm/flac/ogg short-circuited past ffmpeg when the backend already
  produced the requested format; with the silence pad that's no longer
  possible, so every format goes through ffmpeg (adelay + re-encode). When
  `ROUTER_LEADING_SILENCE_MS=0` and the backend format already matches the
  client format, we still take the legacy fast path — the refactor is
  strictly additive, not a regression for operators who opt out.

## [0.6.0] - 2026-04-24

Release hardens the self-hosted TTS pipeline against wheel drift and ships a
beginner-friendly Discord bot setup walkthrough. Triggered by a real
production regression where the 2026-04-22 cu130 torch wheels baked into
`openclaw-tts-en` and `openclaw-tts-f5hun` shipped without Blackwell kernels:
every synthesis request returned 500, OpenClaw's TTS provider chain fell
through to Microsoft Edge TTS, and Hungarian voice requests came back with
an English-accented Microsoft voice instead of the bundled F5-TTS. A
`docker compose build --no-cache` on the two TTS services picks up the
now-Blackwell-ready wheel; the new runtime guard ensures the same failure
mode can't recur silently — it exits with an actionable rebuild command at
startup if the wheel's `arch_list` can't cover the current GPU. The guard
also handles PTX forward-compat correctly: GB10 reports `sm_121` while the
current cu130 wheel's top kernel is `sm_120`, which JIT-compiles to sm_121
fine; the health endpoint surfaces this as `gpu_compat: ok ptx-fwd (...)`
so operators can distinguish an exact-match happy-path from a
forward-compat one that will JIT on first call.

### Added
- **TTS backend fail-fast GPU kernel guard.** `openclaw-tts-en/server/app.py`
  and `openclaw-tts-f5hun/server/app.py` now run `_verify_gpu_compat()` at
  import time: if `KOKORO_DEVICE=cuda` / `F5_DEVICE=cuda` but the installed
  torch wheel lacks the host GPU's compute capability (e.g. sm_120 missing on
  a stale cu130 wheel), the service exits with an actionable error naming the
  exact rebuild command. Prevents the previous silent-degradation failure
  mode where the router's 500 propagated to OpenClaw's fallback chain and
  Microsoft Edge TTS answered with wrong-accent audio. Both backends also
  expose `device` + `gpu_compat` in `/healthz` so external monitors can
  see the GPU state without guessing. Covered in
  `docs/TROUBLESHOOTING.md` → "TTS backend container crash-loops with torch
  wheel was built without sm_NNN kernels" and "openclaw infer tts convert
  returns success but provider=microsoft instead of openai".
- **Discord bot setup walkthrough (`docs/discord-bot-setup.md`).**
  Beginner-friendly step-by-step for operators who've never created a
  Discord bot: three-layer permission model primer (OAuth2 scopes vs bot
  permissions vs privileged gateway intents), Developer Portal flow, invite
  URL generation with per-permission explanations, server invite + role
  hierarchy + channel-level overrides, pre-OpenClaw checkpoint, and
  Discord-side troubleshooting. Cross-linked from `docs/CUSTOMIZATION.md`
  (new teaser on the Voice-controlled agent section) and `.env.example`
  (one-line pointer in the `DISCORD_BOT_TOKEN` comment block).
  `docs/CUSTOMIZATION.md` Step 1 also corrected: slash-command-only voice
  bots need **no** privileged intents by default (previously over-requested
  both Server Members + Message Content Intent; neither is needed for the
  default path).
- **Env-pinnable OpenClaw image tag + upgrade runbook.** All three
  `openclaw-{config-init,gateway,cli}` services now resolve the image via
  `ghcr.io/openclaw/openclaw:${OPENCLAW_IMAGE_TAG:-latest}`, so operators can
  pin a specific digest or release tag for reproducible deploys and roll
  back cleanly when a new upstream release regresses. Default `latest`
  preserves the previous behaviour. `docs/CUSTOMIZATION.md` adds an
  "Upgrading the OpenClaw gateway" section covering pre-flight digest
  recording, config + state tarball backup, patcher-log verification,
  post-upgrade smoke tests (healthz, `memory status --deep`, agent turn,
  `channels list`), and rollback via env-var pin or tarball restore.
- **Discord voice-controlled agent: scaffolding + setup runbook.** New
  opt-in plumbing (`DISCORD_BOT_TOKEN`, `DISCORD_AGENT_NAME`,
  `DISCORD_AUTOJOIN_GUILD_ID`, `DISCORD_AUTOJOIN_VOICE_CHANNEL_ID` in
  `.env.example`; env passthrough on both `openclaw-config-init` and
  `openclaw-cli` so setup commands can reference the token without
  re-entry) for joining an OpenClaw-controlled bot to a Discord voice
  channel and driving an agent by voice — speech-in via the bundled
  Whisper, plan + execute via Gemma 4 with cautious exec-policy,
  speech-out via the bundled Kokoro/F5-TTS router. Workspace is
  deliberately isolated at `~/.openclaw/workspace-discord/` (separate from
  the operator's primary workspace) so anyone with access to the bound
  voice channel cannot reach personal memory or files. Full runbook in
  `docs/CUSTOMIZATION.md` → "Voice-controlled agent over Discord";
  deeper schema + isolation + threat-model notes in
  `docs/reference/discord-voice-agent.md`. No patcher step yet —
  `openclaw channels add` remains the canonical write path while the
  `channels.discord.*` leaf schema stabilizes.

## [0.5.0] - 2026-04-24

Release rolling up the Speech-to-Text stack. A new OpenAI-compatible
`openclaw-stt-whisper` service joins the default compose (loopback publish
`127.0.0.1:8093`), wired into OpenClaw's `tools.media.audio` pipeline via a
new idempotent patcher step 14 — voice-note upload in the Control UI chat,
Discord voice channels, VoiceCall CLI, and Talk / Voicewake nodes all
transcribe through it. Built from `./openclaw-stt-whisper/server/` on a
CUDA 13 base (Blackwell-ready CT2 compiled from source) with a ~150 LOC
FastAPI wrapper around `faster-whisper`. Default model: the Hungarian
Whisper fine-tune `Trendency/whisper-large-v3-hu` (Apache-2.0) — measurably
better on Hungarian at the same English accuracy per the included
validation. Service count `8 → 9` default, patcher step count `13 → 14`,
`rotate-secrets.sh` default set grows by `STT_API_TOKEN`. See the entries
already documented under `[Unreleased]` above (this release absorbs them
wholesale — no further changes).

### Changed
- **STT default model — Trendency/whisper-large-v3-hu (Apache-2.0)**. After
  cross-benchmarking on GB10 (2026-04-24), the community Hungarian fine-tune
  measurably outperformed the vanilla Whisper large-v3 baseline on both
  Hungarian use cases we stress-tested:
  - **Clean HU (LibriVox János Vitéz, 3-min slice)**: 7-8 fewer
    Whisper-typical mis-hearings per chapter — `"Szerelem tüze ég"` vs
    vanilla's `"Szerelem tüzejék"`, `"csillámló habjára"` vs `"csillám
    lóhabjára"`, `"térdecskéje"` vs `"térdeskéje"`, `"patak habjain"` vs
    `"patakhapjaim"`, proper ékezet on `"kökény"` / `"Dúlt fúlt"`.
  - **Noisy HU (same slice + telephone bandpass + pink noise)**: Trendency
    kept the 36-segment structure steady (vanilla fragmented to 61
    segments), finished faster (22.5 s vs 27.3 s wall), and accumulated
    only 1-2 new mis-hearings under the degradation.
  - **EN (JFK 11-sec reference clip)**: identical output to vanilla
    (`"And so, my fellow Americans, ask not what your country can do for
    you, ask what you can do for your country."`) — no English-side
    regression at smoke scale.
  - **Runtime characteristics unchanged**: ~3 GB VRAM at float16,
    ~0.11-0.12× real-time factor warm, same OpenAI-compat
    `/v1/audio/transcriptions` interface, same `tools.media.audio`
    wiring. One observed tulajdonnév-regression: `"Majlinger Diána"` →
    `"Meilinger Diána"` (Hungarian-only fine-tunes lose some proper-noun
    specificity). Trade off deemed worth it for the general quality
    gain.

  **First-boot cost**: the Trendency repo ships safetensors (no CT2
  weights), so the first transcribe on a fresh volume runs
  `ct2-transformers-converter --quantization float16` in-process (~3 min
  including the ~8 GB HF download). The converted CT2 artefacts are
  cached in the `stt-whisper-hf-cache` volume; subsequent boots load
  instantly.

  **How to swap back to vanilla**: uncomment `STT_WHISPER_MODEL=Systran/faster-whisper-large-v3`
  in `.env`. The vanilla CT2 weights are Systran-hosted and load without
  a conversion step, so flipping between the two is instant once both
  have been booted at least once.

### Removed
- **benmajor27/whisper-large-v3-hu_full as a recommended swap option.**
  The community fine-tune was documented in the STT stack reference as a
  possible Hungarian swap, but 2026-04-24 validation on GB10 proved it
  unusable for real-world audio: the model collapses into compression-ratio
  loops on out-of-distribution input (LibriVox Petőfi, noisy synthetic
  phone-grade), emitting `"Tüz. Tüz. Tüz."`, long runs of repeated dashes
  and Unicode replacement characters, and random English tokens where
  Hungarian should go. The 8.86% CV17 WER from the model card is
  evaluation-overfit (trained on the full CV17 set, evaluated on the
  same). Removed from `.env.example` examples, the model catalog in
  `docs/reference/stt-stack.md`, and the `openclaw-stt-whisper/README.md`.
  The runtime auto-conversion path still works for any non-CT2 HF repo id
  the operator puts in `STT_WHISPER_MODEL` — the removal is purely
  documentation / recommendation.


### Added
- **STT stack — Whisper large-v3 with a self-built CUDA 13 image.** New
  `openclaw-stt-whisper` service built from `./openclaw-stt-whisper/server/`
  on `nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04` + cu130 PyTorch wheels +
  `faster-whisper>=1.2` + ~150 LOC FastAPI wrapper. Serves OpenAI-compatible
  `/v1/audio/transcriptions`, `/v1/audio/translations`, `/v1/models`, `/health`
  running `Systran/faster-whisper-large-v3` (MIT). ~3 GB VRAM at float16,
  autodetects language (FLEURS Hungarian WER 14.1%, the best validated number
  among the OpenAI-compat candidates — details in `docs/reference/stt-stack.md`).
  Consumed by OpenClaw's voice-note upload in the Control UI chat, Discord
  voice channels, the VoiceCall CLI, and the Talk / Voicewake node pipelines.
  The Control UI realtime mic button is a separate path — it uses the
  browser's native Web Speech API (`speech.ts`) and does NOT go through this
  service; that is OpenClaw's design choice, not a wiring limitation here.
  Loopback-only publish by default (`127.0.0.1:8093`), Bearer-auth via
  `STT_API_TOKEN`. Alternates documented in `docs/CUSTOMIZATION.md`:
  `deepdml/faster-whisper-large-v3-turbo-ct2` (8× faster, ~1.6 GB VRAM,
  HU WER unpublished — benchmark before flipping), and a `WHISPER_COMPUTE_TYPE`
  fallback ladder (`float16` → `bfloat16` → `int8_float16` → `int8_bfloat16`
  → `int8` → `float32`) for cases where a specific kernel class isn't
  compiled into the CTranslate2 that ships with `faster-whisper`.

  **Why self-built**: the upstream `ghcr.io/speaches-ai/speaches` image —
  originally picked to avoid maintaining any custom code — publishes only
  CUDA 12.6.3 variants, and on GB10 (sm_120) that image's CTranslate2
  rejects every low-precision compute type and numerically destabilizes on
  `float32`. A CUDA 13 base + cu130 PyTorch wheels is the proven GB10 path
  (matches the `vllm-llm` and `openclaw-tts-en` wheel pattern). Swap
  `build:` back to `image:` in `docker-compose.yml` when the speaches
  upstream publishes a Blackwell-tensor-core variant — the wrapper retires
  in a ~15-line diff.

  **Build steps the Dockerfile handles**: CTranslate2 aarch64 wheels on
  PyPI are CPU-only and upstream publishes no sdist, so the Dockerfile
  clones `v4.7.1` from github, `sed`s out CT2's call to the deprecated
  `cuda_select_nvcc_arch_flags` (CMake FindCUDA doesn't know sm_120),
  replaces it with explicit `-gencode arch=compute_80,code=sm_80
  -gencode arch=compute_120,code=sm_120`, then cmake+nvcc the native lib
  via Ninja. CMake >=3.30 is pulled from pip (Ubuntu 24.04's 3.28 is too
  old for the CMake-side CUDA arch mappings). torch is installed AFTER the
  CT2 Python binding so torch's fake-op hook registration doesn't crash
  setuptools during bdist build. First `docker compose build` takes ~20
  min; subsequent builds hit the Docker layer cache.

  **HU validation (2026-04-24 GB10)**: one-pass transcription of
  `janosvitez_1_petofi_64kb.mp3` (LibriVox, 10m39s, Petőfi "János vitéz"
  Majlinger Diána reading) returned `language: hu`, 122 segments, correct
  recognition of proper nouns ("Petőfi Sándor", "Majlinger Diána"), accents
  and rhyme structure intact, a small number of Whisper-typical mis-hearings
  on archaic vocabulary ("Pillancsi", "tüzejék") consistent with the FLEURS
  14.1% HU WER baseline. Wall clock 77.5 s → ~0.12× real-time factor at
  float16 on Blackwell.
- **Patcher step 14 — `tools.media.audio` wiring.** New env-gated step
  in `patch-config.mjs` that upserts an entry into
  `tools.media.audio.models[]` with `provider: "openai"`, the configured
  model id, `baseUrl: http://openclaw-stt-whisper:8080/v1/`, and a
  per-entry `headers.Authorization: Bearer $STT_API_TOKEN`. Writing the
  Bearer to `headers` (instead of the `apiKey` field) keeps the Whisper
  token orthogonal to the global `models.providers.openai.apiKey` —
  users with a separate cloud OpenAI account aren't affected. Upsert-by-
  `baseUrl` preserves unrelated user-added entries (Deepgram, a local
  whisper-cpp CLI entry, …). Env-gated: skips cleanly when
  `STT_API_TOKEN` is unset, which combined with `profiles: ["never"]` on
  the service is the full STT opt-out path.
- **`docs/reference/stt-stack.md`** — new reference doc paralleling
  `tts-stack.md`. Covers the backend choice (decision matrix vs
  NVIDIA Parakeet/Canary, Microsoft Phi-4 Multimodal, Distil-Whisper),
  the three OpenClaw voice surfaces (Control UI mic vs voice-note
  upload vs Discord/Talk/VoiceCall), the `tools.media.audio` schema,
  the model catalog (large-v3 default / turbo alternate /
  int8_float16 fallback), verification recipe, and troubleshooting.
- **`rotate-secrets.sh`** picks up `STT_API_TOKEN` in the default
  (`--all`) set. Restart matrix updated: rotating `STT_API_TOKEN`
  force-recreates `openclaw-stt-whisper openclaw-config-init
  openclaw-gateway openclaw-cli` so the backend Bearer and the patched
  `tools.media.audio.models[].headers.Authorization` stay in lockstep.
- **`docs/TROUBLESHOOTING.md` → "Embedder crashed mid-index"** entry in
  the `vllm-embedding` section. Documents the observed failure mode
  where a transient `cudaErrorNotPermitted` crash leaves the vector
  store partially populated (e.g. `Indexed: 2/10 files · 14 chunks`)
  while OpenClaw still reports `Dirty: no` — and the
  `memory index --force` workaround. Upstream OpenClaw gap tracked as
  [openclaw/openclaw#70567](https://github.com/openclaw/openclaw/issues/70567);
  until upstream fixes that, any embedder restart is a cue to run a
  forced reindex.

### Changed
- Patcher step count `13 → 14` (new step 14 wires STT). Header docs in
  `patch-config.mjs`, `docker-compose.yml`, `CLAUDE.md`,
  `docs/ARCHITECTURE.md`, `README.md`, `SETUP.md` all updated.
- Service count `8 → 9` default (`10` with `--profile hu` active) —
  `openclaw-stt-whisper` joins the default profile.

## [0.4.3] - 2026-04-23

Live secret rotation release. Adds `rotate-secrets.sh` as a safe,
re-runnable sibling to `bootstrap.sh` for rotating the auto-generated
secrets in an existing `.env` after install — and documents the three
operator scenarios that call for it.

### Added
- **`rotate-secrets.sh` — live secret rotation.** Sibling to `bootstrap.sh`
  for rotating the auto-generated secrets in an existing `.env` after
  install. Atomic write (temp file + `mv`), timestamped `.env.backup-*`
  before any change, post-write `docker compose config` validation with
  automatic restore on failure. Prints the deduped
  `docker compose up -d --force-recreate …` command for the services
  that consume each rotated key; the script does not restart anything
  itself so the operator picks the moment. Default set (`--all`):
  `VLLM_API_KEY`, `SEARXNG_SECRET`, `OPENCLAW_TTS_ROUTER_API_KEY`,
  `TTS_API_TOKEN`, plus `F5HUN_API_TOKEN` when already set (empty = HU
  TTS opted out of the CC-BY-NC model). `OPENCLAW_GATEWAY_TOKEN` is
  opt-in via `--include-gateway-token` because post-onboarding the real
  gateway auth lives in `openclaw.json`'s `gateway.auth.token`. The
  `^CHANGE_ME` gate `bootstrap.sh` uses is deliberately absent, so a
  fresh install can `cp .env.example .env && ./rotate-secrets.sh --all`
  to fill every placeholder, and live rotations don't need a special
  flag either.
- **`docs/CUSTOMIZATION.md` → "Rotating secrets" section** — operator
  runbook for the three rotation scenarios (routine hygiene, post-leak,
  pre-onboarding fill).

## [0.4.2] - 2026-04-22

Documentation release. Publishes the deep-dive knowledge base under
`docs/reference/`, declares an English-only documentation policy for public
repo content, translates the six reference files to English, and renames the
gitignored private-artifacts folder from `operator/` to `private/`.

### Added
- **`docs/reference/` knowledge base** — six public reference documents
  alongside the end-user docs: `llm-stack.md`, `tts-stack.md`,
  `tts-research-hungarian.md`, `openclaw-internals.md` (the deep-dive
  counterpart to `docs/ARCHITECTURE.md`: schema, 3-store credential
  layout, patcher step detail, CLI overhead), `patterns.md`, and an
  index `README.md`. The root `CLAUDE.md` "When in doubt" section
  points here as the next stop for deeper questions.
- **Documentation-language policy in `CLAUDE.md`** — new `Working principles`
  subsection declaring that every public repo file (README, SETUP, CLAUDE,
  CHANGELOG, `docs/**`, compose/patcher inline comments, commit messages,
  PR/issue templates, release notes) is written in English. Imported
  non-English material is translated before commit. No grandfathered
  exceptions — if non-English prose slips through, translate it rather
  than commit as-is.

### Changed
- **`docs/reference/` written in English.** All six files carry native
  English prose, and the former `tts-research-magyar.md` is renamed to
  `tts-research-hungarian.md` for filename consistency. The public repo
  is now linguistically uniform end to end.
- **`.gitignore` — `operator/` → `private/` rename.** The gitignored
  per-machine / private-artifacts folder is now `private/`, so the
  privacy state (not the role of the owning person) is the first-class
  label. Only the `.gitignore` line changes in the public tree; public
  repo consumers never interacted with this folder either way.

## [0.4.1] - 2026-04-22

Polish release rolling up the post-v0.4.0 fix batch. Two new idempotent patcher
steps harden the `openclaw agent` CLI path against token / credential drift, a
handful of defaults are flipped for less friction on a fresh clone, and the docs
catch up with the two patcher steps added post-tag.

### Added
- **Patcher step 12** — mirror `gateway.auth.token` into `gateway.remote.token`.
  The onboarding wizard writes `auth.token` but leaves `remote.token` unset, so
  the loopback CLI's WS connect failed with `unauthorized: gateway token
  mismatch` and silently fell back to an embedded-runner path (a side-car, not
  the production agent route). Step 12 keeps the two fields in lockstep on
  every `up`.
- **Patcher step 13** — sync the per-agent `auth-profiles.json`
  `vllm:default.key` with `VLLM_API_KEY`. The agent runner reads the vLLM
  credential from this per-agent store, *not* from
  `models.providers.vllm.apiKey`; drift after a `.env` rotation produced HTTP
  401 from vLLM even when the config-file apiKey was correct.
- **`operator/` gitignored** — per-operator private artifacts (local `.env`,
  `.claude/settings.local.json`, handoff notes, userscripts, TTS samples) live
  outside the public tree and are never part of a clone.

### Changed
- **`.env.example` defaults converged for public ergonomics:**
  - `CONTAINER_NAME_PREFIX=` (empty) — bare container names
    (`openclaw-gateway`, `vllm-llm`, …) in `docker ps`. Set to a prefix only
    if running multiple stacks on one host.
  - `OPENCLAW_ENABLE_DREAMING=1` — nightly memory consolidation on by default
    (the current stable OpenClaw image supports it; flip to 0 only if you've
    pinned a pre-2026.4.15 tag that rejects the memory-core plugin schema).
  - `HUGGING_FACE_HUB_TOKEN=` (empty) — replaces the `hf_CHANGE_ME` placeholder
    (HF tokens aren't openssl-generable like the rest). `bootstrap.sh` prompts
    for it on first run.
- **`openclaw-cli` service env simplified.** Dropped the redundant
  `OPENCLAW_GATEWAY_TOKEN` (the CLI prefers the env token over
  `gateway.remote.token` when both are present; drift between bootstrap-rotated
  `.env` and wizard-generated `auth.token` produced token-mismatch failures).
  Added `OPENAI_API_KEY` / `OPENAI_BASE_URL` mirror from `VLLM_API_KEY` so the
  embedded-runner fallback has a valid credential if the primary WS path ever
  hiccups.

### Fixed
- **vLLM healthcheck uses `python3`.** The `vllm/vllm-openai:gemma4-cu130`
  image ships no `python` alias; the healthcheck was red on every fresh pull
  even though the server was live.
- **Patcher step 11 `messages.tts.auto` / `messages.tts.mode` enum values.**
  Now writes the OpenClaw-accepted strings (`always` / `final`); previous
  values were silently rejected by the schema, so TTS stayed off even when
  the full provider block was wired.

### Docs
- Audience-first polish pass across `README.md`, `SETUP.md`, `CLAUDE.md`,
  `docs/*`, `docker-compose.yml`, and `patch-config.mjs`.
- Post-v0.4.0 polish: the fresh-install gateway crash-loop is documented as
  the intended two-phase onboarding flow (OpenClaw security model), the
  `--force-recreate openclaw-config-init openclaw-gateway openclaw-cli` trio
  is surfaced as the canonical recreate command, and all env-driven knobs
  are first-class in the docs.
- Patcher step count synced 11 → 13 across `README.md`, `CLAUDE.md`,
  `docker-compose.yml`, and `docs/ARCHITECTURE.md`.

## [0.4.0] - 2026-04-22

### Added
- **`CONTAINER_NAME_PREFIX` env var** — every service's `container_name:` is now
  `${CONTAINER_NAME_PREFIX:-dgx-}<service>`. Default keeps the existing
  `dgx-openclaw-gateway`, `dgx-vllm-llm`, … shape; set empty to drop the prefix
  for clean `openclaw-gateway`-style names. Bridge DNS reachability (the actual
  network plane) is unaffected — services resolve each other by compose service
  name + `hostname:` directive regardless of the container_name label.
- **`VLLM_HF_CACHE_VOLUME_NAME` env var** — Docker volume label for the shared
  HF cache is now env-driven (default `dgx-openclaw-hf-cache`). Lets sibling
  LLM stacks bind-mounting the same `VLLM_HF_CACHE_DIR` host path show up
  under one consistent label in `docker volume ls`.
- **TTS host bind/port env vars** (`TTS_EN_BIND`, `TTS_EN_PORT`,
  `TTS_F5HUN_BIND`, `TTS_F5HUN_PORT`, `TTS_ROUTER_BIND`, `TTS_ROUTER_PORT`).
  All three TTS services now publish their port on the host with a default of
  `127.0.0.1` — loopback only, ideal for `curl <port>/healthz` debugging
  without exposing the service on the LAN. Set `*_BIND=0.0.0.0` to expose any
  service to LAN clients (Bearer-token-protected via the existing TTS tokens).
  Sibling containers continue to use bridge DNS regardless of the binding.

### Changed
- **Patcher step 11 now writes the top-level `messages.tts.{enabled,auto,mode}`
  switches** (in addition to the existing `providers.openai` block and
  `voiceAliases`). Without these, the OpenClaw voice surfaces (Discord, agent
  `tts` skill) silently treat TTS as off even with the provider correctly
  wired. Web chat UI is unaffected (it's hard-wired to the browser's native
  `speechSynthesis` — known OpenClaw limitation, see CLAUDE.md).

## [0.3.0] - 2026-04-22

### Added
- **Bilingual TTS surface** wired into OpenClaw via the sanctioned
  `messages.tts.providers.openai.baseUrl` override (closed upstream issues
  #13907 / #29224). Three new services:
  - `openclaw-tts-en` — Kokoro 82M (Apache 2.0, TTS Arena #1 open-weights),
    OpenAI-compatible `/v1/audio/speech` on the bridge network. ~500 MB – 1 GB
    VRAM, coexists with the Gemma 4 NVFP4 LLM on the same GB10 GPU. Ten A/A-/B-
    grade Kokoro voices baked into the image at build time (`af_heart` default,
    plus `af_bella`, `af_nicole`, `af_aoede`, `af_kore`, `af_sarah`,
    `am_michael`, `am_fenrir`, `am_puck`, `bf_emma`).
  - `openclaw-tts-router` — ~150 LOC FastAPI router that fronts the EN backend
    (mandatory) and the optional Hungarian backend. ffmpeg bundled for
    wav→mp3/opus/aac transcoding. Maps the OpenAI default voice catalog
    (`alloy`, `coral`, `shimmer`, …) to closest Kokoro voices so the gateway
    never gets a 404.
  - `openclaw-tts-f5hun` — **OPT-IN** Hungarian TTS backed by
    `sarpba/F5-TTS_V1_hun_v2`. Wrapper code is MIT; model weights are
    CC-BY-NC-4.0 (non-commercial only). Gated three ways: `profiles: ["hu"]`
    on the service block (does not start without `--profile hu` or
    `COMPOSE_PROFILES=hu`), router needs `F5HUN_API_TOKEN` + `F5HUN_URL` set,
    and `bootstrap.sh` prompts once on first run. Default reference voice
    (Diana Majlinger / "Egri csillagok", LibriVox public domain) baked in;
    drop custom `<name>.wav` + `<name>.txt` into the `tts-f5hun-voices`
    Docker volume to add cloned voices.
- **Hungarian autodetect.** When the HU backend is active and the gateway
  sends one of the OpenAI default voices on input that contains Hungarian
  diacritics (`áéíóöőúüű`), the router silently re-routes to the HU backend
  so phonetics aren't mangled by the EN G2P. No-op when the HU profile isn't
  active.
- **Patcher step 11** — env-gated TTS provider wiring. When
  `OPENCLAW_TTS_ROUTER_API_KEY` is set, writes `messages.tts.providers.openai`
  (baseUrl, apiKey, model, voiceId) and `voiceAliases` (`english`, `narrator`,
  `male`, `female`, `magyar`, `hungarian`). Unset → step skips cleanly so users
  can opt out of TTS by simply leaving the var empty (and parking the two TTS
  services with `profiles: ["never"]`).
- **`bootstrap.sh`** — generates `OPENCLAW_TTS_ROUTER_API_KEY` and
  `TTS_API_TOKEN` alongside the existing secrets, regex-gated so re-runs never
  overwrite real values. Also asks once whether to opt into Hungarian TTS;
  on yes, generates `F5HUN_API_TOKEN`, sets `F5HUN_URL` to the in-compose
  service hostname, and adds `COMPOSE_PROFILES=hu` to `.env` so the next
  `docker compose up -d` brings up the HU service automatically.

## [0.2.0] - 2026-04-22

### Added
- **Remote vLLM / cloud-LLM backend support.** Three new env overrides
  (`OPENAI_BASE_URL`, `LLM_BASE_URL`, `EMBED_BASE_URL`) wire the gateway and
  the patcher to off-host LLM endpoints. Park the local `vllm-llm` /
  `vllm-embedding` services with `profiles: ["never"]` to skip them. Verified
  end-to-end on a GPU-less host pointing at a remote vLLM over LAN.
  Walkthrough: [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md) → "Run with a
  remote vLLM backend".
- **`CLAUDE.md`**: contributor / coding-agent guide. Covers the patcher
  contract, the two-phase fresh-install flow, 14 implementation gotchas
  (network namespace sharing, bridge DNS, `profiles: ["never"]`, SearxNG
  `keep_only` quirk, baseUrl trailing-slash asymmetry, …) and copy-paste
  verification recipes.
- **SearxNG meta-search** (privacy-respecting, self-hosted) wired into
  OpenClaw's native `webSearch` provider with a strict engine whitelist
  (DuckDuckGo, Brave, Mojeek, Qwant, Startpage, Wikipedia family, Reddit,
  GitHub, arXiv) — queries never reach Google / Bing / Yandex / Yahoo / Baidu.
- **Hybrid (BM25 + vector) `memorySearch`** with MMR re-rank.
- **Gemma 4 31B Superchip emphasis** in the README — keyword discovery,
  hardware-targets table, NVFP4 / 128 GB unified memory tuning rationale.
- **Funding** (`.github/FUNDING.yml`).

### Changed
- **`patch-config.mjs` no longer writes
  `plugins.entries.searxng.config.webSearch.categories`.** The gateway
  forwarded the static string as a Python-list literal in the SearxNG POST
  form, which SearxNG rejected with a validation warning (search still worked
  via fallback defaults — log noise only). Per-query categories come from the
  agent's tool call instead.

## [0.1.0] - 2026-04-21

### Added
- Initial public release.
- One-file `docker-compose.yml` for OpenClaw + vLLM (Gemma 4 31B NVFP4) +
  bge-m3 multilingual embeddings, calibrated for the NVIDIA GB10 Superchip
  (DGX Spark / ASUS Ascent GB10).
- Idempotent `patch-config.mjs` — 10-step deep-merge patcher that survives
  re-runs of the OpenClaw onboarding wizard.
- `bootstrap.sh` non-destructive first-time setup (regex-gated secret
  rotation, host-path prompts, prerequisite checks).
- `templates/tool_chat_template_gemma4.jinja` so vLLM emits proper
  `tool_calls` JSON instead of raw `call:tool{...}` content.
- Documentation: `README.md`, `SETUP.md`, `docs/ARCHITECTURE.md`,
  `docs/CUSTOMIZATION.md`, `docs/TROUBLESHOOTING.md`.

[Unreleased]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.7.3...v0.8.0
[0.7.3]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.7.2...v0.7.3
[0.7.2]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.4.3...v0.5.0
[0.4.3]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/chestercs/dgx-openclaw-stack/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/chestercs/dgx-openclaw-stack/releases/tag/v0.1.0
