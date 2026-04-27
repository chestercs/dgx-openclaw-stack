# Troubleshooting

Common failure modes and their fixes, grouped by the service you're most likely inspecting when you hit them. The two most frequent first-boot issues — the gateway crash-loop before onboarding and the vllm-llm weights download — live at the top of their respective sections.

For a structured map of which media features render on which surfaces (web chat / Discord text / agent skill API / control UI), see `docs/reference/chat-surface-capability-matrix.md`. For pre-flight verification before merging a new media-bridge, see `docs/reference/media-bridge-checklist.md`.

## Media surfaces — first-glance fixes

These are the symptoms operators hit first when integrating image-gen, TTS, or any new media feature. Each links to the deeper reference for context.

### "I generated an image but it doesn't show up in the web chat"

The OpenClaw chat web UI's markdown sanitizer drops both `![alt](url)` image syntax and `[text](url)` external-origin links. Only `mailto:` links and plain auto-linkified URLs survive (clickable, opens new tab). This is a verified upstream limitation through `2026.4.25` — see `docs/reference/image-comfyui-bridge.md` "Future paths" for the three candidate fix paths.

**Quick workaround:** the agent's reply contains a `display_markdown` URL (something like `https://vision.<your-host>/view?token=...`); copy it from the tool-output JSON bubble and open in a new tab. Direct navigation sends Basic auth correctly.

**On Discord text channels:** images attached as files via the bot API render inline natively. The chat UI is the only surface with this gap.

### "Discord bot acts like it's typing but never sends text"

Almost always the Discord text-channel TTS-attachment path crashing on the missing ffmpeg in the gateway image. Check:

```bash
docker logs ${PROJ}openclaw-gateway 2>&1 | grep -E "final reply failed.*ffmpeg" | tail -3
```

If you see `ffmpeg not found in trusted system directories`, set `OPENCLAW_TTS_AUTO=tagged` in `.env` and `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli`. Patcher step 11 honors the override; the agent then only TTS-tags replies when the LLM explicitly marks them, leaving normal text flow uncluttered. See `docs/reference/tts-stack.md` for the full enum.

### "Web chat 'Read aloud' button speaks bad Hungarian"

The chat bundle is hard-wired to the browser's `speechSynthesis` API. The OS default Hungarian voice is poor on most platforms. Mitigation: a Tampermonkey userscript that monkey-patches `speechSynthesis.speak()` to fetch from the openclaw-tts-router instead (HU autoroute via diacritic detection, plays via `new Audio(blob)`). See `docs/reference/tts-stack.md` "Web chat workaround" and `templates/userscripts/openclaw-chat-hu-tts.user.js`.

If something here doesn't match your symptom, the most productive next step is:

```bash
docker compose logs --tail=200 <service>
```

