# Setup — DGX OpenClaw Stack

This guide walks you from a fresh DGX Spark / ASUS GB10 box to a running agent, step by step.

**Time budget**: ~20–30 minutes end-to-end on a first install (most of it the initial model download). Subsequent `docker compose up -d` cycles complete in under two minutes once weights are cached.

**Audience**: end-users and sysadmins setting up the stack for the first time. For "how do I customize X" go to [`docs/CUSTOMIZATION.md`](docs/CUSTOMIZATION.md); for design rationale go to [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). If something is wrong, jump straight to [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

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
14 patcher steps (vllm provider wiring, hybrid memory + MMR, SearxNG
enablement, dreaming, trustedProxies, TTS provider, STT provider — see
`patch-config.mjs`).
You'll see `[patch-config]` lines for each change. The gateway restarts and
picks up the patched config.

> **Why all three together.** `openclaw-cli` shares the gateway's network
> namespace (`network_mode: "service:openclaw-gateway"`). If you recreate the
> gateway alone, the still-running CLI ends up pointing at a dead namespace
> and silently loses connectivity. Always recreate the trio together.

## 7. First conversation & sanity checks

The shell snippets below use `${PROJ}` for the container-name prefix — default `dgx-` (set via `CONTAINER_NAME_PREFIX` in `.env`; set it empty for bare names like `openclaw-gateway`). Source it once:

```bash
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2)
PROJ=${PROJ:-dgx-}
```

### Required: confirm the trio is healthy

```bash
curl -sS http://127.0.0.1:18789/healthz                 # → {"ok":true,"status":"live"}
docker exec ${PROJ}openclaw-cli openclaw memory status --deep
# Embeddings ready, Vector ready (dims 1024 for bge-m3).
```

If either command fails or returns the wrong shape, stop here and check [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

### Recommended: smoke-test each capability end-to-end

Open the Chrome extension and try these in a single chat session:

| Capability | What to ask | What you should see |
|---|---|---|
| **Tool calling** | "What's the current time in UTC?" | The agent calls `get_time` and answers with a real timestamp (not "I don't have access"). |
| **Image input** | Drag an image into the chat and ask "What's in this picture?" | A description. Vision prefill adds ~1–2 s and ~280 tokens per image. |
| **Memory recall** | First turn: "Remember my favorite color is ultramarine." Open a *new* chat, ask: "What's my favorite color?" | The new session calls `memory_search` and recovers `ultramarine`. |
| **Web search** | "Use the `web_search` tool to find the current prime minister of Hungary, then cite the source URL." | The agent calls `web_search`, hits the bundled SearxNG, and answers with a URL. |
| **Voice-note STT** | Drop a short wav/mp3/m4a into the chat composer and send with any message. | OpenClaw's `tools.media.audio` pipeline POSTs the file to Whisper, substitutes the transcript into the message body, and the agent replies based on the transcribed content. |

Smaller models occasionally skip tool calls on conversational prompts — naming the tool in the question makes the call deterministic during smoke-testing.

Spot-check SearxNG directly:

```bash
docker exec ${PROJ}openclaw-cli curl -s 'http://searxng:8080/search?q=test&format=json' | head
# Should return a JSON blob with a non-empty `results` array.
```

Spot-check the Whisper STT backend directly:

```bash
STT_KEY=$(grep '^STT_API_TOKEN=' .env | cut -d= -f2-)
curl -s http://127.0.0.1:8093/health | jq .
# → {"status": "healthy", ...}

# Hungarian autodetect on a short sample (drop in your own HU wav/mp3):
curl -sS -X POST http://127.0.0.1:8093/v1/audio/transcriptions \
  -H "Authorization: Bearer $STT_KEY" \
  -F file=@/path/to/hu_sample.wav \
  -F model=Systran/faster-whisper-large-v3 \
  -F response_format=verbose_json | jq '.language, .text'
# → "hu" + accurate transcript
```

## 8. (Optional) Enable browser automation

If you want the agent to reach login-gated, JS-heavy sites (private GitHub
wikis, private Notion pages, MediaWiki instances, Patreon archives), the
`openclaw-browser` service is the path. Re-running `bootstrap.sh` now
prompts you to opt in; if you said "no" earlier and changed your mind, the
manual path is:

```bash
# 1. Generate the two browser secrets if they aren't already in .env
#    (bootstrap.sh did this if you ran it):
grep '^BROWSER_API_TOKEN=' .env || \
  echo "BROWSER_API_TOKEN=$(openssl rand -base64 48 | tr -d '\n')" >> .env
grep '^BROWSER_VNC_PASSWORD=' .env || \
  echo "BROWSER_VNC_PASSWORD=$(openssl rand -base64 24 | tr -d '\n=+/' | head -c 32)" >> .env

# 2. Add 'browser' to the active profile set:
#    Either add `COMPOSE_PROFILES=browser` to .env, or pass --profile browser
#    on every compose command.

# 3. Build + start the service (~1.7 GB image on first build):
docker compose --profile browser up -d --build openclaw-browser
```

Once the service is up, onboard each credential **once** via the noVNC
helper. The bridge is always-on (since v0.7.0) — the persistent password
in `BROWSER_VNC_PASSWORD` lets you peek any time the container is up:

```bash
./bootstrap-browser-login.sh github-user1
```

The script prints a noVNC URL and (when you're SSH'd in) the autossh
tunnel recipe to expose it on your laptop. Open the URL, log in to the
target service with **password + TOTP / SMS OTP / magic link**, hit Enter
in the terminal — cookies persist for the rest of the upstream session
(~14d GitHub, ~30d Notion, etc.).

**Limitations to know about:**

- Passkeys (FIDO2/WebAuthn, including Apple Keychain, Windows Hello,
  Google Password Manager, USB YubiKey) **don't work** over noVNC by W3C
  origin-bound spec. Use password+TOTP for the noVNC flow, or a
  long-lived API token (PAT, integration token, service account) and
  bypass the browser entirely.
- Vanilla Playwright Chromium has `navigator.webdriver=true`, so
  Cloudflare-fronted hostile sites will block. The boundary, the
  rationale, and the optional Patchright swap are documented in
  [`docs/reference/browser-automation.md`](docs/reference/browser-automation.md).

After onboarding, the agent can reach authenticated content via
`browser.navigate(url=..., profile="github-user1")`. Re-run the same
helper script when GitHub's 28-day 2FA window or Notion's ~30-day
session expires.

## 9. (Optional) Reverse proxy + TLS

For remote access over `wss://`, put any reverse proxy in front of port `18789`. A common setup:

- **Nginx Proxy Manager** container on host networking (easiest).
- **Caddy** with automatic Let's Encrypt.
- **Cloudflared tunnel** for no-open-ports public access.

If the proxy terminates TLS and talks plain `ws://` to the gateway on the private network, keep `OPENCLAW_ALLOW_INSECURE_PRIVATE_WS=1` in `.env`.

If the proxy sits on the **host network** and sends `X-Forwarded-For`, you're covered by the default `gateway.trustedProxies` (the `172.16.0.0/12` docker-bridge range already includes the gateway's bridge IP).

If you hit the gateway **directly from your LAN**, add your LAN CIDR via `OPENCLAW_LAN_CIDR=192.168.1.0/24` (or whatever) and re-run `docker compose up -d`. The patcher will update `trustedProxies`.

## 10. (Optional) Daily operations

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

## 11. Uninstall

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
