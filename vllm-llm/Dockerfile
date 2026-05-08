# Custom vLLM image for this stack with two surgical patches over the upstream
# `vllm/vllm-openai:gemma4-cu130` base image:
#
#   1. patch_parser.py — extends the gemma4 tool-call parser regex (line 338
#      of gemma4_tool_parser.py) so it accepts colon namespaces. Upstream
#      `[\w\-\.]+` rejects colons; the OpenClaw Discord plugin uses
#      `discord:add_reaction` (colon-namespaced), and without this patch every
#      reaction tool-call leaks into Discord as literal envelope text.
#
#   2. patch_gemma4_loader.py — fills in the NVFP4 expert scale-suffix matcher
#      in `vllm/model_executor/models/gemma4.py`'s Gemma4Model.load_weights.
#      This mirrors upstream PR #39045 (merged 2026-04-09 as commit 3aecdf08).
#      The base image's `gemma4-cu130` tag manifest predates the fix on some
#      pulls, leaving the loader's expert_params_mapping unable to handle
#      `.input_scale` / `.input_global_scale` / `.weight_scale` suffixes — both
#      `nvidia/Gemma-4-26B-A4B-NVFP4` and `RedHatAI/gemma-4-26B-A4B-it-NVFP4`
#      crash on boot with `KeyError: 'layers.0.experts.0.down_proj.input_scale'`.
#      See vllm-project/vllm#38912 for the bug, #39045 for the fix.
#
# Both patches are idempotent (str.replace) and assert their OLD shapes
# pre-application, so any future upstream change forces a build-time failure
# here rather than silently shipping a broken image — the operator must
# either re-derive the patch or retire it (when the base image manifest
# already contains the upstream fix).
#
# Build context: ./vllm-llm/ (this directory). Image name is set in
# docker-compose.yml's `vllm-llm.image` field, so the build step caches and
# the runtime always picks up the patched layer.
ARG VLLM_BASE_IMAGE=vllm/vllm-openai:gemma4-cu130
FROM ${VLLM_BASE_IMAGE}

COPY patch_parser.py /tmp/patch_parser.py
RUN python3 /tmp/patch_parser.py && rm /tmp/patch_parser.py

COPY patch_gemma4_loader.py /tmp/patch_gemma4_loader.py
RUN python3 /tmp/patch_gemma4_loader.py && rm /tmp/patch_gemma4_loader.py
