#!/usr/bin/env python3
"""女性模型锚点实测（第 3 步）：从 data/female_verts.json 顶点云聚类出
眼/五指/趾/唇/耳/鼻/胸锚点（归一化世界坐标，身高 5 系），打印聚类细节供人工核对。

用法：cd client && ./.venv/bin/python tools/measure_female_anchors.py
输出：stdout 报告 + data/female_anchors.json（中间产物，供 build_female_labels.py 组装正式表）
"""

import json
import os

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")

SIDE_ZH = {"left": "左", "right": "右"}


def load_parts():
    d = json.load(open(os.path.join(DATA, "female_verts.json")))
    parts = {}
    for name, chunks in d["parts"].items():
        pts = []
        for arr in chunks:
            a = np.array(arr, dtype=np.float64).reshape(-1, 3)
            pts.append(a)
        parts[name] = np.vstack(pts)
    return d, parts


def kmeans(pts, k, iters=200, seed=0):
    """小规模 numpy k-means（k<=10, n<4k），最远点初始化 + 确定性。"""
    rng = np.random.default_rng(seed)
    centroids = [pts[np.argmax(pts[:, 0] + pts[:, 2] * 1e-3)]]
    for _ in range(k - 1):
        d2 = np.min(((pts[:, None, :] - np.array(centroids)[None]) ** 2).sum(-1), axis=1)
        centroids.append(pts[np.argmax(d2)])
    c = np.array(centroids)
    for _ in range(iters):
        lab = ((pts[:, None, :] - c[None]) ** 2).sum(-1).argmin(1)
        nc = np.array([pts[lab == i].mean(0) if (lab == i).any() else c[i] for i in range(k)])
        if np.allclose(nc, c, atol=1e-7):
            c = nc
            break
        c = nc
    lab = ((pts[:, None, :] - c[None]) ** 2).sum(-1).argmin(1)
    return c, lab


def cluster_report(name, pts, labels, cents):
    print(f"\n== {name}: {len(pts)} 顶点 → {len(cents)} 簇 ==")
    order = np.argsort(cents[:, 0])
    for rank, ci in enumerate(order):
        m = labels == ci
        c = cents[ci]
        print(f"  簇{rank} n={m.sum():>4} 质心=({c[0]:+.4f},{c[1]:+.4f},{c[2]:+.4f}) "
              f"x[{pts[m,0].min():+.3f},{pts[m,0].max():+.3f}] "
              f"y[{pts[m,1].min():+.3f},{pts[m,1].max():+.3f}] "
              f"z[{pts[m,2].min():+.3f},{pts[m,2].max():+.3f}]")


