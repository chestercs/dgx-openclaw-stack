# Setup — DGX OpenClaw Stack

This guide walks you from a fresh DGX Spark / ASUS GB10 box to a running agent, step by step. Expect 20–30 minutes end-to-end (most of it is the first model download).

If anything goes wrong, see [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

---

## 0. Host prerequisites

On the GB10 host you need:

- **Ubuntu 24.04** (shipped with both DGX Spark and ASUS GB10) or similar ARM64 Linux.
- **Docker Engine 24.0+** with the Compose v2 plugin.
- **NVIDIA Container Toolkit** configured as a Docker runtime.
- ~25 GB free disk space for model weights + container images.

Verify:

```bash
docker --version                               # 24.0+
docker compose version                         # v2.20+
docker info | grep -i 'runtimes.*nvidia'       # should show nvidia
nvidia-smi                                     # should list the GB10 GPU
```

If `docker info` doesn't show the nvidia runtime:

```bash
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

## 1. HuggingFace account + model access

The Gemma 4 NVFP4 weights are gated. You need to:

1. Create (or log into) your HuggingFace account.
2. Accept the Gemma 4 license at: https://huggingface.co/nvidia/Gemma-4-31B-IT-NVFP4
3. Create a read-scope access token at: https://huggingface.co/settings/tokens

Keep that `hf_...` token handy — you'll paste it into `.env` in step 3.

The bge-m3 embedding model is **not** gated; no extra steps needed.

## 2. Clone the repo

```bash
git clone https://github.com/chestercs/dgx-openclaw-stack.git
cd dgx-openclaw-stack
```

## 3. Run the bootstrap script

```bash
./bootstrap.sh
```

The script is **non-destructive**: it asks, it never overwrites, it can be re-run safely. It will:

1. Check prerequisites (`docker`, `docker compose`, `nvidia` runtime).
2. Create `.env` from `.env.example` if one doesn't exist yet.
3. Generate strong random secrets for `VLLM_API_KEY`, `OPENCLAW_GATEWAY_TOKEN`, and `SEARXNG_SECRET` — only if they still hold the shipped placeholder values.
4. Prompt for your `HUGGING_FACE_HUB_TOKEN`.
5. Prompt for three host paths (defaults: `/opt/dgx-openclaw/{hf-cache,openclaw-config,workspace}`) and create them if they don't exist.

At the end it prints your next command.

### What if I want to edit `.env` manually?

You can. Skip the bootstrap and do:

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Every variable is commented in-line in `.env.example`.

## 4. Review the tunables

Before the first `docker compose up`, open `.env` and check:

- `LLM_GPU_MEM_UTIL`: defaults to `0.68`. Keep as-is if you want the embedding stack alongside. Raise to `0.85` if you remove the embedding service.
- `OPENCLAW_HEARTBEAT_TZ`: default `UTC`. Set to your local zone (e.g. `America/Los_Angeles`) so the agent's active-hours window and dreaming schedule match your daily rhythm.
- `OPENCLAW_LAN_CIDR`: if you plan to hit the gateway directly from your LAN (not through a reverse proxy), fill this in (e.g. `192.168.1.0/24`).
- `OPENCLAW_ENABLE_DREAMING`: leave at `0` unless your OpenClaw image is `2026.4.15` or newer.

## 5. Start the stack (phase 1)

```bash
docker compose up -d
docker compose logs -f
```

Expected timeline on a cold start (no cached weights yet):

- `openclaw-config-init` — runs in ~1–2 seconds. On a fresh install it logs
  *"openclaw.json not found — skipping (run onboarding first)"* and exits `0`.
  This is expected.
- `searxng` — ~5–10 seconds to boot on first image pull, then instant.
- `vllm-embedding` — ~1–2 minutes after pulling image, first boot only (bge-m3 is tiny).
- `vllm-llm` — 5–15 minutes the first time (safetensors download + NVFP4 kernel JIT). Subsequent boots: ~3–4 minutes.
- `openclaw-gateway` — **will crash-loop with** `Missing config. Run openclaw setup …`
  on a fresh install. This is expected — the gateway requires an `openclaw.json`,
  which step 6 below creates via onboarding. After onboarding, restart
  `openclaw-config-init` + `openclaw-gateway` + `openclaw-cli` together (phase 2).
- `openclaw-cli` — always-up utility container, ready immediately.

Service status:

```bash
docker compose ps
# vllm-* should be `Up (healthy)`; openclaw-gateway is `Restarting`
# until you complete onboarding.
```

> **Why the crash-loop is intentional.** The OpenClaw security model requires
> explicit onboarding (you choose the gateway token and pair the UI) before
> the gateway will accept connections. The patcher honors this: it skips
> cleanly when `openclaw.json` doesn't exist yet, then writes the wired-up
> configuration once onboarding has created the file.

## 6. Onboarding — pair the Chrome extension (UI) (phase 2a)

Install the official OpenClaw Chrome extension. After install:

1. Click the extension icon → **Add gateway**.
2. Paste the gateway URL: `ws://<your-host-ip>:18789` (use `wss://your-domain` if you've already put a reverse proxy in front).
3. Paste your `OPENCLAW_GATEWAY_TOKEN` (from `.env`) as the token.
4. The extension's onboarding wizard launches. Pick:
   - **Provider**: vLLM (OpenAI-compatible).
   - **Model**: `nvidia/Gemma-4-31B-IT-NVFP4` (the patcher will register this anyway in phase 2b; if the wizard offers a different default, pick whatever and the patcher will fix it).
   - **Tool calling**: enabled.
   - **Memory search**: enabled, `BAAI/bge-m3`.

Onboarding writes `openclaw.json` to your `OPENCLAW_CONFIG_DIR` and the
gateway stays alive. The wizard's wiring choices may not be production-ready
on their own — the next step ensures they are.

Headless alternative (no Chrome extension): inside the gateway container,
`openclaw onboard --non-interactive --token "$OPENCLAW_GATEWAY_TOKEN"` writes
the same `openclaw.json` non-interactively.

## 6b. Re-run the patcher (phase 2b)

```bash
docker compose up -d --force-recreate \
  openclaw-config-init openclaw-gateway openclaw-cli
```

The `openclaw-config-init` container now finds `openclaw.json` and applies all
11 patcher steps (vllm provider wiring, hybrid memory + MMR, SearxNG
enablement, dreaming, trustedProxies, TTS provider — see `patch-config.mjs`).
You'll see `[patch-config]` lines for each change. The gateway restarts and
picks up the patched config.

> **Why all three together.** `openclaw-cli` shares the gateway's network
> namespace (`network_mode: "service:openclaw-gateway"`). If you recreate the
> gateway alone, the still-running CLI ends up pointing at a dead namespace
> and silently loses connectivity. Always recreate the trio together.

## 7. First conversation & sanity checks

The shell snippets below use `${PROJ}` to refer to the container name prefix —
default `dgx-` (set via `CONTAINER_NAME_PREFIX` in `.env`). Source it once:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)
PROJ=${PROJ:-dgx-}
```

Three quick checks via the Chrome extension:

**Tool calling works:**
- Ask the agent: "What's the current time in UTC?"
- It should call the built-in `get_time` tool and reply with a timestamp, not say "I don't have access".

**Image input works:**
- Drag an image into the chat. The agent should describe it.
- Vision prefill adds ~1–2s and ~280 tokens per image by default.

**Memory search works (after you save something):**
- Tell the agent a fact (e.g. "Remember that my favorite color is ultramarine").
- Close the chat, open a new one, ask the agent to recall your favorite color. It should retrieve from memory.

Spot-check memory from the CLI:

```bash
docker exec ${PROJ}openclaw-cli openclaw memory status --deep
# Should print Embeddings ready, Vector ready (dims 1024).
```

**Web search works:**
- Ask the agent something that only a live search can answer (e.g. "Who's the current prime minister of <country>? Use web_search.").
- The agent should call the `web_search` tool, hit the local SearxNG instance, and reply with a URL-backed answer. If you see a generic "I'm not sure" without a tool call, pair the question with an explicit "call the web_search tool" instruction — small models sometimes skip tools on conversational prompts.
- Spot-check from the host:
  ```bash
  docker exec ${PROJ}openclaw-cli curl -s 'http://searxng:8080/search?q=test&format=json' | head
  ```
  Should return a JSON blob with a `results` array.

## 8. (Optional) Reverse proxy + TLS

For remote access over `wss://`, put any reverse proxy in front of port `18789`. A common setup:

