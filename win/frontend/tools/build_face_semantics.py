# -*- coding: utf-8 -*-
"""build_face_semantics.py — v19 逐三角面语义表构建器。

对 Custom3d66.glb 的每个 mesh 实例逐三角面分类（基于面质心世界坐标 + 面法线 +
连通域聚类），输出 web/face-semantics.json：
  { model, version, fit_info, meshes: { runtimeMeshName: { faces, segments: [[start,end,zoneId],…] } }, labels: { zoneId: {zh, conf, cat, yband?} } }

关键事实（tools/glb_io.py + 运行时探针验证）：
- 主体全身 = mesh-6（31016 面），GLTFLoader 按 primitive 拆为 5 个运行时 Mesh：
  -6-141_1(4608 头颈) _2(4116 面部中带) _3(5836 躯干) _4(7564 下肢+足) _5(8892 上肢+躯干侧)
  faceIndex 为各 primitive 局部编号（base 偏移 [0,4608,8724,14560,22124]）。
- 内裤覆盖件 = mesh-13（6794 面，ny≈0.457–0.566）→ 会阴/臀沟上段被遮挡。
- 头发 = mesh-14/15/16/17；眼球前部 = mesh-8/11；瞳孔 = mesh-9/12；
  眼睑 = mesh-2/3；眼眶周 = mesh-7/10；眼镜框 = mesh-1。
- 臀沟为真实镂空（左右臀顶点最近 |x|≈0.069）→ 标沟壁；耳被发型覆盖（无耳廓语义）。
- 趾部按趾尖层（z>0.35）x 一维聚类分趾；每趾趾尖 = 该趾 z 最大值前 2.2cm。

诚实性约束：无逐面标注数据/骨骼/UV 语义，本表为「几何聚类 + 人体测量学比例」推断，
每个 zone 附 provenance；会阴/耳等被遮挡部位如实标注遮挡来源。
"""
import json
import re
import sys
import numpy as np

sys.path.insert(0, "tools")
from glb_io import GLB, register_transform

GLB_PATH = "web/vendor/models/Custom3d66.glb"
OUT_PATH = "web/face-semantics.json"

# v20 手部逐节参数：来自 web/anatomy-labels.json 的五指锚点（z_band + 指尖 x），
# 掌/腕分界与掌指分界为 mesh-6 实测（tools 构建日志）。
# 指部归属不用 z_band 硬切（指根随扇形展开偏离锚点带）→ 用「点到指轴线距离」（xz 平面）。
LAB_PATH = "web/anatomy-labels.json"
with open(LAB_PATH, encoding="utf-8") as f:
    _lab = json.load(f)
FINGER_ZB = {}     # k -> (z0, z1) 右手带（|z| 镜像适用左手）
FINGER_TIPX = {}   # k -> |tip_x|（指尖 |x|，左右镜像）
for _e in _lab["entries"]:
    _m = re.match(r"right_(thumb|index|middle|ring|little)_finger$", _e["anatomical_label"])
    if _m:
        _k = _m.group(1)
        FINGER_ZB[_k] = (float(_e["z_band"][0]), float(_e["z_band"][1]))
        FINGER_TIPX[_k] = abs(float(_e["world_xyz"][0]))
assert len(FINGER_ZB) == 5, "anatomy-labels.json 五指锚点缺失：%s" % sorted(FINGER_ZB)
WRIST_X1 = 1.44    # 前臂/腕分界（腕最窄处实测 x≈1.44-1.52）
WRIST_X2 = 1.52    # 腕/掌分界
PALM_X1 = 1.65     # 掌指分界（掌块 1.52-1.65，指自掌缘伸出）
MCP_BAND = 0.008   # 关节带半宽（沿轴向 8mm）
THUMB_MCP_X = 1.70  # 拇指掌指关节 |x|（拇指短，MCP 比四指更靠远端；实测虎口脊线拐点）
THUMB_IP_FRAC = 0.55  # 拇指 IP 关节在指长中的比例（两节：近节略长）

# 指轴（xz 平面）：MCP2D → TIP2D。MCP z 由指尖带中心向掌心收拢 55%（扇形展开实测近似）。
_MCP_Z0 = 0.088
FINGER_AXES = {}
FINGER_SEG = {}
for _k in ("thumb", "index", "middle", "ring", "little"):
    _tipz = (_FZ := FINGER_ZB[_k]) and (_FZ[0] + _FZ[1]) / 2
    _mcpz = _MCP_Z0 + (_tipz - _MCP_Z0) * 0.55
    _mcpx = THUMB_MCP_X if _k == "thumb" else PALM_X1
    FINGER_AXES[_k] = (_mcpx, _mcpz, FINGER_TIPX[_k], _tipz)
    _L = FINGER_TIPX[_k] - _mcpx
    # 指节分界（t 比例，0=MCP 1=tip）：四指三等分（PIP 1/3、DIP 2/3）；拇指 IP 在 0.55
    if _k == "thumb":
        FINGER_SEG[_k] = {"ip": THUMB_IP_FRAC}
    else:
        FINGER_SEG[_k] = {"pip": 1 / 3.0, "dip": 2 / 3.0}
    print("v20 指轴[%s] MCP(%.3f,%.3f)->TIP(%.3f,%.3f) L=%.1fmm" % (_k, _mcpx, _mcpz, FINGER_TIPX[_k], _tipz, _L * 1000))

FINGER_HALF_TOL = 0.030   # 到指轴最大归属距离（半指厚+指缝），超出=非指部
def _finger_assign(px, pz):
    """xz 点 → (finger|None, t)：最近指轴与轴向参数（0=MCP 1=tip）。"""
    best, bt, bd = None, 0.0, 1e9
    for fk in ("thumb", "index", "middle", "ring", "little"):
        ax, az, bx, bz = FINGER_AXES[fk]
        dx, dz = bx - ax, bz - az
        t = ((px - ax) * dx + (pz - az) * dz) / (dx * dx + dz * dz)
        t = min(max(t, 0.0), 1.0)
        d = ((px - ax - t * dx) ** 2 + (pz - az - t * dz) ** 2) ** 0.5
        if d < bd:
            best, bt, bd = fk, t, d
    if bd > FINGER_HALF_TOL:
        return None, 0.0
    return best, bt

g = GLB(GLB_PATH)
s, off, fmin, fmax = register_transform(g)
H = fmax[1] - fmin[1]
CX = (fmin[0] + fmax[0]) / 2
CZ = (fmin[2] + fmax[2]) / 2
insts = g.mesh_instances()
body = next(it for it in insts if it["mesh_name"].endswith("-6-141"))
P, T = g.mesh_geometry(body)
W = P * s + off
C = W[T].mean(axis=1)
v0, v1, v2 = W[T[:, 0]], W[T[:, 1]], W[T[:, 2]]
N = np.cross(v1 - v0, v2 - v0)
N /= (np.linalg.norm(N, axis=1, keepdims=True) + 1e-12)

