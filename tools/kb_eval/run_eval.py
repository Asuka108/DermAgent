#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""k 离线评估：NDCG 饱和点 + 查询难度聚类 → k artifact（JSON）。

输入：
- queries.jsonl（gen_queries.py 生成）
- candidates.jsonl（pool_candidates.py 生成，按 query_id/rank 排序）
- labels.csv（query_id,query,book_id,chunk_anchor,grade；grade 0/1/2）
- query_vectors.jsonl（可选；缺失时用本地 bge 现算）

无标注时输出 labels_present=false / global_k=默认（20），不伪造评估结果。
本脚本只写 JSON artifact，不写数据库；应用见 apply_spec.py。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

EVAL_DIR = os.path.join(common.DATA_DIR, "kb", "eval")


def _fail(reason: str, **extra) -> int:
    print(json.dumps({"ok": False, "reason": reason, **extra}, ensure_ascii=False))
    return 2


def _group_rankings(rows: list) -> dict:
    grouped = {}
    order = {}
    for idx, row in enumerate(rows):
        qid = row.get("query_id")
        if not qid:
            continue
        order[qid] = order.get(qid, 0) + 1
        grouped.setdefault(qid, []).append((int(row.get("rank") or order[qid]), idx, row))
    out = {}
    for qid, items in grouped.items():
        items.sort(key=lambda x: (x[0], x[1]))
        out[qid] = [row for _, _, row in items]
    return out


def _placeholder(total: int, labels_path: str) -> dict:
    try:
        from kb import config as kb_config
        default_k = max(1, min(kb_config.k_max(), kb_config.k_default()))
    except Exception:  # noqa: BLE001
        default_k = 20
    return {
        "artifact_version": common.artifact_version(),
        "global_k": default_k,
        "labels_present": False,
        "created_at": common.now_iso(),
        "clusters": [],
        "queries": {},
        "eval": {
            "queries_total": total, "queries_labeled": 0, "unlabeled": total,
            "unrated": 0, "labels_matched": 0, "labels_unmatched": 0,
            "labels_path": labels_path, "note": "labels_missing_or_empty",
            "rho": common.RHO, "delta": common.DELTA, "k_max": common.K_MAX,
        },
    }


def _query_vectors(qids: list, vectors_path: str) -> tuple:
    cache = {}
    if vectors_path and os.path.isfile(vectors_path):
        for row in common.load_jsonl(vectors_path):
            vec = row.get("vector")
            if row.get("query_id") and isinstance(vec, list) and vec:
                cache[row["query_id"]] = vec
    missing = [(qid, text) for qid, text in qids if qid not in cache]
    if missing:
        try:
            from kb import embed as embed_mod
            if not embed_mod.ensure().get("ok"):
                return cache, "embedding_unavailable"
            for qid, text in missing:
                vec = embed_mod.embed_query(text)
                if not vec:
                    return cache, "embedding_unavailable"
                cache[qid] = vec
        except Exception as exc:  # noqa: BLE001
            return cache, "embedding_unavailable:" + exc.__class__.__name__
    return cache, ""