Shell snippets use `${PROJ}` for the container-name prefix (default `dgx-`, set via `CONTAINER_NAME_PREFIX` in `.env`). Source it once:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)
PROJ=${PROJ:-dgx-}
```

---

## vllm-llm

### Stays in `starting` for 10+ minutes on first boot

Normal if the weights aren't cached yet (~16 GB download + NVFP4 kernel JIT). Follow the progress:

```bash
docker compose logs -f vllm-llm
```

You should see `Downloading shards:` lines, then `Loading safetensors checkpoint shards:`, then `INFO: Started server process`. The healthcheck `start_period` is 180s; on slower networks, the first boot can exceed that and the container will be marked unhealthy temporarily. Wait it out; `restart: unless-stopped` will keep it alive and healthy status flips once the API is up.

### `HfHubHTTPError: 401 Client Error: Unauthorized`

Your `HUGGING_FACE_HUB_TOKEN` is missing, wrong, or your account hasn't accepted the Gemma 4 license. Fix:

1. Accept the license: https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4
2. Create a **read-scope** token at https://huggingface.co/settings/tokens
3. Put it in `.env` as `HUGGING_FACE_HUB_TOKEN=hf_xxxxx`
4. `docker compose up -d --force-recreate vllm-llm`

### `torch.OutOfMemoryError: CUDA out of memory`

Both vLLM services are starting in parallel and racing for the unified memory. Check:

- `LLM_GPU_MEM_UTIL=0.68` (with embedding) or `0.85` (without embedding) in `.env`.
- `EMBED_GPU_MEM_UTIL=0.03` in `.env`.

If the problem persists, something else on the host is holding GPU memory. `nvidia-smi` will show which process.

### `ValueError: The quantization method modelopt is not supported for the current GPU`

You're not on a GB10 / Blackwell (`sm_120` / `sm_121`). The NVFP4 kernels require Blackwell. Either move to supported hardware, or swap to a non-NVFP4 model — see `CUSTOMIZATION.md`.

### Generates raw `call:name{"args":...}` text instead of tool calls

The Gemma 4 chat template isn't being picked up. Check:

```bash
docker exec ${PROJ}vllm-llm ls /templates/
# Should show: tool_chat_template_gemma4.jinja
```

If the file is missing, something went wrong with the volume mount. Ensure you have `templates/tool_chat_template_gemma4.jinja` in the repo, then `docker compose up -d --force-recreate vllm-llm`.

## vllm-embedding

### `docker compose up` fails with OOM before vllm-llm finishes loading

The LLM has grabbed more memory than `LLM_GPU_MEM_UTIL` suggests (vLLM util is fraction-of-free, and it starts allocating before the embedding service does). Options:

- Lower `LLM_GPU_MEM_UTIL` by `0.02–0.05`.
- Start the embedding service first: `docker compose up -d vllm-embedding`, wait for it to become healthy, then `docker compose up -d`.

### `docker compose exec vllm-embedding curl localhost:8005/v1/embeddings -d '{...}'` returns 401

`VLLM_API_KEY` isn't set, or you forgot the `-H "Authorization: Bearer $VLLM_API_KEY"` header. The embedding service requires the same key as the LLM.

### Embedder crashed mid-index — memory partially vectorized, `Dirty` flag still reports clean

Under unified-memory contention on GB10, the embedder can transiently die with `torch.AcceleratorError: CUDA error: operation not permitted` (`cudaErrorNotPermitted`). Docker's `restart: unless-stopped` brings it back in ~10 s and the service resumes serving — but any `/v1/embeddings` calls in flight at the crash instant return 500, and the chunks OpenClaw was indexing at that moment get silently skipped.

OpenClaw's indexer does not re-dirty files whose embed step failed. `openclaw memory status` then reports `Dirty: no` even though the vector store is incomplete. Real-world example: `Indexed: 2/10 files · 14 chunks` with `Dirty: no` against a workspace that should have 10 files fully indexed.

**Detect:**

```bash
docker exec ${PROJ}openclaw-cli openclaw memory status \
  | grep -E "^(Indexed|Dirty|By source)"
docker inspect ${PROJ}vllm-embedding --format "RestartCount: {{.RestartCount}}"
```

If the indexed file count is below the actual file count in `~/.openclaw/workspace/memory/` and `RestartCount` is non-zero, you're in this state.

**Fix — force full reindex:**

```bash
docker exec ${PROJ}openclaw-cli openclaw memory index --force
```

Ignores the `Dirty` flag, re-embeds every chunk. Expect the post-fix status to show full coverage (e.g. `Indexed: 10/10 files · 34 chunks`).

**Preventive habit:** after any `vllm-embedding` restart, run `memory index --force` once. This is a workaround for an OpenClaw-side gap — the repo's restart policy catches the GPU-level incident, but OpenClaw's indexer state tracking won't auto-repair.

Tracked upstream: [openclaw/openclaw#70567](https://github.com/openclaw/openclaw/issues/70567). When upstream fixes the indexer to re-dirty files whose embed step returned 5xx, this workaround can be retired.

## openclaw-gateway

### `Missing config. Run openclaw setup …` (gateway crash-loops on a fresh install)

Expected on the very first `docker compose up -d` of a fresh install. The OpenClaw security model requires explicit onboarding (you choose the gateway token and pair the UI) before the gateway accepts connections, and the patcher honors this — it skips cleanly when `openclaw.json` doesn't exist yet.

Two-phase fix:

1. **Onboard.** Either pair the Chrome extension (which runs the wizard interactively), or use the headless CLI:
   ```bash
   docker exec ${PROJ}openclaw-cli openclaw onboard \
     --non-interactive --token "$OPENCLAW_GATEWAY_TOKEN"
   ```
   This writes `openclaw.json` to your `OPENCLAW_CONFIG_DIR`.
2. **Re-apply the patcher** so the 11 deterministic-state steps run on the new file:
   ```bash
   docker compose up -d --force-recreate \
     openclaw-config-init openclaw-gateway openclaw-cli
   ```

The trio of services must be recreated together — see "openclaw-cli loses connectivity after a gateway recreate" below.

### `Profile vllm:default timed out` on first connection from the extension

The gateway's provider config still has a placeholder API key. Either:

- Wait for `openclaw-config-init` to run (it runs before the gateway on every `up`); the next gateway start will have the correct key.
- Force it: `docker compose run --rm openclaw-config-init`, then `docker compose restart openclaw-gateway`.

Verify:

```bash
docker exec ${PROJ}openclaw-gateway cat /home/node/.openclaw/openclaw.json \
  | grep -A1 '"vllm"' | head -20