cny = (C[:, 1] - fmin[1]) / H
cnx = (C[:, 0] - CX) / ((fmax[0] - fmin[0]) / 2)
cnz = (C[:, 2] - CZ) / ((fmax[2] - fmin[2]) / 2)
axn = np.abs(cnx)
# v31 侧别修正：本 GLB 面向 +z 摆放，模型解剖学右肢在 x<0（用户两轮实测口述一致：
# u7 背面 x<0「右边大臂」、u10 正面 x<0「右手大臂」；且出厂眼球/眼睑/眼眶 mesh
# （-8-983/-3-634/-7-343）位于 x<0 —— 与 anatomy-labels 出厂标注的 left_* 同半球，
# 证实出厂即为观察者镜像误标）。x<0 → 解剖学右，id 与半球重新绑定（display 词不变）。
side = np.where(C[:, 0] < 0, "r", "l")
sideZh = np.where(C[:, 0] < 0, "右", "左")
MN_Y, HX = fmin[1], H

labels = {}   # zoneId -> {zh, conf, cat, yband?}
def Z(zid, zh, conf, cat, note=None):
    labels[zid] = {"zh": zh, "conf": conf, "cat": cat}
    if note:
        labels[zid]["note"] = note
    return zid

# —— zone 定义（先注册，供 body 分类引用）——
Z("neck", "颈", 0.85, "body")
Z("head_base", "头部（发覆盖基底）", 0.6, "head")
Z("nose_region", "鼻区", 0.85, "face")
Z("mouth_region", "唇区（上下唇未细分）", 0.8, "face")
Z("chin", "颏（下巴）", 0.8, "face")
# v21 删除死标签 Z("chest")：v20 躯干细分后分类器不再产出 chest（被 sternum/breast/abdomen 等取代），
# 保留会在表中注册 0 面幽灵标签 —— 与诚实性约束冲突。
Z("sternum", "胸骨", 0.85, "torso")
Z("chest_midline", "胸前中带（胸骨旁）", 0.75, "torso")
Z("breast_l", "左胸（乳房/胸大肌区）", 0.85, "torso")
Z("breast_r", "右胸（乳房/胸大肌区）", 0.85, "torso")
Z("chest_side_l", "左胸外侧", 0.8, "torso")
Z("chest_side_r", "右胸外侧", 0.8, "torso")
Z("chest_lower_l", "左胸下缘（肋弓下）", 0.8, "torso")
Z("chest_lower_r", "右胸下缘（肋弓下）", 0.8, "torso")
Z("costal_margin_l", "左肋缘（肋弓）", 0.8, "torso")
Z("costal_margin_r", "右肋缘（肋弓）", 0.8, "torso")
Z("xiphoid", "剑突区", 0.8, "torso")
Z("epigastrium", "上腹部", 0.85, "torso")
Z("thoraco_abdominal_border", "胸腹交界（边界位置，当前模型语义不足）", 0.4, "torso",
  "胸/腹解剖分界（膈）不可见于体表，交界带内无法从网格可靠区分")
Z("abdomen", "腹", 0.85, "torso")
# v25：胸上区/锁骨上带/颈根（修复 y 1.24-1.78 前侧兜底漏入 abdomen —— 用户实测斜方肌处
# 点击报「腹」）。实测前中线轮廓：y 1.60 起前缘 z 回升 = 颈圆柱起点；y 1.80 骤降 = 下颌。
Z("chest_upper", "胸上区（胸骨上窝以下，锁骨水平以上）", 0.8, "torso")
Z("supraclavicular_l", "左锁骨上区（颈根外侧）", 0.75, "torso",
  "锁骨上窝与斜方肌前缘体表连续，无法从网格精确分界")
Z("supraclavicular_r", "右锁骨上区（颈根外侧）", 0.75, "torso",
  "锁骨上窝与斜方肌前缘体表连续，无法从网格精确分界")
Z("back_upper", "背（上）", 0.85, "torso")
Z("back_lower_waist", "腰/背（下）", 0.8, "torso")
Z("flank", "侧腹", 0.75, "torso")
Z("hip_front", "髋前（腹股沟韧带区）", 0.75, "torso")
# v26：hip_front 逐面表已删除（前侧腹股沟区被内裤覆盖，可见皮肤=大腿根）；
# 此标签仅为 zoneFromPoint 几何兜底保留同名带（|x|>=0.14 前侧 ny<=0.44）。
Z("chin", "颏（下巴）", 0.8, "face")
Z("cheek_ear_zone", "面颊/耳前区", 0.7, "face")
Z("forehead", "额", 0.75, "face")
Z("occiput", "后脑", 0.75, "head")
Z("head_top", "头顶", 0.7, "head")
Z("head_side", "头侧（颞区，发际内）", 0.6, "hair")
for _sd in ("l", "r"):
    Z(f"shoulder_up_{_sd}", ("左" if _sd == "l" else "右") + "肩", 0.8, "limb")
    Z(f"elbow_tip_{_sd}", ("左" if _sd == "l" else "右") + "肘尖", 0.85, "limb")
    Z(f"upper_arm_{_sd}", ("左" if _sd == "l" else "右") + "上臂", 0.85, "limb")
# v26 删除 thigh_hip_border_*（前侧髋-大腿过渡带）：v26 重定界后前外侧裸露区即大腿，
# 仅保留背面臀褶 gluteal_fold_* 作为可见解剖标志，0 面幽灵标签不再注册。
# v21 删除死标签 Z("shoulder")：肩部由 shoulder_up_*（肩带）承担，0 面幽灵标签删除。
Z("armpit_l", "左腋窝", 0.8, "limb")
Z("armpit_r", "右腋窝", 0.8, "limb")
Z("perineum_body", "会阴周围（体表，内裤覆盖）", 0.55, "torso",
  "模型裆部被内裤 mesh 遮挡，无裸露会阴语义")
