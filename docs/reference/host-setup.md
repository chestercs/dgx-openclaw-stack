# What runs on the host, outside Docker

Everything the stack needs on the GB10 besides its containers lives in `scripts/` and is installed and
checked by one script, [`scripts/gb10-host-setup.sh`](../../scripts/gb10-host-setup.sh):

```bash
bash scripts/gb10-host-setup.sh check              # compare the host with the repo; changes nothing
bash scripts/gb10-host-setup.sh cron               # (re)write the stack's crontab block, as the operator
sudo bash scripts/gb10-host-setup.sh system        # systemd units + polkit rule
sudo bash scripts/gb10-host-setup.sh resilience    # sysctl, hardware watchdog, sshd OOM shield
```

Each step is idempotent. `check` prints one line per piece: `ok`, `DIFF` (installed but not what the repo
has), `MISSING`, or `UNKNOWN`. Run it after an upgrade, after a reboot, or whenever something looks off.

## The pieces

| Piece | Installed as | What it does | Why |
|---|---|---|---|
| [`gb10-clock-cap.service`](../../scripts/gb10-clock-cap.service) | systemd unit, enabled | `nvidia-smi -lgc 300,2200` at boot | an uncapped GB10 powers off at ~94 °C under sustained load ([gb10-power-thermal.md](gb10-power-thermal.md)) |
| [`gb10-drop-caches.service`](../../scripts/gb10-drop-caches.service) | systemd unit, static (started on request) | drops the clean page cache | Strata sizes its GPU tier once, from free memory, and `cudaMalloc` does not reclaim the cache |
| [`gb10-drop-caches.rules`](../../scripts/gb10-drop-caches.rules) | `/etc/polkit-1/rules.d/50-gb10-drop-caches.rules` | lets the docker group start that one unit without a password | `llm-ensure.sh` runs as the operator, from cron |
| [`gb10-resilience-setup.sh`](../../scripts/gb10-resilience-setup.sh) | sysctl.d, systemd.conf.d, an sshd drop-in | SysRq keys, `vm.min_free_kbytes` 1 GiB, `vm.swappiness` 10, hardware watchdog, sshd OOM shield | a full unified pool left the box at load 500+ with no SSH |
| [`gb10-thermguard.sh`](../../scripts/gb10-thermguard.sh) | cron, `@reboot` + every 5 min | kills the model containers at 93 °C, restarts them below 75 °C | backstop for the clock cap |
| [`gb10-memguard.sh`](../../scripts/gb10-memguard.sh) | cron, `@reboot` + every 5 min | kills the model containers below 6 GB available | the GB10 stalls instead of OOM-killing |
| [`llm-ensure.sh`](../../scripts/llm-ensure.sh) | cron, `@reboot boot` + every 5 min | starts the chat model after a guard's kill or a reboot; Strata only after the stack has loaded, after a cache drop | a killed container counts as stopped by hand; Strata has no restart policy |
| [`netns-dependents-up.sh`](../../scripts/netns-dependents-up.sh) | cron, `@reboot` + every 5 min | re-attaches `openclaw-cli` and the MeshCore bridge to a recreated gateway's network namespace | they share the gateway's namespace and lose the network when it is recreated |
| [`daily-status.sh`](../../scripts/daily-status.sh) | cron, 09:15 | one structure-only health block in `~/daily-status/` | quiet failures (claim-guard, jobs, thermals) get seen |

The crontab block is generated from `.env` and sits between `# >>> dgx-openclaw-stack` and
`# <<< dgx-openclaw-stack` markers; lines outside it stay untouched. The guards watch `vllm-llm` and `comfyui`,
plus `strata` when `COMPOSE_FILE` has `compose/strata.yml`, with `CONTAINER_NAME_PREFIX` applied.
`GB10_GUARD_EXTRA` adds more containers to both guards, `DAILY_STATUS_IGNORE` lists containers stopped on
purpose. The guards hold a lock and read their list once: after the list changes, kill the running guard
(`pkill -f '^bash [^ ]*scripts/gb10-thermguard\.sh$'`, same for memguard) and cron starts it again within 5 minutes.

## State on the host

| Path | What |
|---|---|
| `~/thermguard/` | thermguard events and temperature CSV, memguard log, `llm-ensure.log`, `stopped.list` |
| `~/daily-status/` | the daily health blocks |
| `~/netns-dependents.log` | the netns watcher's log |
| `~/.llm-ensure-off` | when present, `llm-ensure.sh` does nothing (GPU needed for something else) |
| `STRATA_SRC`, `STRATA_MODEL_DIR`, `STRATA_DATA_DIR` | the Strata engine build, the model, parked conversations ([openclaw-strata/README.md](../../openclaw-strata/README.md)) |

## Undo

- systemd: `sudo systemctl disable --now gb10-clock-cap`; `sudo rm /etc/systemd/system/gb10-{clock-cap,drop-caches}.service
  /etc/polkit-1/rules.d/50-gb10-drop-caches.rules && sudo systemctl daemon-reload`
- resilience: the end of [`gb10-resilience-setup.sh`](../../scripts/gb10-resilience-setup.sh)
- cron: delete the marked block with `crontab -e`
