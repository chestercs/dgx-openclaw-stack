#!/bin/bash
# usage: run.sh <name> [patcher-file]   (no file = the patcher in the stack checkout)
# Copies only what the patcher reads/writes (config, workspaces, bin, agent dirs
# without databases) from the live state and runs a patcher on the copy.
# EVERY host dir the config-init service mounts is redirected into the copy:
# the main workspace, canvas, media and sandbox are separate mounts, and a
# missed one lets the test write to the live state.
set -eu
NAME=$1; SRC=${2:-}
STACK=/home/chestercs/Docker/dgx-openclaw-stack
LIVE=$(grep '^OPENCLAW_CONFIG_DIR=' $STACK/.env | cut -d= -f2 | tr -d '"')
OUT=$HOME/pc-harness/$NAME
[ -d "$OUT" ] && docker run --rm -v $HOME/pc-harness:/h alpine rm -rf "/h/$NAME"
mkdir -p "$OUT/state/canvas" "$OUT/state/media/inbound" "$OUT/state/sandbox"
rsync -a --prune-empty-dirs \
  --include 'openclaw.json' --include 'bin/***' --include 'canvas/' \
  --include 'workspace*/' --include 'workspace*/**' --exclude 'workspace/sandbox/**' \
  --include 'agents/' --include 'agents/*/' --include 'agents/*/agent/' --include 'agents/*/agent/*.json' --include 'agents/*/agent/*.md' \
  --exclude '*.sqlite*' --exclude '*' "$LIVE/" "$OUT/state/"
cd $STACK
if [ -z "$SRC" ]; then
  ISO="OPENCLAW_CONFIG_DIR=$OUT/state OPENCLAW_WORKSPACE_DIR=$OUT/state/workspace OPENCLAW_CANVAS_DIR=$OUT/state/canvas OPENCLAW_MEDIA_INBOUND_DIR=$OUT/state/media/inbound OPENCLAW_SANDBOX_DIR=$OUT/state/sandbox"
  env $ISO docker compose run --rm --no-deps -T openclaw-config-init > "$OUT/log.txt" 2>&1 || echo "patcher exit $?"
else
  d=$(dirname "$SRC"); f=$(basename "$SRC")
  ISO="OPENCLAW_CONFIG_DIR=$OUT/state OPENCLAW_WORKSPACE_DIR=$OUT/state/workspace OPENCLAW_CANVAS_DIR=$OUT/state/canvas OPENCLAW_MEDIA_INBOUND_DIR=$OUT/state/media/inbound OPENCLAW_SANDBOX_DIR=$OUT/state/sandbox"
  env $ISO docker compose run --rm --no-deps -T -v "$d:/opt/newpatcher:ro" --entrypoint node openclaw-config-init "/opt/newpatcher/$f" > "$OUT/log.txt" 2>&1 || echo "patcher exit $?"
fi
echo "done: $OUT $(du -sh $OUT | cut -f1) ($(grep -c '\[patch-config\]' "$OUT/log.txt") log lines)"
