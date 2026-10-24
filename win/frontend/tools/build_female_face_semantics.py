#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_female_face_semantics.py — 女性模型（Female3d66.glb）逐三角面语义表构建器。

与男性 build_face_semantics.py 同 schema / 同运行时消费方式（faceSemFor 二分查段），
但分割策略更硬：女性模型材质语义命名完整（Arms/Legs/Torso/Face/Ears/Lips/Teeth/
Fingernails/Toenails/眼球件/发片/shirt/trims/内裤），网格边界本身就是解剖分割真值，
网格内部再按女性实测地标细分。

全部地标由本脚本从 GLB 顶点云实测（无手工猜测值）：
- 腕/肘 = 臂轴参数 t 的截面宽度极值；踝/膝 = 腿 y 分箱宽度极值
- 颈/肩/腰/髋 = 躯干 y 分箱宽度极值
- 指簇 = Fingernails k-means(k=5)，身份由 anatomy-labels 逐指锚点最近邻判定
- 趾簇 = Toenails k-means(k=5)，z 降序 = 拇趾→小趾（A-pose 足尖朝前，拇趾最前）
- 面部细分区 = anatomy-labels-Female3d66.json 锚点距离带

诚实性约束与男性版一致：本表为「材质语义网格边界 + 几何聚类 + 人体测量学比例」推断，
每个 zone 附 conf/note；被衣物覆盖处如实注明覆盖来源；0 面标签不注册（无幽灵标签）。

