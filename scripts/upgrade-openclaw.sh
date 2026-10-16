#!/usr/bin/env bash
# Run on the Docker host. Candidate migration uses a separate state copy;
# failed activation returns to the original image and untouched original data.
set -Eeuo pipefail
umask 077

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_dir"
compose=(docker compose --profile meshcore)

usage() {
    cat <<'USAGE'
Usage:
  bash scripts/upgrade-openclaw.sh --prepare [2026.10.4]
  bash scripts/upgrade-openclaw.sh --apply [2026.10.4]
  bash scripts/upgrade-openclaw.sh --rollback <upgrade-directory>

--prepare builds the pinned candidate and bridge without stopping services.
--apply also stops OpenClaw, archives its mounted state, migrates a copy,
        validates it, then activates the copy. It leaves vLLM and other
        model/tool containers running. A failed apply restores the old setup.
--rollback restores the saved environment and old image, using the original
           state directories. It does not merge messages from the new version.

Close interactive OpenClaw clients before --apply. Keep the printed upgrade
directory: after success its state/ directory is the active persistent state.
USAGE
}

mode=${1:---help}
case "$mode" in
    --help|-h) usage; exit 0 ;;
    --prepare|--apply|--rollback) ;;
    *) usage >&2; exit 2 ;;
esac
[[ -f .env && -f docker-compose.yml ]] || { echo 'Run from an initialized stack checkout.' >&2; exit 1; }
command -v python3 >/dev/null || { echo 'Python 3 is required to edit .env safely.' >&2; exit 1; }
"${compose[@]}" config --quiet

