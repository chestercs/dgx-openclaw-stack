# openclaw-python-sandbox

Self-hosted Python code-execution backend for the OpenClaw agent. Wired
in via MCP (`mcp.servers.python_sandbox`, transport: streamable-http) so
the agent can write Python in tool calls and get stdout / stderr / inline
matplotlib plots / structured errors back.

This service is **opt-in**: it only starts when you set `COMPOSE_PROFILES`
to include `python` and put a non-empty `PYTHON_SANDBOX_API_TOKEN` in
`.env`. With either piece missing it stays parked.

## What's in the box

- **Persistent ipykernel per session.** Same `session_id` reuses the
  same kernel so a multi-step analysis (load CSV → reduce → plot →
  export) keeps state without re-loading on each call.
- **Data-science libraries pre-installed**: `pandas`, `numpy`,
  `matplotlib` (`Agg` backend, plots returned as base64 PNGs),
  `scikit-learn`, `scipy`. No network egress means `pip install` will
  fail by design — anything that's not pre-installed has to be added at
  build time.
- **MCP Streamable-HTTP wire protocol**, hand-rolled in `server/app.py`
  (no SDK dependency). One POST handler, JSON-RPC 2.0 dispatch, ~250
  LOC. Tools advertised: `python_exec`, `python_session_reset`.
- **Bearer auth** on `POST /mcp`; the `/healthz` probe is unauth'd so
  the docker healthcheck doesn't need the token.

## Running it standalone

```bash
docker compose --env-file .env --profile python build openclaw-python-sandbox
docker compose --env-file .env --profile python up -d openclaw-python-sandbox

# Smoke test (substitute the token from .env)
TOKEN=$(grep '^PYTHON_SANDBOX_API_TOKEN=' .env | cut -d= -f2-)
curl -fsS http://127.0.0.1:8094/healthz                          # → ok kernels=0

curl -sS -X POST http://127.0.0.1:8094/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq .

curl -sS -X POST http://127.0.0.1:8094/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"python_exec","arguments":{"code":"print(2+2)"}}}' \
  | jq -r '.result.content[0].text' | jq .
# → { "stdout": "4\n", "stderr": "", "result": null, "plots": [], ... }
```

## How OpenClaw reaches it

The patcher (step 18 in `patch-config.mjs`) writes:

```json
"mcp": {
  "servers": {
    "python_sandbox": {
      "url": "http://openclaw-python-sandbox:8094/mcp",
      "transport": "streamable-http",
      "connectionTimeoutMs": 10000,
      "headers": { "Authorization": "Bearer <PYTHON_SANDBOX_API_TOKEN>" }
    }
  }
}
```

The gateway connects, runs `tools/list`, and surfaces `python_exec` /
`python_session_reset` in the agent's tool catalog. From the agent's
perspective they look identical to any other tool.

## Threat model

- **Trusted-prompt only.** The container has a default user (UID 1000)
  and namespace isolation, but Python introspection can break out of
  the interpreter into the container, and a kernel-exploit chain could
  in principle escape the container. We don't ship gVisor / firecracker
  — if you're running this on a multi-tenant box, that's the upgrade
  path.
- **No network egress** by default (`PYTHON_SANDBOX_NETWORK=none` in
  `.env`). Code can read/write `/workspace` but can't reach SearxNG,
  vLLM, or the LAN. Flip to `bridge` only if you trust the agent
  prompt source.
- **Resource caps**: 8 GB RAM and 4 CPUs by default
  (`PYTHON_SANDBOX_MEMORY_MB`, `PYTHON_SANDBOX_CPUS`). A kernel that
  goes over the limit gets OOM-killed by the docker engine, the next
  call against that session_id transparently starts a new one.
- **Idle reap**: kernels not used for 30 min
  (`PYTHON_SANDBOX_IDLE_TTL_S`) are shut down on a 5-min sweep loop.
  This is for memory hygiene, not security.

## Known limits

- **No GUI**, no `tkinter`, no Jupyter widgets — the kernel runs
  headless, and `MPLBACKEND=Agg` is hard-set so `plt.show()` returns
  silently and the agent only sees the figure as a base64 PNG via the
  iopub `display_data` channel.
- **No `pip install`** at runtime (no egress). To add a library, add it
  to `server/requirements.txt` and rebuild the image.
- **One execute_request at a time per session_id.** Per-session async
  locks serialize calls so the kernel doesn't get crossed iopub
  messages. Different `session_id`s are fully independent and run
  concurrently.
- **`agents.defaults.sandbox` is a separate concern.** That OpenClaw
  feature controls where shell-tools (`exec`, `read`, `write`, ...)
  execute. This MCP server is its own track — for *Python code*
  specifically, with persistent state, plots, and a Python-shaped tool
  surface.
