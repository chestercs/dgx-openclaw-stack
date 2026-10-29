#!/bin/sh
# Entry point of the strata container (compose/strata.yml): writes the server config from the environment
# and starts Strata's API server, which launches the engine through ds4-uma.sh.
#
# The model folder is mounted at its host path because the pack (tensors.txt) stores absolute file paths.
set -eu
: "${STRATA_MODEL_DIR:?set STRATA_MODEL_DIR}"
PACK=${STRATA_PACK:-$STRATA_MODEL_DIR/strata-pack-dspark}
CFG=/tmp/strata-dsv41.json
# the release's engine package (engine-dsv41/ in the checkout) or a build of its own
export STRATA_ENGINE=/strata/${STRATA_ENGINE:-engine-dsv41/strata-dsv41}

python3 - "$CFG" "$PACK" <<'EOF'
import json, os, shlex, sys
cfg, pack = sys.argv[1], sys.argv[2]
e = os.environ.get
args = ["--pack", pack,
        "--max-context", e("STRATA_MAX_CONTEXT", "131072"),
        "--eos", "1", "--vision", "off",
        # DSpark drafts with the pack's drafter: ~2x on code and tool JSON, no gain on prose
        "--dspark", e("STRATA_DSPARK", "auto"), "--dspark-conf", e("STRATA_DSPARK_CONF", "0.65"),
        "--prefill-chunk", e("STRATA_PREFILL_CHUNK", "16384"),
        # no RAM expert store: the CPU lane reads its experts in place, through the page cache, as with engine
        # 0.1.52. The engine's auto store takes the RAM free at load less 12 GB, sized for Strata alone.
        "--ssd-cache", "0",
        "--cpu-threads", e("STRATA_CPU_THREADS", "10")]
args += shlex.split(e("STRATA_EXTRA_ARGS", ""))
json.dump({
    "exe": "/opt/openclaw-strata/ds4-uma.sh",
    "args": args,
    "cwd": "/strata",
    "tokenizer": os.path.join(pack, "tokenizer"),
    "model_name": e("STRATA_MODEL_ID", "deepseek-v4.1-flash"),
    "arch": "deepseek41",
    "log": "/data/strata-dsv41.log",
    # the engine package's own CUDA libraries first, then the host toolkit mounted at its host path
    "lib_dirs": [os.path.dirname(e("STRATA_ENGINE")),
                 os.path.join(e("STRATA_CUDA_DIR", "/usr/local/cuda-13.0"), "lib64")],
    "port": 8095,
    "engine_ready_s": 3600,
    # Claude Code asks for thinking per request; OpenClaw sends enable_thinking through the chat template
    "anthropic_thinking": "on_request",
    "env": {"DS4_ASYNC_T": "4"},
}, open(cfg, "w"), indent=1)
EOF

export UMA_DROP_DIR=$STRATA_MODEL_DIR
cd /strata
exec python3 -m serve.server --engine strata --config "$CFG" --host 0.0.0.0 --port 8095
