# Troubleshooting

Common failure modes and their fixes, ordered roughly by how often they bite first-time users. If something here doesn't match your symptom, the most productive next step is:

```bash
docker compose logs --tail=200 <service>
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
docker exec dgx-vllm-llm ls /templates/
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
docker exec dgx-openclaw-gateway cat /home/node/.openclaw/openclaw.json \
  | grep -A1 '"vllm"' | head -20
```

The `apiKey` field should match your `VLLM_API_KEY` in `.env` (same length, 88 chars for a base64-64 secret).

### `Config invalid: must NOT have additional properties` on boot

You have `OPENCLAW_ENABLE_DREAMING=1` in `.env` but your OpenClaw image is older than `2026.4.15`. Either:

- Pull a newer image: `docker compose pull openclaw-gateway && docker compose up -d`.
- Disable dreaming: `OPENCLAW_ENABLE_DREAMING=0` in `.env`, then `docker compose up -d` (the patcher will clean up the invalid `memory-core` entry).

### `gateway.trustedProxies is empty` security warning

Impossible with the shipped `patch-config.mjs` — step 7 populates it. If you still see this, something is preventing the patcher from writing. Check:

```bash
docker logs dgx-openclaw-config-init
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

You're probably running `docker exec dgx-openclaw-cli openclaw` but the container isn't up. Check `docker compose ps openclaw-cli`. It should be `Up` (no healthcheck, just running `sleep infinity`).

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
docker exec dgx-openclaw-cli cat /home/node/.openclaw/openclaw.json \
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
docker exec dgx-openclaw-tts-router curl -sS http://openclaw-tts-f5hun:8080/healthz
```

### Hungarian text comes out with English phonetics

The diacritic autodetect didn't fire — either the HU backend isn't wired
(check `/healthz` on the router; `f5hun_enabled` should be `true`), or the
input genuinely has no `áéíóöőúüűÁÉÍÓÖŐÚÜŰ` characters and the autodetect
correctly fell through to the EN backend. To force HU explicitly, ask the
agent to use voice id `default_hu` or `hu_diana`.

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

The HF cache at `$VLLM_HF_CACHE_DIR` grows by ~16 GB per model version. If you bump image tags, you may want to `docker volume rm dgx-openclaw-hf-cache` (or just `rm -rf` that host path when the stack is down) to re-download from scratch.

### Gateway service shows `(unhealthy)` forever but logs look fine

The healthcheck uses `node -e "fetch('http://127.0.0.1:18789/healthz')"`. On very slow first boots, the internal port binding takes longer than the healthcheck timeout. Raise `healthcheck.start_period` from 20s to 60s as a first troubleshooting step.