- **Nginx Proxy Manager** container on host networking (easiest).
- **Caddy** with automatic Let's Encrypt.
- **Cloudflared tunnel** for no-open-ports public access.

If the proxy terminates TLS and talks plain `ws://` to the gateway on the private network, keep `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1` in `.env`.

If the proxy sits on the **host network** and sends `X-Forwarded-For`, you're covered by the default `gateway.trustedProxies` (the `172.16.0.0/12` docker-bridge range already includes the gateway's bridge IP).

If you hit the gateway **directly from your LAN**, add your LAN CIDR via `OPENCLAW_LAN_CIDR=192.168.1.0/24` (or whatever) and re-run `docker compose up -d`. The patcher will update `trustedProxies`.

## 9. (Optional) Daily operations

- **Stop everything**: `docker compose down` (keeps volumes + config).
- **Restart a single non-gateway service**: `docker compose up -d --force-recreate vllm-llm`.
- **Restart the gateway** — always recreate the trio so `openclaw-cli` doesn't end up in a dead namespace:
  ```bash
  docker compose up -d --force-recreate openclaw-config-init openclaw-gateway openclaw-cli
  ```
- **Pull newer images**: `docker compose pull && docker compose up -d`.
- **View gateway logs**: `docker compose logs -f openclaw-gateway`.
- **Enter the CLI container**: `docker exec -it ${PROJ}openclaw-cli openclaw`.
- **Back up memory** (do this regularly!): `tar czf openclaw-$(date +%F).tar.gz -C $OPENCLAW_CONFIG_DIR .`.

## 10. Uninstall

```bash
docker compose down -v                           # containers + non-bind volumes
rm -rf /opt/dgx-openclaw/                        # host data (IRREVERSIBLE)
docker image prune                               # optional: reclaim image space
```

Note: the HF cache volume is a host bind-mount (default `/opt/dgx-openclaw/hf-cache`)
and survives `down -v`. Removing the host directory above wipes the cached
~16 GB of model weights — a fresh `up` will re-download them.

Your `.env` is kept. Delete it only when you're sure you're done.

---

Still stuck? → [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md). Want to swap models or add your own agents? → [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md).
