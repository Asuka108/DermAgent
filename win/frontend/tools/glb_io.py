# -*- coding: utf-8 -*-
"""glb_io.py — GLB 二进制解析 + 运行时装配归一化的逐位复刻（v19 逐三角面语义表基础）。

复刻 client/web/main.js registerHuman() 的装配公式（无骨骼分支，v28 缩放感知版）：
  box   = Box3.setFromObject(modelRoot)            # gltf.scene 空间（未加变换前的世界盒）
  scale = 5.0 / box.size.y
  position.x -= box.center.x * scale; position.z -= box.center.z * scale
  position.y += (-2.5 - box.min.y * scale)         # v28：平移量必须乘 scale（world = s·local + pos）
  最终顶点 = scale * v + position                     # v 为 gltf.scene 空间顶点

正确性以运行时实测交叉验证（fitInfo / modelRest / 解剖锚点），见 verify 输出。
"""
import json
import struct
import numpy as np

GLB_PATH = None  # 由调用方注入


def _mat_identity():
    return np.eye(4, dtype=np.float64)


def _node_local_matrix(node):
    if "matrix" in node:
        # glTF matrix 为列主序（与 three.js Matrix4.fromArray 一致）
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
    m = _mat_identity()
    t = node.get("translation", [0, 0, 0])
    r = node.get("rotation", [0, 0, 0, 1])   # xyzw
    s = node.get("scale", [1, 1, 1])
    x, y, z, w = r
    # 四元数旋转矩阵（列主序展开为行存储再转置）
    R = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ], dtype=np.float64)
    T = _mat_identity(); T[:3, 3] = t
    S = _mat_identity(); S[0, 0], S[1, 1], S[2, 2] = s
    R4 = _mat_identity(); R4[:3, :3] = R
    return T @ R4 @ S


class GLB:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            data = f.read()
        magic, version, length = struct.unpack_from("<III", data, 0)
        assert magic == 0x46546C67, "非 GLB 文件"
        off = 12
        self.json = None
        self.bin = None
        while off < length:
            clen, ctype = struct.unpack_from("<II", data, off)
            off += 8
            chunk = data[off:off + clen]
            off += clen
            if ctype == 0x4E4F534A:
                self.json = json.loads(chunk.decode("utf-8"))
            elif ctype == 0x004E4942:
                self.bin = chunk
        assert self.json is not None and self.bin is not None

    # ---------- accessor 读取 ----------
    def _accessor_array(self, acc_idx):
        j = self.json
        acc = j["accessors"][acc_idx]
        bv = j["bufferViews"][acc["bufferView"]]
        comp_type = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2", 5125: "u4", 5126: "f4"}[acc["componentType"]]
        ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[acc["type"]]
        dtype = np.dtype(comp_type).newbyteorder("<")
        start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        count = acc["count"]
        stride = bv.get("byteStride") or dtype.itemsize * ncomp
        if stride == dtype.itemsize * ncomp:
            arr = np.frombuffer(self.bin, dtype=dtype, count=count * ncomp, offset=start)
        else:
            # 带步长：逐行取
            rows = np.frombuffer(self.bin, dtype=np.uint8, count=stride * count, offset=start)
            rows = rows.reshape(count, stride)
            arr = np.frombuffer(rows[:, :dtype.itemsize * ncomp].tobytes(), dtype=dtype)
        return arr.reshape(count, ncomp), acc

    # ---------- 节点树 ----------
    def node_world_matrices(self):
        """返回 {node_index: 4x4 world matrix}（scenes[scene] 根开始）。"""
        j = self.json
        scene = j.get("scene", 0)
        roots = j["scenes"][scene]["nodes"]
        out = {}

        def walk(idx, parent_m):
            m = parent_m @ _node_local_matrix(j["nodes"][idx])
            out[idx] = m
            for c in j["nodes"][idx].get("children", []):
                walk(c, m)

        for r in roots:
            walk(r, _mat_identity())
        return out

    def mesh_instances(self):
        """返回 [{node_index, node_name, mesh_index, mesh_name, matrix}]。"""
        j = self.json
        mats = self.node_world_matrices()
        out = []
        for idx, n in enumerate(j["nodes"]):
            if "mesh" not in n:
                continue
            out.append({
                "node_index": idx,
                "node_name": n.get("name", ""),
                "mesh_index": n["mesh"],
                "mesh_name": j["meshes"][n["mesh"]].get("name", ""),
                "matrix": mats.get(idx, _mat_identity()),
            })
        return out

    def mesh_geometry(self, inst):
        """单个 mesh 实例：返回 (positions_glscene[N,3] float64, triangles[M,3] int64)。
        positions 已应用节点世界矩阵（= 运行时 gltf.scene 空间，因 gltf.scene 恒等变换）。"""
        j = self.json
        mesh = j["meshes"][inst["mesh_index"]]
        pos_all = []
        tri_all = []
        vbase = 0
        for prim in mesh["primitives"]:
            pa, _ = self._accessor_array(prim["attributes"]["POSITION"])
            pts = pa.astype(np.float64)
            if "indices" in prim:
                ia, _ = self._accessor_array(prim["indices"])
                idx = ia.astype(np.int64).reshape(-1)
            else:
                idx = np.arange(len(pts), dtype=np.int64)
            pos_all.append(pts)
            tri_all.append(idx.reshape(-1, 3) + vbase)
            vbase += len(pts)
        P = np.vstack(pos_all)
        T = np.vstack(tri_all)
        # 应用节点世界矩阵（只含刚体/缩放；用齐次变换）
        M = inst["matrix"]
        Pw = P @ M[:3, :3].T + M[:3, 3]
        return Pw, T


