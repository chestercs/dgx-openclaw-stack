# Custom vLLM image for this stack with a 1-line patch to the Gemma4 tool-call
# parser regex (line 338 of gemma4_tool_parser.py). Upstream regex is
#   `<\|tool_call>call:([\w\-\.]+)\{(.*?)\}<tool_call\|>`
# — character class `[\w\-\.]` allows letters/digits/underscores/hyphens/dots
# but NOT colons. The OpenClaw Discord plugin in 2026.4.x publishes its
# agent-facing tools with colon namespaces (e.g. `discord:add_reaction`),
# unlike every other plugin which uses `__` separators. Without this patch,
# Gemma 4 NVFP4 emits the canonical `<|tool_call>call:discord:add_reaction{...}<tool_call|>`
# envelope, but the parser regex stops capturing at the second colon, fails to
# extract the call, and the literal envelope leaks into the model's content
# field — Discord chat shows garbage text instead of an actual reaction.
#
# Adding ':' to the char class (-> `[\w\-\.:]`) makes the parser accept colon
# namespaces transparently. The patch is idempotent (str.replace) and asserts
# the upstream pattern is present, so any future upstream regex change forces
# a manual review of this Dockerfile rather than silently shipping a broken
# image.
#
# Build context: ./vllm-llm/ (this directory). Image name is set in
# docker-compose.yml's `vllm-llm.image` field, so the build step caches and
# the runtime always picks up the patched layer.
ARG VLLM_BASE_IMAGE=vllm/vllm-openai:gemma4-cu130
FROM ${VLLM_BASE_IMAGE}

COPY patch_parser.py /tmp/patch_parser.py
RUN python3 /tmp/patch_parser.py && rm /tmp/patch_parser.py