# Compose dotenv is data, not a shell script; never source a file with secrets.
set_env() {
    python3 - "$repo_dir/.env" "$@" <<'PY'
import json, os, re, stat, sys, tempfile
filename, *pairs = sys.argv[1:]
if len(pairs) % 2:
    raise SystemExit('Expected key/value pairs')
updates = dict(zip(pairs[::2], pairs[1::2]))
with open(filename, encoding='utf-8') as source:
    lines = source.read().splitlines()
seen, result = set(), []
for line in lines:
    match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
    key = match.group(1) if match else None
    if key in updates:
        if key not in seen:
            result.append(key + '=' + json.dumps(updates[key]))
            seen.add(key)
    else:
        result.append(line)
for key, value in updates.items():
    if key not in seen:
        result.append(key + '=' + json.dumps(value))
fd, temporary = tempfile.mkstemp(prefix='.openclaw-upgrade-', dir=os.path.dirname(filename))
try:
    os.fchmod(fd, stat.S_IMODE(os.stat(filename).st_mode))
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
        output.write('\n'.join(result) + '\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, filename)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

restore_previous() {
    local directory=$1 rollback_tag
    [[ -f "$directory/.env.before" && -f "$directory/rollback-image-tag" ]] || {
        echo "Missing rollback metadata in $directory" >&2; return 1;
    }
    rollback_tag=$(<"$directory/rollback-image-tag")
    docker image inspect "openclaw-base-ext:$rollback_tag" >/dev/null
    cp -p -- "$directory/.env.before" "$repo_dir/.env"
    set_env OPENCLAW_BASE_EXT_VERSION "$rollback_tag"
    if [[ -f "$directory/serial-device-path" ]]; then
        set_env MESHCORE_SERIAL_DEVICE "$(<"$directory/serial-device-path")"
    fi
    local restore_services=(openclaw-gateway openclaw-cli)
    if [[ -f "$directory/meshcore-enabled" ]]; then
        local bridge_tag
        bridge_tag=$(<"$directory/rollback-bridge-tag")
        docker image inspect "openclaw-meshcore-bridge:$bridge_tag" >/dev/null
        set_env MESHCORE_BRIDGE_VERSION "$bridge_tag"
        restore_services+=(openclaw-meshcore-bridge)
    fi
    "${compose[@]}" up -d --no-deps --force-recreate "${restore_services[@]}" || return $?
    echo "Original image and original state restored. Candidate data remains in $directory/state."
}

if [[ "$mode" == --rollback ]]; then
    [[ -n ${2:-} ]] || { usage >&2; exit 2; }
    upgrade_dir=$(cd -- "$2" && pwd -P)
    restore_previous "$upgrade_dir"
    exit 0
fi

version=${2:-2026.10.4}
[[ "$version" =~ ^[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}$ ]] || { echo 'Use an explicit stable release, e.g. 2026.10.4.' >&2; exit 2; }
upstream="ghcr.io/openclaw/openclaw:$version"
candidate_tag="candidate-$version"
candidate="openclaw-base-ext:$candidate_tag"
migration_versions=()
gateway=$("${compose[@]}" ps -aq openclaw-gateway)
if [[ -n "$gateway" ]] && [[ $(docker inspect --format '{{.State.Running}}' "$gateway") == true ]]; then
    current_version=$(docker exec "$gateway" openclaw --version | sed -n 's/^OpenClaw \(20[0-9][0-9]\.[0-9]*\.[0-9]*\).*$/\1/p' | head -n 1)
    [[ -n "$current_version" ]] || { echo 'Could not identify the running OpenClaw version.' >&2; exit 1; }
    for bridge_version in 2026.9.5 2026.9.7; do
        if [[ "$current_version" != "$bridge_version" && $(printf '%s\n' "$current_version" "$bridge_version" | sort -V | head -n 1) == "$current_version" ]] \
            && [[ $(printf '%s\n' "$bridge_version" "$version" | sort -V | head -n 1) == "$bridge_version" ]]; then
            migration_versions+=("$bridge_version")
        fi
    done
fi
# Pin running image IDs before builds can replace a previously used tag.
if [[ "$mode" == --apply ]]; then
    gateway=$("${compose[@]}" ps -aq openclaw-gateway)
    [[ -n "$gateway" ]] || { echo 'The existing gateway container is required to capture its mounted state.' >&2; exit 1; }
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    rollback_tag="rollback-$stamp"
    docker tag "$(docker inspect --format '{{.Image}}' "$gateway")" "openclaw-base-ext:$rollback_tag"
    meshcore=$("${compose[@]}" ps -aq openclaw-meshcore-bridge)
    bridge_running=0
    if [[ -n "$meshcore" ]] && [[ $(docker inspect --format '{{.State.Running}}' "$meshcore") == true ]]; then
        docker tag "$(docker inspect --format '{{.Image}}' "$meshcore")" "openclaw-meshcore-bridge:$rollback_tag"
        bridge_running=1
    fi
fi
docker pull "$upstream"
upstream_digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$upstream")
[[ "$upstream_digest" == ghcr.io/openclaw/openclaw@sha256:* ]] || { echo 'Could not resolve an immutable upstream digest.' >&2; exit 1; }
docker build --build-arg "OPENCLAW_BASE=$upstream_digest" -t "$candidate" openclaw-base-ext
docker run --rm --entrypoint openclaw "$candidate" --version
docker build -t openclaw-meshcore-bridge:0.1.1 openclaw-meshcore-bridge
for bridge_version in "${migration_versions[@]}"; do
    bridge_image="ghcr.io/openclaw/openclaw:$bridge_version"
    docker pull "$bridge_image"
    bridge_digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$bridge_image")
    [[ "$bridge_digest" == ghcr.io/openclaw/openclaw@sha256:* ]] || { echo 'Could not pin the intermediate migration image.' >&2; exit 1; }
    docker tag "$bridge_digest" "openclaw-base-ext:migration-$bridge_version"
done
if [[ "$mode" == --prepare ]]; then
    echo "Prepared $candidate. Run this script with --apply $version to migrate and activate."
    exit 0
fi

upgrade_dir="$repo_dir/private/openclaw-upgrades/$stamp"
mkdir -p -- "$upgrade_dir/state"
chmod 700 "$upgrade_dir" "$upgrade_dir/state"
cp -p -- .env "$upgrade_dir/.env.before"
# Tool containers may share canvas, inbound media, or the sandbox with the
# Gateway. Keep those exact mounts shared without recreating the tool services.
# Save the override through COMPOSE_FILE so later ordinary compose commands
# continue using the same directories, too.
python3 - "$gateway" "$upgrade_dir" <<'PY'
import json, os, subprocess, sys
gateway_id, directory = sys.argv[1:]
gateway = json.loads(subprocess.check_output(['docker', 'inspect', gateway_id]))[0]
labels = gateway['Config']['Labels']
project = labels['com.docker.compose.project']
ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project], text=True).split()
containers = json.loads(subprocess.check_output(['docker', 'inspect', *ids]))
configured = set(subprocess.check_output(['docker', 'compose', '--profile', '*', 'config', '--services'], text=True).split())
core = {'openclaw-gateway', 'openclaw-cli', 'openclaw-config-init', 'openclaw-meshcore-bridge'}
roots = sorted([(m['Source'], m['Destination']) for m in gateway['Mounts'] if m['Destination'] in ('/home/node/.openclaw', '/home/node/.openclaw/workspace')], key=lambda x: -len(x[0]))
shared, siblings = {}, {}
for container in containers:
    service = container['Config']['Labels'].get('com.docker.compose.service')
    if not service or service in core:
        continue
    for mount in container['Mounts']:
        for source, target in roots:
            if mount['Source'] == source:
                raise SystemExit('A tool shares a whole state root; review its mounts before upgrading: ' + service)
            if mount['Source'].startswith(source + '/'):
                destination = target + mount['Source'][len(source):]
                shared[destination] = {'type': 'bind', 'source': mount['Source'], 'target': destination}
                if service in configured:
                    siblings.setdefault(service, []).append({'type': 'bind', 'source': mount['Source'], 'target': mount['Destination'], 'read_only': not mount['RW']})
                break