```

The `apiKey` field should be the exact same string as `VLLM_API_KEY` in your `.env`. If it looks like `CHANGE_ME_…` instead, the placeholder was never replaced — check that `bootstrap.sh` actually ran, or set the value by hand and re-run the patcher.

### `Config invalid: must NOT have additional properties` on boot

You have `OPENCLAW_ENABLE_DREAMING=1` in `.env` but your OpenClaw image is older than `2026.4.15`. Either:

- Pull a newer image: `docker compose pull openclaw-gateway && docker compose up -d`.
- Disable dreaming: `OPENCLAW_ENABLE_DREAMING=0` in `.env`, then `docker compose up -d` (the patcher will clean up the invalid `memory-core` entry).

### `gateway.trustedProxies is empty` security warning

Impossible with the shipped `patch-config.mjs` — step 7 populates it. If you still see this, something is preventing the patcher from writing. Check:

```bash
docker logs ${PROJ}openclaw-config-init
```

Common cause: wrong ownership on `$OPENCLAW_CONFIG_DIR`. The init container runs as UID 1000 and needs write access to that directory.

### Chrome extension can't connect (`ws://` fails)

- Make sure the port is actually reachable: `curl http://<host>:18789/healthz` should return 200.
- If you're using a reverse proxy and see `403`: check that the proxy is forwarding the `Upgrade: websocket` and `Connection: upgrade` headers.
- If the extension complains about TLS on `ws://` but not `wss://`: modern browsers only allow `ws://` on localhost. Use `wss://` via a reverse proxy for remote access, or run the extension's origin as localhost.

### `openclaw memory status --deep` reports vector index 0 chunks

Nothing has been embedded yet. Start a chat, write a memory, wait ~1s. `--deep` will then show the chunk count matching your recent memory writes.

## openclaw-cli

### `openclaw: command not found` in the cli container

You're probably running `docker exec ${PROJ}openclaw-cli openclaw` but the container isn't up. Check `docker compose ps openclaw-cli`. It should be `Up` (no healthcheck, just running `sleep infinity`).

### `ENETUNREACH` when calling `openclaw memory status --deep`

Impossible with this stack — the CLI shares the gateway's network namespace and resolves both vLLM services by compose DNS. If you're seeing this, you're probably on an older standalone compose file. Migrate to this repo's `docker-compose.yml`.

### Every CLI command takes ~5 s before output

That's Node.js module-loading cold start. It's not your infra — every invocation spins up a fresh Node process inside the container. Not fixable from this stack. For high-frequency scripted use, consider calling the gateway's HTTP API directly.

## TTS (openclaw-tts-router / -en / -f5hun)

### Discord/voice surfaces are silent even though the gateway accepted the message

The most common cause on a stack upgraded from < 0.4.0: patcher step 11 wired
`messages.tts.providers.openai` correctly, but the top-level
`messages.tts.{enabled,auto,mode}` switches were never written, so the gateway
silently treats TTS as off. Fix:

```bash
docker exec ${PROJ}openclaw-cli cat /home/node/.openclaw/openclaw.json \
  | jq '.messages.tts | {enabled, auto, mode, provider: .providers.openai.baseUrl}'
```

Expected output:

```json
{ "enabled": true, "auto": true, "mode": "openai", "provider": "http://openclaw-tts-router:8080/v1" }
```

If `enabled` / `auto` / `mode` are missing or false, re-run the patcher:

```bash
docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
```

### `OPENCLAW_TTS_ROUTER_API_KEY not set — skipping TTS provider config`

Patcher step 11 logs this and exits cleanly when the env var is missing. By
design — TTS is opt-in. If you wanted TTS, set the var in `.env` and re-run
the patcher trio (see above). If you didn't, ignore; the rest of the stack is
unaffected.

### `openclaw-tts-f5hun` doesn't start

The Hungarian backend is profile-gated. Plain `docker compose up -d` won't
start it. Either:

- One-shot: `docker compose --profile hu up -d`.
- Persistent: add `COMPOSE_PROFILES=hu` to `.env`, then plain
  `docker compose up -d` brings it up.

Once running, the router auto-detects it via `F5HUN_URL` + `F5HUN_API_TOKEN`
in the router's environment — no separate router restart needed.

### Router returns 502 on HU requests but EN works

`F5HUN_URL` or `F5HUN_API_TOKEN` is set on the router but the f5hun service
itself is unreachable (not started, wrong network, healthcheck failing).
Check:

```bash
docker compose ps openclaw-tts-f5hun       # should be Up (healthy)
docker exec ${PROJ}openclaw-tts-router curl -sS http://openclaw-tts-f5hun:8080/healthz
```

### TTS backend container crash-loops with `torch wheel was built without sm_NNN kernels`

Visible in `docker compose logs openclaw-tts-en` (or `openclaw-tts-f5hun`) as a
`RuntimeError` from `_verify_gpu_compat()` naming the missing compute
capability. Means the cu130 torch wheel baked into the image predates your
GPU's arch (e.g. sm_120 on GB10 before Blackwell-ready cu130 wheels shipped).
Rebuild with a fresh wheel pull:

```bash
# EN backend
docker compose build --no-cache openclaw-tts-en
# HU backend (requires the `hu` profile flag)
docker compose --profile hu build --no-cache openclaw-tts-f5hun
# Then recreate
docker compose --profile hu up -d --force-recreate openclaw-tts-en openclaw-tts-f5hun
```

Confirm the fix via the backend's healthz — the `gpu_compat` field now reports
`ok (sm_XXX; arch_list=[...])`:

```bash
curl -sS http://127.0.0.1:8091/healthz | jq '{device, gpu_compat}'
curl -sS http://127.0.0.1:8090/healthz | jq '{device, gpu_compat}'  # f5hun
```

If you can't rebuild right now, set `KOKORO_DEVICE=cpu` (EN) and
`F5HUN_DEVICE=cpu` (HU) in `.env`, then recreate — CPU is slower (EN ~5-6s
first request, HU ~30-100s per clip) but functional.

### `openclaw infer tts convert` returns success but `provider=microsoft` instead of `openai`

Symptom chain: router returned 500 to the OpenAI-compat provider, OpenClaw
fell through its provider chain, and Microsoft Edge TTS (built-in, cloud,
free-tier) answered. User-visible outcome: wrong accent / wrong voice.
Diagnose with `--json`:

```bash
docker exec ${PROJ}openclaw-cli openclaw infer tts convert \
  --text "probe" --voice af_heart --output /tmp/t.mp3 --json
```

Look at `attempts[]` — the `openai` entry will show `outcome: "failed"` with
a `reasonCode` and error string. Common reasons:

- `provider_error` with `500 Internal Server Error` → backend crashed. See
  preceding entry (rebuild) or `docker logs openclaw-tts-en`.
- `provider_error` with `401` → `OPENCLAW_TTS_ROUTER_API_KEY` in `.env`
  doesn't match `TTS_API_TOKEN` the router is enforcing.
- `timeout` → backend alive but slow (CPU fallback mid-synthesis).

The provider chain with a healthy local router should always land `openai` as
the successful attempt; seeing `microsoft` is a signal that something upstream
needs fixing — not an acceptable steady state.

### Hungarian text comes out with English phonetics

The diacritic autodetect didn't fire — either the HU backend isn't wired
(check `/healthz` on the router; `f5hun_enabled` should be `true`), or the
input genuinely has no `áéíóöőúüűÁÉÍÓÖŐÚÜŰ` characters and the autodetect
correctly fell through to the EN backend. To force HU explicitly, ask the
agent to use voice id `default_hu` or `hu_diana`.

## Browser automation (openclaw-browser)

### `session_expired` from a profile that worked yesterday

Upstream sites expire cookies on their own schedule. Approximate 2026
defaults:

| Service | Typical session lifetime |
|---|---|
| GitHub | 14 days inactive — but **2FA re-prompt at 28 days** from the last 2FA event |
| Notion (free / pro) | ~30 days |
| Notion Enterprise | up to 180 days (admin-configurable) |
| Google consumer | 14 days default; "Stay signed in" extends |
| MediaWiki | 30 days (default `$wgCookieExpiration`) |
| Discord | 7 days OAuth tokens |

GitHub's 28-day 2FA window is the surprise: a session can be active and
still re-prompt for 2FA. Re-onboard with:

```bash
./bootstrap-browser-login.sh github-user1
```

(idempotent — runs in place against the existing user-data-dir).

### `bootstrap-browser-login.sh` says "openclaw-browser not reachable"

The browser service is opt-in and not started by `docker compose up -d`
without `--profile browser`. Either add `browser` to `COMPOSE_PROFILES`
in `.env` or pass `--profile browser`:

```bash
docker compose --profile browser up -d --build openclaw-browser
docker compose --profile browser ps openclaw-browser
```

