#!/usr/bin/env python3
"""组装女性模型解剖锚点表：data/female_anchors.json（聚类产物）+ 手指身份判定
→ web/anatomy-labels-Female3d66.json（与男性 anatomy-labels.json 同 schema）。

手指身份判定依据（解剖学，非猜测）：A-pose 掌心朝体（朝中线），四指沿 z 从前（桡侧/食指）
到后（尺侧/小指）排列；拇指独立成簇且与食指间距最大（40mm vs 指间 24mm）。
z_band = 相邻锚点 z 中点分割（与男性表同法）；tip_x = 簇内 |x| 最大（指尖）。

用法：cd client && ./.venv/bin/python tools/build_female_labels.py
"""

import hashlib
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
GLB = os.path.join(HERE, "web", "vendor", "models", "Female3d66.glb")
OUT = os.path.join(HERE, "web", "anatomy-labels-Female3d66.json")

MESH = {   # 材质名 → 运行时 mesh 名（probe 实测）
    "eye": "Obj3d66-853441-4-467_*", "lips": "Obj3d66-853441-4-467_9",
    "face": "Obj3d66-853441-4-467_5", "nails": "Obj3d66-853441-4-467_6",
    "toenails": "Obj3d66-853441-4-467_14", "ears": "Obj3d66-853441-4-467_3",
    "torso": "Obj3d66-853441-4-467_15",
}

# 左手 5 簇（measure_female_anchors.py 输出，身份判定见文件头注释）
LEFT_HAND = {
    "thumb":  {"xyz": [1.4946, 0.2671, 0.2486], "tip_x": 1.511, "z_core": [0.233, 0.261]},
    "index":  {"xyz": [1.7026, 0.1677, 0.1348], "tip_x": 1.711, "z_core": [0.121, 0.148]},
    "middle": {"xyz": [1.7358, 0.1454, 0.0671], "tip_x": 1.744, "z_core": [0.053, 0.081]},
    "ring":   {"xyz": [1.7106, 0.1672, -0.0083], "tip_x": 1.719, "z_core": [-0.021, 0.005]},
    "little": {"xyz": [1.6534, 0.2138, -0.0719], "tip_x": 1.660, "z_core": [-0.082, -0.061]},
}
# z 带边界 = 相邻锚点 z 中点；带外端外推 8cm（与男性表带宽容忍一致）
BOUNDS = [0.33, 0.1917, 0.1009, 0.0294, -0.0401, -0.16]   # thumb|…|little 6 个边界
ORDER = ["thumb", "index", "middle", "ring", "little"]
FINGER_ZH = {"thumb": "拇指", "index": "食指", "middle": "中指", "ring": "无名指", "little": "小指"}


def finger_entries(side, mirror_from=None):
    sgn = 1 if side == "left" else -1
    out = []
    for i, fin in enumerate(ORDER):
        spec = LEFT_HAND[fin]
        # 左右镜像只翻 x（z/y 为前后/上下向，镜像不变）；z_band/z_core 同理不翻
        xyz = [round(spec["xyz"][0] * sgn, 4), spec["xyz"][1], spec["xyz"][2]]
        prov = ("顶点几何聚类（世界坐标 z 间隙+身份判定：A-pose 掌心朝体，四指沿 z 桡→尺排列，"
                "拇指独立簇与食指间距最大；指甲簇每指 68 顶点，左右镜像对称）")
        if mirror_from:
            prov = "右侧锚点镜像（模型左右完全对称；镜像只翻 x，z/y 与左侧一致——右指甲簇实测 z 与镜像吻合）"
        lo, hi = BOUNDS[i + 1], BOUNDS[i]     # z 带：lo < hi（z 从小到大），不随侧别翻转
        band = [round(lo, 4), round(hi, 4)]
        out.append({
            "anatomical_label": f"{side}_{fin}_finger",
            "display_zh": f"{SIDE_ZH[side]}手{FINGER_ZH[fin]}",
            "mesh_names": [MESH["nails"]],
            "side": side,
            "world_xyz": xyz,
            "pick_hint": "指甲簇质心（指尖背侧）" if not mirror_from else "左侧锚点镜像（仅翻 x）",
            "confidence": 0.85 if not mirror_from else 0.8,
            "provenance": prov,
            "z_band": band,
            "tip_x": round(spec["tip_x"] * sgn, 4),
            "z_core": sorted(round(v, 4) for v in spec["z_core"]),
        })
    return out


SIDE_ZH = {"left": "左", "right": "右"}