def main() -> int:
    ap = argparse.ArgumentParser(description="评估 k 饱和点与查询聚类")
    ap.add_argument("--queries", default=os.path.join(EVAL_DIR, "queries.jsonl"))
    ap.add_argument("--candidates", default=os.path.join(EVAL_DIR, "candidates.jsonl"))
    ap.add_argument("--vectors", default=os.path.join(EVAL_DIR, "query_vectors.jsonl"))
    ap.add_argument("--labels", default=os.path.join(EVAL_DIR, "labels.csv"))
    ap.add_argument("--out", default="", help="artifact 路径；默认 data/kb/exports/kspec-*.json")
    ap.add_argument("--k-max", type=int, default=common.K_MAX)
    ap.add_argument("--clusters", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20261030)
    ap.add_argument("--rho", type=float, default=common.RHO)
    ap.add_argument("--delta", type=float, default=common.DELTA)
    ap.add_argument("--dry-run", action="store_true",
                    help="缺标注时仍输出占位 artifact（labels_present=0 / global_k=默认）")
    args = ap.parse_args()

    if not os.path.isfile(args.queries):
        return _fail("queries_missing", path=args.queries)
    queries = common.load_jsonl(args.queries)
    labels = common.load_labels(args.labels)
    if not labels:
        if not args.dry_run:
            return _fail("labels_missing", path=args.labels)
        artifact = _placeholder(len(queries), args.labels)
        path = args.out or os.path.join(common.EXPORT_DIR, artifact["artifact_version"] + ".json")
        common.write_json(path, artifact)
        print(json.dumps({"ok": True, "labels_present": False,
                          "global_k": artifact["global_k"], "out": path}, ensure_ascii=False))
        return 0

    if not os.path.isfile(args.candidates):
        return _fail("candidates_missing", path=args.candidates)
    rankings = _group_rankings(common.load_jsonl(args.candidates))
    label_map = {}
    for row in labels:
        key = (row["query_id"], row["book_id"], row["chunk_anchor"])
        label_map[key] = max(label_map.get(key, 0), int(row["grade"]))

    by_qid = {}
    for row in labels:
        by_qid.setdefault(row["query_id"], []).append(row)
    ideal_grades = {}
    for qid, rows in by_qid.items():
        seen_grade = {}
        for row in rows:
            key = (row["book_id"], row["chunk_anchor"])
            seen_grade[key] = max(seen_grade.get(key, 0), row["grade"])
        ideal_grades[qid] = list(seen_grade.values())

    curves, k_stars, effective = {}, {}, []
    labels_matched = labels_unmatched = 0
    query_text = {str(q.get("query_id")): str(q.get("query") or "") for q in queries}
    for qid, candidates in rankings.items():
        keys = [(qid, c.get("book_id"), c.get("chunk_anchor") or "") for c in candidates]
        matched = sum(1 for key in keys if key in label_map)
        labels_matched += matched
        if qid in by_qid:
            labels_unmatched += sum(
                1 for row in by_qid[qid]
                if (qid, row["book_id"], row["chunk_anchor"]) not in set(keys))
        ranked_grades = [label_map.get(key, 0) for key in keys]
        curve = common.ndcg_curve(ranked_grades, args.k_max,
                                  ideal_grades=ideal_grades.get(qid))
        curves[qid] = curve
        k_star = common.saturation_k(curve, args.k_max, rho=args.rho, delta=args.delta)
        k_stars[qid] = k_star
        if k_star is not None:
            effective.append(qid)

    total = len(queries)
    unlabeled = total - len(by_qid)
    unrated = len(by_qid) - len(effective)
    ks = [k_stars[q] for q in effective]
    global_k = int(min(args.k_max, max(1, round(common.percentile(ks, 90))))) if ks else 0
    if not ks:
        try:
            from kb import config as kb_config
            global_k = max(1, min(kb_config.k_max(), kb_config.k_default()))
        except Exception:  # noqa: BLE001
            global_k = 20

    clusters, clustering = [], "skipped:no_effective_queries"
    if effective:
        vectors, reason = _query_vectors([(q, query_text.get(q, "")) for q in effective],
                                         args.vectors)
        if reason:
            clustering = "skipped:" + reason
        else:
            try:
                clusters = common.cluster_k_stars([vectors[q] for q in effective],
                                                  [k_stars[q] for q in effective],
                                                  k=args.clusters, seed=args.seed)
                clustering = "ok"
            except RuntimeError as exc:
                clustering = "skipped:" + str(exc)
            except Exception as exc:  # noqa: BLE001
                clustering = "skipped:" + exc.__class__.__name__

    artifact = {
        "artifact_version": common.artifact_version(),
        "global_k": global_k,
        "labels_present": True,
        "created_at": common.now_iso(),
        "clusters": clusters,
        "queries": {q: {"k_star": k_stars[q], "ndcg": curves[q]} for q in curves},
        "eval": {
            "queries_total": total, "queries_labeled": len(by_qid),
            "unlabeled": unlabeled, "unrated": unrated,
            "labels_matched": labels_matched, "labels_unmatched": labels_unmatched,
            "ndcg_mean_at_k_max": round(
                sum(c.get(args.k_max, 0.0) for c in curves.values()) / max(len(curves), 1), 6),
            "mean_k_star": round(sum(ks) / len(ks), 2) if ks else 0,
            "p90_k_star": common.percentile(ks, 90) if ks else 0,
            "rho": args.rho, "delta": args.delta, "k_max": args.k_max,
            "clustering": clustering, "clusters": len(clusters),
            "queries_path": args.queries, "candidates_path": args.candidates,
            "labels_path": args.labels,
        },
    }
    path = args.out or os.path.join(common.EXPORT_DIR, artifact["artifact_version"] + ".json")
    common.write_json(path, artifact)
    print(json.dumps({"ok": True, "labels_present": True, "global_k": global_k,
                      "queries_labeled": len(by_qid), "effective": len(effective),
                      "clusters": len(clusters), "clustering": clustering, "out": path},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
