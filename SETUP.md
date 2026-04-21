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
3. Generate strong random secrets for `VLLM_API_KEY` and `OPENCLAW_GATEWAY_TOKEN` — only if they still hold the shipped placeholder values.
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

## 5. Start the stack

```bash
docker compose up -d
docker compose logs -f
```

Expected timeline on a cold start (no cached weights yet):

- `openclaw-config-init` — runs in ~1–2 seconds and exits `0`.
- `vllm-embedding` — ~1–2 minutes after pulling image, first boot only (bge-m3 is tiny).
- `vllm-llm` — 5–15 minutes the first time (safetensors download + NVFP4 kernel JIT). Subsequent boots: ~3–4 minutes.
- `openclaw-gateway` — waits for both vllm services to be healthy, then boots in ~20–30 seconds.
- `openclaw-cli` — always-up utility container, ready immediately.

Once all are healthy:

```bash
docker compose ps
# Should show all services `Up (healthy)` (openclaw-cli just `Up`).
```

## 6. Pair the Chrome extension (UI)

Install the official OpenClaw Chrome extension. After install:

1. Click the extension icon → **Add gateway**.
2. Paste the gateway URL: `ws://<your-host-ip>:18789` (use `wss://your-domain` if you've already put a reverse proxy in front).
3. Paste your `OPENCLAW_GATEWAY_TOKEN` (from `.env`) as the token.
4. You should see the gateway go online and the first model (`nvidia/Gemma-4-31B-IT-NVFP4`) listed.

If you have onboarding questions in the extension:

- **Provider**: vLLM (OpenAI-compatible).
- **Model**: `nvidia/Gemma-4-31B-IT-NVFP4` (already registered by `patch-config.mjs`; pick it from the list).
- **Tool calling**: enabled.
- **Memory search**: enabled, `BAAI/bge-m3` (already wired up).

The patcher re-runs on every `docker compose up`, so if the wizard writes a placeholder API key or skips the model entry, it gets corrected automatically on the next restart.

## 7. First conversation & sanity checks

Three quick checks:

**Tool calling works:**
- Ask the agent: "What's the current time in UTC?"
- It should call the built-in `get_time` tool and reply with a timestamp, not say "I don't have access".

**Image input works:**
- Drag an image into the chat. The agent should describe it.
- Vision prefill adds ~1–2s and ~280 tokens per image by default.

**Memory search works (after you save something):**
- Tell the agent a fact (e.g. "Remember that my favorite color is ultramarine").
- Close the chat, open a new one, ask the agent to recall your favorite color. It should retrieve from memory.

You can also spot-check memory from the CLI:

```bash
docker exec openclaw-cli openclaw memory status --deep
# Should print Embeddings ready, Vector ready (dims 1024).
```

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
- **Rebuild / restart a single service**: `docker compose up -d --force-recreate vllm-llm`.
- **Pull newer images**: `docker compose pull && docker compose up -d`.
- **View gateway logs**: `docker compose logs -f openclaw-gateway`.
- **Enter the CLI container**: `docker exec -it openclaw-cli openclaw`.
- **Back up memory** (do this regularly!): `tar czf openclaw-$(date +%F).tar.gz -C $OPENCLAW_CONFIG_DIR .`.

## 10. Uninstall

```bash
docker compose down -v                           # containers + named volumes
rm -rf /opt/dgx-openclaw/                        # host data (IRREVERSIBLE)
docker image prune                               # optional: reclaim image space
```

Your `.env` is kept. Delete it only when you're sure you're done.

---

Still stuck? → [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md). Want to swap models or add your own agents? → [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md).
