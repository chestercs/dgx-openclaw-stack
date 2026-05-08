"""Patch the vLLM Gemma4 model loader to handle NVFP4 expert scale-key suffixes.

Bug: vllm-project/vllm#38912 — `vllm/model_executor/models/gemma4.py`'s
`expert_params_mapping` and the corresponding for-loop in `Gemma4Model.load_weights`
do not enumerate NVFP4 expert scale-key suffixes (`.input_scale`,
`.input_global_scale`, `.weight_scale`, `.weight_scale_2`). Both NVIDIA's
`Gemma-4-26B-A4B-NVFP4` and Red Hat AI's `gemma-4-26B-A4B-it-NVFP4` ship
those suffixed keys, and the loader walks past the expert-mapping branch
into the bare-name fallback, where `params_dict[name]` raises
`KeyError: 'layers.0.experts.0.down_proj.input_scale'` (or `.input_global_scale`
on the RedHatAI variant).

Upstream fix: PR #39045 (merged 2026-04-09 as commit 3aecdf08) restructures
both the `expert_params_mapping` literal (param_name as a `_`-suffixed prefix,
weight_name with a trailing dot) and the for-loop's match-branch (prefix
matching that handles "experts.X.proj.SCALE_SUFFIX" and bare "experts.X.proj"
in two separate branches, falling through to `continue` if neither matches).

Why a build-time str.replace patch instead of a wholesale file replace:
- The base image's `gemma4-cu130` tag manifest predates the upstream fix on
  some pulls, but everything else in the file (forward pass, vision tower,
  chat-template handling) is fine. Replacing the whole file risks pulling
  in unrelated upstream changes that may not be ABI-compatible with other
  vLLM modules pinned in the same image.
- The fix is contained to two well-bounded blocks (the mapping literal +
  the matching branch). Two str.replace calls cover it precisely.
- The patch is idempotent and asserts the OLD shape pre-application, so a
  future image bump that fixes this upstream forces a build-time failure
  here, prompting the operator to retire this patch rather than silently
  shipping a broken image.

Retire when: the base image's `gemma4-cu130` manifest digest changes to one
that already contains PR #39045's gemma4.py — the idempotency guard makes
the script a no-op once the new shape is detected, and the patch can then
be removed from the Dockerfile.

Run during Docker image build (see ../Dockerfile):

    COPY patch_gemma4_loader.py /tmp/patch_gemma4_loader.py
    RUN python3 /tmp/patch_gemma4_loader.py
"""

import sys

LOADER_PATH = "/usr/local/lib/python3.12/dist-packages/vllm/model_executor/models/gemma4.py"

# Block 1: expert_params_mapping literal — param_name strings drop "weight"
# suffix, weight_name format string gains a trailing dot. The change makes
# the mapping a *prefix* mapping rather than a base-name mapping, so a single
# replace on `weight_name` matches both bare weights and any scale-suffix.
OLD_MAPPING = '''        expert_params_mapping = [
            # (param_name, weight_name, expert_id, shard_id)
            (
                "experts.w13_weight"
                if proj_name in ["gate_proj", "up_proj"]
                else "experts.w2_weight",
                f"experts.{expert_id}.{proj_name}",
                expert_id,
                shard_id,
            )'''

NEW_MAPPING = '''        expert_params_mapping = [
            # (param_name, weight_name, expert_id, shard_id)
            (
                "experts.w13_"
                if proj_name in ["gate_proj", "up_proj"]
                else "experts.w2_",
                f"experts.{expert_id}.{proj_name}.",
                expert_id,
                shard_id,
            )'''

# Block 2: for-loop body — replace the simple "if not in name: continue ;
# moe_name = name.replace(...)" with the dual-branch matcher (suffix vs bare),
# drop the dim()==2 assertion (scale tensors are 1D / scalar), and pass
# `moe_name` to the weight_loader instead of `weight_name + ".weight"`.
OLD_LOOP = '''                ) in expert_params_mapping:
                    if weight_name not in name:
                        continue
                    moe_name = name.replace(weight_name, param_name)
                    if moe_name not in params_dict:
                        continue
                    if is_pp_missing_parameter(moe_name, self):
                        continue
                    param = params_dict[moe_name]
                    # Expert weights are already in the correct
                    # orientation for FusedMoE after _weight_iterator:
                    #   gate/up: [I, H] → w1/w3 expects [I, H]
                    #   down:    [H, I] → w2 expects [H, I]
                    assert loaded_weight.dim() == 2, (
                        f"Expected 2D expert weight for {weight_name}, "
                        f"got shape {loaded_weight.shape}"
                    )
                    weight_loader = param.weight_loader
                    weight_loader(
                        param,
                        loaded_weight,
                        weight_name + ".weight",
                        shard_id=shard_id,
                        expert_id=expert_id,
                    )'''

