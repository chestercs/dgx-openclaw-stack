#!/usr/bin/env bash
# Runs scripts/discord-eval.mjs inside the openclaw-cli container and keeps the
# report on the host. Cases default to the private set (real Discord history,
# never committed); evals/discord/examples.yaml is the public sample.
#
#   scripts/discord-eval.sh [--cases <file>] [--smoke] [--id <id>] [--runs N]
#                           [--concurrency N] [--rejudge <earlier report.json>]
set -euo pipefail
cd "$(dirname "$0")/.."
PROJ=$(grep -E '^CONTAINER_NAME_PREFIX=' .env 2>/dev/null | cut -d= -f2 || true)
CLI="${PROJ}openclaw-cli"
CASES=private/evals/discord/cases.yaml
PRIOR=
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --cases) CASES=$2; shift 2 ;;
    --rejudge) PRIOR=$2; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
OUT_DIR=private/evals/discord/runs
mkdir -p "$OUT_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker cp scripts/discord-eval.mjs "$CLI:/tmp/discord-eval.mjs"
docker cp "$CASES" "$CLI:/tmp/discord-eval-cases.yaml"
if [ -n "$PRIOR" ]; then
  docker cp "$PRIOR" "$CLI:/tmp/discord-eval-prior.json"
  ARGS+=(--rejudge /tmp/discord-eval-prior.json)
  STAMP="$STAMP-rejudge"
fi
docker exec "$CLI" node /tmp/discord-eval.mjs --cases /tmp/discord-eval-cases.yaml --out /tmp/discord-eval-report.json ${ARGS[@]+"${ARGS[@]}"}
docker cp "$CLI:/tmp/discord-eval-report.json" "$OUT_DIR/$STAMP.json"
docker exec "$CLI" rm -f /tmp/discord-eval.mjs /tmp/discord-eval-cases.yaml /tmp/discord-eval-report.json /tmp/discord-eval-prior.json
echo "report: $OUT_DIR/$STAMP.json"
