#!/bin/bash
# usage: IMAGE=<openclaw-base-ext tag> diff.sh <nameA> <nameB>
# Runs diff.mjs (copied next to this script in ~/pc-harness) in any image with node.
docker run --rm -v $HOME/pc-harness:/h --entrypoint node ${IMAGE:-openclaw-base-ext:local} /h/diff.mjs "/h/$1" "/h/$2"
