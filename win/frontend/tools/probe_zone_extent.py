#!/usr/bin/env python3
"""zone 空间范围诊断：复用 build_female_face_semantics 的 Phase A/B/C，
对每个 zone 输出面质心均值与空间范围 → data/female_zone_extent.json。
用途：判定点击普查中的「未命中」是表错位还是相机遮挡（被误判标签的面
若真的位于样本点附近 → 表 bug；远离样本点 → 前景遮挡，点击行为正确）。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

import build_female_face_semantics as B

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    g = B.GLB(B.GLB_PATH)
    s, off, fmin, fmax = B.register_transform(g)
    anat = json.load(open(B.ANAT_PATH, encoding="utf-8"))
    B.A_ANCH = {e["anatomical_label"]: np.array(e["world_xyz"]) for e in anat["entries"]}
    anch = json.load(open(B.ANCHORS_PATH, encoding="utf-8"))["anchors"]
    A_chest = np.array(anch["chest"]["world_xyz"])

    j = g.json
    mats_json = j["materials"]
    prims = []
    for inst in g.mesh_instances():
        mesh = j["meshes"][inst["mesh_index"]]
        multi = len(mesh["primitives"]) > 1
        for pi, prim in enumerate(mesh["primitives"]):
            mat = mats_json[prim["material"]].get("name", "?")
            pa, _ = g._accessor_array(prim["attributes"]["POSITION"])
            ia, _ = g._accessor_array(prim["indices"])
            T = ia.astype(np.int64).reshape(-1, 3)
            P = pa.astype(np.float64) @ inst["matrix"][:3, :3].T + inst["matrix"][:3, 3]
            C, N = B.face_data(P, T, s, off)
            name = mesh.get("name", "") + (("_%d" % (pi + 1)) if multi else "")
            prims.append({"name": name, "mat": mat, "C": C, "N": N, "nf": len(T)})

    LM = B.measure_landmarks(prims, B.A_ANCH, A_chest)

    zone_pts = {}
    for p in prims:
        B.classify(p, LM, fmin, fmax, zone_pts)

    out = {}
    for zid, pts in zone_pts.items():
        P = np.array(pts)[:, :3]                 # sink 行 = [cx,cy,cz,nx,ny,nz]
        mean = P.mean(0)
        out[zid] = {
            "n": int(len(P)),
            "mean": [round(float(v), 4) for v in mean],
            "min": [round(float(v), 4) for v in P.min(0)],
            "max": [round(float(v), 4) for v in P.max(0)],
        }
    with open(os.path.join(HERE, "data", "female_zone_extent.json"), "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print("zones=%d → data/female_zone_extent.json" % len(out))

    # —— 真实未命中案例：样本点 vs 被误报标签的 zone 中心距离 ——
    zs = json.load(open(os.path.join(HERE, "data", "female_zone_samples.json")))["zones"]
    labels = json.load(open(os.path.join(HERE, "web", "face-semantics-Female3d66.json")))["labels"]
    zh2zid = {v["zh"]: k for k, v in labels.items()}
    CASES = [
        ("proximal_phalanx_l_ring", "左手背"),
        ("proximal_phalanx_r_ring", "右手背"),
        ("middle_phalanx_l_ring", "左食指近节"),
        ("middle_phalanx_r_ring", "右食指中节"),
        ("distal_phalanx_r_index", "右中指中节"),
        ("proximal_phalanx_r_middle", "右食指近节"),
        ("middle_phalanx_r_middle", "右食指远节"),
        ("distal_phalanx_r_middle", "右食指远节"),
        ("proximal_phalanx_r_thumb", "右掌（手心）"),
        ("fingernail_l_index", "左中指中节"),
        ("fingernail_l_middle", "左食指远节"),
        ("fingernail_r_index", "右食指远节"),
        ("fingernail_r_middle", "右中指远节"),
        ("toe_r_3", "右第三趾"),
        ("arch_l", "左第三趾"),
        ("arch_r", "右第二趾"),
        ("lower_lip", "上唇"),
        ("mouth_region", "下唇"),
    ]
    for zid, wrong_zh in CASES:
        sp = np.array(zs[zid]["point"])
        wz = zh2zid.get(wrong_zh)
        if wz and wz in out:
            wc = np.array(out[wz]["mean"])
            print("%-26s 样本%s  误报[%s] 中心距 %.3f" % (
                zid, np.round(sp, 3), wrong_zh, float(np.linalg.norm(sp - wc))))
        else:
            print("%-26s 样本%s  误报[%s] 无 zone 映射" % (zid, np.round(sp, 3), wrong_zh))


if __name__ == "__main__":
    main()