if shared:
    services = {name: {'volumes': list(shared.values())} for name in sorted(core - {'openclaw-meshcore-bridge'})}
    services.update({name: {'volumes': mounts} for name, mounts in siblings.items()})
    filename = os.path.join(directory, 'shared-tools.compose.json')
    with open(filename, 'w') as output:
        json.dump({'services': services}, output, indent=2)
    files = labels['com.docker.compose.project.config_files'].split(',') + [filename]
    with open(os.path.join(directory, 'compose-file-list'), 'w') as output:
        output.write(':'.join(files))
    print('Preserving shared tool directories:', ', '.join(sorted(shared)))
PY
printf '%s\n' "$rollback_tag" > "$upgrade_dir/rollback-image-tag"
state_kib=$(docker exec "$gateway" du -sk --apparent-size /home/node/.openclaw | awk '{print $1}')
available_kib=$(df -Pk "$upgrade_dir" | awk 'END {print $4}')
[[ "$state_kib" =~ ^[0-9]+$ && "$available_kib" =~ ^[0-9]+$ ]] || { echo 'Could not check backup disk capacity.' >&2; exit 1; }
(( available_kib > 2 * state_kib + 1048576 )) || { echo 'Insufficient disk space for a full archive, state copy, and migration headroom.' >&2; exit 1; }

services=(openclaw-gateway openclaw-cli)
if (( bridge_running )); then
    printf '%s\n' "$rollback_tag" > "$upgrade_dir/rollback-bridge-tag"
    services+=(openclaw-meshcore-bridge)
    : > "$upgrade_dir/meshcore-enabled"
fi

stopped=0
on_exit() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 && stopped )); then
        echo "Upgrade failed; restoring the original deployment. Backup: $upgrade_dir" >&2
        failed_gateway=$("${compose[@]}" ps -aq openclaw-gateway 2>/dev/null || true)
        if [[ -n "$failed_gateway" ]]; then
            docker logs --tail 120 "$failed_gateway" > "$upgrade_dir/gateway-failure.log" 2>&1 || true
        fi
        restore_previous "$upgrade_dir" || echo "Automatic restore failed. Run --rollback '$upgrade_dir' after resolving the reported Docker error." >&2
    fi
    exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# The patcher owns the Discord skill files and AGENTS.md marker blocks. Bring
# the live deployment to the current patcher wiring before the workspace
# baseline is captured; otherwise the audit below sees the staged patcher run
# rewrite those files and refuses activation (2026.10.4 attempt, 2026-10-16).
"${compose[@]}" run --rm -T --no-deps openclaw-config-init

echo "Stopping OpenClaw writers to capture consistent SQLite and workspace state. Backup: $upgrade_dir"
stopped=1
"${compose[@]}" stop --timeout 60 "${services[@]}"
# volumes-from includes the separately mounted workspace inside the state tree.
# Archive as root to preserve permissions; no live file or database is deleted.
docker run --rm --user 0 --volumes-from "$gateway:ro" \
    --mount "type=bind,source=$upgrade_dir,target=/backup" \
    --entrypoint tar "$candidate" -cpf /backup/state.tar -C /home/node/.openclaw .
docker run --rm --user 0 \
    --mount "type=bind,source=$upgrade_dir,target=/backup,readonly" \
    --mount "type=bind,source=$upgrade_dir/state,target=/restore" \
    --entrypoint tar "$candidate" -xpf /backup/state.tar -C /restore