for sd in ("l", "r"):
    szh = "左" if sd == "l" else "右"
    Z(f"buttock_{sd}", szh + "臀", 0.9, "body")
    Z(f"cleft_{sd}", szh + "臀沟（臀裂壁）", 0.85, "body")
    Z(f"gluteal_fold_{sd}", szh + "臀腿交界（臀褶）", 0.85, "body")
    Z(f"thigh_{sd}", szh + "大腿", 0.9, "limb")
    Z(f"knee_{sd}", szh + "膝", 0.85, "limb")
    Z(f"popliteal_{sd}", szh + "膝窝（腘）", 0.85, "limb")
    Z(f"calf_{sd}", szh + "小腿", 0.9, "limb")
    Z(f"ankle_{sd}", szh + "踝", 0.85, "limb")
    Z(f"ankle_calf_{sd}", szh + "踝-小腿交界", 0.8, "limb")
    Z(f"heel_{sd}", szh + "脚跟", 0.9, "foot")
    Z(f"heel_back_{sd}", szh + "脚跟后侧", 0.85, "foot")
    Z(f"arch_{sd}", szh + "足弓", 0.85, "foot")
    Z(f"forefoot_{sd}", szh + "前脚掌", 0.85, "foot")
    Z(f"instep_{sd}", szh + "脚背", 0.85, "foot")
    Z(f"foot_rim_{sd}", szh + "足缘（脚背-脚底过渡）", 0.75, "foot")
    # v21 删除 finger_zone_*（v20 的「指级由锚点细分」低置信带）：v20 起手部已逐面细分
    # （逐指轴归属），该带从未产出面 → 0 面幽灵标签。运行时的锚点让位逻辑（fsemYieldsToFinger）
    # 保留无害——表中已无 finger_zone_ 前缀，条件恒 False。
    # v20 手指逐节：每指 3 节（拇指 2 节）+ 节间关节带（MCP 掌指 / PIP 近端指间 / DIP 远端指间 / 拇指 IP）
    for fk, fzh in (("index", "食指"), ("middle", "中指"), ("ring", "无名指"), ("little", "小指")):
        f = "%s_%s" % (sd, fk)
        Z(f"proximal_phalanx_{f}", f"{szh}{fzh}近节", 0.85, "finger")
        Z(f"middle_phalanx_{f}", f"{szh}{fzh}中节", 0.85, "finger")
        Z(f"distal_phalanx_{f}", f"{szh}{fzh}远节", 0.85, "finger")
        Z(f"mcp_{f}", f"{szh}{fzh}掌指关节（指根）", 0.8, "finger")
        Z(f"pip_{f}", f"{szh}{fzh}近端指关节", 0.8, "finger")
        Z(f"dip_{f}", f"{szh}{fzh}远端指关节", 0.8, "finger")
    # v21 拇指细分（拇指真实两节 + 掌骨）：掌骨区（第一掌骨，虎口-掌指间桡侧）、
    # 近节、远节、指间关节 IP、指尖（远节最远端指腹）。拇指无 PIP/DIP/中节——
    # v20 表曾注册 pip/dip/middle_phalanx_*_thumb 幽灵标签（0 面，伪造解剖），v21 删除。
    Z(f"metacarpal_{sd}_thumb", f"{szh}拇指掌骨区（第一掌骨/虎口桡侧）", 0.75, "finger",
      "拇指掌骨与第一背侧骨间肌（虎口）体表连续，无法从网格精确分界")
    Z(f"proximal_phalanx_{sd}_thumb", f"{szh}拇指近节", 0.85, "finger")
    Z(f"distal_phalanx_{sd}_thumb", f"{szh}拇指远节", 0.85, "finger")
    Z(f"ip_{sd}_thumb", f"{szh}拇指指间关节", 0.8, "finger")
    # v24 修复：mcp_*_thumb 在分类器中产出（|x| 距 THUMB_MCP_X ≤8mm 的关节带），
    # 但此前从未注册 label → faceSemFor 对该带返回 null（落锚点/型带兜底）。补注册。
    Z(f"mcp_{sd}_thumb", f"{szh}拇指掌指关节（指根）", 0.8, "finger")
    Z(f"tip_{sd}_thumb", f"{szh}拇指指尖", 0.85, "finger")
    for ti, tn in enumerate(["拇趾", "第二趾", "第三趾", "第四趾", "小趾"]):
        Z(f"toe_{sd}_{ti}", f"{szh}{tn}", 0.9, "toe")
        Z(f"toe_tip_{sd}_{ti}", f"{szh}{tn}尖", 0.9, "toe")
    Z(f"shoulder_up_{sd}", szh + "肩", 0.8, "limb")
    Z(f"upper_arm_{sd}", szh + "上臂", 0.85, "limb")
    Z(f"elbow_{sd}", szh + "肘", 0.85, "limb")
    Z(f"cubital_{sd}", szh + "肘窝（肘前内侧）", 0.8, "limb")
    Z(f"elbow_tip_{sd}", szh + "肘尖", 0.85, "limb")
    Z(f"forearm_{sd}", szh + "前臂", 0.9, "limb")
    Z(f"wrist_{sd}", szh + "腕", 0.85, "limb")
    Z(f"palm_{sd}", szh + "掌", 0.8, "hand")
Z("head_hair", "头发（发型层）", 0.9, "hair")
Z("ear_zone_l", "左耳区（被发型覆盖，当前 GLB 无耳廓语义）", 0.5, "hair",
  "耳廓未建模，被发型网格覆盖；需导入逐部位分层模型或语义分割贴图")
Z("ear_zone_r", "右耳区（被发型覆盖，当前 GLB 无耳廓语义）", 0.5, "hair",
  "耳廓未建模，被发型网格覆盖；需导入逐部位分层模型或语义分割贴图")
Z("eyeball_l", "左眼球（前部）", 0.9, "eye")
Z("eyeball_r", "右眼球（前部）", 0.9, "eye")
Z("iris_l", "左瞳孔（虹膜区）", 0.9, "eye")
Z("iris_r", "右瞳孔（虹膜区）", 0.9, "eye")
Z("eyelid_l", "左眼睑区", 0.8, "eye")
Z("eyelid_r", "右眼睑区", 0.8, "eye")
Z("periorbital_l", "左眼眶区", 0.75, "eye")
Z("periorbital_r", "右眼眶区", 0.75, "eye")
Z("glasses_frame", "眼镜框", 0.9, "glasses")
Z("briefs_front", "下腹/腹股沟（内裤覆盖）", 0.8, "garment")
Z("briefs_back", "腰骶带（内裤覆盖）", 0.8, "garment")
Z("briefs_side", "髋侧（内裤覆盖）", 0.8, "garment")
Z("briefs_perineum", "会阴周围（内裤覆盖，模型无裸露会阴）", 0.6, "garment",
  "会阴被内裤遮挡，仅能给出覆盖层位置与精确坐标")
Z("briefs_cleft", "臀沟上段（内裤覆盖）", 0.7, "garment")

