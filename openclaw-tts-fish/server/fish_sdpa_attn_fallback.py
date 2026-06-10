# SPDX-License-Identifier: MIT
"""Pure-torch SDPA replacement for sgl_kernel.flash_attn.flash_attn_with_kvcache.

Why this exists: the Fish S2 Pro audio-decoder model code
(sglang_omni/.../fish_speech/models/text2semantic/modeling.py) imports
FlashAttention-3 DIRECTLY from sgl_kernel — bypassing sglang's
attention_backend abstraction entirely. FA3 ships Hopper-only (sm_90) SASS
with no PTX, so on GB10 (sm_121) the first synthesis dies with "no kernel
image is available for execution on the device". The Dockerfile sed-swaps the
single import in modeling.py to this module.

Semantics mirrored from flash_attn_with_kvcache (the subset modeling.py uses):
  q:                (batch, seqlen_q, n_heads, head_dim)
  k_cache/v_cache:  (batch, max_seqlen, n_kv_heads, head_dim) — preallocated
                    buffers, UPDATED IN PLACE when k/v are passed
  k/v:              (batch, seqlen_new, n_kv_heads, head_dim) — appended into
                    the caches at [cache_seqlens : cache_seqlens+seqlen_new]
  cache_seqlens:    (batch,) int — cache fill levels BEFORE the append
  causal:           flash uses BOTTOM-RIGHT alignment: query row j attends to
                    cache positions [0 : total_len - seqlen_q + j + 1). For
                    seqlen_q == 1 (the AR decode hot path) that's simply "all
                    of the cache", no mask needed.
  num_splits:       performance hint in FA — ignored here.

Performance: the decoder is a small transformer with batch <= 4 and a 4096
cap; SDPA over bf16 is plenty for real-time TTS. The per-batch-row Python
loop keeps the in-place cache semantics simple and is amortized by the
kernel launch cost at these sizes.
"""
from __future__ import annotations

import torch
import torch.nn.functional as F


def flash_attn_with_kvcache(
    q: torch.Tensor,
    k_cache: torch.Tensor,
    v_cache: torch.Tensor,
    k: torch.Tensor | None = None,
    v: torch.Tensor | None = None,
    cache_seqlens: torch.Tensor | None = None,
    causal: bool = False,
    num_splits: int = 0,
) -> torch.Tensor:
    b, sq, nh, _ = q.shape

    if cache_seqlens is None:
        lens = torch.full((b,), k_cache.shape[1], dtype=torch.long, device=q.device)
    else:
        lens = cache_seqlens.to(torch.long)
        if lens.ndim == 0:
            lens = lens.expand(b).clone()

    if k is not None:
        sn = k.shape[1]
        for i in range(b):
            s = int(lens[i])
            k_cache[i, s : s + sn] = k[i].to(k_cache.dtype)
            v_cache[i, s : s + sn] = v[i].to(v_cache.dtype)
        total = lens + sn
    else:
        total = lens

    rep = nh // k_cache.shape[2]
    out = torch.empty_like(q)
    for i in range(b):
        L = int(total[i])
        qi = q[i].permute(1, 0, 2)            # (nh, sq, d)
        ki = k_cache[i, :L].permute(1, 0, 2)  # (nkv, L, d)
        vi = v_cache[i, :L].permute(1, 0, 2)
        if rep > 1:  # GQA: expand kv heads to query heads
            ki = ki.repeat_interleave(rep, dim=0)
            vi = vi.repeat_interleave(rep, dim=0)
        ki = ki.to(qi.dtype)
        vi = vi.to(qi.dtype)
        if causal and sq > 1:
            # Bottom-right-aligned causal mask (True = attend).
            kpos = torch.arange(L, device=q.device)
            qpos = torch.arange(sq, device=q.device)
            mask = kpos.unsqueeze(0) <= (L - sq + qpos).unsqueeze(1)
            oi = F.scaled_dot_product_attention(qi, ki, vi, attn_mask=mask)
        else:
            # seqlen_q == 1 decode (or non-causal): full attention over cache.
            oi = F.scaled_dot_product_attention(qi, ki, vi)
        out[i] = oi.permute(1, 0, 2)
    return out
