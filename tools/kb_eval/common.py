# -*- coding: utf-8 -*-
"""k 评估共用：路径、NDCG、饱和点、确定性 k-means、产物读写。

所有脚本默认只读本地镜像/模型；缺依赖时如实报错，不生成伪造标注或指标。
"""
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVER_DIR = os.path.join(ROOT, "web", "backend", "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

DATA_DIR = os.environ.get("DERM_DATA_DIR") or os.path.join(SERVER_DIR, "data")
EXPORT_DIR = os.path.join(DATA_DIR, "kb", "exports")

RHO = 0.97          # NDCG@k 达到 NDCG@K_max 的 97% 视为饱和
DELTA = 0.005       # 饱和点后边际增益上限
K_MAX = 50


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def artifact_version(prefix: str = "kspec") -> str:
    return prefix + "-" + datetime.now().strftime("%Y%m%d-%H%M%S")


def load_jsonl(path: str) -> list:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: str, rows: list) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def load_labels(path: str) -> list:
    """labels.csv：query_id,query,book_id,chunk_anchor,grade（grade 0/1/2）。"""
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                grade = int(str(row.get("grade") or "0").strip())
            except ValueError:
                continue
            if grade not in (0, 1, 2):
                continue
            rows.append({
                "query_id": str(row.get("query_id") or "").strip(),
                "query": str(row.get("query") or "").strip(),
                "book_id": str(row.get("book_id") or "").strip(),
                "chunk_anchor": str(row.get("chunk_anchor") or "").strip(),
                "grade": grade,
            })
    return [r for r in rows if r["query_id"]]


def dcg(grades: list) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(grades))


def ndcg_at_k(ranked_grades: list, k: int, ideal_grades: list = None) -> float:
    """NDCG@k，IDCG 固定为该 query 全部相关标注的理想排序。

    不用「前 k 个标注重新计算 IDCG」的变体：那种算法在 top-1 高相关时 NDCG@1=1，
    会让 k 饱和点永远落在小 k，无法反映召回是否完整。
    """
    top = list(ranked_grades[:k])
    ideal = sorted(ideal_grades if ideal_grades is not None else ranked_grades,
                   reverse=True)
    idcg = dcg(ideal)
    return (dcg(top) / idcg) if idcg > 0 else 0.0


def ndcg_curve(ranked_grades: list, k_max: int = K_MAX, ideal_grades: list = None) -> dict:
    ideal = ideal_grades if ideal_grades is not None else ranked_grades
    return {k: round(ndcg_at_k(ranked_grades, k, ideal), 6)
            for k in range(1, k_max + 1)}


def saturation_k(ndcgs: dict, k_max: int = K_MAX, rho: float = RHO,
                 delta: float = DELTA):
    """最小 k：NDCG@k 达 NDCG@K_max 的 rho 且相对 k-1 的边际增益 < delta。

    NDCG@K_max 为 0（无相关文档）时返回 None，由调用方计为不可评估。
    """
    full = float(ndcgs.get(k_max) or 0.0)
    if full <= 0:
        return None
    target = rho * full
    solver = None
    for k in range(1, k_max + 1):
        value = float(ndcgs.get(k) or 0.0)
        gain = value - float(ndcgs.get(k - 1) or 0.0) if k > 1 else value
        if value >= target and gain < delta:
            return k
        if solver is None and value >= target:
            solver = k
    return solver or k_max


def percentile(values: list, p: float) -> float:
    """最近秩百分位（与 numpy 'higher' 近似），空集返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    idx = max(0, math.ceil(p / 100.0 * len(ordered)) - 1)
    return ordered[min(idx, len(ordered) - 1)]


def _cosine(a: list, b: list) -> float:
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    return dot / (na * nb) if na and nb else 0.0


def kmeans(vectors: list, k: int, seed: int = 20261030, iters: int = 60) -> dict:
    """确定性 k-means（余弦距离 = 归一化后欧氏）；返回 labels/centroids/sample_counts。

    numpy 缺失时抛 RuntimeError("numpy_unavailable")，由调用方如实标注。
    """
    try:
        import numpy as np
    except ImportError as exc:  # noqa: BLE001
        raise RuntimeError("numpy_unavailable") from exc
    if not vectors:
        return {"labels": [], "centroids": [], "sample_counts": []}
    x = np.asarray(vectors, dtype="float64")
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    x = x / np.maximum(norms, 1e-12)
    k = max(1, min(int(k), len(x)))
    rng = np.random.RandomState(int(seed))
    centers = [x[rng.randint(len(x))]]
    for _ in range(1, k):
        dist = np.min([np.sum((x - c) ** 2, axis=1) for c in centers], axis=0)
        total = float(dist.sum())
        idx = rng.choice(len(x)) if total <= 0 else int(rng.choice(len(x), p=dist / total))
        centers.append(x[idx])
    centers = np.asarray(centers, dtype="float64")
    labels = np.zeros(len(x), dtype="int32")
    for _ in range(int(iters)):
        dist = ((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = np.argmin(dist, axis=1)
        new_centers = centers.copy()
        for cid in range(k):
            members = x[new_labels == cid]
            if len(members):
                new_centers[cid] = members.mean(axis=0)
        if np.array_equal(new_labels, labels) and np.allclose(new_centers, centers):
            labels = new_labels
            break
        labels, centers = new_labels, new_centers
    counts = [int((labels == cid).sum()) for cid in range(k)]
    return {
        "labels": [int(v) for v in labels],
        "centroids": [[round(float(v), 6) for v in c] for c in centers],
        "sample_counts": counts,
    }


def cluster_k_stars(vectors: list, k_stars: list, k: int = 4, seed: int = 20261030) -> list:
    """按查询向量聚类，每簇 k* = 成员 k* 最大值；质心取成员均值。"""
    fitted = kmeans(vectors, k, seed=seed)
    buckets = {}
    for idx, cid in enumerate(fitted["labels"]):
        if k_stars[idx] is None:
            continue
        buckets.setdefault(cid, []).append(k_stars[idx])
    clusters = []
    for cid in sorted(buckets):
        members = buckets[cid]
        clusters.append({
            "cluster_id": cid,
            "centroid": fitted["centroids"][cid],
            "k_star": int(max(members)),
            "sample_count": len(members),
        })
    return clusters


__all__ = [
    "ROOT", "SERVER_DIR", "DATA_DIR", "EXPORT_DIR", "RHO", "DELTA", "K_MAX",
    "now_iso", "artifact_version", "load_jsonl", "write_jsonl", "write_json",
    "load_labels", "dcg", "ndcg_at_k", "ndcg_curve", "saturation_k",
    "percentile", "kmeans", "cluster_k_stars", "_cosine",
]