# ============ mesh-6（主体）逐面分类 ============
def classify_body_clean():
    y = C[:, 1]
    nyf, nzf, nxf = N[:, 1], N[:, 2], N[:, 0]
    zid = np.full(len(T), None, dtype=object)

    def put(mask, fn, force_over=None):
        # force_over: 仅当当前标签属于该集合时强制覆盖（v21 臂区从 flank/armpit 夺回被抢面）
        idx = np.where(mask)[0]
        for i in idx:
            if zid[i] is None:
                zid[i] = fn(i)
            elif force_over and zid[i] in force_over:
                zid[i] = fn(i)

    foot = y < -2.24
    toe_zone = foot & (C[:, 2] > 0.295)
    sole = foot & (nyf < -0.5) & ~toe_zone
    dorsum = foot & (nyf > 0.4) & ~toe_zone
    rim = foot & ~toe_zone & ~sole & ~dorsum
    tip_layer = foot & (C[:, 2] > 0.35)
    toe_bins, toe_zmax = {}, {}
    for sd, sgn in (("l", 1), ("r", 1)):   # 用 |x| 统一处理
        m = tip_layer & (np.abs(C[:, 0]) > 0.3) & ((C[:, 0] < 0) if sd == "r" else (C[:, 0] > 0))   # v31 侧别：x<0=右
        xs = np.sort(np.abs(C[m, 0]))
        ctr = np.linspace(xs.min() + 0.004, xs.max() - 0.004, 5)
        for _ in range(80):
            idxm = np.argmin(np.abs(xs[:, None] - ctr[None, :]), axis=1)
            for k in range(5):
                if (idxm == k).any():
                    ctr[k] = xs[idxm == k].mean()
        ctr = np.sort(ctr)
        bounds = (ctr[:-1] + ctr[1:]) / 2
        toe_bins[sd] = (ctr, bounds)
        print("  趾尖层聚类[%s] 中心=%s 边界=%s" % (sd, np.round(ctr, 4), np.round(bounds, 4)))
        for ti in range(5):
            lo = bounds[ti - 1] if ti > 0 else 0.0
            hi = bounds[ti] if ti < 4 else 10.0
            mm = toe_zone & ((C[:, 0] < 0) if sd == "r" else (C[:, 0] > 0)) & (np.abs(C[:, 0]) >= lo) & (np.abs(C[:, 0]) < hi)   # v31 侧别：x<0=右
            toe_zmax[(sd, ti)] = C[mm, 2].max() if mm.any() else 0.4
    print("  趾 zmax:", {"%s%d" % k: round(v, 4) for k, v in toe_zmax.items()})

    for sd in ("l", "r"):
        sg = (C[:, 0] < 0) if sd == "r" else (C[:, 0] > 0)   # v31 侧别：x<0=右
        ctr, bounds = toe_bins[sd]
        xm = np.abs(C[:, 0])
        for ti in range(5):
            lo = bounds[ti - 1] if ti > 0 else 0.0
            hi = bounds[ti] if ti < 4 else 10.0
            m = toe_zone & sg & (xm >= lo) & (xm < hi)
            zmax = toe_zmax[(sd, ti)]
            put(m & (C[:, 2] > zmax - 0.022), lambda i, ti=ti, sd=sd: f"toe_tip_{sd}_{ti}")
            put(m, lambda i, ti=ti, sd=sd: f"toe_{sd}_{ti}")
        put(sole & sg & (C[:, 2] < -0.08), lambda i, sd=sd: f"heel_{sd}")
        # v21 脚跟后侧：法线朝正后方（nzf<-0.6）的足部弧面（跟腱-跟骨后突），v20 幽灵 zone（0 面）修复。
        # 必须在 sole/rim 之前 put；法线朝后与朝下(sole)/朝上(dorsum)几乎互斥，误吞风险极小。
        put(foot & sg & (nzf < -0.6), lambda i, sd=sd: f"heel_back_{sd}")
        put(sole & sg & (C[:, 2] >= -0.08) & (C[:, 2] < 0.10), lambda i, sd=sd: f"arch_{sd}")
        put(sole & sg, lambda i, sd=sd: f"forefoot_{sd}")
        put(dorsum & sg, lambda i, sd=sd: f"instep_{sd}")
        put(rim & sg, lambda i, sd=sd: f"foot_rim_{sd}")

    leg = (y >= -2.24) & (y < -1.44)
    put(leg & (y < -2.14), lambda i: f"ankle_{side[i]}")
    put(leg & (y < -2.075), lambda i: f"ankle_calf_{side[i]}")
    put(leg, lambda i: f"calf_{side[i]}")
    # v28 膝带以「用户实测点击 + 人体测量学」双锚定界（v25-v27 三版皆以几何代理
    # 定界而反复错位）。决定性证据：
    #   1. 用户实测髌骨点击 world y −1.037/−1.065（z +0.144/+0.141，正落前凸面
    #      zmax 峰）三轮报「大腿」；上轮 v26 同位点 −1.070/−1.076 同诉。
    #   2. 膝关节线人体测量学：膝中心 ≈ 身高 28% → y = −2.44 + 0.28×5 ≈ −1.04，
    #      与用户点击吻合；v27 误把宽峰 −1.50~−1.44 当「髁」——实为腓肠肌最宽点
    #      （关节线下 14cm，属小腿），膝带因此整体低了 0.3~0.5。
    #   3. 前凸剖面（x −0.50..−0.25，0.02 层）：zmax 自 −1.30（+0.027，髌韧带/
    #      胫骨粗隆）升至 −1.00（+0.160，髌骨体顶），髌骨中心 ≈ −1.05。
    # 新带：knee [−1.44,−0.942)（髌骨±关节区，用户膝点 −1.037..−1.076 全部距
    # 边界 ≥0.10）；calf 上缘 −1.56→−1.44（腓肠肌最宽点 −1.50~−1.44 归小腿）；
    # popliteal 背面 z<−0.20 同步；与 thigh 前/臀背带 [−0.942,−0.19) 无缝拼接。
    put((y >= -1.44) & (y < -0.942) & (C[:, 2] < -0.20), lambda i: f"popliteal_{side[i]}")
    put((y >= -1.44) & (y < -0.942), lambda i: f"knee_{side[i]}")
    # [−0.942,−0.19) 由下方既有分支处理（v29 起：背面 y<−0.36 大腿 / [−0.36,−0.13) 臀 / 前外侧 thigh / 前中 perineum_body）
    # v26 下身重定界（用户实测：裆上点击报「髋前·上份」、大腿中上点击报「近端偏中」——
    # 整体下移感）。实测几何：视觉大腿根（裆底/股会合）y≈-0.33，内裤下缘 y≈-0.20，
    # 膝上缘 y≈-1.44；旧 thigh band 只从 -0.942 起把大腿上 58% 划给了「髋前/侧腹」，
    # relT 分母被截断 → 描述系统性偏低。新口径：
    #   大腿   = y -1.441..-0.19 前外侧裸露皮肤（含股根到内裤下缘；腹股沟皱褶本身
    #          被内裤覆盖 → 诚实标注走 briefs_front，可见皮肤即大腿根）
    #   臀     = y -0.36..-0.13 背面（v29 重定界，见下方注释）
    #   会阴周 = y -0.46..-0.19 前中 |x|<0.16（裆底裸露缝）
    # hip_front（髋前）在前侧再无可见皮肤，标签随之删除（zoneFromPoint 兜底仍保留同名带）。
    medial = nxf * C[:, 0] < 0
    # v29 臀腿背面重定界（用户 u4/u5 四点实测：臀褶下/大腿后侧报「臀」「腰背」）。
    # 旧带 buttock [−0.942,−0.19) 把大腿后侧整段吞成臀。重测（剔除内裤 mesh 后纯皮肤面
    # 宽度剖面 y −1.00..−0.12 无台阶——旧「臀沟≈−0.60」系内裤面混入的假象）；改用双锚：
    #   u4 右点 world y −0.247 视觉正落臀褶 + 裆底 −0.33 → 臀块下缘（臀沟）≈ −0.34。
    #   臀     = y [−0.36,−0.13) 背面（上缘 −0.36 与腰骶留过渡，下缘含臀褶褶面）
    #   臀褶   = y [−0.36,−0.22) 背面 z<−0.06（褶皱凹带）
    #   大腿背面 = y [−0.942,−0.36)（u5 两点 −0.570/−0.597 落带中央）
    #   大腿前外 = y [−0.942,−0.19)（不变）；perineum_body 不变
    put((y >= -0.942) & (y < -0.36) & (C[:, 2] < 0.0), lambda i: f"thigh_{side[i]}")
    gl = (y >= -0.36) & (y < -0.13) & (C[:, 2] < 0.0)
    put(gl & (y < -0.22) & (C[:, 2] < -0.06), lambda i: f"gluteal_fold_{side[i]}")
    put(gl & (C[:, 2] < -0.02) & (np.abs(C[:, 0]) < 0.16) & medial, lambda i: f"cleft_{side[i]}")
    put(gl, lambda i: f"buttock_{side[i]}")
    put((y >= -0.46) & (y < -0.19) & (np.abs(C[:, 0]) < 0.16) & (C[:, 2] >= -0.02), lambda i: "perineum_body")
    put((y >= -0.942) & (y < -0.19) & (C[:, 2] >= -0.02), lambda i: f"thigh_{side[i]}")
    # v29 腋窝收窄（用户 u1 两点肩前/三角肌前缘报「腋窝」）：旧 mask nyf<0.35 & y<1.44
    # 把肩前吞进来。真腋窝与肩前的区分特征（u3 vs u1 实测法线）：
    #   腋窝凹陷底 u3：nyf −0.53..−0.65（强朝下）、z −0.02..−0.16（腋窝腔偏背侧）
    #   肩前三角肌 u1：nyf −0.18..+0.47（朝前/平）、z +0.00..+0.02（前缘）
    # 新带：y [1.0,1.32) & nyf<−0.20（强朝下=凹陷底，u1 各点 nyf≥−0.18 全排除）&
    # z −0.20..−0.02（v30 加下界：真腋窝底实测 z −0.02..−0.16；更靠后的臂根后/后腋褶
    # 下缘 nyf 也朝下但 z −0.32，曾被误吞成「腋窝·后侧」→ 归 v30 臂根后带 upper_arm）；
    # y≥1.32 或 nyf>−0.20 的肩前区由 shoulder 前带接住。
    amp = (np.abs(C[:, 0]) > 0.50) & (np.abs(C[:, 0]) < 0.90) & (y >= 1.0) & (y < 1.32) & (nyf < -0.20) & (C[:, 2] > -0.20) & (C[:, 2] < -0.02)
    put(amp, lambda i: f"armpit_{side[i]}")
    # v29 肩前带（u1 两点 world y 1.13/1.42 nyf −0.18..+0.47 nzf +0.64..+0.95）：
    # 三角肌前缘-锁骨下窝外侧，属「肩」；nyf>−0.25 排除腋窝凹陷底（实测 ≤−0.53），
    # z>−0.10 排除腋后，y<1.45 上限防吞 supraclavicular（颈根外侧 y≥1.45）。
    # 必须在躯干 chest_side/flank 之前（axn 0.28-0.67 会被 chest_side>0.42 抢）。
    shfront = (np.abs(C[:, 0]) >= 0.42) & (np.abs(C[:, 0]) < 1.03) & (y >= 1.12) & (y < 1.45) & (nyf > -0.25) & (C[:, 2] > -0.10)
    put(shfront, lambda i: f"shoulder_up_{side[i]}", force_over={"flank", "chest_side_l", "chest_side_r", "hip_front"})
    # 臂：A-pose 外张，用臂轴投影 t 分带（轴：肩(0.92,1.40,0.05)→指尖(1.85,0.04,0.10)）
    # ——必须在躯干 flank 之前（否则肘/腕/掌被 axn>0.42 的 flank 抢走）
    # 左右镜像：把 |x| 折到右臂坐标系再投影（镜像面 x=0，z/y 不变），否则左臂 t 全为 0
    AX_S = np.array([0.92, 1.40, 0.05]); AX_F = np.array([1.85, 0.04, 0.10])
    d = AX_F - AX_S
    Cm = np.stack([np.abs(C[:, 0]), C[:, 1], C[:, 2]], axis=1)
    at = np.clip(((Cm - AX_S) @ d) / (d @ d), 0, 1.2)
    arm = (at > 0.14) & (axn > 0.55) & (y >= -0.44) & (y < 1.778)
    # v20 手部逐节（必须在 at 分带前处理——at 投影在手部失真：at=0.88 对应 x≈1.74，
    # 旧 forearm/wrist/palm 带把掌块和指根整体吞掉。手部按世界 |x| + 指轴几何分段）：
    #   腕 |x| 1.44-1.52 → 掌块 |x| 1.52-1.65 → 指部 = 指轴归属（点到指轴距离 ≤30mm）
    axw = np.abs(C[:, 0])
    _hand = arm & (axw >= WRIST_X1) & (C[:, 1] >= -0.44)
    put(_hand & (axw < WRIST_X2), lambda i: f"wrist_{side[i]}")
    put(_hand & (axw >= WRIST_X2) & (axw < PALM_X1 - MCP_BAND), lambda i: f"palm_{side[i]}")
    # 指部：逐面算最近指轴（xz 平面）→ 指名 + 轴向 t（0=MCP 1=tip）；近/中/远节 + 关节带
    # 拇指两节（IP）；四指三节（PIP 近端指间 / DIP 远端指间）；MCP 掌指关节 = PALM_X1 ± 8mm
    azabs = np.abs(C[:, 2])
    for i in np.where(_hand)[0]:
        if zid[i] is not None:
            continue
        fk, t = _finger_assign(axw[i], azabs[i])
        if fk is None:
            continue   # 掌背中央/虎口等远离指轴 → palm 兜底
        sd2 = side[i]
        if fk == "thumb":
            # v21 拇指五区：指尖（轴向最远端 12%）→ 远节 → IP → 近节 → 掌骨区（t≈0 段）
            if abs(axw[i] - THUMB_MCP_X) <= MCP_BAND:
                zid[i] = f"mcp_{sd2}_thumb"
            elif t >= 0.88:
                zid[i] = f"tip_{sd2}_thumb"
            elif t >= THUMB_IP_FRAC - 0.09 and t <= THUMB_IP_FRAC + 0.09:
                zid[i] = f"ip_{sd2}_thumb"
            elif t > THUMB_IP_FRAC + 0.09:
                zid[i] = f"distal_phalanx_{sd2}_thumb"
            elif t < 0.18:
                zid[i] = f"metacarpal_{sd2}_thumb"
            else:
                zid[i] = f"proximal_phalanx_{sd2}_thumb"
        else:
            sg = FINGER_SEG[fk]
            if abs(axw[i] - PALM_X1) <= MCP_BAND:
                zid[i] = f"mcp_{sd2}_{fk}"
            elif t >= sg["dip"] - 0.09 and t <= sg["dip"] + 0.09:
                zid[i] = f"dip_{sd2}_{fk}"
            elif t >= sg["pip"] - 0.09 and t <= sg["pip"] + 0.09:
                zid[i] = f"pip_{sd2}_{fk}"
            elif t > sg["dip"] + 0.09:
                zid[i] = f"distal_phalanx_{sd2}_{fk}"
            elif t < sg["pip"] - 0.09:
                zid[i] = f"proximal_phalanx_{sd2}_{fk}"
            else:
                zid[i] = f"middle_phalanx_{sd2}_{fk}"
    # 掌部兜底：hand 内远离指轴的剩余面（掌背中央/虎口，x≥掌指界处是拇指蹼）
    put(_hand & (zid == None), lambda i: f"palm_{side[i]}")
    # —— 肘/前臂/上臂（仅手部以外的臂面）——
    # v21 修复：v20 用 axn>0.55（实为 |x|>1.017）定义 arm → 上臂主体（|x| 0.85-1.017）被
    # flank/armpit 吞掉；forearm(0.76-0.88) 全被 hand(at≥0.56) 与 elbow(0.50-0.76) 吞 → 0 面。
    # 实测沿轴：腕 |x|=1.44 → at≈0.56；肘尖带 at 0.15-0.35（后突 z<-0.17）；肘窝 at 0.39-0.70。
    # 新分带（put 顺序 = 优先级）：肘尖 → 肘环 → 肘窝 → 上臂(|x|≥0.85, at<0.34, 腋底以上) →
    # 前臂(at 0.34-0.72, |x|<1.44, 腕上) → 肩带（|x| 0.50-1.03 & y 1.30-1.78 & 法线朝上外）。
    _rest = arm & (axw < WRIST_X1)
    _STEAL = {"flank", "hip_front", "shoulder_up_l", "shoulder_up_r"}   # v32: 肩带下缘的三角肌近端归上臂
    # 距臂轴距离守卫：臂柱面 dist<0.25；躯干侧壁（侧腰/胸外侧）dist>0.35 —— 高置信分离
    _armdist = np.linalg.norm(Cm - (AX_S + np.clip(at, 0, 1.2)[..., None] * d), axis=1)
    put(_rest & (C[:, 2] < -0.17) & (at < 0.42), lambda i: f"elbow_tip_{side[i]}", force_over=_STEAL)
    put(_rest & (C[:, 2] < -0.10) & (at < 0.42), lambda i: f"elbow_{side[i]}", force_over=_STEAL)  # 肘尖周边过渡环
    put(_rest & (C[:, 2] > 0.04) & medial & (at < 0.60), lambda i: f"cubital_{side[i]}", force_over=_STEAL)
    # 上臂：|x|≥0.80 的臂柱段（腋底 y≥0.80 至肘 at 0.34）。v30b：dist 守卫 0.25→0.36、
    # |x| 下界 0.85→0.80（用户实测臂根前外柱面 axw 0.839/armdist 0.268 报「胸外侧」——
    # 双重卡在旧阈值外；at>0.10 守卫仍隔离躯干侧壁——躯干侧壁沿轴投影 <0.08）。
    put((axw >= 0.80) & (axw < WRIST_X1) & (at > 0.10) & (at < 0.34) & (y >= 0.80) & (y < 1.10) & (_armdist < 0.36),
        lambda i: f"upper_arm_{side[i]}", force_over=_STEAL)
    # v31 臂根前外窄带（用户 u10：正面屏幕左臂上端前外 local(-0.269,1.301,0.002) 报「腹，
    # 前外侧」——命中面 axw 0.734/armdist 0.364，与 v30b 带（axw≥0.80 & dist<0.36）双重卡外，
    # 落 abdomen 兜底。axw 0.70-0.80 y 0.95-1.12 窗口仅 10 面/侧（现表 abdomen 12 +
    # chest_side 6），y 下界 0.95 防吞下方 chest_side；armpit 先 put 的面不夺（first-wins）。
    put((axw >= 0.70) & (axw < 0.80) & (at > 0.10) & (at < 0.34) & (y >= 0.95) & (y < 1.12) & (_armdist < 0.40) & (C[:, 2] >= -0.15),
        lambda i: f"upper_arm_{side[i]}", force_over=_STEAL)
    # v32：三角肌近端可见面修正。用户实测屏幕右上臂点（局部约 0.252,1.335,0）
    # 命中 world y≈1.14、臂轴 at≈0.20，却被前肩带先归为 shoulder_up；该处已明显位于
    # 肱骨段而非肩峰关节。以臂轴投影 + 距离守卫扩展上臂到肩带下缘，保留 at≤0.14 的肩关节。
    put((axw >= 0.55) & (axw < 0.86) & (at > 0.04) & (at < 0.30) & (y >= 1.05) & (y < 1.24) & (_armdist < 0.48) & (C[:, 2] >= -0.12),
        lambda i: f"upper_arm_{side[i]}", force_over=_STEAL)
    # v30 臂根后带（用户实测：背面臂根后侧/三角肌后缘点击 world y 1.226, |x| 0.71-0.79,
    # z −0.31 报「背(上)·后侧·中份」，应为大臂）。落空链：shfront 只管 z>−0.10、shband
    # 要求 y≥1.30 & nyf>0.25、旧 upper_arm 要求 at>0.10（该面 at≈0.02）→ 被 back_upper
    # 兜底吞掉。剖面证据：背面 y 1.00-1.50 各层 |x| 0.5-0.6 处近空（0 面），躯干背
    # （|x|<0.5）与臂根后（|x|≥0.6）天然分离。守卫：armdist<0.48（躯干背该高度 >0.5）、
    # nyf>−0.5（排除朝下的腋窝底）、y<1.38 上限（肩顶背面留 shband）。
    put((axw >= 0.60) & (axw < 1.03) & (y >= 1.02) & (y < 1.38) & (C[:, 2] <= -0.10) & (nyf > -0.5) & (_armdist < 0.48),
        lambda i: f"upper_arm_{side[i]}", force_over=_STEAL)
    # v29 前臂守卫放宽（用户 u2/u6 前臂前/背面报「侧腹」「腰背(下)」）：旧 armdist<0.25
    # 只接住臂柱核心面，柱缘（armdist 0.30-0.35）被 flank/back_lower_waist 兜底吞掉。
    # 实测 u2 点 armdist 0.310、u6 点 0.323；躯干侧壁 armdist>0.40，0.36 留 0.04 余量。
    put((axw >= 0.85) & (axw < WRIST_X1) & (at >= 0.34) & (at < 0.72) & (y < 0.90) & (_armdist < 0.36),
        lambda i: f"forearm_{side[i]}", force_over=_STEAL)
    shband = (axw >= 0.50) & (axw < 1.03) & (y >= 1.30) & (y < 1.78) & (nyf > 0.25)
    put(shband, lambda i: f"shoulder_up_{side[i]}", force_over=_STEAL)
    torso = (y >= -0.19) & (y < 1.778)
    put(torso & (C[:, 2] < -0.15) & (y >= 0.62) & (y < 1.62), lambda i: "back_upper")
    # v25：背面 y>=1.62（项部/斜方肌上缘，后缘 z 已收窄至 -0.32~-0.24）归颈，不再算背
    put(torso & (C[:, 2] < -0.15) & (y >= 1.62) & (axn < 0.30), lambda i: "neck")
    put(torso & (C[:, 2] < -0.15) & (y >= 1.62) & (axn >= 0.30) & (axn < 0.50),
        lambda i: f"supraclavicular_{side[i]}")   # 斜方肌前缘过渡带
    put(torso & (C[:, 2] < -0.15), lambda i: "back_lower_waist")
    # —— v20 躯干前面细分（实测：胸骨区前缘 y 0.92-1.20 微凸 z≈0.29，
    #     剑突/上腹凹 y 0.84-0.92，胸乳区 y 0.64-1.06，肋缘斜带 y 0.72-0.92 外高内低，
    #     胸/腹旧界 y=0.62 误把中腹上部归胸 —— 全部重画）——
    fr = torso & (C[:, 2] >= -0.15)
    axc = np.abs(C[:, 0])
    # 1) 胸腹交界诚实带（y 0.78-0.84 中心区）：膈分界体表不可见，不强行猜测
    put(fr & (y >= 0.78) & (y < 0.84) & (axc < 0.30), lambda i: "thoraco_abdominal_border")
    # 2) 胸骨（|x|<0.075, y 0.92-1.24 前中窄带）
    put(fr & (y >= 0.92) & (y < 1.24) & (axc < 0.075), lambda i: "sternum")
    # 3) 剑突（y 0.84-0.92 中带）
    put(fr & (y >= 0.84) & (y < 0.92) & (axc < 0.075), lambda i: "xiphoid")
    # 4) 胸骨旁中带（y 0.92-1.24，0.075<=|x|<0.14）
    put(fr & (y >= 0.92) & (y < 1.24) & (axc < 0.14), lambda i: "chest_midline")
    # 5) 肋缘（y 0.78-0.96 侧前斜带）：外高内低的肋弓下缘
    put(fr & (y >= 0.78) & (y < 0.96) & (axc >= 0.14) & (axc < 0.32), lambda i: f"costal_margin_{side[i]}")
    # 6) 胸下缘（y 0.72-0.78 两侧带，肋缘下方过渡）
    put(fr & (y >= 0.72) & (y < 0.78) & (axc >= 0.14) & (axc < 0.32), lambda i: f"chest_lower_{side[i]}")
    # 7) 胸乳区（y 0.84-1.24 两侧带 0.14<=|x|<0.32）
    put(fr & (y >= 0.84) & (y < 1.24) & (axc < 0.32), lambda i: f"breast_{side[i]}")
    # 8) 胸外侧（|x|>=0.32 或 flank 带上部）
    put(torso & (axn > 0.42) & (y >= 0.72), lambda i: f"chest_side_{side[i]}")
    put(torso & (axn > 0.42), lambda i: "flank")
    # 9) 上腹（y 0.42-0.78 前面中带）；腹（其余前面）
    put(fr & (y >= 0.42) & (y < 0.78) & (axc < 0.32), lambda i: "epigastrium")
    # v25：y>=1.24 前侧不再漏入 abdomen —— 胸上区 / 锁骨上带 / 颈根
    # 实测轮廓（前中线）：y 1.24-1.45 = 胸骨上窝以下胸上区；y 1.45-1.62 = 锁骨上/颈根
    # （前缘 z 收窄至 0.06-0.12）；y>=1.62 前缘 z 回升（颈圆柱）→ neck 前段下探到 1.62。
    put(fr & (y >= 1.24) & (y < 1.45) & (axc < 0.42), lambda i: "chest_upper")
    put(fr & (y >= 1.45) & (y < 1.62) & (axc < 0.20), lambda i: "neck")   # 颈根前中（胸骨上窝上）
    put(fr & (y >= 1.45) & (y < 1.778) & (axc >= 0.20) & (axc < 0.50),
        lambda i: f"supraclavicular_{side[i]}")
    put(fr & (y >= 0.42) & (y < 1.24), lambda i: "abdomen")
    put(fr, lambda i: "abdomen")
    head = y >= 1.778
    put(head & (y < 1.845) & (cnz <= 0.30), lambda i: "neck")
    put(head & (y < 1.925) & (cnz > 0.25), lambda i: "chin")
    put(head & (y < 1.972) & (cnz > 0.25), lambda i: "mouth_region")
    put(head & (y < 2.01) & (cnz > 0.22), lambda i: "nose_region")
    put(head & (y < 2.17) & (cnz > 0.22), lambda i: "cheek_ear_zone")
    put(head & (y >= 2.17) & (cnz > 0.10), lambda i: "forehead")
    put(head & (cnz < -0.30), lambda i: "occiput")
    put(head & (y > 2.36), lambda i: "head_top")
    put(head & (axn > 0.075) & (axn < 0.17) & (y >= 2.05) & (y < 2.30), lambda i: "head_side")
    put(head, lambda i: "head_base")
    return zid, toe_bins

