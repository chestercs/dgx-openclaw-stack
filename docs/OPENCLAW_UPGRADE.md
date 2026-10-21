# Upgrade the Dockerized OpenClaw stack

Use the host-side image upgrade flow. `openclaw update` inside the running
container attempts to replace a package owned by root; changing its npm prefix
would create a second installation and would not update the Gateway image.

This stack builds `openclaw-base-ext` on top of the official image. A pull of
`openclaw-gateway` alone does not rebuild that extension. The extension adds
ffmpeg, git and curl, `people-digest.mjs` and the local plugins in
`openclaw-base-ext/plugins/` (people-context, claim-guard, dm-guild-gate,
mcp-image-saver). It does not patch the upstream bundle and keeps the upstream
entrypoint, including its automatic state migration, so an upgrade has no
runtime patches to re-check.

## Prepare and apply

Run on the Docker host, from this repository, with Docker access and Python 3.
Tested transitions: **2026.6.1 to 2026.10.3** and **2026.10.3 to 2026.10.4**.

```bash
# Builds the candidate and the MeshCore bridge while the current services keep running.
bash scripts/upgrade-openclaw.sh --prepare 2026.10.4

# Close OpenClaw terminal clients, then migrate and activate the candidate.
bash scripts/upgrade-openclaw.sh --apply 2026.10.4
```

Apply first runs config-init against the live state, so the files it owns (the
Discord skill files and the `AGENTS.md` marker blocks) are current before the
workspace baseline is taken. It then stops the Gateway, the CLI, and the MeshCore
bridge if it is running. Model and tool services stay running. It pins the old
image for rollback, saves `.env`, archives the complete mounted OpenClaw state
including the nested workspace, and extracts a separate copy.

On that copy, in this order:

1. For older installations, the official 2026.9.5 and 2026.9.7 Doctor migrations,
   without starting intermediate Gateways. This preserves formats the current
   version has retired, including legacy cron history and channel sidecars.
2. Doctor of the new version, which migrates the copy and repairs configured
   plugins. It has to come before config-init: config-init writes the 2026.10
   schema directly and refuses a config that still has the pre-2026.10
   `agents.list`.
3. Config-init (`config-init/patch-config.mjs`), which reapplies this stack's
   wiring.
4. Doctor again, to admit that exact patched state, then the new CLI validates the
   schema.

Initialization on the current schema preserves the migrated SQLite approval policy
and never recreates `exec-approvals.json`. Only after validation does the script
switch `.env` to the candidate and the copied state, recreate the Gateway and CLI,
wait for readiness, start the MeshCore bridge, and check authenticated Gateway
health. The Gateway gets a two-minute startup grace for its automatic Doctor; the
explicit readiness wait allows up to five minutes. The local CLI shares the
Gateway's PID namespace as well as its network, so memory and configuration
commands can verify the current state owner while it runs.

Plugin repairs may need npm registry access and several minutes. If a repair
replaces an already-loaded plugin, Doctor gets one retry in a fresh process.
Persistent failures still stop the upgrade. Legacy cron JSONL sources left behind
by an existing `.migrated` archive move into a separate recovery directory only
after every record and its fields have been verified in SQLite. The source bytes
and a checksum manifest are retained without overwriting any existing archive.

The archive is verified after extraction and gets a SHA-256 checksum. A workspace
manifest checks every existing file. It accepts byte-identical moves and verifies
the specific upstream TOOLS.md/AGENTS.md and heartbeat migrations, including
instruction text and legacy task cadences at their new locations. Original
versions of migrated documents also remain in `workspace-originals/`. Unexpected
edits prevent activation. Shared tool directories retain their original host
mounts through a saved Compose override and `COMPOSE_FILE`, so canvas, inbound
media, and sandbox files stay shared without restarting the tool containers.

The new state uses `private/openclaw-upgrades/<UTC-timestamp>/state`. **Keep this
directory** after a successful update: it is active data, not a disposable test
folder. The original agent state remains available for rollback; shared tool
directories remain active in their original locations. Full archive plus copy
requires roughly twice the existing state's disk usage, plus migration headroom.
The script checks available space before stopping services.

Existing agent workspaces and credentials remain part of the migrated state.
Settings upstream has retired use upstream defaults; config-init no longer writes
them.

## Verify and roll back

```bash
docker compose --profile meshcore ps
docker exec openclaw-cli openclaw --version
docker exec openclaw-cli openclaw health --timeout 60000
docker exec openclaw-cli openclaw plugins inspect people-context --runtime --json
docker logs --tail 40 openclaw-meshcore-bridge
```

Use prefixed container names if `CONTAINER_NAME_PREFIX` is configured. The
`plugins inspect` line checks that a local plugin still loads on the new version
(`typedHooks` lists its hooks); repeat it for `claim-guard` and `dm-guild-gate`.
With `OPENCLAW_WEB_SEARCH_PROVIDER=parallel-free`, check that `openclaw plugins list`
shows `parallel` at the new version (`openclaw plugins install
@openclaw/parallel-plugin@<version> --force` otherwise) and run
`scripts/websearch-bench.py parallel-free`.
Try a normal chat turn, and the radio's `/ping` and `/status` if you run MeshCore.
The script checks service health; a real model turn and radio delivery still need
end-to-end verification on the actual hardware.

A failed apply automatically restores the saved environment and original
OpenClaw image against the original state. The same action is available later:

```bash
bash scripts/upgrade-openclaw.sh --rollback /absolute/path/printed/by/the/script
```

Rollback returns to the pre-upgrade state; it does not merge new-version
messages into the original databases. It preserves both state trees for review.
Do not delete SQLite files, lease records, or migration backups to bypass a
maintenance ownership refusal. Wait for the reported lease expiry and retry.

## Upgrade the operator's host client

The Windows host client in `openclaw-host-client/` is a separate Compose project
and is not touched by `upgrade-openclaw.sh`. After the Gateway is healthy on the
new release, bump the client's default image in `openclaw-host-client/compose.yml`
to the same tag and upstream digest, then recreate it on the Windows machine:

```powershell
docker compose --file openclaw-host-client/compose.yml up -d
docker exec openclaw-host-client openclaw --version
docker exec openclaw-host-client openclaw doctor --fix --non-interactive
docker exec openclaw-host-client openclaw health --timeout 60000
docker exec openclaw-host-client openclaw gateway call sessions.list --json
```

Recreating keeps the client's named volumes, so its device identity and pairing
survive. Never use `down --volumes` for an upgrade. The `doctor --fix` step
applies the client's local state migrations; without it after the 2026.10 bump a
legacy `identity/*.json` stayed behind, and the CLI then failed every read and
write RPC with `missing scope` even though `health` passed.

## A nonresponsive MeshCore companion

`No response from meshcore node` happens in the serial handshake with the radio,
before any agent turn, so a Gateway upgrade cannot cause or fix it. Recovery:
[`reference/meshcore-bridge.md`](reference/meshcore-bridge.md).

Official reference: [container image upgrades](https://docs.openclaw.ai/install/docker#upgrading-container-images).
See also the [intermediate migration requirements](https://docs.openclaw.ai/gateway/doctor/config-migrations).
