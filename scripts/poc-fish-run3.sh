#!/usr/bin/env bash
# PoC run 3 for openclaw-tts-fish:0.2.0-poc on GB10 — containment edition.
# Run 2 lesson: an uncontained engine start can wedge the WHOLE HOST
# (userspace freeze: sshd banner timeouts, gateway tunnel dark) while kernel
# ICMP stays healthy. Two containment layers so that can't recur:
#   --memory/--memory-swap 26g  hard cgroup cap, swap DENIED — the engine
#                               OOMs inside the container instead of pushing
#                               the host into swap thrash (vLLM survives).
#   --cpus 12                   leaves 8 cores for sshd / vLLM / gateway
#                               during the PTX JIT + warmup storm.
set -euo pipefail

docker rm -f tts-fish-poc >/dev/null 2>&1 || true

# Run-2 autopsy: the GB10 yaml DID apply (KV 2.85+2.85 GB, 13.6 GB avail
# after pool) and the host STILL livelocked at "Loading Fish audio decoder"
# with load average ~384 on 20 cores — a THREAD storm, not a memory squeeze.
# Hence the explicit thread caps below on top of the cpu quota.
docker run -d --name tts-fish-poc --gpus all --dns 8.8.8.8 \
  --memory 26g --memory-swap 26g --cpus 12 \
  -e OMP_NUM_THREADS=4 \
  -e OPENBLAS_NUM_THREADS=4 \
  -e MKL_NUM_THREADS=4 \
  -e NUMEXPR_NUM_THREADS=4 \
  -e TTS_API_TOKEN=pocsmoke123 \
  -e TTS_FISH_DEFAULT_VOICE=default_hu \
  -e FISH_S2PRO_CONFIG=/opt/configs/s2pro_tts_gb10.yaml \
  -e SGLANG_OMNI_STARTUP_TIMEOUT=3000 \
  -e FISH_ENGINE_READY_DEADLINE_S=3300 \
  -e CUDA_CACHE_MAXSIZE=4294967296 \
  -e CUDA_CACHE_PATH=/cuda-jit-cache/ComputeCache \
  -v /tmp/s2pro_tts_gb10.yaml:/opt/configs/s2pro_tts_gb10.yaml:ro \
  -v tts-fish-cuda-jit-cache:/cuda-jit-cache \
  -v dgx-openclaw-tts-fish-voices:/app/voices \
  -p 127.0.0.1:18094:8080 \
  openclaw-tts-fish:0.2.0-poc

echo "started; watch: docker logs -f tts-fish-poc"