zid6, toe_bins = classify_body_clean()
# —— v33：用户实测点击点的最终人工命名覆盖 ——
# 这些是可见表面真实命中面，不是屏幕区域猜测：
#   1) world≈(1.280, 0.622, 0.072)：前侧肘下缘，用户确认应显示「左前臂」，
#      不再沿用 cubital（肘窝）标签；
#   2) world≈(0.649, 1.091, -0.066)：后侧上臂，用户确认应显示「左上臂」，
#      不再沿用 armpit（腋窝）标签。
# 以全局主体 mesh-6 三角面质心锁定，生成表与运行时 faceIndex 一一对应。
_MEASURED_OVERRIDES = [
    (np.array([1.2795, 0.6221, 0.0723]), "forearm_l", "v33 前侧肘下缘"),
    (np.array([0.6494, 1.0913, -0.0665]), "upper_arm_l", "v33 后侧上臂"),
]
for _p, _label, _note in _MEASURED_OVERRIDES:
    _i = int(np.argmin(np.linalg.norm(C - _p, axis=1)))
    _d = float(np.linalg.norm(C[_i] - _p))
    if _d > 0.025:
        raise RuntimeError("v33 实测覆盖点未找到对应主体面：%s distance=%.4f" % (_label, _d))
    print("  %s：face=%d centroid=%s distance=%.4f" % (_note, _i, np.round(C[_i], 4), _d))
    zid6[_i] = _label