用法：cd client && ./.venv/bin/python tools/build_female_face_semantics.py
输出：web/face-semantics-Female3d66.json + data/female_zone_samples.json（verify 抽查点）
"""
import hashlib
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
os.chdir(HERE)

from glb_io import GLB, register_transform           # noqa: E402
from measure_female_anchors import kmeans            # noqa: E402

GLB_PATH = "web/vendor/models/Female3d66.glb"
OUT_PATH = "web/face-semantics-Female3d66.json"
ANAT_PATH = "web/anatomy-labels-Female3d66.json"
ANCHORS_PATH = "data/female_anchors.json"
SAMPLES_PATH = "data/female_zone_samples.json"

HAIR_MATS = {"Back", "BackUnder", "Band", "Bangs1", "Bangs2", "Cap",
             "Front1", "Front2", "Side1", "Side2"}
GARMENT_MATS = {"shirt", "trims"}
BRIEFS_PREFIX = "Mat3d66_853441"                      # 内裤材质（未语义命名，按前缀识别）
SIDE_ZH = {"l": "左", "r": "右"}

# ---------------------------------------------------------------- 基础工具


def face_data(P, T, s, off):
    """顶点(世界系) + 三角 → 面质心 C / 面法线 N（世界系，单位化）。"""
    W = P * s + off
    C = W[T].mean(axis=1)
    v0, v1, v2 = W[T[:, 0]], W[T[:, 1]], W[T[:, 2]]
    N = np.cross(v1 - v0, v2 - v0)
    N /= (np.linalg.norm(N, axis=1, keepdims=True) + 1e-12)
    return C, N


def width_by_y(C, nbins=64):
    """y 分箱 → (yc, 宽度)。宽度 = 2×到分箱质心距离的 95 分位（近似截面直径，抗离群）。"""
    ys = np.linspace(C[:, 1].min(), C[:, 1].max(), nbins + 1)
    yc, ws = [], []
    for k in range(nbins):
        m = (C[:, 1] >= ys[k]) & (C[:, 1] < ys[k + 1])
        if m.sum() < 3:
            continue
        q = C[m][:, [0, 2]]
        yc.append((ys[k] + ys[k + 1]) / 2)
        ws.append(float(np.quantile(np.linalg.norm(q - q.mean(0), axis=1), 0.95)) * 2)
    return np.array(yc), np.array(ws)


def width_by_t(P, axis_pt, axis_dir, nbins=60):
    """沿轴 t 分箱 → (tc, 宽度)，t 归一化到 [0,1]（轴上投影 min→0, max→1）。"""
    rel = P - axis_pt
    t = rel @ axis_dir
    t0, t1 = float(t.min()), float(t.max())
    tn = (t - t0) / (t1 - t0 + 1e-12)
    perp = rel - np.outer(t, axis_dir)
    pw = np.linalg.norm(perp, axis=1)
    tc, ws = [], []
    for b in range(nbins):
        m = (tn >= b / nbins) & (tn < (b + 1) / nbins)
        if m.sum() < 3:
            continue
        tc.append((b + 0.5) / nbins)
        ws.append(float(np.quantile(pw[m], 0.95)) * 2)
    return np.array(tc), np.array(ws), t0, t1 - t0


def extremum(tc, ws, lo, hi, mode):
    """tc∈[lo,hi] 窗口内宽度 mode 极值处的 t；空窗返回 None。要求 lo<hi。"""
    m = (tc >= lo) & (tc <= hi)
    if not m.any():
        return None
    i = int(np.argmax(ws[m])) if mode == "max" else int(np.argmin(ws[m]))
    return float(tc[m][i])


def local_min(tc, ws, lo, hi):
    """tc∈[lo,hi] 窗口内宽度的局部极小（以全剖面为邻域），取最显著（最窄）者；
    无局部极小则回退窗口全局最小。"""
    m = (tc >= lo) & (tc <= hi)
    if not m.any():
        return None
    cands = [i for i in range(1, len(ws) - 1) if m[i] and ws[i] < ws[i - 1] and ws[i] < ws[i + 1]]
    if not cands:
        mm = np.where(m)[0]
        return float(tc[mm[int(np.argmin(ws[mm]))]])
    return float(tc[min(cands, key=lambda i: ws[i])])


# ---------------------------------------------------------------- 标签注册

labels = {}
_order = []


def Z(zid, zh, conf, cat, note=None, yband=None):
    if zid not in labels:
        _order.append(zid)
        e = {"zh": zh, "conf": conf, "cat": cat}
        if note:
            e["note"] = note
        if yband:
            e["yband"] = [round(float(yband[0]), 4), round(float(yband[1]), 4)]
        labels[zid] = e
    return zid


class Filler:
    """顺序填充器：set() 只填未分配面；分类器结束时必须无剩余。
    sink 记录 [cx,cy,cz,nx,ny,nz]（质心+法线），供样本点按法线定前/后视角。"""

    def __init__(self, nf, C, sink, N=None):
        self.zid = np.empty(nf, dtype=object)
        self.done = np.zeros(nf, dtype=bool)
        self.C = C
        self.N = N if N is not None else np.zeros((nf, 3))
        self.sink = sink

    def _rec(self, zid, rows_c, rows_n):
        self.sink.setdefault(zid, []).extend(
            np.concatenate([rows_c, rows_n], axis=1).tolist())

    def set(self, mask, zid):
        mm = np.asarray(mask) & (~self.done)
        if mm.any():
            self.zid[mm] = zid
            self.done[mm] = True
            self._rec(zid, self.C[mm], self.N[mm])
        return int(mm.sum())

    def setid(self, idx, zid):
        idx = np.asarray(idx, dtype=int)
        mm = idx[~self.done[idx]]
        if len(mm):
            self.zid[mm] = zid
            self.done[mm] = True
            self._rec(zid, self.C[mm], self.N[mm])
        return len(mm)

    def all(self, zid):
        self.set(np.ones(len(self.zid), dtype=bool), zid)

    def fill_rest(self, zid):
        return self.set(~self.done, zid)

    def fill_nearest(self):
        """残余面按已分类面的最近邻（世界坐标）归属（边界过渡的诚实兜底）。"""
        rest = np.where(~self.done)[0]
        if not len(rest):
            return 0
        known = np.where(self.done)[0]
        Q, K = self.C[rest], self.C[known]
        # 分块最近邻（面数 ≤ 数千，直接全量）
        d = ((Q[:, None, :] - K[None, :, :]) ** 2).sum(-1)
        nn = known[d.argmin(1)]
        for i, src in zip(rest, nn):
            z = str(self.zid[src])
            self.zid[i] = z
            self.done[i] = True
            self._rec(z, self.C[i][None, :], self.N[i][None, :])
        return len(rest)

    def remaining(self):
        return int((~self.done).sum())


# ---------------------------------------------------------------- 地标实测


def measure_landmarks(prims, A, A_chest):
    LM = {}
    by_mat = {}
    for p in prims:
        by_mat.setdefault(p["mat"], []).append(p)

    def cat(mat):
        return np.vstack([p["C"] for p in by_mat[mat]]) if mat in by_mat else None

    # —— 躯干剖面：颈/肩/腰/髋 ——
    C = cat("Torso")
    yc, ws = width_by_y(C, 64)
    LM["neck_y"] = extremum(yc, ws, 1.50, 1.95, "min")
    LM["shoulder_y"] = extremum(yc, ws, 1.30, 1.75, "max")
    LM["waist_y"] = extremum(yc, ws, 0.00, 1.00, "min")
    LM["hip_y"] = extremum(yc, ws, -0.90, -0.10, "max")
    LM["torso_half"] = float(np.abs(C[:, 0]).max())
    print("躯干: neck_y=%.3f shoulder_y=%.3f waist_y=%.3f hip_y=%.3f half=%.3f y[%.3f,%.3f]"
          % (LM["neck_y"], LM["shoulder_y"], LM["waist_y"], LM["hip_y"], LM["torso_half"],
             C[:, 1].min(), C[:, 1].max()))

    # —— 臂：每侧轴 S→F（|x| 两端），腕/肘 = 宽度极值 ——
    C = cat("Arms")
    for v, sgn in (("l", 1), ("r", -1)):
        P = C[C[:, 0] * sgn > 0]
        S = P[int(np.argmin(np.abs(P[:, 0])))]
        F = P[int(np.argmax(np.abs(P[:, 0])))]
        axd = (F - S) / np.linalg.norm(F - S)
        tc, ws, t0, tl = width_by_t(P, S, axd, 60)
        wrist_t = extremum(tc, ws, 0.62, 0.95, "min") or 0.80
        # 宽度剖面肩→腕单调收窄、无肘部凹点（实测），肘按臂比例取肩-腕中点（诚实低敏）
        elbow_t = 0.50 * wrist_t
        LM["arm_" + v] = {"S": S, "axd": axd, "t0": t0, "tl": tl,
                          "wrist_t": wrist_t, "elbow_t": elbow_t}
        print("臂[%s]: S=%s 轴=%s wrist_t=%.3f elbow_t=%.3f（比例）"
              % (v, np.round(S, 3), np.round(axd, 3), wrist_t, elbow_t))

    # —— 掌心：腕远侧截面积心（xz） ——
    for v in ("l", "r"):
        sgn = 1 if v == "l" else -1
        a = LM["arm_" + v]
        P = C[C[:, 0] * sgn > 0]
        tn = ((P - a["S"]) @ a["axd"] - a["t0"]) / a["tl"]
        pm = (tn > a["wrist_t"] + 0.02) & (tn < a["wrist_t"] + 0.12)
        LM["palm_" + v] = P[pm][:, [0, 2]].mean(0) if pm.sum() >= 3 else P[:, [0, 2]].mean(0)
        print("掌心[%s] = %s" % (v, np.round(LM["palm_" + v], 3)))

    # —— 指甲 → 逐指身份：逐面取最近指锚点（确定性，不做 k-means——右手簇曾漂移） ——
    C = cat("Fingernails")
    fkeys = {"thumb": "thumb_finger", "index": "index_finger", "middle": "middle_finger",
             "ring": "ring_finger", "little": "little_finger"}
    for v, sgn in (("l", 1), ("r", -1)):
        P = C[C[:, 0] * sgn > 0]
        mapping = {}
        D = np.stack([np.linalg.norm(P[:, [0, 2]]
                                     - A["%s_%s" % ("left" if v == "l" else "right", key)][[0, 2]],
                                     axis=1)
                      for key in fkeys.values()])
        fid, dmin = D.argmin(0), D.min(0)
        for k, fk in enumerate(fkeys):
            own = P[(fid == k) & (dmin < 0.025)]
            if len(own) < 40:
                raise RuntimeError("指甲[%s_%s] 仅 %d 面命中锚点（预期≈112），锚点表疑似有误"
                                   % (v, fk, len(own)))
            mapping[fk] = {"center": own[:, [0, 2]].mean(0), "n": int(len(own))}
        LM["nail_" + v] = mapping
        print("指甲[%s]: " % v + ", ".join("%s(n=%d,c=%s)" % (fk, m["n"], np.round(m["center"], 3))
                                             for fk, m in mapping.items()))

    # —— 趾甲簇：z 降序 = 拇趾→小趾；打印 x 序交叉校验 ——
    C = cat("Toenails")
    for v, sgn in (("l", 1), ("r", -1)):
        P = C[C[:, 0] * sgn > 0]
        cents, lab = kmeans(P, 5)
        zord = np.argsort(cents[:, 2])[::-1]         # 按 z（前后向）排序，拇趾最前
        LM["toe_" + v] = cents[zord][:, [0, 2]]      # xz（趾序 拇趾→小趾）
        xrank = sorted(int(np.where(zord == i)[0][0]) for i in np.argsort(np.abs(cents[zord, 0])))
        print("趾簇[%s] z序=%s（拇趾→小趾）; |x|升序簇的 z 序下标=%s（应=0,1,2,3,4）"
              % (v, np.round(cents[zord], 3), xrank))

    # —— 腿：踝/膝 = y 宽度极值；网格顶 = 臀腿交界 ——
    C = cat("Legs")
    LM["legs_top_y"] = float(C[:, 1].max())
    print("腿网格顶 y=%.3f" % LM["legs_top_y"])
    for v, sgn in (("l", 1), ("r", -1)):
        P = C[C[:, 0] * sgn > 0]
        yc, ws = width_by_y(P, 48)
        ankle_y = extremum(yc, ws, -2.25, -2.00, "min") or -2.13
        knee_y = local_min(yc, ws, -1.45, -0.95) or -1.15
        LM["leg_" + v] = {"ankle_y": ankle_y, "knee_y": knee_y}
        print("腿[%s]: ankle_y=%.3f knee_y=%.3f" % (v, ankle_y, knee_y))

    # —— 足 y/z 范围（跟/弓/前掌分带用） ——
    LM["foot"] = {}
    for v, sgn in (("l", 1), ("r", -1)):
        P = C[(C[:, 0] * sgn > 0) & (C[:, 1] < LM["leg_" + v]["ankle_y"] - 0.05)]
        LM["foot"][v] = {"zmin": float(P[:, 2].min()), "zmax": float(P[:, 2].max()),
                         "ymin": float(P[:, 1].min())}
        print("足[%s]: z[%+.3f,%+.3f] ymin=%.3f"
              % (v, LM["foot"][v]["zmin"], LM["foot"][v]["zmax"], LM["foot"][v]["ymin"]))

    # —— 内裤 y 范围 ——
    briefs_mat = [m for m in by_mat if m.startswith(BRIEFS_PREFIX)]
    C = cat(briefs_mat[0])
    LM["briefs_top"] = float(C[:, 1].max())
    LM["briefs_bot"] = float(C[:, 1].min())
    LM["briefs_ymid"] = (LM["briefs_top"] + LM["briefs_bot"]) / 2
    LM["briefs_half"] = float(np.abs(C[:, 0]).max())
    print("内裤: y[%.3f,%.3f] half=%.3f" % (LM["briefs_top"], LM["briefs_bot"], LM["briefs_half"]))

    # —— 胸带中心（向导实测锚点） ——
    LM["chest_y"] = float(A_chest[1])
    print("胸锚点 y=%.3f" % LM["chest_y"])
    return LM


# ---------------------------------------------------------------- 分类器

A_ANCH = None       # anatomy 锚点（main 注入）


def classify(p, LM, fmin, fmax, sink):
    global A_ANCH
    C, N, mat = p["C"], p["N"], p["mat"]
    fmax_z_half = (fmax[2] - fmin[2]) / 2
    nz = C[:, 2] / fmax_z_half
    sd = np.where(C[:, 0] > 0, "l", "r")
    axn = np.abs(C[:, 0]) / LM["torso_half"]
    f = Filler(len(C), C, sink, N)

    if mat in HAIR_MATS:
        # 发片报「被发遮盖的身体部位」：点击头发不再报「头发」，按面片位置给出
        # 发下体区——与内裤「左臀（内裤覆盖）」同口径。分带依据（均为实测）：
        # eye_y≈2.134（额/面分界=眶上缘）、lip_y≈1.947（面/颈前分界=唇下）、
        # jaw_y=neck_y+0.18≈1.942（后脑/后颈分界）、颈底=neck_y-0.10≈1.662
        # （再往下是肩/上胸/背）。前后向按 z=±0.06 分（头前伸 z>0，脑后 z<0）。
        eye_y = float(A_ANCH["left_eye"][1])
        lip_y = float(A_ANCH["upper_lip"][1])
        neck_lo = LM["neck_y"] - 0.10
        jaw_y = LM["neck_y"] + 0.18
        hfront = C[:, 2] > 0.06
        hback = C[:, 2] < -0.06
        hside = ~(hfront | hback)
        f.set(hfront & (C[:, 1] > eye_y + 0.055),
              Z("hair_forehead", "额（发覆盖）", 0.75, "hair"))
        f.set(hfront & (C[:, 1] <= eye_y + 0.055) & (C[:, 1] > lip_y - 0.06),
              Z("hair_face", "面部（发覆盖）", 0.7, "hair"))
        f.set(hfront & (C[:, 1] <= lip_y - 0.06) & (C[:, 1] > neck_lo),
              Z("hair_neck_front", "颈前（发覆盖）", 0.7, "hair"))
        f.set((hfront | hside) & (C[:, 1] <= neck_lo) & (sd == "l"),
              Z("hair_shoulder_l", "左肩/上胸（发覆盖）", 0.7, "hair"))
        f.set((hfront | hside) & (C[:, 1] <= neck_lo) & (sd == "r"),
              Z("hair_shoulder_r", "右肩/上胸（发覆盖）", 0.7, "hair"))
        f.set(hback & (C[:, 1] > jaw_y),
              Z("hair_occiput", "后脑（发覆盖）", 0.75, "hair"))
        f.set(hback & (C[:, 1] <= jaw_y) & (C[:, 1] > neck_lo),
              Z("hair_nape", "后颈（发覆盖）", 0.7, "hair"))
        f.set(hback & (C[:, 1] <= neck_lo),
              Z("hair_back_upper", "背上（发覆盖）", 0.7, "hair"))
        f.set(hside & (C[:, 1] > eye_y + 0.055),
              Z("hair_crown", "头顶（发覆盖）", 0.75, "hair"))
        f.set(hside & (C[:, 1] <= eye_y + 0.055) & (C[:, 1] > lip_y - 0.06),
              Z("hair_ear_cheek", "耳颊区（发覆盖）", 0.7, "hair"))
        f.set(hside & (C[:, 1] <= lip_y - 0.06) & (C[:, 1] > neck_lo),
              Z("hair_neck_side", "颈侧（发覆盖）", 0.7, "hair"))
        f.fill_rest(Z("hair_rest", "头发（发覆盖区未定）", 0.5, "hair",
                      "位置带未覆盖的残余发面（应为空）"))
    elif mat in ("Sclera", "Cornea", "EyeMoisture", "adskMatEyeMoisture"):
        for v in ("l", "r"):
            f.set(sd == v, Z("eyeball_" + v, SIDE_ZH[v] + "眼球", 0.9, "eye"))
    elif mat in ("Irises", "Pupils"):
        for v in ("l", "r"):
            f.set(sd == v, Z("iris_" + v, SIDE_ZH[v] + "瞳孔（虹膜）", 0.9, "eye"))
    elif mat == "EyeSocket":
        for v in ("l", "r"):
            f.set(sd == v, Z("eyelid_" + v, SIDE_ZH[v] + "眼睑区", 0.8, "eye"))
    elif mat == "Eyelashes":
        for v in ("l", "r"):
            f.set(sd == v, Z("eyelashes_" + v, SIDE_ZH[v] + "睫毛", 0.85, "eye"))
    elif mat == "Lips":
        ymid = (C[:, 1].min() + C[:, 1].max()) / 2
        f.set(C[:, 1] > ymid, Z("upper_lip", "上唇", 0.85, "face"))
        f.set(C[:, 1] <= ymid, Z("lower_lip", "下唇", 0.85, "face"))
    elif mat == "Mouth":
        f.all(Z("mouth_inner", "口腔（唇内面，闭口不可见）", 0.7, "face"))
    elif mat == "Teeth":
        f.all(Z("teeth", "牙齿", 0.9, "face"))
    elif mat == "Ears":
        for v in ("l", "r"):
            f.set(sd == v, Z("ear_" + v, SIDE_ZH[v] + "耳（真实耳廓网格）", 0.8, "face",
                             "可被发片遮挡；发片命中报发下体区（耳颊区（发覆盖））"))
    elif mat == "Fingernails":
        fkn = {"thumb": "拇指", "index": "食指", "middle": "中指", "ring": "无名指", "little": "小指"}
        for v in ("l", "r"):
            m = sd == v
            P2 = C[m][:, [0, 2]]
            w = np.where(m)[0]
            for fk, info in LM["nail_" + v].items():
                d = np.linalg.norm(P2 - info["center"], axis=1)
                got = f.setid(w[d < 0.03],
                              Z("fingernail_%s_%s" % (v, fk), SIDE_ZH[v] + "手" + fkn[fk] + "指甲",
                                0.9, "finger"))
                if got < 40:
                    raise RuntimeError("指甲[%s_%s] 仅 %d 面命中（预期≈112），锚点疑似有误"
                                       % (v, fk, got))
        f.fill_rest(Z("fingernail_rest", "指甲（侧别/指别未定）", 0.5, "finger",
                      "距所有指甲簇中心 >3cm 的残余面"))
    elif mat == "Toenails":
        tnames = ["拇趾", "第二趾", "第三趾", "第四趾", "小趾"]
        for v in ("l", "r"):
            m = sd == v
            P2 = C[m][:, [0, 2]]
            w = np.where(m)[0]
            cents = LM["toe_" + v]
            d = np.linalg.norm(P2[:, None, :] - cents[None, :, :], axis=2)
            near, tid = d.min(1), d.argmin(1)
            for ti in range(5):
                f.setid(w[(tid == ti) & (near < 0.02)],
                        Z("toenail_%s_%d" % (v, ti), SIDE_ZH[v] + "脚" + tnames[ti] + "趾甲",
                          0.85, "toe"))
        f.fill_rest(Z("toenail_rest", "趾甲（侧别/趾别未定）", 0.5, "toe"))
    elif mat in GARMENT_MATS:
        classify_garment(f, C, nz, sd, axn, LM)
    elif mat.startswith(BRIEFS_PREFIX):
        classify_briefs(f, C, nz, LM)
    elif mat == "Face":
        classify_face(f, C, A_ANCH)
    elif mat == "Arms":
        classify_arms(f, C, N, sd, LM)
    elif mat == "Legs":
        classify_legs(f, C, nz, sd, LM)
    elif mat == "Torso":
        classify_torso(f, C, nz, sd, axn, LM)
    else:
        raise RuntimeError("未处理材质 " + mat)

    if f.remaining():
        raise RuntimeError("%s（%s）剩余 %d 面未分类" % (p["name"], mat, f.remaining()))
    return f.zid


# ---------------------------------------------------------------- 分区实现


def classify_garment(f, C, nz, sd, axn, LM):
    """吊带背心/边饰 → 按所覆盖体表映射（与 Torso 同一带位，注记衣物覆盖）。"""
    cy = LM["chest_y"]
    back = nz < -0.35
    front = ~back
    f.set(back & (C[:, 1] > LM["waist_y"]),
          Z("back_upper", "背（上）", 0.7, "torso", "吊带背心覆盖"))
    f.set(back & (C[:, 1] <= LM["waist_y"]),
          Z("back_lower_waist", "腰/背（下）", 0.7, "torso", "吊带背心下摆覆盖"))
    for v in ("l", "r"):
        f.set(front & (C[:, 1] > LM["neck_y"] - 0.05) & (axn > 0.55) & (sd == v),
              Z("shoulder_up_" + v, SIDE_ZH[v] + "肩（肩带覆盖）", 0.65, "limb",
                "吊带肩带覆盖，肩部精确分界不可见"))
    f.set(front & (C[:, 1] >= cy + 0.32) & (axn <= 0.55),
          Z("chest_upper", "胸上区（锁骨以下，乳房上缘以上）", 0.7, "torso", "衣物覆盖"))
    for v in ("l", "r"):
        f.set(front & (C[:, 1] >= cy - 0.28) & (C[:, 1] < cy + 0.32)
              & (np.abs(C[:, 0]) > 0.08) & (sd == v),
              Z("breast_" + v, SIDE_ZH[v] + "胸（乳房区）", 0.7, "torso",
                "乳房精确轮廓不可见（衣物覆盖）"))
    f.set(front & (C[:, 1] >= cy - 0.28) & (C[:, 1] < cy + 0.32) & (np.abs(C[:, 0]) <= 0.08),
          Z("sternum", "胸骨", 0.7, "torso", "衣物覆盖"))
    for v in ("l", "r"):
        f.set(front & (C[:, 1] >= cy - 0.45) & (C[:, 1] < cy - 0.28) & (axn > 0.30) & (sd == v),
              Z("costal_margin_" + v, SIDE_ZH[v] + "肋缘（肋弓）", 0.7, "torso", "衣物下摆覆盖"))
    f.set(front & (C[:, 1] >= cy - 0.45) & (C[:, 1] < cy - 0.28) & (axn <= 0.30),
          Z("epigastrium", "上腹部", 0.7, "torso", "衣物下摆覆盖"))
    f.set(front & (C[:, 1] < cy - 0.45) & (axn <= 0.55),
          Z("abdomen", "腹", 0.75, "torso", "衣物下摆以下可直视"))
    f.set(front & (axn > 0.55) & (C[:, 1] <= LM["neck_y"] - 0.05),
          Z("flank", "侧腹", 0.7, "torso", "衣物覆盖"))


def classify_briefs(f, C, nz, LM):
    """内裤 → 覆盖层分区（前/侧/后/臀沟/会阴），复用男性语义概念。"""
    back = nz < -0.25
    side = np.abs(nz) <= 0.25
    axb = np.abs(C[:, 0]) / LM["briefs_half"]
    f.set(nz > 0.25, Z("briefs_front", "下腹/腹股沟（内裤覆盖）", 0.8, "garment"))
    f.set(side & (axb > 0.55), Z("briefs_side", "髋侧（内裤覆盖）", 0.8, "garment"))
    f.set(back & (C[:, 1] > LM["briefs_ymid"]),
          Z("briefs_back", "骶臀上带（内裤覆盖）", 0.8, "garment"))
    f.set(back & (C[:, 1] <= LM["briefs_ymid"]) & (np.abs(C[:, 0]) < 0.055),
          Z("briefs_cleft", "臀沟（内裤覆盖）", 0.7, "garment"))
    # 臀部主体分侧（旧规则漏掉：背面 y≤ymid 且 |x|≥0.12 的臀部主体曾落入会阴兜底）
    f.set(back & (C[:, 1] <= LM["briefs_ymid"]) & (C[:, 0] >= 0.055),
          Z("briefs_buttock_l", "左臀（内裤覆盖）", 0.8, "garment"))
    f.set(back & (C[:, 1] <= LM["briefs_ymid"]) & (C[:, 0] <= -0.055),
          Z("briefs_buttock_r", "右臀（内裤覆盖）", 0.8, "garment"))
    f.fill_rest(Z("briefs_perineum", "会阴周围（内裤覆盖，模型无裸露会阴）", 0.6, "garment",
                  "会阴被内裤遮挡，仅能给覆盖层位置与精确坐标"))


def classify_face(f, C, A):
    """Face 网格 → 眶周/鼻/唇周/颏/额/颊（锚点距离带）。"""
    dl = np.linalg.norm(C - A["left_eye"], axis=1)
    dr = np.linalg.norm(C - A["right_eye"], axis=1)
    dn = np.linalg.norm(C - A["nose"], axis=1)
    dmu = np.linalg.norm(C - A["upper_lip"], axis=1)
    dml = np.linalg.norm(C - A["lower_lip"], axis=1)
    f.set(dl < 0.085, Z("periorbital_l", "左眼眶区", 0.75, "eye"))
    f.set(dr < 0.085, Z("periorbital_r", "右眼眶区", 0.75, "eye"))
    f.set(dn < 0.065, Z("nose_region", "鼻区", 0.85, "face"))
    f.set((dmu < 0.055) | (dml < 0.055), Z("mouth_region", "唇区（周围）", 0.75, "face"))
    f.set((C[:, 1] < A["lower_lip"][1] - 0.045) & (np.abs(C[:, 0]) < 0.09),
          Z("chin", "颏（下巴）", 0.8, "face"))
    f.set(C[:, 1] > A["left_eye"][1] + 0.055, Z("forehead", "额", 0.75, "face"))
    f.fill_rest(Z("cheek_ear_zone", "面颊/耳前区", 0.7, "face"))


def classify_arms(f, C, N, sd, LM):
    """臂：肩→指沿臂轴 t 分带；手部逐指轴归属 + 掌/背。"""
    for v in ("l", "r"):
        s = 1.0 if v == "l" else -1.0
        m = sd == v
        if not m.any():
            continue
        idx = np.where(m)[0]
        a = LM["arm_" + v]
        tn = ((C[idx] - a["S"]) @ a["axd"] - a["t0"]) / a["tl"]
        wt, et = a["wrist_t"], a["elbow_t"]
        zh = SIDE_ZH[v]
        nx_m = N[idx, 0] * s          # 朝体侧分量（掌面）
        nz_f = N[idx, 2]              # 朝前分量（手背）

        # 肩/大臂分界 = 腋平面（过臂轴起点 S 的垂面，tn=0；S 为臂根最内侧点，实测 y≈1.365）：
        # 面在垂面上方=肩帽（三角肌），下方=大臂。旧边界 tn<0.14 曾把腋下整段大臂划进肩部。
        f.setid(idx[tn < 0],
                Z("shoulder_up_" + v, zh + "肩（三角肌区）", 0.75, "limb",
                  "肩与上臂分界取腋平面（三角肌下缘为近似）"))
        m_ua = (tn >= 0) & (tn < et - 0.035)
        if m_ua.any():
            f.setid(idx[m_ua], Z("upper_arm_" + v, zh + "上臂", 0.85, "limb",
                                 yband=(C[idx[m_ua]][:, 1].min(), C[idx[m_ua]][:, 1].max())))
        m_el = np.abs(tn - et) <= 0.035
        f.setid(idx[m_el & (nz_f > 0.3)], Z("cubital_" + v, zh + "肘窝（肘前）", 0.8, "limb"))
        f.setid(idx[m_el & (nz_f < -0.3)], Z("elbow_tip_" + v, zh + "肘尖", 0.85, "limb"))
        f.setid(idx[m_el], Z("elbow_" + v, zh + "肘", 0.85, "limb"))
        m_fa = (tn >= et + 0.035) & (tn < wt - 0.02)
        if m_fa.any():
            f.setid(idx[m_fa], Z("forearm_" + v, zh + "前臂", 0.9, "limb",
                                 yband=(C[idx[m_fa]][:, 1].min(), C[idx[m_fa]][:, 1].max())))
        f.setid(idx[(tn >= wt - 0.02) & (tn < wt + 0.03)], Z("wrist_" + v, zh + "腕", 0.85, "limb"))
        # —— 手部 ——
        hidx = idx[tn >= wt + 0.03]
        if not len(hidx):
            continue
        px = C[hidx][:, [0, 2]]
        used = np.zeros(len(hidx), dtype=bool)
        fkn = {"thumb": "拇指", "index": "食指", "middle": "中指", "ring": "无名指", "little": "小指"}
        palm2 = LM["palm_" + v]
        for fk in ("thumb", "index", "middle", "ring", "little"):
            info = LM["nail_" + v].get(fk)
            if info is None:
                continue
            nail2 = info["center"]
            mcp2 = nail2 + (palm2 - nail2) * 0.28       # 掌心→指甲 28% 处 ≈ 掌指关节
            d2 = nail2 - mcp2
            L2 = float(np.linalg.norm(d2))
            if L2 < 1e-6:
                continue
            u = d2 / L2
            q = px - mcp2
            tt = q @ u
            dist = np.abs(q[:, 0] * u[1] - q[:, 1] * u[0])   # 点到指轴距离（xz 平面）
            near = (dist < 0.030) & (tt > -0.015) & (tt < L2 + 0.02) & (~used)
            if not near.any():
                continue
            sp = np.clip(tt[near] / L2, 0, 1)
            sel = hidx[near]
            # 指节（不单设关节带：关节面位于两节指骨交界，独立分带只会产生 0 面幽灵标签）
            if fk == "thumb":
                zones = [(0.00, 0.50, "proximal_phalanx_%s_%s" % (v, fk), zh + "拇指近节"),
                         (0.50, 1.01, "distal_phalanx_%s_%s" % (v, fk), zh + "拇指远节")]
            else:
                zones = [(0.00, 1 / 3, "proximal_phalanx_%s_%s" % (v, fk), zh + fkn[fk] + "近节"),
                         (1 / 3, 2 / 3, "middle_phalanx_%s_%s" % (v, fk), zh + fkn[fk] + "中节"),
                         (2 / 3, 1.01, "distal_phalanx_%s_%s" % (v, fk), zh + fkn[fk] + "远节")]
            for lo, hi, zidn, zzh in zones:
                mm2 = (sp >= lo) & (sp < hi)
                if mm2.any():
                    f.setid(sel[mm2], Z(zidn, zzh, 0.85, "finger"))
            used[near] = True
        # 指轴外的手部：掌（法线朝体侧）/手背（法线朝前）；残余取两者中法线更接近者
        rest = hidx[~used]
        if len(rest):
            rmed, rfront = N[rest, 0] * s, N[rest, 2]
            f.setid(rest[rmed < -0.4], Z("palm_" + v, zh + "掌（手心）", 0.8, "hand"))
            f.setid(rest[rfront > 0.4], Z("hand_" + v, zh + "手背", 0.8, "hand"))
            rest2 = rest[(rmed >= -0.4) & (rfront <= 0.4)]
            if len(rest2):
                med2 = N[rest2, 0] * s
                front2 = N[rest2, 2]
                f.setid(rest2[med2 < front2], Z("palm_" + v, zh + "掌（手心）", 0.8, "hand"))
                f.setid(rest2[med2 >= front2], Z("hand_" + v, zh + "手背", 0.8, "hand"))


def classify_legs(f, C, nz, sd, LM):
    """腿：臀/臀褶/大腿/膝/腘/小腿/踝/足（跟/弓/背/前掌/逐趾）。"""
    tnames = ["拇趾", "第二趾", "第三趾", "第四趾", "小趾"]
    for v in ("l", "r"):
        m = sd == v
        if not m.any():
            continue
        idx = np.where(m)[0]
        x, y, z = C[idx, 0], C[idx, 1], C[idx, 2]
        nzm = nz[idx]
        lg, top = LM["leg_" + v], LM["legs_top_y"]
        ay, ky = lg["ankle_y"], lg["knee_y"]
        zh = SIDE_ZH[v]

        m_bk = (nzm < -0.3) & (y > top - 0.30)
        f.setid(idx[m_bk & (np.abs(x) < 0.055)],
                Z("gluteal_cleft", "臀沟（臀裂壁）", 0.85, "body"))
        f.setid(idx[m_bk], Z("buttock_" + v, zh + "臀", 0.9, "body"))
        f.setid(idx[(nzm < -0.3) & (y <= top - 0.30) & (y > top - 0.36)],
                Z("gluteal_fold_" + v, zh + "臀腿交界（臀褶）", 0.8, "body"))
        m_th = y > ky + 0.06        # 膝以上全部（含髋前/股四头肌上部；臀褶以下背面已先归类）
        if m_th.any():
            f.setid(idx[m_th], Z("thigh_" + v, zh + "大腿", 0.9, "limb",
                                 yband=(y[m_th].min(), y[m_th].max())))
        m_kn = (y <= ky + 0.06) & (y > ky - 0.06)
        f.setid(idx[m_kn & (nzm < -0.3)], Z("popliteal_" + v, zh + "膝窝（腘）", 0.85, "limb"))
        f.setid(idx[m_kn], Z("knee_" + v, zh + "膝", 0.85, "limb"))
        m_cf = (y <= ky - 0.06) & (y > ay + 0.06)
        if m_cf.any():
            f.setid(idx[m_cf], Z("calf_" + v, zh + "小腿", 0.9, "limb",
                                 yband=(y[m_cf].min(), y[m_cf].max())))
        m_an = (y <= ay + 0.06) & (y > ay - 0.05)
        f.setid(idx[m_an & (nzm < -0.4)], Z("heel_back_" + v, zh + "脚跟后侧", 0.85, "foot"))
        f.setid(idx[m_an], Z("ankle_" + v, zh + "踝", 0.85, "limb"))
        # —— 足（脚背先取，前掌只收剩余前段；顺序决定归属） ——
        m_ft = y <= ay - 0.05
        fz = LM["foot"][v]
        span = fz["zmax"] - fz["zmin"]
        f.setid(idx[m_ft & (z < fz["zmin"] + 0.30 * span)], Z("heel_" + v, zh + "脚跟", 0.9, "foot"))
        f.setid(idx[m_ft & (y < fz["ymin"] + 0.03) & (z >= fz["zmin"] + 0.30 * span)],
                Z("arch_" + v, zh + "足弓/足底", 0.75, "foot", "足底面（内侧纵弓区）"))
        m_toe = m_ft & (z > LM["toe_" + v][:, 1].min() - 0.02)   # 覆盖最靠后的趾（小趾）
        if m_toe.any():
            cents = LM["toe_" + v]
            p2 = C[idx[m_toe]][:, [0, 2]]
            d = np.linalg.norm(p2[:, None, :] - cents[None, :, :], axis=2)
            near, tid = d.min(1), d.argmin(1)
            tidx = idx[m_toe]
            for ti in range(5):
                # 趾体：簇周 6cm 内、且不低于该趾簇后 8cm（跖球后方归前脚掌）
                zfz = z[m_toe]
                mm2 = (tid == ti) & (near < 0.06) & (zfz > cents[ti, 1] - 0.08)
                f.setid(tidx[mm2], Z("toe_%s_%d" % (v, ti), zh + tnames[ti], 0.9, "toe"))
        f.setid(idx[m_ft & (nzm > 0.35)], Z("instep_" + v, zh + "脚背", 0.85, "foot"))
        f.setid(idx[m_ft & (z > fz["zmax"] - 0.35)], Z("forefoot_" + v, zh + "前脚掌", 0.85, "foot"))
        f.setid(idx[m_ft], Z("foot_rim_" + v, zh + "足缘（背-底过渡）", 0.7, "foot"))


def classify_torso(f, C, nz, sd, axn, LM):
    """躯干基底：头底/颈/肩/背/胸/乳房/胸骨/肋缘/上腹/腹/髋前/臀/会阴。"""
    neck_y, shoulder_y = LM["neck_y"], LM["shoulder_y"]
    waist_y, hip_y = LM["waist_y"], LM["hip_y"]
    cy, jaw_y = LM["chest_y"], LM["neck_y"] + 0.18
    back = nz < -0.35
    front = ~back

    f.set(C[:, 1] > jaw_y, Z("head_base", "头部（发覆盖基底）", 0.6, "head",
                             "头皮/后脑被发片覆盖，报基底位置"))
    f.set((C[:, 1] > neck_y - 0.10) & (C[:, 1] <= jaw_y) & (np.abs(C[:, 0]) < 0.35),
          Z("neck", "颈", 0.8, "body", yband=(neck_y - 0.10, jaw_y)))
    for v in ("l", "r"):
        f.set((np.abs(C[:, 0]) > 0.35) & (C[:, 1] > shoulder_y - 0.18) & (sd == v),
              Z("shoulder_up_" + v, SIDE_ZH[v] + "肩（斜方肌/三角肌根部）", 0.75, "limb"))
    f.set(back & (C[:, 1] > waist_y), Z("back_upper", "背（上）", 0.85, "torso"))
    # 臀上界 = 骶臀分界（briefs_ymid，与内裤层 骶臀上带/臀 分侧同一条线）。
    # 旧界 hip_y+0.02=-0.089 太低：臀下部皮肤（内裤腿缘以下）曾被标成腰/背（下）。
    butt_line = LM["briefs_ymid"]
    f.set(back & (C[:, 1] <= waist_y) & (C[:, 1] > butt_line),
          Z("back_lower_waist", "腰/背（下）", 0.8, "torso"))
    for v in ("l", "r"):
        f.set(front & (C[:, 1] >= cy - 0.28) & (C[:, 1] < cy + 0.32)
              & (np.abs(C[:, 0]) > 0.08) & (sd == v),
              Z("breast_" + v, SIDE_ZH[v] + "胸（乳房/胸大肌区）", 0.85, "torso"))
    f.set(front & (C[:, 1] >= cy - 0.28) & (C[:, 1] < cy + 0.32) & (np.abs(C[:, 0]) <= 0.08),
          Z("sternum", "胸骨", 0.85, "torso"))
    f.set(front & (C[:, 1] >= cy + 0.32) & (C[:, 1] <= jaw_y - 0.02) & (axn <= 0.60),
          Z("chest_upper", "胸上区（锁骨以下，乳房上缘以上）", 0.8, "torso"))
    f.set(front & (axn > 0.60) & (C[:, 1] <= cy + 0.35) & (C[:, 1] > hip_y),
          Z("flank", "侧腹", 0.75, "torso"))
    for v in ("l", "r"):
        f.set(front & (C[:, 1] >= cy - 0.45) & (C[:, 1] < cy - 0.28) & (axn > 0.30) & (sd == v),
              Z("costal_margin_" + v, SIDE_ZH[v] + "肋缘（肋弓）", 0.8, "torso"))
    f.set(front & (C[:, 1] >= cy - 0.45) & (C[:, 1] < cy - 0.28) & (axn <= 0.30),
          Z("epigastrium", "上腹部", 0.85, "torso"))
    f.set(front & (C[:, 1] >= cy - 0.52) & (C[:, 1] < cy - 0.45) & (axn <= 0.60),
          Z("thoraco_abdominal_border", "胸腹交界（边界带）", 0.4, "torso",
            "膈不可见于体表，交界带内无法从网格可靠区分胸/腹"))
    # 下段：髋前（腹股沟带）先取，其余前下面全部归腹（含内裤覆盖下的基底皮肤）
    f.set(front & (C[:, 1] <= hip_y + 0.02) & (np.abs(C[:, 0]) > 0.12),
          Z("hip_front", "髋前（腹股沟区）", 0.75, "torso"))
    f.set(front & (C[:, 1] < cy - 0.52) & (axn <= 0.60),
          Z("abdomen", "腹", 0.85, "torso",
            "内裤覆盖段的基底皮肤仍按腹标注（点击命中内裤网格时报内裤覆盖层）"))
    # 臀：骶臀分界以下的背面（躯干与腿两个网格都有臀面，同 id 合并）
    f.set(back & (C[:, 1] <= butt_line) & (np.abs(C[:, 0]) < 0.055),
          Z("gluteal_cleft", "臀沟（臀裂壁）", 0.85, "body"))
    for v in ("l", "r"):
        f.set(back & (C[:, 1] <= butt_line) & (sd == v),
              Z("buttock_" + v, SIDE_ZH[v] + "臀", 0.9, "body"))
    f.set((C[:, 1] <= hip_y + 0.02) & (np.abs(C[:, 0]) <= 0.12),
          Z("perineum_body", "会阴周围（体表，内裤覆盖）", 0.55, "torso",
            "模型裆部被内裤网格遮挡，无裸露会阴语义"))
    n = f.fill_nearest()
    if n:
        print("Torso 边界兜底 fill_nearest: %d 面" % n)


# ---------------------------------------------------------------- 主流程


def main():
    global A_ANCH
    g = GLB(GLB_PATH)
    s, off, fmin, fmax = register_transform(g)
    print("scale=%.6f fit=%s..%s" % (s, np.round(fmin, 4), np.round(fmax, 4)))

    anat = json.load(open(ANAT_PATH, encoding="utf-8"))
    A_ANCH = {e["anatomical_label"]: np.array(e["world_xyz"]) for e in anat["entries"]}
    anch = json.load(open(ANCHORS_PATH, encoding="utf-8"))["anchors"]
    A_chest = np.array(anch["chest"]["world_xyz"])

    # ---------- Phase A：逐 primitive 收集面质心/法线 ----------
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
            C, N = face_data(P, T, s, off)
            name = mesh.get("name", "") + (("_%d" % (pi + 1)) if multi else "")
            prims.append({"name": name, "mat": mat, "C": C, "N": N, "nf": len(T)})

    found = sorted({p["mat"] for p in prims})
    print("材质清单:", found)
    known = HAIR_MATS | GARMENT_MATS | {"Arms", "Legs", "Torso", "Face", "Ears", "Lips",
                                        "Mouth", "Teeth", "Fingernails", "Toenails", "Sclera",
                                        "Irises", "Pupils", "Cornea", "EyeMoisture",
                                        "adskMatEyeMoisture", "EyeSocket", "Eyelashes"}
    unknown = [m for m in found if m not in known and not m.startswith(BRIEFS_PREFIX)]
    if unknown:
        raise RuntimeError("未预期材质，需扩展分类器: %s" % unknown)

    # ---------- Phase B：实测地标 ----------
    LM = measure_landmarks(prims, A_ANCH, A_chest)

    # ---------- Phase C：逐 primitive 分类 ----------
    meshes_out = {}
    zone_faces = {}
    zone_pts = {}
    for p in prims:
        zid = classify(p, LM, fmin, fmax, zone_pts)
        segs = []
        start = 0
        for i in range(1, len(zid) + 1):
            if i == len(zid) or zid[i] != zid[start]:
                segs.append([int(start), int(i), str(zid[start])])
                start = i
        meshes_out[p["name"]] = {"faces": int(p["nf"]), "segments": segs}
        for z in zid:
            zone_faces[z] = zone_faces.get(z, 0) + 1
        print("%-30s faces=%6d segs=%5d mat=%s" % (p["name"], p["nf"], len(segs), p["mat"]))

    # ---------- Phase D：幽灵标签剔除 + 汇总 ----------
    ghosts = [z for z in _order if zone_faces.get(z, 0) == 0]
    for zg in ghosts:
        labels.pop(zg)
    if ghosts:
        print("剔除幽灵标签:", ghosts)
    bad = [z for z in zone_faces if z not in labels]
    if bad:
        raise RuntimeError("segments 引用了未注册标签: %s" % bad)

    with open(GLB_PATH, "rb") as fh:
        sha8 = hashlib.sha256(fh.read()).hexdigest()[:8]
    doc = {
        "model": "Female3d66.glb",
        "sha256_prefix": sha8,
        "version": 1,
        "generated_by": "tools/build_female_face_semantics.py（材质语义网格边界 + 女性实测地标几何聚类）",
        "fit_info": {"min": [round(float(v), 4) for v in fmin],
                     "max": [round(float(v), 4) for v in fmax]},
        "honesty_note": (
            "女性模型逐面语义表。分割真值优先级：①材质语义网格边界（Arms/Legs/Torso/Face/Ears/Lips/"
            "Teeth/Fingernails/Toenails/眼球件/发片/衣物/内裤）为建模时硬边界；②网格内部细分为女性"
            "实测地标几何聚类（腕/肘/踝/膝/颈/肩/腰/髋=截面宽度极值，指/趾=k-means 簇，面区=锚点距离带），"
            "全部坐标由 glb_io 复刻运行时装配公式计算（与运行时 fitInfo 逐位吻合）。限制：乳房/会阴等"
            "被衣物覆盖处给出覆盖层下的区域归属并注明覆盖；头皮被发片覆盖报 head_base；发片本身按"
            "面片位置报发下体区（额/面部/颈/肩胸/后脑/后颈/背 …（发覆盖）），不报「头发」；"
            "胸腹交界（膈）不可见于体表，交界带按低置信标注。"),
        "meshes": meshes_out,
        "labels": {z: labels[z] for z in _order if z in labels},
    }
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    total_faces = sum(m["faces"] for m in meshes_out.values())
    total_segs = sum(len(m["segments"]) for m in meshes_out.values())
    print("\n→ %s  meshes=%d faces=%d segs=%d labels=%d（剔除幽灵 %d）"
          % (OUT_PATH, len(meshes_out), total_faces, total_segs, len(labels), len(ghosts)))
    print("zone 面数：")
    for z in sorted(zone_faces, key=lambda z: -zone_faces[z]):
        print("  %-30s %6d" % (z, zone_faces[z]))

    samples = {}
    for z, pts in zone_pts.items():
        pts = np.array(pts)                      # [cx,cy,cz,nx,ny,nz]
        pc, pn = pts[:, :3], pts[:, 3:]
        mean = pc.mean(0)
        k = int(np.argmin(np.linalg.norm(pc - mean, axis=1)))
        near = pc[k]
        # 视角按样本面法线定：肘窝/踝前等面位于 z<0 但朝前，从背视点击必被臂/腿背面遮挡
        samples[z] = {"point": [round(float(v), 4) for v in near],
                      "faces": int(zone_faces[z]),
                      "view": "front" if pn[k, 2] >= 0 else "back"}
    with open(SAMPLES_PATH, "w", encoding="utf-8") as fh:
        json.dump({"model": "Female3d66.glb", "zones": samples}, fh, ensure_ascii=False, indent=1)
    print("→ %s  zones=%d" % (SAMPLES_PATH, len(samples)))


if __name__ == "__main__":
    main()
