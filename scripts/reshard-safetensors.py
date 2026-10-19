#!/usr/bin/env python3
"""Rewrite a safetensors checkpoint into small shards (byte-level, no torch).

vLLM keeps each safetensors file mapped until every tensor in it is loaded, so
peak host memory during load grows with the shard size. On a unified-memory
box a 35 GB shard next to 73 GB of GPU weights does not fit. Small shards
bound that peak. Tensors are copied as raw byte ranges, so any dtype works
(NVFP4 uint8, float8 scales) and memory use stays at one tensor at a time.

usage: reshard-safetensors.py SRC_DIR DST_DIR [MAX_SHARD_GB]

Run it where the HF cache is mounted, e.g. inside the vLLM image:
  docker run --rm -v vllmHFCacheDir:/hf -v "$PWD/scripts:/w:ro" --entrypoint python3
    vllm/vllm-openai:v0.32.0 -I /w/reshard-safetensors.py
    /hf/hub/models--ORG--NAME/snapshots/<rev> /hf/resharded/NAME 2
(one command; line breaks added for reading).
then serve with --model /root/.cache/huggingface/resharded/NAME (keep --served-model-name).
"""
import json, os, shutil, struct, sys

src, dst = sys.argv[1], sys.argv[2]
max_bytes = int(float(sys.argv[3] if len(sys.argv) > 3 else 2) * 1024**3)
os.makedirs(dst, exist_ok=True)

def read_header(path):
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        hdr = json.loads(f.read(n))
    return hdr, 8 + n

files = sorted(p for p in os.listdir(src) if p.endswith(".safetensors"))
tensors = []  # (name, dtype, shape, src_path, abs_begin, nbytes)
metadata = {}
for fn in files:
    path = os.path.join(src, fn)
    hdr, base = read_header(path)
    metadata.update(hdr.pop("__metadata__", {}) or {})
    for name, t in hdr.items():
        b, e = t["data_offsets"]
        tensors.append((name, t["dtype"], t["shape"], path, base + b, e - b))

shards, cur, cur_size = [], [], 0
for t in tensors:
    if cur and cur_size + t[5] > max_bytes:
        shards.append(cur); cur, cur_size = [], 0
    cur.append(t); cur_size += t[5]
if cur:
    shards.append(cur)

weight_map, total = {}, 0
handles = {}
for i, shard in enumerate(shards, 1):
    out_name = f"model-{i:05d}-of-{len(shards):05d}.safetensors"
    hdr, off = {}, 0
    for name, dtype, shape, _, _, nbytes in shard:
        hdr[name] = {"dtype": dtype, "shape": shape, "data_offsets": [off, off + nbytes]}
        off += nbytes
        weight_map[name] = out_name
    if metadata:
        hdr["__metadata__"] = metadata
    hb = json.dumps(hdr, separators=(",", ":")).encode()
    hb += b" " * ((8 - len(hb) % 8) % 8)
    tmp = os.path.join(dst, out_name + ".part")
    with open(tmp, "wb") as out:
        out.write(struct.pack("<Q", len(hb)))
        out.write(hb)
        for _, _, _, path, begin, nbytes in shard:
            f = handles.get(path) or handles.setdefault(path, open(path, "rb"))
            f.seek(begin)
            left = nbytes
            while left:
                chunk = f.read(min(left, 64 * 1024 * 1024))
                out.write(chunk)
                left -= len(chunk)
    os.replace(tmp, os.path.join(dst, out_name))
    total += off
    print(f"{out_name} {off / 1024**3:.2f} GiB", flush=True)

with open(os.path.join(dst, "model.safetensors.index.json"), "w") as f:
    json.dump({"metadata": {"total_size": total}, "weight_map": weight_map}, f)

for fn in os.listdir(src):
    if fn.endswith(".safetensors") or fn == "model.safetensors.index.json":
        continue
    p = os.path.join(src, fn)
    if os.path.isfile(p):
        shutil.copyfile(p, os.path.join(dst, fn))
print(f"done: {len(tensors)} tensors, {len(shards)} shards, {total / 1024**3:.2f} GiB", flush=True)
