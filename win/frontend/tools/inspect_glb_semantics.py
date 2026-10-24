#!/usr/bin/env python3
"""GLB 语义信息正式检查：骨骼/Skin 权重/Mesh-Node 名称与层级/材质分组。

输出 JSON 结论（stdout），供 anatomy-labels.json 的 provenance 与回归测试引用：
- 若存在骨骼 → 报告 joints 列表（可按骨骼自动识别部位）
- 若无骨骼 → 明确 "boneless"，自动分区须走几何路线
用法：cd client && ./.venv/bin/python tools/inspect_glb_semantics.py [glb路径]
"""
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_glb(path):
    with open(path, "rb") as f:
        magic, version, total = struct.unpack("<III", f.read(12))
        assert magic == 0x46546C67, "not glTF-Binary"
        clen, ctype = struct.unpack("<II", f.read(8))
        assert ctype == 0x4E4F534A, "JSON chunk missing"
        gltf = json.loads(f.read(clen).decode("utf-8"))
        blen, btype = struct.unpack("<II", f.read(8))
        bin_data = f.read(blen) if btype == 0x004E4942 else b""
    return gltf, bin_data


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "web", "vendor", "models", "Custom3d66.glb")
    gltf, bin_data = read_glb(path)
    out = {"file": os.path.basename(path), "size_bytes": os.path.getsize(path)}

    # —— 骨骼 / Skin ——
    skins = gltf.get("skins", [])
    out["skins"] = len(skins)
    out["animations"] = len(gltf.get("animations", []))
    bones = []
    for s in skins:
        joints = s.get("joints", [])
        names = [gltf["nodes"][j].get("name", f"node#{j}") for j in joints]
        bones.extend(names)
    out["bones"] = bones if bones else None
    out["has_skeleton"] = bool(bones)

    # —— Node 层级与命名 ——
    nodes = gltf.get("nodes", [])
    out["node_count"] = len(nodes)
    named_nodes = [n.get("name") for n in nodes if n.get("name")]
    out["named_nodes_sample"] = named_nodes[:12]
    # 语义名称检测：常见解剖词
    anatomical_words = ["eye", "hand", "finger", "thumb", "index", "middle", "ring", "pinky",
                        "little", "toe", "foot", "head", "nose", "ear", "mouth", "arm", "leg",
                        "shoulder", "elbow", "wrist", "knee", "ankle", "hip", "chest", "torso"]
    semantic_hits = sorted({w.strip() for n in named_nodes for w in anatomical_words
                            if w.strip() in n.lower()})
    out["semantic_node_names"] = semantic_hits if semantic_hits else None

    # —— Mesh 清单（名称 + 材质 + 数量） ——
    meshes = []
    for i, m in enumerate(gltf.get("meshes", [])):
        prims = m.get("primitives", [])
        mats = sorted({gltf.get("materials", [{}])[p.get("material", 0)].get("name", f"mat#{p.get('material', 0)}")
                       for p in prims if "material" in p})
        meshes.append({"index": i, "name": m.get("name", f"mesh#{i}"),
                       "primitives": len(prims), "materials": mats})
    out["meshes"] = meshes

    # —— 材质分组 ——
    out["materials"] = [m.get("name", f"mat#{i}") for i, m in enumerate(gltf.get("materials", []))]

    # —— 结论 ——
    if out["has_skeleton"]:
        out["verdict"] = "有骨骼：可按 joints 自动识别部位"
    elif semantic_hits:
        out["verdict"] = "无骨骼但有语义节点命名：可按 node/mesh 名称映射"
    else:
        out["verdict"] = ("无骨骼、无解剖语义命名（mesh 为流水号自动编号）→ 几何自动分区 + 少量抽查校准；"
                          "眼球/虹膜等独立 mesh 仅能以「独立小 mesh + 头部前端位置」间接佐证")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