def register_transform(glb):
    """复刻 registerHuman 装配：返回 (scale, offset[3], fit_min[3], fit_max[3])。"""
    insts = glb.mesh_instances()
    all_pts = []
    for inst in insts:
        P, _ = glb.mesh_geometry(inst)
        all_pts.append(P)
    V = np.vstack(all_pts)
    mn, mx = V.min(axis=0), V.max(axis=0)
    size = mx - mn
    # 躺姿扶正判断（本模型应不触发，仅对齐运行时逻辑）
    if max(size[0], size[2]) > size[1] * 1.5:
        raise RuntimeError("模型躺姿：运行时会旋转 -90°，本脚本需扩展复刻该分支")
    s = 5.0 / size[1]
    center = (mn + mx) / 2.0
    # v28 缩放感知：world = s·local + pos → 平移量必须乘 s（与 main.js registerHuman 一致）。
    # 男性模型原始 center≈0/mn_y≈0 时新旧公式数值巧合一致；女性模型不修则整体错位约 0.245。
    offset = np.array([-center[0] * s, -2.5 - mn[1] * s, -center[2] * s])
    fit_min = s * mn + offset
    fit_max = s * mx + offset
    return s, offset, fit_min, fit_max


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "web/vendor/models/Custom3d66.glb"
    g = GLB(path)
    j = g.json
    print("meshes:", len(j["meshes"]), "nodes:", len(j["nodes"]),
          "accessors:", len(j["accessors"]), "skins:", len(j.get("skins", [])),
          "animations:", len(j.get("animations", [])))
    insts = g.mesh_instances()
    print("mesh instances:", len(insts))
    for it in insts:
        P, T = g.mesh_geometry(it)
        print("  node=%s mesh=%s verts=%d tris=%d" %
              (it["node_name"], it["mesh_name"], len(P), len(T)))
    s, off, fmin, fmax = register_transform(g)
    print("scale=%.6f offset=%s" % (s, np.round(off, 5)))
    print("fitInfo min=%s max=%s" % (np.round(fmin, 4), np.round(fmax, 4)))
    print("fit size=%s" % np.round(fmax - fmin, 4))
