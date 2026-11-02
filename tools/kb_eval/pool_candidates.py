#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建标注候选池：向量 top-50 + BM25 top-50，避免只标注已召回的片段。

读取 queries.jsonl，写出 candidates.jsonl（含 rank/source/分数字段）与
query_vectors.jsonl（供 run_eval 聚类）。Milvus/embedding 缺失时默认失败
（`--allow-bm25-only` 可只建 BM25 池，但标注覆盖不完整，产物会标注 used_vector=false）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

DEFAULT_QUERIES = os.path.join(common.DATA_DIR, "kb", "eval", "queries.jsonl")
DEFAULT_CANDIDATES = os.path.join(common.DATA_DIR, "kb", "eval", "candidates.jsonl")
DEFAULT_VECTORS = os.path.join(common.DATA_DIR, "kb", "eval", "query_vectors.jsonl")


def _fail(reason: str, **extra) -> int:
    print(json.dumps({"ok": False, "reason": reason, **extra}, ensure_ascii=False))
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 k 评估候选池")
    ap.add_argument("--queries", default=DEFAULT_QUERIES)
    ap.add_argument("--out", default=DEFAULT_CANDIDATES)
    ap.add_argument("--vectors-out", default=DEFAULT_VECTORS)
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--allow-bm25-only", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.queries):
        return _fail("queries_missing", path=args.queries)
    queries = common.load_jsonl(args.queries)

    from kb.bm25 import BM25Index
    from kb.store import SQLiteMirrorStore
    mirror = SQLiteMirrorStore()
    try:
        books = [b for b in mirror.list_books(status="ready")]
    except Exception as exc:  # noqa: BLE001
        return _fail("mirror_unavailable", detail=str(exc)[:120])
    if not books:
        return _fail("no_ready_books")
    current = {b["book_id"]: int(b.get("current_generation") or 0) for b in books}
    bm25 = BM25Index(mirror)

    embed_ok, vector_ok, embed_reason = False, False, ""
    embed_mod, KBVectorStore = None, None
    try:
        from kb import embed as embed_mod  # noqa: PLC0415
        from kb.vector import KBVectorStore  # noqa: PLC0415
        embed_ok = embed_mod.ensure().get("ok", False)
        vector_ok = KBVectorStore().health().get("ok", False)
    except Exception as exc:  # noqa: BLE001
        embed_reason = exc.__class__.__name__
    if not (embed_ok and vector_ok):
        if not args.allow_bm25_only:
            return _fail("vector_pool_unavailable",
                         embedding=embed_ok, milvus=vector_ok,
                         detail=embed_reason or "embedding/milvus 不可用")
    vectors = KBVectorStore() if (embed_ok and vector_ok) else None

    rows, vector_rows = [], []
    for query in queries:
        qid, text = query.get("query_id"), str(query.get("query") or "").strip()
        if not (qid and text):
            continue
        picked, seen = [], set()
        if vectors is not None:
            vec = embed_mod.embed_query(text)
            if vec:
                vector_rows.append({"query_id": qid, "vector": vec})
                for hit in vectors.search(vec, limit=max(1, args.top)):
                    bid = hit.get("book_id")
                    if bid not in current or int(hit.get("generation") or 0) != current.get(bid, -1):
                        continue
                    key = hit.get("chunk_id")
                    if key in seen:
                        continue
                    seen.add(key)
                    picked.append({**hit, "source": "milvus"})
        try:
            for hit in bm25.search(text, args.top):
                key = hit.get("chunk_id")
                if key in seen:
                    continue
                seen.add(key)
                picked.append({**hit, "source": "bm25"})
        except Exception as exc:  # noqa: BLE001
            return _fail("mirror_unavailable", detail=str(exc)[:120])
        for rank, hit in enumerate(picked[: 2 * args.top], 1):
            rows.append({
                "query_id": qid, "query": text, "rank": rank,
                "source": hit.get("source"), "book_id": hit.get("book_id"),
                "chunk_id": hit.get("chunk_id"),
                "chunk_anchor": hit.get("anchor_hash") or "",
                "section_path": hit.get("section_path") or "",
                "page": hit.get("page_hint"),
                "content": (hit.get("content") or "")[:300],
                "vector_score": hit.get("vector_score"),
                "bm25_score": hit.get("bm25_score"),
            })

    common.write_jsonl(args.out, rows)
    common.write_jsonl(args.vectors_out, vector_rows)
    print(json.dumps({"ok": True, "queries": len(queries), "candidates": len(rows),
                      "used_vectors": bool(vector_rows), "out": args.out,
                      "vectors_out": args.vectors_out,
                      "warning": "" if vector_rows else "bm25_only_pool"},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