def main():
    dump, parts = load_parts()
    print("model:", dump["model"], " fitInfo size:", dump["size"],
          " min:", dump["min"], " max:", dump["max"])
    out = {"model": dump["model"], "fitInfo": {"min": dump["min"], "max": dump["max"]}, "anchors": {}}

    # —— 中线自检：面部中线应≈0（registerHuman 中线修复后）——
    face = parts["Face"]
    midline = np.median((face[np.abs(face[:, 0]) < 0.25][:, 0])) if len(face) else None
    lips = parts["Lips"]
    print(f"中线自检：Lips 质心 x={lips[:,0].mean():+.4f}（应为≈0）")

    # —— 眼：眼球件合并（Sclera+Irises+Pupils+Cornea），按 x 符号分左右，质心=眼球中心 ——
    eye = np.vstack([parts[k] for k in ("Sclera", "Irises", "Pupils", "Cornea")])
    for side, sgn in (("left", 1), ("right", -1)):
        pts = eye[eye[:, 0] * sgn > 0]
        c = pts.mean(0)
        out["anchors"][f"{side}_eye"] = {"world_xyz": c.tolist(), "n": int(len(pts))}
        print(f"{side}_eye: n={len(pts)} 质心=({c[0]:+.4f},{c[1]:+.4f},{c[2]:+.4f})")

    # —— 手：Fingernails 按手分侧 → k=5 聚类指甲 ——
    nails = parts["Fingernails"]
    for side, sgn in (("left", 1), ("right", -1)):
        pts = nails[nails[:, 0] * sgn > 0]
        cents, lab = kmeans(pts, 5)
        cluster_report(f"Fingernails/{side}", pts, lab, cents)
        out["anchors"][f"{side}_hand_nails"] = [
            {"centroid": cents[i].tolist(), "n": int((lab == i).sum()),
             "min": pts[lab == i].min(0).tolist(), "max": pts[lab == i].max(0).tolist()}
            for i in range(5)]

    # —— 足：Toenails 按足分侧 → k=5 聚类 + 整足聚合质心 ——
    toes = parts["Toenails"]
    for side, sgn in (("left", 1), ("right", -1)):
        pts = toes[toes[:, 0] * sgn > 0]
        cents, lab = kmeans(pts, 5)
        cluster_report(f"Toenails/{side}", pts, lab, cents)
        agg = pts.mean(0)
        out["anchors"][f"{side}_toenails"] = {
            "aggregate": agg.tolist(), "n": int(len(pts)),
            "clusters": [{"centroid": cents[i].tolist(), "n": int((lab == i).sum())} for i in range(5)]}
        print(f"{side}_toes 聚合质心=({agg[0]:+.4f},{agg[1]:+.4f},{agg[2]:+.4f}) n={len(pts)}")

    # —— 唇：y 中线分割上下唇（ Male 同法）；唇聚合 = 上下锚点中点 ——
    ly_mid = (lips[:, 1].min() + lips[:, 1].max()) / 2
    upper = lips[lips[:, 1] > ly_mid]
    lower = lips[lips[:, 1] <= ly_mid]
    for nm, pts in (("upper_lip", upper), ("lower_lip", lower)):
        c = pts.mean(0)
        out["anchors"][nm] = {"world_xyz": c.tolist(), "n": int(len(pts))}
        print(f"{nm}: n={len(pts)} 质心=({c[0]:+.4f},{c[1]:+.4f},{c[2]:+.4f})")
    lc = (upper.mean(0) + lower.mean(0)) / 2
    out["anchors"]["lips"] = {"world_xyz": lc.tolist()}
    print(f"lips(聚合): ({lc[0]:+.4f},{lc[1]:+.4f},{lc[2]:+.4f})")

    # —— 耳：Ears 按侧分，质心 ——
    ears = parts["Ears"]
    for side, sgn in (("left", 1), ("right", -1)):
        pts = ears[ears[:, 0] * sgn > 0]
        c = pts.mean(0)
        out["anchors"][f"{side}_ear"] = {"world_xyz": c.tolist(), "n": int(len(pts))}
        print(f"{side}_ear: n={len(pts)} 质心=({c[0]:+.4f},{c[1]:+.4f},{c[2]:+.4f})")

    # —— 鼻：Face 中线前突带（|x|<0.03），y 介于唇上缘与眼下缘，取 z 最大簇质心 ——
    eye_y_min = min(out["anchors"][s]["world_xyz"][1] for s in ("left_eye", "right_eye"))
    lip_y_max = upper[:, 1].max()
    band = face[(np.abs(face[:, 0]) < 0.03) & (face[:, 1] > lip_y_max) & (face[:, 1] < eye_y_min)]
    if len(band):
        # 鼻尖 = 带内 z 最大顶点附近（2cm 球）质心
        tip = band[np.argmax(band[:, 2])]
        near = band[np.linalg.norm(band - tip, axis=1) < 0.02]
        c = near.mean(0)
        out["anchors"]["nose"] = {"world_xyz": c.tolist(), "n": int(len(near))}
        print(f"nose: n={len(near)} 质心=({c[0]:+.4f},{c[1]:+.4f},{c[2]:+.4f})")

    # —— 胸（向导抽查点）：Torso 中线 |x|<0.05、ny∈[0.66,0.78] 前面（z 最大侧）——
    torso = parts["Torso"]
    y0 = dump["min"][1]
    size_y = dump["size"][1]
    ny = (torso[:, 1] - y0) / size_y
    cb = torso[(np.abs(torso[:, 0]) < 0.05) & (ny > 0.66) & (ny < 0.78)]
    if len(cb):
        zmax = cb[:, 2].max()
        front = cb[cb[:, 2] > zmax - 0.02]
        c = front.mean(0)
        out["anchors"]["chest"] = {"world_xyz": c.tolist(), "n": int(len(front))}
        print(f"chest: n={len(front)} 质心=({c[0]:+.4f},{c[1]:+.4f},{c[2]:+.4f})")

    with open(os.path.join(DATA, "female_anchors.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n→ data/female_anchors.json")


if __name__ == "__main__":
    main()
