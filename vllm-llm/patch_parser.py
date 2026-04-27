"""Patch the vLLM Gemma4 tool-call parser regex to accept colon namespaces.

Upstream `gemma4_tool_parser.py:338` defines the tool-call extraction regex as:

    re.compile(r"<\\|tool_call>call:([\\w\\-\\.]+)\\{(.*?)\\}<tool_call\\|>", re.DOTALL)

The character class `[\\w\\-\\.]` allows word chars (letters/digits/underscore),
hyphens, and dots — but NOT colons. The OpenClaw Discord plugin in 2026.4.x
publishes its agent-facing tools with colon namespaces (`discord:add_reaction`),
unlike every other plugin which uses `__` separators. The colon stops the regex
from capturing past the second colon, so every reaction tool-call from Gemma 4
becomes literal envelope text in the model's content field instead of an
actual structured tool call.

This script swaps the regex to `[\\w\\-\\.:]+`, which allows the colon
character. The change is idempotent (str.replace finds and replaces exactly
the upstream literal). The assertion guards against future upstream changes:
if the line shape changes, the build fails loudly rather than silently
shipping a broken image — the operator must then re-derive the patch.

Run during Docker image build (see ../Dockerfile):

    COPY patch_parser.py /tmp/patch_parser.py
    RUN python3 /tmp/patch_parser.py
"""

import sys

PARSER_PATH = "/usr/local/lib/python3.12/dist-packages/vllm/tool_parsers/gemma4_tool_parser.py"

OLD = r'<\|tool_call>call:([\w\-\.]+)\{(.*?)\}<tool_call\|>'
NEW = r'<\|tool_call>call:([\w\-\.:]+)\{(.*?)\}<tool_call\|>'

with open(PARSER_PATH, "r", encoding="utf-8") as f:
    src = f.read()

if NEW in src:
    print("[gemma4-parser-patch] colon char-class already present — skipping (idempotent re-run).")
    sys.exit(0)

if OLD not in src:
    sys.stderr.write(
        "[gemma4-parser-patch] FATAL: upstream tool_call_regex literal not found at "
        f"{PARSER_PATH}. Upstream may have changed the regex shape; this patch's "
        "premise no longer holds. Inspect the file manually and rederive the "
        "char-class extension before continuing.\n"
    )
    sys.exit(1)

patched = src.replace(OLD, NEW, 1)

with open(PARSER_PATH, "w", encoding="utf-8") as f:
    f.write(patched)

print(f"[gemma4-parser-patch] colon char-class added to tool_call_regex at {PARSER_PATH}")
