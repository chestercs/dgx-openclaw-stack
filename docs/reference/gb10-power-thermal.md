# GB10 power, thermals and firmware

> Field notes from an ASUS Ascent GX10 (GB10) running 120B-class models 24/7
> (measured on firmware v0106). Community findings are marked as such; NVIDIA
> publishes no official guidance on clock caps for GB10.

GB10 boxes are desktop machines: one 240 W USB-C PSU, a small heatsink, and
one 121 GiB unified memory pool shared by the GPU and the OS. A stack that
keeps a 120B MoE model busy for hours treats them like a server. These
are the failure modes that showed up, and what fixed them.

## 1. Log-less hard power-off under sustained load

**Symptom.** The box switches off mid-inference. There is no shutdown in the
journal and no power outage, and it does not come back on by itself. On the
reference host it happened after hours of Nemotron decode, and again after
6 minutes of a prefill-heavy load (2 × 60k-token prompts in a loop). The last
sample before the second one was hottest-acpitz 94 °C, nvidia-smi 86 °C, ~80 W.

**What the numbers mean.**
- The ACPI critical trip says 104.8 °C, but the boxes die far below it.
  Community reports put the cut-off at acpitz 94.6–97.8 °C, so it is the EC or
  power protection, not the ACPI trip.
- `nvidia-smi` reads 8–15 °C lower than the hottest `acpitz` zone. Watch
  `/sys/class/thermal/thermal_zone*/temp`, not only the GPU sensor.
- Prefill heats the box much faster than decode. Decode is memory-bandwidth
  bound and draws less.

**Fix: cap the GPU clock** ([`scripts/gb10-clock-cap.service`](../../scripts/gb10-clock-cap.service)).
- `nvidia-smi -pl` is N/A on GB10. `nvidia-smi -lgc 300,2200` is the knob that
  works. It is lost on reboot, so install it as the systemd unit.
- **Measured with the cap** (firmware v0106):

  | Test | Uncapped | Capped at 2200 MHz |
  |---|---|---|
  | bf16 matmul | 92.3 TFLOPS, 93 W | 90.0 TFLOPS, 82 W |
  | Prefill stress, 25 min, 88 × 42k-token prompts | powered off at ~6 min, 94 °C | **acpitz max 87 °C** (flat after 10 min), avg 52 W, 0 errors |
  | Nemotron decode, single stream | — | 19.8 tok/s (no measurable loss) |
  | Prefill (99k-token prompt) | — | 2588 tok/s |
- **Community stable values** (unofficial): 2100–2300 MHz; 2200 is the common
  pick. If the box still shuts down, step to 2100, then 2000.

**The CPU has no cap.** It shares the package and the cooler with the GPU, so a
chat model that runs part of its weights on the CPU (Strata's DeepSeek,
`compose/strata.yml`) heats the box with the GPU capped. 18 engine threads
for a 20-min agent turn held acpitz at 87–92 °C until thermguard killed the
engine. Pinning the engine to the Cortex-A725 cores (0-4, 10-14; the X925 cores
are 5-9, 15-19) works without root. 15-min code generation beside the stack, 10
threads:

| Cores | tok/s | acpitz max |
|---|---|---|
| unpinned (threads land on 0-9, mixed) | 33.2 | 84 °C |
| X925 only | 30.7 | 85 °C |
| A725 only | 29.6 | 72 °C |

A CPU clock cap is not used: the firmware runs CPPC autonomous selection
(`cpufreq/policy*/auto_select` = 1) and whether `scaling_max_freq` binds there
is unverified (users report both outcomes).

**Backstop: [`scripts/gb10-thermguard.sh`](../../scripts/gb10-thermguard.sh).**
- It polls the GPU sensor and the hottest acpitz zone every 2 s.
- At 93 °C it SIGKILLs the LLM containers.
- It restarts them once the box has stayed below 75 °C for 2 minutes.
- 97 °C with 5 s polling and `docker stop -t 10` was too late: the box
  powered off first.
- A killed container counts as manually stopped, so `restart:
  unless-stopped` does not bring it back. Thermguard restarts what it
  killed itself;
  [`scripts/llm-ensure.sh`](../../scripts/llm-ensure.sh) (from cron) is the
  fallback for memguard kills and reboots (`vllm-llm`, or `strata` with
  `compose/strata.yml`, which it starts only after the stack has loaded).

**Other community mitigations** (unverified here):
- airflow and orientation (stand it up, do not stack units);
- re-pasting;
- `partnerdiag --field` — a failing PowerStress / thermal test is RMA-eligible.

## 2. GPU stuck at 611 MHz after a crash ("PD wedge")

**Symptom.**
- After a thermal power-off the GPU reads 611 MHz, idle and under load alike.
- It draws ~10 W under load (healthy: ~2200 MHz, 80–90 W).
- `nvidia-smi` shows no throttle reason.
- Everything runs at roughly a third of its usual speed, and model loads slow down too.

**Quick check.** Run `nvidia-smi --query-gpu=clocks.sm,power.draw --format=csv`
under load.

**Fix: a cold drain.** `sudo nvidia-smi -rgc` does not help.
1. Run `sudo poweroff`.
2. Unplug the PSU from the wall **and** from the box, and unplug every USB-C
   device.
