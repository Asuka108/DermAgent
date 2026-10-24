#!/usr/bin/env python3
"""GLB 贴图瘦身：保持几何不动，把内嵌贴图降采样后重新打包（体积 → 适合桌面端加载）。

用法：.venv/bin/python tools/optimize_glb_textures.py <in.glb> <out.glb> [--max 1024] [--jpeg-quality 80]

实现：glTF-Binary 结构级重排——
  1. 逐 image 取 bufferView 字节 → 临时文件 → macOS 自带 `sips` 降采样（PNG 保 alpha 通道，
     发片/睫毛等 alpha 混合贴图不损失透明度；JPEG 转指定质量 JPEG）；
  2. 重建二进制 buffer：按原 bufferView 顺序重排——非贴图 view 原样拷贝，贴图 view 换成
     新字节（4 字节对齐），同步改写 JSON 的 bufferView.byteOffset/byteLength 与 image.mimeType；
  3. 不改 mesh/primitive/accessor/材质参数，几何与拓扑逐字节不变。
"""
import argparse
import json
import os
import struct
import subprocess
import tempfile
from pathlib import Path


def parse_glb(path):
    with open(path, "rb") as f:
        magic, ver, length = struct.unpack("<III", f.read(12))
        if magic != 0x46546C67:
            raise SystemExit(f"不是 glb：{path}")
        clen, ctype = struct.unpack("<II", f.read(8))
        js = json.loads(f.read(clen))
        bin_len = struct.unpack("<I", f.read(4))[0]
        f.read(4)  # BIN chunk type
        blob = f.read(bin_len)
    return js, blob


def write_glb(path, js, blob):
    js_bin = json.dumps(js, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    js_bin += b" " * (-len(js_bin) % 4)
    blob += b"\x00" * (-len(blob) % 4)
    total = 12 + 8 + len(js_bin) + 8 + len(blob)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js_bin), 0x4E4F534A))
        f.write(js_bin)
        f.write(struct.pack("<II", len(blob), 0x004E4942))
        f.write(blob)


def resize_with_sips(src: Path, dst: Path, max_px: int, mime: str, quality: int) -> str:
    """PNG（含 alpha，保透明）原格式缩；JPEG 转指定质量 JPEG。"""
    is_png = mime == "image/png"
    cmd = ["sips", "-Z", str(max_px), "-s", "format", "png" if is_png else "jpeg"]
    if not is_png:
        cmd += ["-s", "formatOptions", str(quality)]
    cmd += [str(src), "--out", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise SystemExit(f"sips 失败：{src.name}\n{r.stderr}")
    return "image/png" if is_png else "image/jpeg"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--max", type=int, default=1024)
    ap.add_argument("--jpeg-quality", type=int, default=80)
    args = ap.parse_args()

    js, blob = parse_glb(args.src)
    views = js.get("bufferViews", [])
    images = js.get("images", [])
    if not images:
        raise SystemExit("GLB 无内嵌贴图，无需瘦身")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # 1) 逐贴图缩放：bufferView 索引 → 新字节
        resized = {}   # view_index -> bytes
        for ii, im in enumerate(images):
            vi = im.get("bufferView")
            if vi is None:
                print(f"  image {ii} 外部 uri 引用，跳过")
                continue
            bv = views[vi]
            off = bv.get("byteOffset", 0)
            old = blob[off:off + bv["byteLength"]]
            ext = "png" if im.get("mimeType") == "image/png" else "jpg"
            src_f = tdp / f"img{ii}.{ext}"
            dst_f = tdp / f"out{ii}.{ext}"
            src_f.write_bytes(old)
            mime = resize_with_sips(src_f, dst_f, args.max, im.get("mimeType", "image/jpeg"), args.jpeg_quality)
            resized[vi] = dst_f.read_bytes()
            im["mimeType"] = mime
            print(f"  image {ii} {im.get('name','')}: {len(old)/1e6:.1f}MB → {len(resized[vi])/1e6:.2f}MB")

        # 2) 按原 view 顺序重排二进制（4 字节对齐），并生成新 bufferView 描述
        new_blob = b""
        out_views = []
        for vi, bv in enumerate(views):
            data = resized.get(vi, blob[bv.get("byteOffset", 0):bv.get("byteOffset", 0) + bv["byteLength"]])
            pad = b"\x00" * (-len(new_blob) % 4)
            new_blob += pad + data
            nv = {k: v for k, v in bv.items() if k != "byteOffset"}
            nv["byteOffset"] = len(new_blob) - len(data)
            nv["byteLength"] = len(data)
            out_views.append(nv)
        js["bufferViews"] = out_views
        js["buffers"][0]["byteLength"] = len(new_blob)

    write_glb(args.dst, js, new_blob)
    print(f"完成：{os.path.getsize(args.src)/1e6:.1f}MB → {os.path.getsize(args.dst)/1e6:.1f}MB → {args.dst}")


if __name__ == "__main__":
    main()
