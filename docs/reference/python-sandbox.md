# Python code-execution sandbox

Self-hosted Python execution backend that the OpenClaw agent reaches over
MCP. Persistent ipykernel per session, batteries-included data-science
stack (pandas, numpy, matplotlib `Agg`, scikit-learn, scipy), Bearer-auth.
Ships in v0.8.0+ as the opt-in `--profile python` service.

## Why this design

Three viable paths were considered.

### OpenClaw's native `code_execution` tool — rejected

OpenClaw exposes a `code_execution` tool, but the implementation routes
to xAI's Responses API (cloud, paid, requires an `XAI_API_KEY`). The
config schema wires it under `plugins.entries.xai.config.codeExecution`.
This stack is self-hosted by design — paying xAI per token to run a
two-line `print(2+2)` defeats the whole posture.

### `agents.defaults.sandbox` + the `exec` tool — rejected

OpenClaw has a generic tool sandbox (`agents.defaults.sandbox`) with
Docker / SSH / OpenShell backends. It controls *where* the existing
`exec` / `read` / `write` / `edit` / `process` tools execute. We could
flip the backend to a Python-equipped image and run Python via `exec`.

The problem: that knob is gateway-wide. Flipping it changes the
execution context for every other tool too — file edits, shell commands,
cwd state. Plus `exec` is one-shot per call: there's no notion of
session-persistent state, so a multi-step analysis (load CSV, then
reduce, then plot) re-loads the dataset every call.

### MCP server we own — what we shipped

OpenClaw added native MCP client support (post-v0.7.0). Config:
`mcp.servers.<name>` with stdio / SSE-HTTP / Streamable-HTTP transports.
Verified against `docs.openclaw.ai/cli/mcp` on 2026-04-26.

We ship `openclaw-python-sandbox` as a sibling docker service, expose
an MCP Streamable-HTTP endpoint at `/mcp`, and the patcher (step 18)
wires it into `mcp.servers.python_sandbox`. Two tools surface to the
agent: `python_exec` and `python_session_reset`. The OpenClaw gateway
auto-discovers the tools via `tools/list` on connect.

This is opt-in (Compose profile `python` + a non-empty
`PYTHON_SANDBOX_API_TOKEN`), Python-specific, and orthogonal to every
other tool's behavior — the design knob the other two paths lacked.

## Architecture

```
agent (in gateway)
   │
   │  tools/call python_exec({code, session_id})
   ▼
gateway MCP client                  ── over bridge DNS, port 8094 ──►   openclaw-python-sandbox
                                                                          │
                                                                          ├─ FastAPI /mcp (Bearer auth)
                                                                          │     │ JSON-RPC dispatch
                                                                          │     ▼
                                                                          ├─ KernelPool (jupyter_client)
                                                                          │     │ session_id → kernel_id
                                                                          │     ▼
                                                                          └─ ipykernel children (one per session)
                                                                                │
                                                                                ▼
                                                                            /workspace
                                                                            (bind from host)
```

One container, one uvicorn process. The kernel pool lives in-process via
`jupyter_client.MultiKernelManager` (no separate Jupyter Kernel Gateway
subprocess — saves a HTTP hop per call and removes second-process
lifecycle coordination). Each `session_id` lazily spawns its own
ipykernel child; the child runs inside the same container's namespace.

Async lifecycle:

- **Per-session lock** serializes `python_exec` calls against the same
  `session_id` (a kernel processes one `execute_request` at a time).
- **Different `session_id`s run concurrently**, so a multi-agent setup
  with one session per agent name doesn't bottleneck.
- **Idle reaper**: kernels not used for `PYTHON_SANDBOX_IDLE_TTL_S`
  (default 30 min) get shut down on a `PYTHON_SANDBOX_REAP_INTERVAL_S`
  (default 5 min) sweep. Memory hygiene only — not security.

## MCP wire protocol

We hand-rolled the wire format inside `server/app.py` rather than
depending on the `mcp` Python SDK (its API has churned across 1.x). Two
JSON-RPC methods are interesting:

