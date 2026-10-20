#!/bin/bash
# usage: diff.sh <nameA> <nameB>
docker run --rm -v $HOME/pc-harness:/h --entrypoint node openclaw-base-ext:candidate-2026.10.4 /h/diff.mjs "/h/$1" "/h/$2"