# v34：截图中用户实际点击命中 primitive face #1749（mesh-6 的全局 face #23873），
# 该相邻表面仍被 v20 的 cubital_l 标为肘窝；按用户确认直接命名为左前臂。
_V34_FACE_OVERRIDES = [
    (23873, "forearm_l", "v34 截图前臂命中面"),
    (23875, "forearm_l", "v34 最新截图前臂命中面"),
    (23631, "upper_arm_l", "v34 最新截图后侧上臂命中面"),
    (23641, "upper_arm_l", "v34 最新截图后侧上臂相邻命中面"),
]
for _i, _label, _note in _V34_FACE_OVERRIDES:
    if not (0 <= _i < len(zid6)):
        raise RuntimeError("v34 face 越界：%d" % _i)
    print("  %s：face=%d centroid=%s" % (_note, _i, np.round(C[_i], 4)))
    zid6[_i] = _label
# 统计（先补漏：任何 None → 归入邻近躯干/体表兜底，但输出告警）
n_none = int(sum(1 for v in zid6 if v is None))
if n_none:
    print("!! 有 %d 面未分类 → 归入 body_fallback（需检查分类器覆盖）" % n_none)
    for i in range(len(zid6)):
        if zid6[i] is None:
            zid6[i] = "body_fallback"
