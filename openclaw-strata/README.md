# Strata: DeepSeek-V4.1-Flash on one GB10

`compose/strata.yml` runs DeepSeek-V4.1-Flash (284B MoE) on [Strata](https://github.com/Niko1221/Strata)'s
own engine, `strata-dsv41` (MIT). It uses ISTA-DASLab's GSQ-RCO IQ1_M quantisation (~1.75 bits per weight, 121
GB plus a 111 GB Engram table). Strata keeps the frequently routed experts on the GPU and computes the rest on
the CPU, straight from the files. It serves OpenAI- and Anthropic-compatible APIs, so the same server can be:

- OpenClaw's chat model (`compose/strata-llm.yml`, instead of the Nemotron vLLM);
- the backend of Claude Code (`compose/strata-router.yml`, see
  [`openclaw-strata-router/README.md`](../openclaw-strata-router/README.md)).

It serves **one request at a time** ([Niko1221/Strata#4365](https://github.com/Niko1221/Strata/issues/4365)
asks for more); everyone on the server queues, but a resumed conversation reads only its new tokens.

## Measured (GB10, 2026-10-27/28)

| | Strata alone | Beside the OpenClaw stack |
|---|---|---|
| Experts on the GPU | 12,326 / 15,744 | 10,860 / 15,744 |
| Expert hits served by the CPU | | ~0.5% |
| Python code (DSpark) | 32–34 tok/s | 20–33 tok/s |
| Prose | 17–18 tok/s | 15–17 tok/s |
| 25K-token prompt read | 516 tok/s | 449 tok/s |
| MemAvailable while loaded | ~11 GB | ~12.5 GB |
| Load time | ~1–2 min | ~1–2 min |

- DSpark (speculative decoding with the pack's drafter): ~87% of drafts accepted on code and tool JSON, so code
  decodes at nearly twice the speed; no gain on prose. Its 7.2 GB of drafter experts push ~650 main-model
  experts to the CPU, and prompt reading is ~7% slower.
- Quality, from Strata's own measurements of this quant: +9–10% perplexity on code and agent work, +53% on
  English prose; ISTA's model card reports 98.7% of the benchmark average kept. Hungarian answers were fine in
  our tests.
- On a GB10 the engine uses 5 of the 6 routed experts by default ("Faster" mode); `--experts 6` (via
  `STRATA_EXTRA_ARGS`) is full quality, not measured here.

## Build the engine (aarch64, sm_121)

There is no ready-made ARM package. Build from source on the host (~15 min, with DGX OS's CUDA 13.0). The patch
(against Strata 5e8a4cc, release 0.2.7) fixes three x86-only spots: a constant defined only in an x86 block, a
CPUID branch, and missing ARM stubs for the DeepSeek CPU lane's AVX kernels (ggml-cpu's NEON code runs there
instead).

```bash
git clone https://github.com/Niko1221/Strata.git ~/strata
cd ~/strata && git checkout 5e8a4cc && git apply ~/dgx-openclaw-stack/openclaw-strata/strata-aarch64.patch
export PATH=/usr/local/cuda-13.0/bin:$PATH
cmake -S . -B build-dsv41 -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON \
  -DSTRATA_BUILD_TESTS=OFF -DSTRATA_BUILD_DSV41=ON -DSTRATA_NATIVE_EXPERTS=ON \
  -DCMAKE_CUDA_ARCHITECTURES=121-real -DCMAKE_CUDA_COMPILER=/usr/local/cuda-13.0/bin/nvcc \
  -DCUDAToolkit_ROOT=/usr/local/cuda-13.0
cmake --build build-dsv41 --target strata-dsv41 -j 12
```

The container mounts this checkout read-only (`STRATA_SRC`) and the host CUDA toolkit (`STRATA_CUDA_DIR`); the
image only adds the server's Python, pinned by the checkout's `requirements.txt`.

## The model and the pack

Into one folder (`STRATA_MODEL_DIR`), from Hugging Face:

- `ISTA-DASLab/DeepSeek-V4.1-Flash-1017-GSQ-RCO-GGUF`: the four `IQ1_M` shards and
  `DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf`;
- `dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf` (the DSpark drafter, 8 GB, from Unsloth's
  `DeepSeek-V4.1-Flash-1017-GGUF`);
- for `--engram-meta`, the first shard of any quant in Unsloth's `DeepSeek-V4.1-Flash-1017-GGUF` (only its
  5.8 MB of metadata is read; it can be deleted afterwards).

The pack is an index into those files and copies no weights (8.5 MB, under a second):

```bash
M=~/models/DeepSeek-V4.1-Flash-1017-GSQ-RCO
cd ~/strata && python3 tools/dsv41/pack_gguf.py --model $M \
  --shards DeepSeek-V4.1-Flash-1017-GSQ-RCO-IQ1_M-0000{1,2,3,4}-of-00004.gguf \
  --engram $M/DeepSeek-V4.1-Flash-Engram-IQ4_NL.gguf \
  --engram-meta <Unsloth first shard> \
  --dspark-gguf $M/dspark-DeepSeek-V4.1-Flash-1017-Q8_0.gguf \
  --dspark-noise-token 128799 --dspark-markov-rank 256 --out $M/strata-pack-dspark
cp data/dsv41/profile-2.00.bin $M/strata-pack-dspark/expert-profile.bin
```

The pack stores absolute paths, so the container mounts the model folder at its host path.

## Run

```bash
# .env
COMPOSE_FILE=docker-compose.yml:compose/strata.yml           # + compose/strata-llm.yml, compose/strata-router.yml
STRATA_SRC=/home/<you>/strata
STRATA_MODEL_DIR=/home/<you>/models/DeepSeek-V4.1-Flash-1017-GSQ-RCO
STRATA_API_KEY=<openssl rand -hex 24>
STRATA_DATA_DIR=/home/<you>/strata-data                      # parked conversation slots (~2 GB) and the engine log

docker compose build strata
docker compose up -d strata         # naming it turns its profile on; loads in 1-2 min, healthy once loaded
```

It has a profile and no restart policy on purpose: it sizes its GPU expert tier from the memory free when it
starts, so it has to start after the rest of the stack (two big GPU users sizing memory at once can livelock
the box). [`scripts/llm-ensure.sh`](../scripts/llm-ensure.sh) does that from cron: at boot it waits until the
gateway is healthy and no container is still loading, then starts Strata, and it brings Strata back after
`gb10-memguard` killed it (thermguard restarts what it killed itself). Until Strata has loaded, a few minutes
after boot, OpenClaw's chat turns fail. Add `strata` (with `CONTAINER_NAME_PREFIX`, if set) to the guards'
container lists (memguard `TARGETS`, thermguard `WATCH`); see
[docs/reference/gb10-power-thermal.md](../docs/reference/gb10-power-thermal.md). The container runs as root, so
`STRATA_DATA_DIR` fills with root-owned files.

Before each start `llm-ensure.sh` drops the clean page cache through
[`scripts/gb10-drop-caches.service`](../scripts/gb10-drop-caches.service), so the GPU tier is sized for the
memory really available. It matters most for a restart after hours of uptime (11,109 experts on the GPU
instead of 9,972). Right after boot the cache is small (9,949 -> 10,112); the rest of the gap there is that
nothing is swapped out yet, and Strata sizes itself only once, at start. Install that unit and its polkit
rule once, with sudo, as its header says; without them Strata still starts, with fewer GPU experts.

## Memory and heat

**Memory.** The engine sizes its GPU tier from CUDA's free memory, which on the GB10's unified pool is
MemFree: page cache counts as used and `cudaMalloc` does not reclaim it. Measured: 85 GB of experts placed with
22 GB still available. `ds4-uma.sh` starts the engine instead:

- it drops the model files' cached pages (`posix_fadvise(DONTNEED)`, no root needed), before sizing and every
  second while the engine loads;
- it sets `--vram-experts` to MemAvailable minus `STRATA_UMA_MARGIN_GB` (default 16, room for the stack's
  growth above memguard's 6 GB floor; 14 with Strata alone).

Only the hot experts matter: beside the stack ~0.5% of expert hits land on the CPU, so a smaller margin buys
almost no speed and risks a memguard kill.

**Heat.** The GPU clock cap does not cover the CPU, which shares the cooler. 18 CPU threads held acpitz at
87–92 °C for a 20-min agent turn until thermguard killed the engine at 93 °C. Defaults since: 10 threads
(`STRATA_CPU_THREADS`), pinned to the Cortex-A725 cores (`STRATA_CPUSET`). 15-min code generation beside the
stack (`soak.sh`):

| Cores | tok/s | acpitz max | last 5 min median |
|---|---|---|---|
| unpinned (threads land on 0–9, mixed) | 33.2 | 84 °C | 79 °C |
| Cortex-X925 only (5–9, 15–19) | 30.7 | 85 °C | 78 °C |
| **Cortex-A725 only (0–4, 10–14)** | **29.6** | **72 °C** | **70 °C** |

## Tools

Run on the Docker host from the stack folder; they read the key and port from `.env`.

| Script | What |
|---|---|
| `bench.sh` | the same five requests (code x2, prose x2, a 25K-token read) with prefill, decode and DSpark numbers |
| `soak.sh` | sustained generation for `DURATION` s with a per-second log of acpitz, GPU, clocks and load per core type; cuts at 89 °C |
| `watch.sh` | one line per finished request, plus memory, temperature, container and memguard alerts |

Knobs (`STRATA_*`): [docs/reference/configuration.md](../docs/reference/configuration.md).
