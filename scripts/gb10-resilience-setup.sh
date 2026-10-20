#!/usr/bin/env bash
# One-time root setup that keeps the GB10 reachable when the unified memory
# pool runs dry: a model load that filled 121/121 GB plus swap left the box at
# load 500+ with no SSH until a power cycle.
# Run once:  sudo bash scripts/gb10-resilience-setup.sh      Undo: see the end.
set -eu
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

cat > /etc/sysctl.d/90-gb10-resilience.conf <<'EOF'
# All Alt+SysRq keys (F = OOM-kill the biggest process, from a local keyboard).
kernel.sysrq = 1
# Keep 1 GiB free for the kernel. With the 45 MB default, reclaim stalls froze
# userspace before any OOM kill could happen.
vm.min_free_kbytes = 1048576
# Drop page cache before swapping; swap on the unified pool thrashed the box.
vm.swappiness = 10
EOF
sysctl --system >/dev/null

# Hardware watchdog (SBSA): if PID 1 stops for 60 s the board reboots by itself
# instead of hanging until someone presses the power button.
mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=60s\nRebootWatchdogSec=10min\n' > /etc/systemd/system.conf.d/90-watchdog.conf

# Keep sshd out of the OOM killer's way so a remote login still works.
mkdir -p /etc/systemd/system/ssh.service.d
printf '[Service]\nOOMScoreAdjust=-900\n' > /etc/systemd/system/ssh.service.d/90-oom.conf

systemctl daemon-reexec
systemctl daemon-reload
systemctl restart ssh   # existing sessions stay open

echo "sysrq=$(cat /proc/sys/kernel/sysrq) min_free_kbytes=$(cat /proc/sys/vm/min_free_kbytes) swappiness=$(cat /proc/sys/vm/swappiness)"
systemctl show -p RuntimeWatchdogUSec -p RebootWatchdogUSec
echo "sshd oom_score_adj=$(cat /proc/$(pgrep -o -x sshd)/oom_score_adj 2>/dev/null)"
echo done
# Undo: rm /etc/sysctl.d/90-gb10-resilience.conf /etc/systemd/system.conf.d/90-watchdog.conf \
#          /etc/systemd/system/ssh.service.d/90-oom.conf && reboot