**`tools/list`** — response shape:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "tools": [
      {
        "name": "python_exec",
        "description": "Execute Python code in a persistent ipykernel sandbox …",
        "inputSchema": { "type": "object", "properties": { … }, "required": ["code"] }
      },
      { "name": "python_session_reset", … }
    ]
  }
}
```

**`tools/call`** — request:

```json
{
  "jsonrpc": "2.0", "id": 2, "method": "tools/call",
  "params": { "name": "python_exec", "arguments": { "code": "print(2+2)" } }
}
```

Response wraps the structured result inside MCP's `content` array:

```json
{
  "jsonrpc": "2.0", "id": 2,
  "result": {
    "content": [
      { "type": "text", "text": "{\"stdout\":\"4\\n\",\"stderr\":\"\",\"result\":null,\"plots\":[],\"duration_ms\":12,\"truncated\":false,\"error\":null}" }
    ],
    "isError": false
  }
}
```

The agent reads `toolResult.content[0].text` and gets a JSON-parseable
payload. `isError: true` is set when the kernel raised an uncaught
exception so the agent can decide whether to retry, change strategy, or
surface the error.

### Tool name prefix in the agent's catalog

OpenClaw surfaces external MCP tools under `<server_name>__<tool_name>`
to keep the namespace flat and unambiguous. So the agent sees:

- `python_sandbox__python_exec`
- `python_sandbox__python_session_reset`

**Refer to the tools by the prefixed name in agent prompts.** Verified
on GB10 with Gemma 4 31B NVFP4 (2026-04-26): a prompt asking for
"the python_exec tool" without the `python_sandbox__` prefix produced
no tool call at all (the model returned an unrelated reply, no
failures logged). The prefixed form `python_sandbox__python_exec`
worked first try with `--thinking medium`. If you write higher-level
tooling that constructs prompts for this sandbox, always use the
prefixed name.

## Threat model

**Trusted-prompt only.** The container has Linux namespaces (PID, NET,
MNT, IPC, UTS, USER) and runs as UID 1000. Python introspection inside
the kernel can do anything the container's seccomp profile allows. The
host filesystem is protected by namespace isolation, but a kernel
exploit chain — uncommon, but not zero — could in principle escape.

What we ship as defense-in-depth:

- **Bearer-token auth on `/mcp`.** A leaked token from a sibling
  container is the realistic attack surface; rotate via
  `rotate-secrets.sh PYTHON_SANDBOX_API_TOKEN`.
- **Loopback-only port publishing** by default
  (`PYTHON_SANDBOX_BIND=127.0.0.1`). The gateway uses bridge DNS, so
  publishing to `0.0.0.0` is purely operator opt-in for LAN debugging.
- **Resource caps** via `mem_limit` and `cpus` — a runaway analysis
  gets OOM-killed by the engine, the next call against that
  `session_id` transparently starts a fresh kernel.
- **`cap_drop: [ALL]`** plus `no-new-privileges:true`. The kernel
  doesn't need any Linux capabilities.

What we explicitly do **not** ship:

- **No gVisor or Kata Containers.** A second-layer kernel (gVisor's
  user-space syscall interceptor) blocks most introspection-driven
  escape chains, at ~30% latency overhead. Add it if your threat model
  is multi-tenant.
- **No hard network egress block.** `PYTHON_SANDBOX_NETWORK=none` is a
  documented placeholder for a future v0.8.x patch that will attach the
  service to an `internal: true` docker network. Today, egress is
  implicitly limited by the absence of network-using libraries in the
  image plus the kernel's lack of root for raw sockets — but `urllib`,
  `http.client`, and `socket` all work as long as the agent's code
  doesn't need elevated privileges. If you need to *enforce* no-egress,
  attach the container to a `--internal` network at runtime.
- **No persistent identity tracking across sessions.** Sessions are
  identified by an arbitrary string the agent picks. Two agents using
  the same `session_id` see each other's state — this is by design (a
  multi-agent setup might share a workspace) but means session IDs
  shouldn't be treated as access-control boundaries.

## Tunables

| Env var | Default | Purpose |
|---|---|---|
| `PYTHON_SANDBOX_API_TOKEN` | (empty) | Bearer token. Empty = service stays parked AND patcher step 18 cleans the entry from openclaw.json. |
| `PYTHON_SANDBOX_PORT` | `8094` | Container port for the MCP endpoint. |
| `PYTHON_SANDBOX_BIND` | `127.0.0.1` | Host bind for the published port. |
| `PYTHON_SANDBOX_KERNEL_TIMEOUT_S` | `30` | Per-execution wall-clock cap; kernel gets `interrupt_kernel` on exceed (not killed). |
| `PYTHON_SANDBOX_MAX_OUTPUT_BYTES` | `10485760` | Truncation cap on `stdout + stderr + plot` returned per call. |
| `PYTHON_SANDBOX_IDLE_TTL_S` | `1800` | Kernels idle longer than this get reaped. |
| `PYTHON_SANDBOX_REAP_INTERVAL_S` | `300` | Reaper sweep frequency. |
| `PYTHON_SANDBOX_MEMORY_MB` | `8192` | docker `mem_limit` for the container (covers all kernels combined). |
| `PYTHON_SANDBOX_CPUS` | `4` | docker `cpus` quota. |
| `PYTHON_SANDBOX_URL` | `http://openclaw-python-sandbox:8094/mcp` | What the patcher writes into `mcp.servers.python_sandbox.url`. Override only if you're routing through a different bridge alias. |