3. Hold the power button for 30 s.
4. Wait 1–5 minutes, plug the PSU back in, and power on.

The community attributes the wedge to the USB-C PD controller firmware. The
reference box had PD 0x00000500, and an earlier `fwupd` update of it had
failed.

**If the box holds your data on a USB drive:** plug it back in **before**
powering on. With `nofail,x-systemd.automount` in fstab the box boots without
it, but every container with a bind mount on it exits (code 128). Recover with
`docker start` on the restart-policy containers once the drive is back.

## 3. Firmware update (ASUS GX10)

**Method.**
- ASUS documents an offline method: download the BIOS/firmware package from
  the GX10 support page, unzip it, and run `capsule_update.sh <file>.cap` once
  per component.
- The script copies the signed FMP capsule to `/boot/efi/EFI/UpdateCapsule`,
  sets `OsIndications` bit `0x4` (file capsule delivery) and reboots. UEFI
  flashes the capsule on the way up.
- This is the same capsule-on-disk path the DGX Dashboard and `fwupdmgr` use.

**Before you start:**
- verify the package SHA-256 against the ASUS page;
- check that `OsIndicationsSupported` has bit `0x4`;
- check that the ESP has room;
- stop the LLM;
- disable any auto-start.

**Order (ASUS):** SoC (includes BIOS) → EC → PD → PD again → TPM, then a cold
drain so the new PD firmware loads.

| Component | Reference box, before | After the v0106 package |
|---|---|---|
| BIOS | 0103 | 0106 |
| SoC | 0x03000005 | 0x03000008 (flash ~8.5 min, do not power off) |
| EC | 0x02000004 | 0x02000007 (3.5.11.4) |
| PD | 0x00000500 | 0x00000516 (5.22) |
| TPM | — | 7.2.4.1 (the OS sees no TPM chip on this box) |

The new firmware did not cool the box by itself: uncapped matmul drew more
power (85 → 93 W) because it clocks higher. The clock cap is what brought the
temperature down.

## 4. Unified memory: big checkpoints can livelock the host

**Symptom.**
- A model load fills `MemAvailable` to zero.
- Load average climbs past 500, SSH times out at banner exchange, and
  `docker ps` hangs.
- The kernel thrashes in reclaim instead of OOM-killing, and swap only makes it
  slower.
- Recover from the local console with `sudo pkill -9 -f '[vV][lL][lL][mM]'`.
  The bracket pattern keeps `pkill` from matching its own `sudo` line.

**Cause seen with Qwen3.5-122B NVFP4.**
- The checkpoint ships as 2 × 35 GB shards.
- vLLM keeps each safetensors file mapped until all its tensors are loaded, so
  the engine's anonymous RSS grows to the shard size. That is 35 GB on top of
  73 GB of GPU weights.
- glibc malloc tuning did not help. Resharding did:
  [`scripts/reshard-safetensors.py`](../../scripts/reshard-safetensors.py)
  rewrites the checkpoint into 2 GB shards at byte level, so any dtype works.
  The peak dropped to 1–3 GB.

**Backstop: [`scripts/gb10-memguard.sh`](../../scripts/gb10-memguard.sh).** It
SIGKILLs the listed trial-model containers when `MemAvailable` stays under
6 GiB for two samples (2 s apart), while docker still answers.

**Keep the box reachable:**
[`scripts/gb10-resilience-setup.sh`](../../scripts/gb10-resilience-setup.sh)
(run once as root) reserves 1 GiB for the kernel, drops page cache before
swapping, enables the SBSA hardware watchdog and all SysRq keys, and keeps
`sshd` away from the OOM killer, so a full pool ends in an OOM kill or a
reboot instead of a hang.

## Sources

- [NVIDIA forum 377365 — PowerStress hard power-off](https://forums.developer.nvidia.com/t/dgx-spark-gb10-partnerdiag-powerstress-reproducibly-hard-powers-off-the-box-acpitz-88-97-8-c-in-5s-all-other-field-tests-pass/377365)
- [NVIDIA forum 368536 — clock-lock benchmarks](https://forums.developer.nvidia.com/t/dgx-spark-stability-out-of-ram-overheating/368536)
- [NVIDIA forum 377069 — EC rollback thread (Founders Edition)](https://forums.developer.nvidia.com/t/nvidia-dgx-spark-gb10-thermal-throttling-fan-curve-fix-via-ec-firmware-rollback/377069)
- [NVIDIA forum 386166 — GX10 capsule update hang](https://forums.developer.nvidia.com/t/asus-gx10-capsule-firmware-update-ec-0x02000007-soc-0x03000008-stuck-on-asus-logo-progress-bar-for-6-hours/386166)
- [ASUS GX10 BIOS & firmware](https://www.asus.com/networking-iot-servers/desktop-ai-supercomputer/ultra-small-ai-supercomputers/asus-ascent-gx10/helpdesk_bios?model2Name=ASUS-Ascent-GX10)
- [ASUS FAQ 1056213 — How to update the GX10](https://www.asus.com/support/faq/1056213/)
- [Sggin1/DGX-SPARK — GX10 PD throttle fix](https://github.com/Sggin1/DGX-SPARK/blob/main/GX10_PD_Throttle_Fix.md)
- [maci0/gb10-thermal-toolkit](https://github.com/maci0/gb10-thermal-toolkit)
