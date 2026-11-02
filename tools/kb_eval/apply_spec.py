#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 run_eval.py 的 k artifact 写入 MySQL（active=1）、SQLite 镜像与 Redis 缓存。

- 旧 artifact 自动 active=0（store.save_k_spec 事务内完成）；
- `--dry-run` 只校验并打印摘要，不写任何存储；
- 无标注占位 artifact（labels_present=false）默认拒绝，避免把默认值伪装成评估结果。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402


def _fail(reason: str, **extra) -> int:
    print(json.dumps({"ok": False, "reason": reason, **extra}, ensure_ascii=False))
    return 2


def _validate(spec: dict) -> str:
    if not isinstance(spec, dict):
        return "invalid_artifact"
    if not str(spec.get("artifact_version") or "").strip():
        return "missing_artifact_version"
    try:
        global_k = int(spec.get("global_k") or 0)
    except (TypeError, ValueError):
        return "invalid_global_k"
    if global_k < 1 or global_k > common.K_MAX:
        return "invalid_global_k"
    clusters = spec.get("clusters")
    if clusters is not None and not isinstance(clusters, list):
        return "invalid_clusters"
    for cluster in clusters or []:
        centroid = cluster.get("centroid") or []
        if centroid and len(centroid) != 768:
            return "invalid_centroid_dim"
        if int(cluster.get("k_star") or 0) < 1:
            return "invalid_cluster_k_star"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="应用 k artifact")
    ap.add_argument("artifact", nargs="?", default="", help="run_eval.py 输出的 JSON")
    ap.add_argument("--artifact", dest="artifact_opt", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-unlabeled", action="store_true")
    ap.add_argument("--no-redis", action="store_true")
    args = ap.parse_args()

    path = args.artifact_opt or args.artifact
    if not path or not os.path.isfile(path):
        return _fail("artifact_missing", path=path)
    with open(path, encoding="utf-8") as fh:
        spec = json.load(fh)
    problem = _validate(spec)
    if problem:
        return _fail(problem)
    if not spec.get("labels_present") and not args.allow_unlabeled:
        return _fail("labels_present_false")
    clusters = spec.get("clusters") or []
    summary = {"ok": True, "artifact_version": spec["artifact_version"],
               "global_k": int(spec["global_k"]), "clusters": len(clusters),
               "dry_run": bool(args.dry_run), "artifact": path}
    if args.dry_run:
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    from kb.store import MySQLKBStore, SQLiteMirrorStore
    try:
        saved = MySQLKBStore().save_k_spec(spec, clusters)
    except Exception as exc:  # noqa: BLE001
        return _fail("mysql_unavailable", detail=str(exc)[:160])
    mirror_ok, mirror_detail = True, ""
    try:
        SQLiteMirrorStore().meta_set("k_spec", spec)
    except Exception as exc:  # noqa: BLE001
        mirror_ok, mirror_detail = False, str(exc)[:160]
    redis_ok, redis_detail = False, "skipped"
    if not args.no_redis:
        try:
            from kb.queue import RedisQueue
            RedisQueue().cache_kspec("active", json.dumps(spec, ensure_ascii=False))
            redis_ok, redis_detail = True, ""
        except Exception as exc:  # noqa: BLE001
            redis_detail = str(exc)[:160]
    summary.update({"saved": saved, "mirror_ok": mirror_ok, "mirror_detail": mirror_detail,
                    "redis_ok": redis_ok, "redis_detail": redis_detail})
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
