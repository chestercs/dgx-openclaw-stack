#!/bin/sh
# Starts strata-dsv41 with an expert budget sized from MemAvailable instead of MemFree.
#
# The engine fills its GPU expert tier from cudaMemGetInfo's free bytes, which on the GB10 is
# MemFree: the page cache (the Engram rows, the model files read while loading) counts as used,
# and cudaMalloc does not reclaim it. Measured: 85 GB of experts placed with 22 GB still
# available, 78.5 GB on a warm restart. So:
#   1. the model files' clean cached pages are dropped (posix_fadvise DONTNEED, no root needed),
#      once before sizing and then every second while the engine loads;
#   2. --vram-experts scales the engine's budget to MemAvailable minus UMA_MARGIN_GB, which
#      leaves room for the dense weights, the KV cache, the prompt buffers and memguard's 6 GB
#      floor. The engine reads free memory later than this, so the budget only comes out lower.
# The CPU lane reads its experts back into the page cache afterwards.
# Engine 0.1.53 (Strata 0.2.7.1) takes max(MemFree, MemAvailable - DS4_UMA_HEADROOM_GIB) itself: a
# floor, not a cap. After the drop MemFree is nearly all of MemAvailable, so without the factor it
# placed ~100 GB of experts beside the stack and memguard killed it at 0.3 GB available, with the
# headroom at 16 or 30 GB alike.
set -eu
MARGIN_GB=${UMA_MARGIN_GB:-14}
RESERVE_MIB=${UMA_RESERVE_MIB:-1000}
DROP_DIR=${UMA_DROP_DIR:-}
ENGINE=${STRATA_ENGINE:-/strata/engine-dsv41/strata-dsv41}

drop() {   # drop(seconds): 0 = one pass
  python3 - "$DROP_DIR" "$1" <<'EOF'
import os, sys, time
d, until = sys.argv[1], time.time() + float(sys.argv[2])
files = [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".gguf")]
while True:
    for p in files:
        fd = os.open(p, os.O_RDONLY)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        os.close(fd)
    if time.time() >= until:
        break
    time.sleep(1)
EOF
}

if [ -n "$DROP_DIR" ]; then
  drop 0
fi
factor=$(awk -v m="$MARGIN_GB" -v r="$RESERVE_MIB" '
  /^MemAvailable:/ {a = $2 / 1024} /^MemFree:/ {f = $2 / 1024}
  END {
    x = (f - r > 0) ? (a - m * 1024) / (f - r) : 0.1
    if (x < 0.1) x = 0.1
    if (x > 1) x = 1
    printf "%.3f", x
  }' /proc/meminfo)
echo "ds4-uma: expert budget MemAvailable - ${MARGIN_GB} GB: --vram-experts $factor" >&2
if [ -n "$DROP_DIR" ]; then
  (drop "${UMA_DROP_SECONDS:-180}"; echo "ds4-uma: page-cache drop finished" >&2) &
fi
exec "$ENGINE" "$@" --reserve-mib "$RESERVE_MIB" --vram-experts "$factor"