## Known limits

- **Headless only**: no `tkinter`, no Jupyter widgets, no GUI. The
  kernel uses the `matplotlib_inline` backend (ipykernel's default,
  hard-set against an Agg override that would silently kill plot
  delivery). Figures published by the kernel land as base64 PNGs in
  `result.plots[]` via iopub `display_data`. To trigger publication,
  let the figure be the last expression of a cell (e.g. `fig` on its
  own line) or call `display(fig)` explicitly.
- **No `pip install` at runtime by design.** No egress + no root means
  the kernel can't bring in new packages. To add a library, edit
  `openclaw-python-sandbox/server/requirements.txt` and rebuild the
  image.
- **One `execute_request` per session at a time.** Per-session async
  locks prevent concurrent calls on the same kernel from getting
  crossed iopub messages.
- **Kernel state ≠ filesystem state.** The kernel's variables live in
  process memory and disappear on `python_session_reset` or container
  restart. Files in `/workspace` (bind from
  `${OPENCLAW_WORKSPACE_DIR}/sandbox/`) survive both.
- **Plot size cap.** Large matplotlib figures (>10 MB base64) hit
  `PYTHON_SANDBOX_MAX_OUTPUT_BYTES` truncation. Save to file instead
  for big visualizations: `plt.savefig('/workspace/foo.png')` then
  return the path.

## Verification recipes

```bash
# Stack is up with the python profile active. Substitute PROJ as in CLAUDE.md.
PROJ=$(grep '^CONTAINER_NAME_PREFIX=' .env | cut -d= -f2); PROJ=${PROJ:-dgx-}
TOKEN=$(grep '^PYTHON_SANDBOX_API_TOKEN=' .env | cut -d= -f2-)

# 1. Healthz
curl -fsS http://127.0.0.1:8094/healthz                    # → ok kernels=0

# 2. tools/list
curl -sS -X POST http://127.0.0.1:8094/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | jq '.result.tools[].name'
# → "python_exec"
# → "python_session_reset"

# 3. Direct python_exec
curl -sS -X POST http://127.0.0.1:8094/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"python_exec",
                 "arguments":{"code":"import pandas as pd; print(pd.__version__)"}}}' \
  | jq -r '.result.content[0].text' | jq .stdout

# 4. Persistence smoke
curl -sS -X POST http://127.0.0.1:8094/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"python_exec",
                 "arguments":{"code":"x = 42","session_id":"demo"}}}'
curl -sS -X POST http://127.0.0.1:8094/mcp \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call",
       "params":{"name":"python_exec",
                 "arguments":{"code":"print(x)","session_id":"demo"}}}' \
  | jq -r '.result.content[0].text' | jq .stdout
# → "42\n"

# 5. Agent end-to-end via gateway. NOTE: refer to the tool by its
#    prefixed name (python_sandbox__python_exec). Without the prefix,
#    Gemma 4 NVFP4 silently fails to call the tool — see "Tool name
#    prefix" above.
docker exec ${PROJ}openclaw-cli openclaw agent --agent main \
  --message 'Call python_sandbox__python_exec with code="print(2**128)". Reply with the printed value prefixed by VAL:' \
  --thinking medium --json --timeout 180 \
  | jq '.toolSummary, .finalAssistantVisibleText'
```

## Related docs

- [`../CUSTOMIZATION.md`](../CUSTOMIZATION.md) → "Python code execution
  sandbox" section — opt-in walkthrough, GPU upgrade path, network
  hardening.
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) → "Python sandbox
  subsystem" subsection — design rationale at the stack level.
- [`../TROUBLESHOOTING.md`](../TROUBLESHOOTING.md) → entries for kernel
  timeouts, MCP-not-registered, OOM-killed kernels.
- [`./openclaw-internals.md`](./openclaw-internals.md) → MCP client
  semantics in the gateway, full patcher step list.