sha256sum "$upgrade_dir/state.tar" > "$upgrade_dir/state.tar.sha256"
docker run --rm --user 0 \
    --mount "type=bind,source=$upgrade_dir,target=/backup,readonly" \
    --entrypoint tar "$candidate" -dpf /backup/state.tar -C /backup/state

audit_workspaces() {
    python3 scripts/audit-openclaw-workspaces.py "$upgrade_dir" "$1"
}

audit_workspaces snapshot

stage_tag="$candidate_tag"
stage_run() {
    env OPENCLAW_BASE_EXT_VERSION="$stage_tag" \
        OPENCLAW_CONFIG_DIR="$upgrade_dir/state" \
        OPENCLAW_WORKSPACE_DIR="$upgrade_dir/state/workspace" \
        "${compose[@]}" run --rm -T --no-deps "$@"
}
run_stage_doctor() {
    if stage_run --entrypoint openclaw openclaw-gateway doctor --fix --non-interactive; then
        return 0
    fi
    # A repair can replace a plugin that the same process already loaded.
    # Retry once in a fresh process; persistent failures still roll back.
    echo 'Doctor did not converge; checking the repaired copy once more in a fresh process.'
    stage_run --entrypoint openclaw openclaw-gateway doctor --fix --non-interactive
}
# Doctor owns versioned migrations; the patcher only maintains stack wiring.
# Run both against the copy while the original state remains untouched.
for bridge_version in "${migration_versions[@]}"; do
    echo "Migrating the state copy through OpenClaw $bridge_version; no intermediate Gateway is started."
    stage_tag="migration-$bridge_version"
    run_stage_doctor
done
stage_tag="$candidate_tag"
if (( ${#migration_versions[@]} )); then
    python3 scripts/archive-imported-cron-logs.py "$upgrade_dir/state" "$upgrade_dir/retired-imported-cron-logs"
fi
run_stage_doctor
stage_run --entrypoint node openclaw-config-init /opt/patch-config.mjs
# Admit the exact patched state before replacing the running deployment.
# Config validation alone cannot detect conflicting retired state files.
run_stage_doctor
stage_run --entrypoint openclaw openclaw-gateway config validate --json
audit_workspaces verify

set_env OPENCLAW_IMAGE_REF "${upstream_digest#ghcr.io/openclaw/openclaw}" \
    OPENCLAW_BASE_EXT_VERSION "$candidate_tag" \
    OPENCLAW_CONFIG_DIR "$upgrade_dir/state" \
    OPENCLAW_WORKSPACE_DIR "$upgrade_dir/state/workspace" \
    MESHCORE_BRIDGE_VERSION 0.1.1
if [[ -f "$upgrade_dir/compose-file-list" ]]; then
    set_env COMPOSE_FILE "$(<"$upgrade_dir/compose-file-list")"
fi
"${compose[@]}" config --quiet
"${compose[@]}" up -d --no-deps --force-recreate openclaw-config-init
init_id=$("${compose[@]}" ps -aq openclaw-config-init)
[[ $(docker wait "$init_id") == 0 ]] || { docker logs --tail 50 "$init_id"; exit 1; }
# Start the bridge after the explicit readiness wait. If all three are given
# to up together, Compose can abort on a transient unhealthy Gateway while its
# automatic startup Doctor is still running, before our wait loop can help.
"${compose[@]}" up -d --no-deps --force-recreate openclaw-gateway openclaw-cli

# The newer upstream entrypoint performs the final exclusive startup admission.
gateway=$("${compose[@]}" ps -aq openclaw-gateway)
ready=0
for (( attempt=0; attempt<60; attempt++ )); do
    if [[ $(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$gateway") == healthy ]]; then
        ready=1
        break
    fi
    sleep 5
done
(( ready )) || { docker logs --tail 60 "$gateway"; exit 1; }
if (( bridge_running )); then
    "${compose[@]}" up -d --no-deps --force-recreate openclaw-meshcore-bridge
fi
cli=$("${compose[@]}" ps -aq openclaw-cli)
docker exec "$cli" openclaw health --timeout 60000 --json > "$upgrade_dir/health.json"
audit_workspaces verify
stopped=0
echo "OpenClaw $version is active. Authenticated Gateway health succeeded."
echo "Active state and rollback metadata: $upgrade_dir"
echo "Rollback: bash scripts/upgrade-openclaw.sh --rollback '$upgrade_dir'"
echo 'Test radio /ping and /status separately; a nonresponsive companion needs USB/radio recovery.'