If it's running but unreachable, check `BROWSER_BIND` (default
`127.0.0.1`) and that you're hitting `http://127.0.0.1:9220/healthz`
from the same host.

### noVNC tab opens but the screen is black

Almost always one of three things:

1. **`shm_size` was reduced.** Chromium needs ≥ 1 GB on `/dev/shm` to render anything non-trivial. Restore the default:
   ```yaml
   shm_size: "1gb"
   ```
2. **Xvfb didn't start in time.** The `time.sleep(0.5)` in `LoginHelper.start()` covers cold starts. If your host is heavily loaded, raise it. Symptoms: x11vnc starts but reports "no display".
3. **Browser is paused waiting for user-data-dir to be writable.** Verify the bind mount:
   ```bash
   docker compose exec openclaw-browser ls -la /storage
   ```
   Should be writable by the container's UID (root in the Playwright base image).

### WebAuthn / passkey screen appears but nothing happens when I click

Expected. WebAuthn over noVNC does NOT work — the W3C spec is
origin-bound, and the platform authenticator on your laptop has no
path to the remote Chromium's origin. Workarounds:

1. Pick "Use password instead" on the auth screen if available.
2. Use a TOTP / SMS OTP option for that account.
3. For accounts that are passkey-only, switch to an API token /
   personal access token and route the agent through the service's
   REST API, bypassing the browser entirely.

USB hardware passkeys (YubiKey) plugged into your laptop are also
inaccessible — the container does not pass through your laptop's USB
bus.

### Chromium SIGKILL'd mid-render

Two common root causes:

1. **`shm_size` too small** — see above.
2. **Docker default seccomp blocks Chromium syscalls.** The compose
   block ships with a placeholder seccomp profile at
   `openclaw-browser/config/seccomp.json`. Pull Playwright's upstream
   profile in for production:
   ```bash
   curl -fsS \
     https://raw.githubusercontent.com/microsoft/playwright/main/utils/docker/seccomp_profile.json \
     > openclaw-browser/config/seccomp.json
   ```
   Then uncomment the `seccomp=` line in the compose service block and
   `docker compose up -d --force-recreate openclaw-browser`.

### Cloudflare Turnstile / DataDome blocks every navigation

Vanilla Playwright Chromium ships `navigator.webdriver=true`, default
WebGL fingerprint, default canvas — all detectable by modern bot
managers. The boundary is documented in
`docs/reference/browser-automation.md`. Two paths forward:

1. Use the site's official API instead of the browser. Most user-owned
   accounts have one (GitHub, GitLab, Notion, Linear).
2. Swap `playwright` for `patchright` in `requirements.txt` and rebuild
   — see `docs/CUSTOMIZATION.md` → "Swap to Patchright if you need
   stealth". Apache 2.0; binary-level patches address the most common
   detection vectors.

### "Profile XYZ exceeds the 20-port range"

You added more than 20 names to `BROWSER_PROFILE_NAMES`. Bump
`BROWSER_MAX_PROFILES` and the corresponding port range in
`docker-compose.yml`. See `docs/CUSTOMIZATION.md` → "Expand beyond 20
profiles".

### `/json/version` returns 401 with the right `?token=`

The token at the URL doesn't reach Chromium (Chromium ignores query
strings on its discovery endpoint), so 401 here is from the **management
API** at port 9220 — you're hitting the wrong port. CDP discovery is
on the per-profile port (default profile = 9222). Confirm:

```bash
curl -sS "http://127.0.0.1:9222/json/version" | jq
# This returns Chromium's debug info — no auth required at this layer.
```

The query-string token is best-effort; production-grade auth needs a
header-auth reverse proxy in front of the CDP ports (see
`docs/CUSTOMIZATION.md` → "Expose CDP on the LAN").

## Python sandbox (openclaw-python-sandbox)

### Agent ignores the tool / replies without calling python_exec

Most likely: you referred to the tool as `python_exec` in the prompt,
but OpenClaw exposes external MCP tools under
`<server_name>__<tool_name>`. The catalog name is
`python_sandbox__python_exec` — Gemma 4 NVFP4 silently fails to match
the bare name and emits an unrelated reply (verified 2026-04-26 on GB10).

Fix: use the prefixed name in your prompt:

```text
Call python_sandbox__python_exec with code="print(2**128)". Reply with VAL: <result>.
```

If the prefixed name still doesn't trigger a call, follow the
"tool not registered" diagnostic chain below.

### Agent says "I don't have a python_exec tool" / "no python_sandbox__python_exec"

The MCP server isn't wired into the gateway. Three failure modes,
diagnose in order:

1. **Service not running.** `docker compose ps openclaw-python-sandbox`
   should show it healthy. If it's missing, you didn't activate the
   profile — add `python` to `COMPOSE_PROFILES` in `.env` (or pass
   `--profile python`) and `docker compose up -d openclaw-python-sandbox`.
2. **Token unset.** `grep '^PYTHON_SANDBOX_API_TOKEN=' .env` returns an
   empty value or the placeholder. The patcher's step 18 skips when
   the token is empty. Either run `bootstrap.sh` and choose opt-in,
   or set the value by hand and re-run the patcher:
   `docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli`.
3. **Patcher hasn't run since opt-in.** `docker compose logs
   openclaw-config-init | grep python_sandbox` should show the
   `mcp.servers.python_sandbox.*` write lines from the last patcher
   run. If absent, force-recreate the init service to retrigger.

Confirm the wiring landed:
```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}
docker exec ${PROJ}openclaw-cli sh -c \
  'cat ~/.openclaw/openclaw.json | jq ".mcp.servers.python_sandbox"'
# Should print { "transport": "streamable-http", "url": "...", "headers": {...} }
```

### `python_exec` returns `TimeoutError: execution exceeded 30.0s`

The kernel was interrupted mid-run. Two common causes:

- **Genuinely long computation.** Bump `PYTHON_SANDBOX_KERNEL_TIMEOUT_S`
  in `.env`, recreate the service. The kernel is *interrupted* (not
  killed) on timeout, so any session state (loaded dataframes,
  imports) survives. The next call against the same `session_id`
  inherits that state.
- **Accidental `time.sleep` or blocking I/O.** If the agent wrote
  `time.sleep(...)`, the kernel was idle, not computing. Inspect with
  `docker exec ${PROJ}openclaw-python-sandbox curl -sS
  http://127.0.0.1:8094/healthz` — if `kernels=N` for `N>0`, kernels
  are alive. Reset the session via `python_session_reset` and
  re-prompt the agent without the sleep.

### Kernel OOM-killed mid-call (no error, just truncated output + fresh state)

The container hit `mem_limit` (default 8 GB) and the docker engine
SIGKILL'd the kernel. The next `python_exec` call against the same
`session_id` transparently starts a fresh kernel — but the prior
state (variables, loaded data) is gone.

Three mitigations:

1. **Bump the cap.** Edit `PYTHON_SANDBOX_MEMORY_MB` in `.env`,
   recreate the service. Watch GB10 vLLM headroom (`docker stats
   ${PROJ}vllm-llm`) — going over ~16 GB sandbox cap on a 128 GB box
   is fine; on smaller systems you may starve the LLM.
2. **Save big intermediate results to `/workspace`.** A 10 GB
   dataframe in RAM gets killed; the same dataframe written to
   `/workspace/data.parquet` and re-loaded as needed survives.
3. **Use `del` and `gc.collect()` after large operations.** ipykernel
   doesn't always free memory aggressively after a `del`; explicit
   `gc.collect()` plus `python_session_reset` for a clean slate is
   the reliable path.

## Image-gen bridge (openclaw-image-comfyui)

### Agent can't find `comfyui_image__generate` / says no such tool

Three failure modes — diagnose in order:

1. **Bridge not running.** The bridge lives in a SEPARATE compose file
   (`openclaw-image-comfyui/docker-compose.yml`). `docker compose up
   -d` from the repo root does NOT start it; you have to bring it up
   explicitly:
   ```bash
   docker compose -f openclaw-image-comfyui/docker-compose.yml \
                  --env-file .env --profile image-gen up -d --build
   ```
2. **Token unset.** `grep '^IMAGE_GEN_API_TOKEN=' .env` returns
   empty. Patcher step 19 skips (and removes any prior wiring) when
   the token is empty. Either run `bootstrap.sh` and choose opt-in
   3e, or use `./rotate-secrets.sh IMAGE_GEN_API_TOKEN`.
3. **Patcher hasn't run since opt-in.** `docker compose logs
   openclaw-config-init | grep comfyui_image` should show
   `mcp.servers.comfyui_image.*` write lines from the last patcher
   run. If absent, force-recreate the init service:
   ```bash
   docker compose up -d --force-recreate \
                  openclaw-config-init openclaw-gateway openclaw-cli
   ```

Confirm the wiring landed:
```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}
docker exec ${PROJ}openclaw-cli sh -c \
  'cat ~/.openclaw/openclaw.json | jq ".mcp.servers.comfyui_image"'