vals, cnts = np.unique(zid6, return_counts=True)
print("== mesh-6 分类统计（%d 面）" % len(T))
for v, c in sorted(zip(vals, cnts), key=lambda t: -t[1]):
    print("   %-18s %6d" % (v, c))

# ============ 输出 segments（按 runtime mesh 拆分 + RLE）============
MESH6_BASE = [0, 4608, 8724, 14560, 22124]
mesh6_counts = [4608, 4116, 5836, 7564, 8892]
meshes_out = {}

def rle(ids):
    segs = []
    start = 0
    for i in range(1, len(ids) + 1):
        if i == len(ids) or ids[i] != ids[start]:
            segs.append([start, i, ids[start]])
            start = i
    return segs

for pi in range(5):
    name = "Obj3d66-19298115-6-141_%d" % (pi + 1)
    sub = zid6[MESH6_BASE[pi]:MESH6_BASE[pi] + mesh6_counts[pi]]
    meshes_out[name] = {"faces": mesh6_counts[pi], "segments": rle(sub)}

# ============ 其他 mesh 逐面/整体分类 ============
def mesh_zone_by_geometry(inst):
    """非主体 mesh：按整 mesh 位置给单一 zone（结构级语义）。"""
    Pm, Tm = g.mesh_geometry(inst)
    Wm = Pm * s + off
    mn, mx = Wm.min(axis=0), Wm.max(axis=0)
    ctr = (mn + mx) / 2
    name = inst["mesh_name"]
    # v31 侧别修正：x<0 = 解剖学右（出厂 -2/-4/-7/-8/-9 在 x<0，误标 l，已对调 id）
    xsgn = "r" if ctr[0] < 0 else "l"
    if name.endswith("-1-966"):
        return ["glasses_frame"] * 2, [504, 200], "眼镜框（2 个 primitive）"
    if name.endswith("-2-219"): return ["eyelid_l"] * 408, [408], "眼部结构（右）"
    if name.endswith("-3-634"): return ["eyelid_r"] * 408, [408], "眼部结构（左）"
    if name.endswith("-4-175"): return ["eyelid_l"] * 280, [280], "眼部结构（右，附件层）"
    if name.endswith("-5-557"): return ["eyelid_r"] * 280, [280], "眼部结构（左，附件层）"
    if name.endswith("-7-343"): return ["periorbital_r"] * 2080, [2080], "眼眶周（左）"
    if name.endswith("-10-116"): return ["periorbital_l"] * 2080, [2080], "眼眶周（右）"
    if name.endswith("-8-983"): return ["eyeball_r"] * 11008, [11008], "眼球前部（左）"
    if name.endswith("-11-506"): return ["eyeball_l"] * 11008, [11008], "眼球前部（右）"
    if name.endswith("-9-481"): return ["iris_r"] * 20, [20], "瞳孔/虹膜（左）"
    if name.endswith("-12-668"): return ["iris_l"] * 20, [20], "瞳孔/虹膜（右）"
    if name.endswith("-13-483"):
        # 内裤逐面细分
        Cc = Wm[Tm].mean(axis=1)
        n = len(Tm)
        zid = [None] * n
        for i in range(n):
            yy, zz, xx = Cc[i, 1], Cc[i, 2], Cc[i, 0]
            if yy < -0.01 and abs(xx) < 0.15 and zz >= -0.10:
                zid[i] = "briefs_perineum"
            elif yy < -0.01 and abs(xx) < 0.15 and zz < -0.10:
                zid[i] = "briefs_cleft"
            elif zz > 0.10:
                zid[i] = "briefs_front"
            elif zz < -0.15:
                zid[i] = "briefs_back"
            else:
                zid[i] = "briefs_side"
        return zid, [n], "内裤覆盖件逐面细分"
    # 头发层：14/15/16/17 —— 逐面分耳区/头发
    Cc = Wm[Tm].mean(axis=1)
    n = len(Tm)
    zid = []
    for i in range(n):
        ay = abs(Cc[i, 0])
        if 0.15 < ay < 0.30 and 2.00 < Cc[i, 1] < 2.28 and -0.10 < Cc[i, 2] < 0.14:
            zid.append("ear_zone_r" if Cc[i, 0] < 0 else "ear_zone_l")   # v31 侧别：x<0=右
        else:
            zid.append("head_hair")
    return zid, [n], "发型层逐面（耳区标记）"

