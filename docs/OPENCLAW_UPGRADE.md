# Upgrade the Dockerized OpenClaw stack

Use the host-side image upgrade flow. `openclaw update` inside the running
container attempts to replace a package owned by root; changing its npm prefix
would create a second installation and would not update the Gateway image.

This stack builds `openclaw-base-ext` on top of the official image. A pull of
`openclaw-gateway` alone does not rebuild that extension. The upgrade script
retains ffmpeg, Homebrew, the runtime patches, and the upstream image's automatic
state migration entrypoint.

## Prepare and apply

Run on the DGX host, from this repository, with Docker access and Python 3.
Tested transitions: **2026.6.1 to 2026.10.3** and **2026.10.3 to 2026.10.4**.

```bash
# Builds the candidate and the bridge while the current services keep running.
bash scripts/upgrade-openclaw.sh --prepare 2026.10.4

# Close OpenClaw terminal clients, then migrate and activate the candidate.
bash scripts/upgrade-openclaw.sh --apply 2026.10.4
```

Apply stops the Gateway, CLI, and the bridge if it is running. Model and tool
services stay running. It pins the old image for rollback, saves `.env`, archives
the complete mounted OpenClaw state including the nested workspace, and extracts
a separate copy. For older installations it first runs the official 2026.9.5
and 2026.9.7 Doctor migrations on that copy, without starting intermediate
Gateways. This preserves formats that the current version has retired,
including legacy cron history and channel sidecars. Doctor migrates that copy and repairs configured plugins; the
patcher reapplies this stack's wiring, Doctor admits that exact patched state,
then the new CLI validates its schema. Current-schema initialization preserves
the migrated SQLite approval policy and never recreates `exec-approvals.json`.
Only after validation does the script switch `.env` to the candidate and copied
state, recreate Gateway and CLI, wait for readiness, start the bridge, and check
authenticated Gateway health. The Gateway gets a two-minute startup grace for
its automatic Doctor; the explicit readiness wait allows up to five minutes.
The local CLI shares the Gateway PID namespace as well as its network, so memory
and configuration commands can verify the current state owner while it runs.
Plugin repairs may need npm registry access and several minutes.
If a repair replaces an already-loaded plugin, Doctor gets one retry in a fresh
process. Persistent failures still stop the upgrade. Legacy cron JSONL sources
left behind by an existing `.migrated` archive move into a separate recovery
directory only after every record and its fields have been verified in SQLite.
The source bytes and a checksum manifest are retained without overwriting any
existing archive.

The archive is verified after extraction and gets a SHA-256 checksum. A workspace
manifest checks every existing file. It accepts byte-identical moves and verifies
the specific upstream TOOLS.md/AGENTS.md and heartbeat migrations, including
instruction text and legacy task cadences at their new locations. Original
versions of migrated documents also remain in `workspace-originals/`. Unexpected
edits prevent activation. Shared tool directories retain their original host mounts through a
saved Compose override and `COMPOSE_FILE`, so canvas, inbound media, and sandbox
files stay shared without restarting the tool containers.

The new state uses `private/openclaw-upgrades/<UTC-timestamp>/state`. **Keep this
directory** after a successful update: it is active data, not a disposable test
folder. The original agent state remains available for rollback; shared tool
directories remain active in their original locations. Full archive plus copy
requires roughly twice the existing state's disk usage, plus migration headroom.
The script checks available space before stopping services.

The patcher understands the current keyed agent roster, root memory/TTS settings,
capability-tagged media models, and exec modes. Upstream-retired settings such as
the hybrid tuning block and stuck-session tuning knobs use upstream defaults.
Existing agent workspaces and credentials remain part of the migrated state.
On the current schema, `OPENCLAW_AGENT_CONTEXT_TOKENS` sets each configured
model's runtime `contextTokens` cap without changing its native `contextWindow`.
An explicit `off` clears these catalog caps; leave the variable unset to retain
manually configured model caps.

## Verify and roll back

```bash
docker compose --profile meshcore ps
docker exec openclaw-cli openclaw --version
docker exec openclaw-cli openclaw health --timeout 60000
docker logs --tail 40 openclaw-meshcore-bridge
```

Use prefixed container names if `CONTAINER_NAME_PREFIX` is configured.
Try a normal chat turn and the radio's `/ping` and `/status` after the update.
The script checks service health; a real model turn and radio delivery still
need end-to-end verification on the actual hardware.

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
applies the client's local state migrations; skipping it after the 2026.10
bump left a legacy `identity/*.json` behind, and the CLI then failed every
read and write RPC with `missing scope` even though `health` passed.

## A nonresponsive MeshCore companion

`No response from meshcore node` occurs during the radio protocol handshake,
before an agent turn. A Gateway update cannot repair that serial handshake.
Check the stable `/dev/serial/by-id/` device, USB data cable, and Companion Radio
USB firmware. If needed, power-cycle the companion by unplugging/replugging USB,
then recreate only the bridge so Docker resolves the device mapping again:

```bash
ls -l /dev/serial/by-id/
docker compose --profile meshcore up -d --no-deps --force-recreate openclaw-meshcore-bridge
docker logs --tail 40 openclaw-meshcore-bridge
```

Bridge 0.1.1 reports a missing companion response directly, uses tested dependency
versions, and makes Gateway opening/handshake and ping deadlines configurable.
These deadlines reduce reconnect churn during a slow Gateway response; they do
not resolve Gateway event-loop stalls or an unresponsive radio.

On a T-Echo, an `nRF UF2` mass-storage interface indicates bootloader mode.
Try one press of the top-left RESET button; a double press enters DFU mode.
See the [manufacturer's button instructions](https://github.com/Xinyuan-LilyGO/T-Echo).
For a fixed USB port, a stable `/dev/serial/by-path/` alias can survive the
product-name change between bootloader and application; keep that same port.

Official reference: [container image upgrades](https://docs.openclaw.ai/install/docker#upgrading-container-images).
See also the [intermediate migration requirements](https://docs.openclaw.ai/gateway/doctor/config-migrations).