# Should print { "transport": "streamable-http", "url": "...", "headers": {...} }
```

Same tool-prefix gotcha as the Python sandbox: in your prompts use
the full `comfyui_image__generate` name, NOT bare `generate` —
Gemma 4 NVFP4 silently fails to match unprefixed names.

### Bridge healthy but generate returns "comfyui_error: connect to host.docker.internal:13036 failed"

The bridge can't reach your existing ComfyUI install. Test the hop
directly:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}
docker exec ${PROJ}openclaw-image-comfyui curl -fsS \
  http://host.docker.internal:13036/system_stats | head -c 200
```

Three common causes:

1. **ComfyUI isn't actually published on `13036`.** Confirm the
   port-publish in your ComfyUI compose: `docker port comfyui` should
   show `8188/tcp -> 0.0.0.0:13036` (or wherever you published it).
   Adjust `COMFYUI_URL` in `.env` to match.
2. **ComfyUI binds loopback inside its container but the publish is
   to `127.0.0.1` only on the host.** That's fine — the bridge uses
   `host.docker.internal:host-gateway` which routes to the host's
   docker bridge IP, and `127.0.0.1`-published ports are reachable
   from that interface on Linux. If you locked the publish to a
   specific external IP, switch the bridge's `COMFYUI_URL` to that IP.
3. **`host-gateway` not supported.** Requires Docker 20.10+. Older
   Docker: replace `host.docker.internal:host-gateway` in
   `openclaw-image-comfyui/docker-compose.yml` with the actual host
   bridge IP (`172.17.0.1` typically), or move to a shared external
   network.

### `comfyui_error: prompt rejected (HTTP 400): … missing model …`

The workflow JSON references a checkpoint name (`ckpt_name`) that
doesn't exist in your ComfyUI's `basedir/models/checkpoints/`. Two
causes:

1. **Workflow has the `REPLACE_ME.safetensors` placeholder** and you
   didn't pass `checkpoint=...`. The bridge will refuse cleanly with
   `_metadata.checkpoint_required` — re-prompt with an explicit
   `checkpoint` argument referring to a file that exists in your
   ComfyUI checkpoints directory.