for inst in insts:
    name = inst["mesh_name"]
    if name.endswith("-6-141"):
        continue
    Pm, Tm = g.mesh_geometry(inst)
    zid, counts, note = mesh_zone_by_geometry(inst)
    j = g.json
    mesh = j["meshes"][inst["mesh_index"]]
    if len(mesh["primitives"]) > 1:
        # mesh-1：两个 primitive → _1/_2
        base = 0
        for pi, prim in enumerate(mesh["primitives"]):
            nface = len(g._accessor_array(prim["indices"])[0]) // 3
            sub = zid[base:base + nface] if len(zid) > base + nface else [zid[0]] * nface
            meshes_out["%s_%d" % (name, pi + 1)] = {"faces": nface, "segments": rle(sub)}
            base += nface
    else:
        meshes_out[name] = {"faces": len(Tm), "segments": rle(zid)}

# —— yband（供运行时相对位置）——
YBAND_ZONES = {}
for key in labels:
    zid_match = [v == key for v in zid6] if not key.startswith(("briefs", "hair", "head_hair", "ear", "eye", "iris", "glasses")) else []
    zid_match = np.array(zid_match) if len(zid_match) else None
    if zid_match is not None and zid_match.any():
        ys = C[zid_match, 1]
        YBAND_ZONES[key] = [round(float(ys.min()), 4), round(float(ys.max()), 4)]
for key, yb in YBAND_ZONES.items():
    if labels[key]["cat"] in ("limb", "body", "foot", "toe", "torso"):
        labels[key]["yband"] = yb

out = {
    "model": "Custom3d66.glb",
    "version": 1,
    "generated_by": "tools/build_face_semantics.py（几何聚类+人体测量学比例，无骨骼/UV/逐面人工标注）",
    "fit_info": {"min": [round(float(v), 4) for v in fmin], "max": [round(float(v), 4) for v in fmax]},
    "honesty_note": "本 GLB 为合并网格（无骨骼/Skin/UV 语义）；表由顶点几何聚类推断。"
                    "被遮挡部位（会阴/耳廓）如实标注遮挡来源；追求完全准确需导入逐部位分层模型、"
                    "语义分割贴图或逐三角面标注数据。",
    "meshes": meshes_out,
    "labels": labels,
}
with open(OUT_PATH, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
nseg = sum(len(m["segments"]) for m in meshes_out.values())
print("== 输出 %s：%d mesh，%d segments，labels %d 项" % (OUT_PATH, len(meshes_out), nseg, len(labels)))

# —— 逐 zone 采样点（供逐部位测试瞄准）——
print("\n== 采样点（zone → 面质心中位数坐标）")
import collections
byz = collections.defaultdict(list)
for i, v in enumerate(zid6):
    byz[v].append(i)  # 面索引，便于取法线
for z in sorted(byz):
    idx = byz[z]
    pts = C[idx]
    med = np.median(pts, axis=0)
    # 取离中位数最近的真实面质心+法线（供测试做「瞄准点+朝外法线」视线校验）
    dist = np.linalg.norm(pts - med, axis=1)
    fi = idx[int(np.argmin(dist))]
    print("  %-18s (%+.3f, %+.3f, %+.3f)  n=%d  normal=(%+.2f,%+.2f,%+.2f)"
          % (z, med[0], med[1], med[2], len(idx),
             N[fi, 0], N[fi, 1], N[fi, 2]))