NEW_LOOP = '''                ) in expert_params_mapping:
                    # Match both:
                    #  - Bare weights: "experts.0.down_proj" (from 3D explosion)
                    #  - With suffix: "experts.0.down_proj.weight_scale" (2D quantized)
                    # weight_name has trailing dot, so check with and without it
                    weight_name_base = weight_name.rstrip(".")
                    if weight_name in name:
                        # Has suffix (e.g., .weight_scale)
                        moe_name = name.replace(weight_name, param_name)
                    elif name.endswith(weight_name_base):
                        # Bare weight (no suffix)
                        moe_name = name.replace(
                            weight_name_base, param_name.rstrip("_") + "_weight"
                        )
                    else:
                        continue
                    if moe_name not in params_dict:
                        continue
                    if is_pp_missing_parameter(moe_name, self):
                        continue
                    param = params_dict[moe_name]
                    # Expert weights are already in the correct
                    # orientation for FusedMoE after _weight_iterator:
                    #   gate/up: [I, H] → w1/w3 expects [I, H]
                    #   down:    [H, I] → w2 expects [H, I]
                    # Scales and other quantization params may be 1D or scalar.
                    weight_loader = param.weight_loader
                    weight_loader(
                        param,
                        loaded_weight,
                        moe_name,  # Pass mapped name (handles both weights and scales)
                        shard_id=shard_id,
                        expert_id=expert_id,
                    )'''


def main() -> int:
    with open(LOADER_PATH, "r", encoding="utf-8") as f:
        src = f.read()

    # Idempotency guard: the new for-loop introduces a `weight_name_base`
    # local variable that doesn't exist anywhere in the unpatched file.
    # If we see it, the file is already patched — skip cleanly.
    if "weight_name_base = weight_name.rstrip" in src:
        print("[gemma4-loader-patch] NVFP4 scale-suffix matcher already present — skipping (idempotent re-run).")
        return 0

    # Pre-apply assertions: both OLD blocks must be present verbatim.
    if OLD_MAPPING not in src:
        sys.stderr.write(
            "[gemma4-loader-patch] FATAL: OLD_MAPPING block not found at "
            f"{LOADER_PATH}. The expert_params_mapping literal does not match the "
            "expected pre-PR#39045 shape. Either upstream has shifted the literal "
            "in another way, or a different patch already touched it. Inspect the "
            "file manually and re-derive the patch before continuing.\n"
        )
        return 1

    if OLD_LOOP not in src:
        sys.stderr.write(
            "[gemma4-loader-patch] FATAL: OLD_LOOP block not found at "
            f"{LOADER_PATH}. The Gemma4Model.load_weights expert-branch does not "
            "match the expected pre-PR#39045 shape. Inspect the file and "
            "re-derive the patch before continuing.\n"
        )
        return 1

    patched = src.replace(OLD_MAPPING, NEW_MAPPING, 1).replace(OLD_LOOP, NEW_LOOP, 1)

    # Post-apply assertion: the new shape must now be present.
    if "weight_name_base = weight_name.rstrip" not in patched:
        sys.stderr.write(
            "[gemma4-loader-patch] FATAL: post-replace verification failed — the "
            "new matcher is not present. Build aborted to prevent shipping a "
            "broken image.\n"
        )
        return 1

    with open(LOADER_PATH, "w", encoding="utf-8") as f:
        f.write(patched)

    print(f"[gemma4-loader-patch] NVFP4 expert scale-suffix matcher applied at {LOADER_PATH}")
    print("[gemma4-loader-patch] (mirrors vllm-project/vllm PR #39045; retire this patch when the base image manifest picks up the fix upstream)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