2. **You passed a checkpoint that isn't installed.** List what's
   actually present:
   ```bash
   ls /media/usb/comfyui/basedir/models/checkpoints/
   ```
   (or wherever your ComfyUI's basedir lives — check the
   `COMFY_UI_APP_BASEDIR_PATH` in your ComfyUI compose's `.env`).

### Generate returns "comfyui_restarted: prompt … disappeared from /history"

Your ComfyUI process restarted (manually, OOM, supervisor) while the
bridge was polling for a result. The bridge surfaces a one-line error
rather than hanging. Check:

```bash
docker compose -f /path/to/your/comfyui/docker-compose.yml ps
docker compose -f /path/to/your/comfyui/docker-compose.yml logs --tail=50 comfyui
```

Re-run the generate call once the ComfyUI is healthy again.

### Everything works but LLM token generation pauses for 20-30s during image rendering

ComfyUI runs on the same GB10 GPU as vLLM. Concurrent generation
pre-empts LLM token gen. Two mitigations:

1. **Default already serializes the bridge.** `IMAGE_GEN_MAX_CONCURRENCY=1`
   means the bridge holds an `asyncio.Lock` so two parallel agent
   calls won't double-tap the GPU. If you've changed this to `0`,
   change it back.
2. **Model swap to a faster workflow.** FLUX Schnell at 4 steps
   finishes in ~3-8s; SDXL at 25 steps takes 20-40s. Use Schnell when
   the agent doesn't need maximum quality.

If the contention is unacceptable: move ComfyUI to a separate GPU/box
and point `COMFYUI_URL` at the new endpoint.

### NPM `[emerg] "auth_basic" directive is duplicate` after adding the /view custom location

You added `auth_basic off;` to the `/view` custom-location Advanced
to "make it token-only" — but NPM auto-emits
`auth_basic "Authorization required";` for every location when the
host has an Access List with Basic auth. Two `auth_basic` directives
in the same location → NGINX `[emerg]` → save fails with "internal
error". Drop the `auth_basic off;` line from the location Advanced.
The right knob is **`Satisfy Any` on the Access List Details tab** —
that lets the `auth_request` 200 result alone satisfy the request,
no Basic creds needed when the token is valid. (See
`openclaw-image-comfyui/README.md` → "Token-protected proxy".)

### `/view` returns 200 even WITHOUT a token (every request lets through)

Your Access List `Satisfy Any` is on, AND the Rules tab has an
`Allow all` IP rule. With `Satisfy Any` the IP-allow alone passes,
no auth needed → wide-open. **Drop the `Allow all` rule** from the
Rules tab. Just leave the auto-fallback `deny all`. Then `Satisfy
Any` falls through to Basic auth or auth_request — one of those
must pass, but the IP check no longer auto-satisfies the request.

### `/view + valid token` returns 401 even though `/auth-validate?token=...` works directly

The NGINX `auth_request /auth-validate;` directive sends a
sub-request with a STATIC URI — the parent request's `?token=...`
does NOT propagate to the sub-request's `$args`. The bridge's
`/auth-validate` sees an empty token query param and returns 401.

Fixed in v0.9.10: the bridge now also reads the token from the
`X-Original-URI` header that the proxy sets to `$request_uri` (NPM's
default `proxy_set_header X-Original-URI $request_uri;` on the
custom auth-validate location). If you're on an older bridge build,
either upgrade or add to the proxy `/auth-validate` Advanced:

```nginx
proxy_pass http://<bridge-host>:9095/auth-validate?token=$arg_token;
```

But note the duplicate-`proxy_pass` trap — NPM auto-emits its own
`proxy_pass` from the Forward Hostname/Port form fields, so an
explicit `proxy_pass` in the Advanced is `[emerg]`. The
`X-Original-URI` fallback in v0.9.10 sidesteps this entirely.

## Agent runs (multi-step tool calls)

### Agent run times out with `Request was aborted` even though vLLM is healthy

Symptom: `openclaw agent --message "..."` returns
`livenessState: blocked` or `Request timed out before a response was
generated.` The gateway log shows
`embedded run timeout: timeoutMs=NNNN` and
`rawErrorPreview: "Request was aborted." failoverReason: "timeout"`.
vLLM logs show successful 200 OK chat-completion responses with normal
generation throughput (~6 tok/s on GB10).

This is **not a tool-call parser bug** — the agent runtime simply
hit the `--timeout` budget mid-run. Multi-step tool-call agents do
several LLM calls per run (system-prompt prefill → tool-call args
→ tool-result digestion → final reply), each producing 100-300
tokens. On GB10 at ~6 tok/s that's ~30s per call; a 3-call
tool-using run easily wants 90-120s. Add a 5-MCP-tool catalog and
the system-prompt prefill for the first call alone burns 3-5s.

Fixes, in order of preference:

1. **Pass a generous `--timeout` to the agent invocation.** For any
   tool-using run with the current MCP catalog (Python sandbox +
   ComfyUI bridge + node tools + memory plugin):
   ```bash
   docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
     --message "..." --thinking off --json --timeout 600
   ```
   `300` is the floor for single-tool runs; `600` is safe for
   multi-step (e.g. `comfyui_image__list_workflows` then
   `comfyui_image__generate` in the same run).
2. **Use `--thinking off` for routine tool calls.** `medium` or
   `high` reasoning multiplies the token budget by 2-3×. Reserve
   reasoning for actually hard prompts.
3. **Trim the tool catalog if you have no use for some.** Drop
   `python` from `COMPOSE_PROFILES` (or empty
   `PYTHON_SANDBOX_API_TOKEN`) if you don't run code; same for
   `image-gen` / `IMAGE_GEN_API_TOKEN`. Each opt-out shaves
   1-2 KB of system prompt and one less round of tool advertising.

The vLLM idle watchdog (`agents.defaults.llm.idleTimeoutSeconds`,
patcher step 8 sets 300) is independent of `--timeout` — it
guards against a stuck LLM connection, not a slow-but-progressing
multi-step run.

## Host-level issues

### `nvidia-smi` inside a container fails with `Failed to initialize NVML`

The nvidia-container-toolkit isn't installed or Docker isn't restarted after configuring it:

```bash
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Disk fills up after a week

Likely `docker logs` on a chatty service. We already set `max-size: 150m` on both vLLM services, but if you've added stuff:

```bash
docker system df           # shows volumes / images / build cache usage
docker image prune -a      # reclaim old images (CAREFUL: removes untagged)
```

The HF cache at `$VLLM_HF_CACHE_DIR` grows by ~16 GB per model version. If you bump image tags, you may want to remove the named cache volume (default `dgx-openclaw-hf-cache`; see `VLLM_HF_CACHE_VOLUME_NAME` in `.env`) via `docker volume rm`, or just `rm -rf` the host path when the stack is down — either forces a fresh download.

### Gateway service shows `(unhealthy)` forever but logs look fine

The healthcheck uses `node -e "fetch('http://127.0.0.1:18789/healthz')"`. On very slow first boots, the internal port binding takes longer than the healthcheck timeout. Raise `healthcheck.start_period` from 20s to 60s as a first troubleshooting step.