def main():
    a = json.load(open(os.path.join(DATA, "female_anchors.json")))
    anchors = a["anchors"]
    entries = []

    def add(label, zh, xyz, side, conf, prov, hint, extra=None):
        e = {"anatomical_label": label, "display_zh": zh, "mesh_names": [],
             "side": side, "world_xyz": [round(v, 4) for v in xyz],
             "pick_hint": hint, "confidence": conf, "provenance": prov}
        if extra:
            e.update(extra)
        entries.append(e)

    eye_prov = "顶点几何聚类（命名眼球件 Sclera/Irises/Pupils/Cornea 质心，每侧 n=295）"
    add("left_eye", "左眼（含虹膜）", anchors["left_eye"]["world_xyz"], "left", 0.9, eye_prov, "眼球中心")
    add("right_eye", "右眼（含虹膜）", anchors["right_eye"]["world_xyz"], "right", 0.9, eye_prov, "眼球中心")
    nose_prov = "顶点几何聚类（Face mesh 中线前突带 z 最大簇，n=11）"
    add("nose", "鼻", anchors["nose"]["world_xyz"], "midline", 0.85, nose_prov, "鼻尖")
    lip_prov = "顶点几何聚类（Lips mesh y 中线分割）"
    add("upper_lip", "上唇", anchors["upper_lip"]["world_xyz"], "midline", 0.75, lip_prov, "唇区 y 中线分割")
    add("lower_lip", "下唇", anchors["lower_lip"]["world_xyz"], "midline", 0.75, lip_prov, "唇区 y 中线分割")
    add("lips", "唇（聚合）", anchors["lips"]["world_xyz"], "midline", 0.8, "上下唇锚点中点", "上下唇锚点中点")
    ear_prov = "顶点几何聚类（命名耳 mesh 质心，每侧 n=229）；耳区可被发片遮挡"
    add("left_ear", "左耳", anchors["left_ear"]["world_xyz"], "left", 0.7, ear_prov, "耳廓质心")
    add("right_ear", "右耳", anchors["right_ear"]["world_xyz"], "right", 0.7, ear_prov, "耳廓质心")

    entries += finger_entries("left")
    entries += finger_entries("right", mirror_from="left")

    toe_prov = ("顶点几何聚类（Toenails mesh 按足分侧聚合质心，每足 5 簇 n=260；"
                "拇趾簇最大 n=60，趾甲位于趾背非脚底）")
    add("left_toes", "左脚趾（聚合）", anchors["left_toenails"]["aggregate"], "left", 0.7,
        toe_prov, "趾甲簇聚合质心", {"aggregate_of": "toenail clusters ×5"})
    add("right_toes", "右脚趾（聚合）", anchors["right_toenails"]["aggregate"], "right", 0.7,
        toe_prov, "趾甲簇聚合质心", {"aggregate_of": "toenail clusters ×5"})

    with open(GLB, "rb") as f:
        sha8 = hashlib.sha256(f.read()).hexdigest()[:8]

    doc = {
        "version": "1.0-female",
        "model": "vendor/models/Female3d66.glb",
        "sha256_prefix": sha8,
        "note": ("3d66 女性模型（853441）解剖锚点表。来源：tools/probe_female_verts.py（真机 QtWebEngine "
                 "dump 命名 mesh 顶点云）+ tools/measure_female_anchors.py（numpy 聚类）+ "
                 "tools/build_female_labels.py（组装）。该模型材质/网格语义命名完整"
                 "（Sclera/Irises/Pupils/Cornea/Fingernails/Toenails/Lips/Ears），锚点为命名几何质心，"
                 "非纯几何推断。限制：耳区可被发片遮挡（发片点击走材质名兜底报「头发」）；"
                 "趾为五趾聚合；手指锚点在指甲背侧，指根点击按型带报「掌」（与男性表同口径）。"),
        "fitInfo": a["fitInfo"],
        "entries": entries,
        "unreliable_or_missing": [
            {"anatomical_label": "toe_2..toe_5", "display_zh": "第二至第五趾（单趾）",
             "reason": "趾甲簇可分（每趾 n=50-60）但趾体网格合片，单趾体表点击归型带「足/趾」带"},
            {"anatomical_label": "hair", "display_zh": "头发",
             "reason": "发片（Back/Bangs/Front/Side/Cap 等 10 材质）非皮肤，逐面语义表按面片位置报发下体区"
                       "（额/面部/颈/肩胸/后脑/后颈/背 …（发覆盖）），点击头发显示被发遮盖的身体部位"}
        ],
        "occlusion_notes": [
            "耳区被发片部分遮挡：锚点为命名耳 mesh 真实几何，点击命中发片时报发下体区（…（发覆盖））",
            "躯干大部分被吊带背心/内裤覆盖：区域按体表位置分区（衣物覆盖不改变解剖区域归属）"
        ],
        "matching": {"side_convention": "x>0=解剖学左（与男性表一致）；中线经 registerHuman v28 缩放感知居中修复后 x≈0"}
    }

    with open(OUT, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"→ {OUT}")
    print(f"entries={len(entries)} sha256_prefix={sha8}")
    for e in entries:
        if "finger" in e["anatomical_label"] or e["anatomical_label"] in ("left_toes", "right_toes"):
            print(f"  {e['anatomical_label']:<24} {e['world_xyz']} z_band={e.get('z_band')}")


if __name__ == "__main__":
    main()
