# Host CLI client for a remote OpenClaw Gateway

This independent Compose project keeps an OpenClaw CLI container running on the
operator's machine. The Gateway, model, agent execution, memory, and conversation
history stay on the DGX. It needs Docker Desktop with Linux containers; it does
not start the GPU stack in the repository's root Compose file.

The image is pinned by tag and digest to the same upstream release the DGX
Gateway runs (currently `2026.10.3`). Keep the two in lockstep: when the Gateway
is upgraded, update the default in `compose.yml` (or set `OPENCLAW_CLIENT_IMAGE`)
and recreate the client:

```powershell
docker compose --file compose.yml up -d
```

Recreating keeps both named volumes, so the device identity and pairing survive.
This is the only supported OpenClaw client on the operator's Windows machine;
do not install the npm `openclaw` package alongside it.

## Initialize on Windows

From this directory:

```powershell
.\Initialize-Client.ps1
```

By default this reads the existing remote connection from
`$env:USERPROFILE\.openclaw\openclaw.json`. A different remote client configuration
can be selected with `-SourceConfig <path>`. Only the Gateway connection settings
are imported. Secrets pass over stdin and are stored in the container's private
state volume; they are not written into this repository or Docker environment
variables. Re-running initialization preserves the container's existing identity
and other local configuration.

The container creates its own Linux device identity. If its first connection
reports `pairing required`, list and approve that specific pending device from
the Gateway's Control UI or trusted Gateway CLI:

```sh
openclaw devices list --timeout 60000
openclaw devices approve <request-id> --timeout 60000
```

Use the current request for this client's device ID. An operator client uses
device pairing, not the separate `node.pair.approve` API. Pending requests can
expire; opening the TUI again creates a fresh request if necessary.

## Operator scopes

`openclaw health` succeeds with any accepted credential, so a healthy container
does not prove that chat works. Chat and agent runs need `operator.write`;
session listing needs at least `operator.read`. If commands fail with
`missing scope: operator.write` (or `operator.read`), the client's device
record on the Gateway was approved with a narrower scope set. A connection that
arrives through the reverse proxy is not local, so the Gateway does not fill in
scopes for the shared token; the approved device record decides.

Fix it on the Gateway side. Open the TUI once so the client retries with the
scopes it needs, then review and approve the pending request from a trusted
Gateway CLI or the Control UI's limited-access request:

```sh
openclaw devices list --timeout 60000
openclaw devices approve <request-id> --timeout 60000
```

Compare the requested and approved access before approving. Verify afterwards:

```powershell
docker exec openclaw-host-client openclaw gateway call sessions.list --json
```

## Use

```powershell
# Interactive chat (Ctrl+C exits; conversations remain on the DGX).
.\oc.ps1
# Equivalent from any terminal:
docker exec -it openclaw-host-client openclaw tui

# Authenticated Gateway connectivity:
.\oc.ps1 health
# Read Gateway sessions without sending a message:
.\oc.ps1 gateway call sessions.list --json
# Shell inside the client:
docker exec -it openclaw-host-client bash
```

The container stays running when the terminal closes; the interactive TUI runs
when you open it. After the first successful CLI/TUI connection, Docker also
checks Gateway connectivity every minute. Until then it reports unhealthy
without repeatedly replacing the pending pairing request. Chat
does not automatically send replies to Discord or other channels (`--deliver`
is opt-in).

Commands such as `memory`, `plugins`, and `mcp` can operate on local files rather
than on the remote Gateway. Run Gateway-local administration on the DGX. Adding
an MCP server to this client alone does not expose its tools to DGX agents.
MCP installation is a separate task.

## Persistence and lifecycle

```powershell
docker compose --file compose.yml ps
docker compose --file compose.yml up -d
docker compose --file compose.yml stop
docker compose --file compose.yml start
```

The restart policy is `unless-stopped`: Docker restarts the container after an
engine restart unless you explicitly stopped it. Docker Desktop must itself be
running (enable its start-at-sign-in option if desired); a sleeping or powered
off Windows host cannot serve the client.

Configuration, device credentials, installed local extensions, and workspace
files use two named Docker volumes. Recreating the container keeps them.
`docker compose down` keeps them too; `docker compose down --volumes` deletes
them and requires initialization and pairing again. No ports, Docker socket,
Windows drives, or SSH keys are exposed to the container.

Official references: [remote Gateway clients](https://docs.openclaw.ai/gateway/remote),
[terminal UI](https://docs.openclaw.ai/cli/tui).
