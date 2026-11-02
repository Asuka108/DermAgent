#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a versioned, face-addressable Anatomy Atlas for one GLB.

The existing face-semantics generator supplies the semantic assignment.  This
builder adds stable mesh identity, GLB fingerprint, source vertex data, and
per-face evidence so runtime selection never needs spatial guessing.

NOTE (v24): 输出体量与主模型面数成正比（Custom3d66 ≈ 600MB），浏览器/QtWebEngine
无法加载，运行时点击识别链也未接线（main.js 的 ATLAS* 为未接线的死变量）。
已从 git 移除（.gitignore）。当前点击识别的唯一语义来源仍是 face-semantics.json
（v23 审计链）。此脚本仅作离线分析工具保留，产物不要提交。"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))
from glb_io import GLB, register_transform  # noqa: E402

DEFAULT_GLB = os.path.join(HERE, "web", "vendor", "models", "Custom3d66.glb")
DEFAULT_SEM = os.path.join(HERE, "web", "face-semantics.json")
DEFAULT_OUT = os.path.join(HERE, "web", "anatomy-atlas.json")


def stable_uuid(mesh_index: int, primitive_index: int, mesh_name: str) -> str:
    raw = f"gltf-mesh:{mesh_index}:primitive:{primitive_index}:{mesh_name}".encode()
    return "atlas-" + hashlib.sha256(raw).hexdigest()[:24]


def rle_lookup(segments, face_index):
    lo, hi = 0, len(segments) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, label = segments[mid]
        if face_index < start:
            hi = mid - 1
        elif face_index >= end:
            lo = mid + 1
        else:
            return label
    return None


def side_of(x):
    return "left" if x < -1e-7 else "right" if x > 1e-7 else "midline"


def surface_of(normal, point):
    nx, ny, nz = normal
    components = [
        (abs(nz), "anterior" if nz >= 0 else "posterior"),
        (abs(nx), "lateral" if nx * point[0] >= 0 else "medial"),
        (abs(ny), "superior" if ny >= 0 else "inferior"),
    ]
    components.sort(reverse=True)
    return components[0][1]


def segment_of(label, y, yband):
    if not yband or yband[1] <= yband[0]:
        return "unknown"
    t = max(0.0, min(1.0, (y - yband[0]) / (yband[1] - yband[0])))
    # Anatomical limbs run proximal (high y) to distal (low y).
    if label and any(k in label for k in ("thigh", "calf", "arm", "forearm", "wrist", "ankle")):
        t = 1.0 - t
    return "proximal" if t < 0.34 else "middle" if t < 0.67 else "distal"


def build(glb_path=DEFAULT_GLB, sem_path=DEFAULT_SEM, out_path=DEFAULT_OUT):
    with open(glb_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    with open(sem_path, encoding="utf-8") as fh:
        semantics = json.load(fh)

    glb = GLB(glb_path)
    scale, offset, fit_min, fit_max = register_transform(glb)
    instances = glb.mesh_instances()
    meshes = {}
    total_faces = 0
    source_json = glb.json

    for inst in instances:
        mesh_index = inst["mesh_index"]
        mesh_name = inst["mesh_name"]
        mesh = source_json["meshes"][mesh_index]
        sem_meshes = semantics.get("meshes", {})
        # GLTFLoader creates one runtime mesh per primitive.  The generated
        # semantic table uses the same deterministic name convention.
        for primitive_index, primitive in enumerate(mesh["primitives"]):
            runtime_name = mesh_name if len(mesh["primitives"]) == 1 else f"{mesh_name}_{primitive_index + 1}"
            sem_entry = sem_meshes.get(runtime_name)
            positions, _ = glb._accessor_array(primitive["attributes"]["POSITION"])
            positions = positions.astype(np.float64)
            if "indices" in primitive:
                indices, _ = glb._accessor_array(primitive["indices"])
                indices = indices.astype(np.int64).reshape(-1)
            else:
                indices = np.arange(len(positions), dtype=np.int64)
            triangles = indices.reshape(-1, 3)
            node_matrix = np.asarray(inst["matrix"], dtype=np.float64)
            world_positions = positions @ node_matrix[:3, :3].T + node_matrix[:3, 3]
            # Match the browser's fixed-height normalization.
            world_positions = world_positions * scale + offset
            material_index = primitive.get("material")
            material_name = None
            if material_index is not None and material_index < len(source_json.get("materials", [])):
                material_name = source_json["materials"][material_index].get("name")
            atlas_uuid = stable_uuid(mesh_index, primitive_index, mesh_name)
            records = []
            for face_index, tri in enumerate(triangles):
                a, b, c = (world_positions[int(tri[0])], world_positions[int(tri[1])], world_positions[int(tri[2])])
                normal = np.cross(b - a, c - a)
                norm = float(np.linalg.norm(normal))
                normal = normal / norm if norm else np.zeros(3)
                centroid = (a + b + c) / 3.0
                label_id = rle_lookup(sem_entry["segments"], face_index) if sem_entry else None
                label = semantics.get("labels", {}).get(label_id) if label_id else None
                yband = label.get("yband") if label else None
                records.append({
                    "faceIndex": face_index,
                    "vertexIndices": [int(tri[0]), int(tri[1]), int(tri[2])],
                    "verticesLocal": [[round(float(v), 7) for v in positions[int(i)]] for i in tri],
                    "centroidLocal": [round(float(v), 7) for v in ((positions[int(tri[0])] + positions[int(tri[1])] + positions[int(tri[2])]) / 3.0)],
                    "side": side_of(float(centroid[0])),
                    "labelId": label_id,
                    "label": label.get("zh") if label else None,
                    "surface": surface_of(normal, centroid),
                    "segment": segment_of(label_id, float(centroid[1]), yband),
                    "confidence": float(label.get("conf", 0.0)) if label else 0.0,
                    "semanticSource": "geometry-proportion-inference" if label else "unresolved",
                    "material": material_name,
                    "skin": None,
                })
            meshes[atlas_uuid] = {
                "meshUuid": atlas_uuid,
                "meshName": runtime_name,
                "sourceMeshName": mesh_name,
                "meshIndex": mesh_index,
                "primitiveIndex": primitive_index,
                "faces": len(records),
                "records": records,
            }
            total_faces += len(records)

    out = {
        "schema": "anatomy-atlas/v1",
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "model": os.path.basename(glb_path),
        "glbSha256": digest,
        "fitInfo": {"min": [round(float(v), 7) for v in fit_min], "max": [round(float(v), 7) for v in fit_max]},
        "meshCount": len(meshes),
        "faceCount": total_faces,
        "labels": semantics.get("labels", {}),
        "meshes": meshes,
        "calibration": {"format": "face-override", "history": []},
        "honesty": {
            "defaultSemanticSource": "geometry-proportion-inference",
            "unresolvedRule": "Missing or low-confidence records must not be replaced by coordinate heuristics.",
            "sourceEvidence": "GLB node/mesh/material metadata and triangle geometry; this GLB has no skins or UV semantic map.",
        },
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {out_path}: {len(meshes)} meshes, {total_faces} faces, sha256={digest[:16]}…")


if __name__ == "__main__":
    args = sys.argv[1:]
    build(args[0] if len(args) > 0 else DEFAULT_GLB,
          args[1] if len(args) > 1 else DEFAULT_SEM,
          args[2] if len(args) > 2 else DEFAULT_OUT)
