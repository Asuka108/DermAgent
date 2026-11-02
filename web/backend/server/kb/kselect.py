# -*- coding: utf-8 -*-
"""k 选择：MySQL active artifact → Redis 缓存 → SQLite 镜像 → 默认 20。

查询向量与簇质心 cosine ≥0.65 且样本 ≥3 用簇 k，否则全局 k；
无标注（labels_present=0）一律 default，不伪装成评估结果。
"""
import json
import math
import threading
import time

from . import config

_store = None
_queue = None
_mirror = None
_cache = {"spec": None, "at": 0.0, "loaded": False}
_lock = threading.Lock()
_TTL = 60.0


def configure(store=None, queue=None, mirror=None) -> None:
    global _store, _queue, _mirror
    _store, _queue, _mirror = store, queue, mirror
    reset()


def reset() -> None:
    with _lock:
        _cache.update({"spec": None, "at": 0.0, "loaded": False})


def _parse(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            out = json.loads(value)
        except ValueError:
            return None
        return out if isinstance(out, dict) else None
    return None


def _load_spec():
    with _lock:
        if _cache["loaded"] and time.time() - _cache["at"] < _TTL:
            return _cache["spec"]
    spec = None
    if _store is not None:
        try:
            spec = _store.active_k_spec()
        except Exception:  # noqa: BLE001 — MySQL 不可用走缓存/镜像
            spec = None
    if not spec and _queue is not None:
        try:
            spec = _parse(_queue.cached_kspec("active"))
        except Exception:  # noqa: BLE001
            spec = None
    if not spec and _mirror is not None:
        try:
            spec = _parse(_mirror.meta_get("k_spec"))
        except Exception:  # noqa: BLE001
            spec = None
    with _lock:
        _cache.update({"spec": spec, "at": time.time(), "loaded": True})
    return spec


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def choose(query_vec: list = None) -> dict:
    """返回 {k, source, version}：source ∈ cluster|global|default。"""
    spec = _load_spec()
    version = (spec or {}).get("artifact_version") or ""
    default_k = max(1, min(config.k_max(), config.k_default()))
    if not spec or not spec.get("labels_present"):
        return {"k": default_k, "source": "default", "version": version}
    global_k = int(spec.get("global_k") or default_k)
    if query_vec:
        best, best_sim = None, -1.0
        for cluster in spec.get("clusters") or []:
            sim = _cosine(query_vec, cluster.get("centroid") or [])
            if sim > best_sim:
                best, best_sim = cluster, sim
        if (best is not None and best_sim >= config.cluster_min_sim()
                and int(best.get("sample_count") or 0) >= config.cluster_min_samples()
                and int(best.get("k_star") or 0) > 0):
            return {"k": max(1, min(config.k_max(), int(best["k_star"]))),
                    "source": "cluster", "version": version}
    return {"k": max(1, min(config.k_max(), global_k)), "source": "global",
            "version": version}
