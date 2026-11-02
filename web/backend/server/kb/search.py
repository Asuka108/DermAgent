# -*- coding: utf-8 -*-
"""检索编排：k 选择 → 查询向量 → Milvus 召回 → 重排 → n 断崖 → 引用；失败降级 BM25。"""
import time

from . import bm25 as bm25_mod
from . import citations as citations_mod
from . import config
from . import embed as embed_mod
from . import kselect
from . import rerank as rerank_mod


def choose_n(scores: list, cap: int = None) -> int:
    """得分降序后的最大相邻落差处截断；无落差取 top-n_max；最终 3≤n≤8。"""
    m = len(scores)
    if m == 0:
        return 0
    if m <= 3:
        n = m
    else:
        best_i, best_mag = None, 0.0
        for i in range(m - 1):
            drop = float(scores[i]) - float(scores[i + 1])
            rel = drop / max(abs(float(scores[i])), 1e-9)
            if rel >= config.gap_rel() or drop >= config.gap_abs():
                magnitude = max(rel / max(config.gap_rel(), 1e-9),
                                drop / max(config.gap_abs(), 1e-9))
                if magnitude > best_mag + 1e-12:
                    best_i, best_mag = i, magnitude
        n = (best_i + 1) if best_i is not None else min(config.n_max(), m)
        n = max(config.n_min(), min(config.n_max(), n, m))
    if cap:
        n = max(1, min(n, int(cap)))
    return n


def _snapshot(books: list) -> str:
    latest = max((b.get("updated_at") or "" for b in books), default="none")
    return f"local-books@{len(books)}books@{str(latest)[:19]}"


class KBSearch:
    def __init__(self, store=None, mirror=None, vectors=None, queue=None, audit=None):
        self.store = store
        self.mirror = mirror
        self.vectors = vectors
        self.queue = queue
        self._audit_cb = audit or (lambda event, detail="": None)
        self.bm25 = bm25_mod.BM25Index(mirror) if mirror is not None else None

    def audit(self, event: str, detail) -> None:
        try:
            self._audit_cb(event, detail)
        except Exception:  # noqa: BLE001
            pass

    # —— 书目 ——
    def ready_books(self) -> list:
        if self.store is not None:
            try:
                return [b for b in self.store.list_books() if b.get("status") == "ready"]
            except Exception:  # noqa: BLE001 — MySQL 不可用走镜像目录
                pass
        if self.mirror is not None:
            try:
                return self.mirror.list_books(status="ready")
            except Exception:  # noqa: BLE001
                return []
        return []

    def _current(self, books: list) -> dict:
        return {b["book_id"]: int(b.get("current_generation") or 0) for b in books}

    def _milvus_hits(self, query_vec, k, current, allowed):
        raw = self.vectors.search(query_vec, limit=min(200, max(2 * k, k)))
        if not raw:
            return []
        max_gen = {}
        for hit in raw:
            bid = hit.get("book_id")
            if bid not in allowed:
                continue
            gen = int(hit.get("generation") or 0)
            if current.get(bid) and gen != current[bid]:
                continue
            max_gen[bid] = max(max_gen.get(bid, 0), gen)
        hits = [h for h in raw
                if h.get("book_id") in allowed
                and (not current.get(h.get("book_id"))
                     or int(h.get("generation") or 0) == current.get(h.get("book_id")))
                and int(h.get("generation") or 0) == max_gen.get(h.get("book_id"))]
        hits.sort(key=lambda h: (-float(h.get("vector_score") or 0), h.get("chunk_id") or ""))
        return hits[:k]

    def search(self, query: str, top_n: int = None) -> dict:
        started = time.perf_counter()
        query = citations_mod.sanitize_query(query)
        books = self.ready_books()
        allowed = {b["book_id"] for b in books}
        current = self._current(books)
        snapshot = _snapshot(books)
        base = {"ok": True, "query": query, "citations": [], "retrieval_mode": "bm25",
                "k_used": 0, "n_used": 0, "k_source": "default", "k_spec_version": "",
                "rerank_state": "skipped", "degraded_reason": "",
                "total_indexed_books": len(books), "kb_snapshot": snapshot,
                "embedding_state": embed_mod.health().get("state", "unknown")}
        if not query:
            base["degraded_reason"] = "empty_query"
            return base
        if not allowed:
            base["degraded_reason"] = "no_ready_books"
            return base

        query_vec = embed_mod.embed_query(query)
        base["embedding_state"] = embed_mod.status().get("state", base["embedding_state"])
        choice = kselect.choose(query_vec)
        k = int(choice["k"])
        k_source, version = choice["source"], choice["version"]
        hits, scores, mode, rerank_state, reason = [], [], "bm25", "skipped", ""

        if self.vectors is not None and query_vec:
            try:
                hits = self._milvus_hits(query_vec, k, current, allowed)
                if not hits:
                    reason = "milvus_empty"
                    hits = []
            except Exception as exc:  # noqa: BLE001 — Milvus 异常降级 BM25
                self.audit("kb_search_degraded", {"reason": "milvus_unavailable",
                                                  "error": str(exc)[:160]})
                hits, reason = [], "milvus_unavailable"
            if hits:
                mode = "milvus"
                rerank_scores = rerank_mod.score(query, [h.get("content") or "" for h in hits])
                if rerank_scores is None:
                    rerank_state = rerank_mod.status().get("state") or "error"
                    scores = [float(h.get("vector_score") or 0) for h in hits]
                else:
                    rerank_state = "ready"
                    scores = rerank_scores
                    mode = "milvus_rerank"
        elif self.vectors is not None:
            reason = "embedding_unavailable"

        if not hits and self.bm25 is not None:
            try:
                hits = self.bm25.search(query, k, book_ids=allowed)
                scores = [float(h.get("bm25_score") or 0) for h in hits]
                mode, rerank_state = "bm25", "skipped"
            except Exception as exc:  # noqa: BLE001 — 镜像不可用：空引用，不伪造
                self.audit("kb_search_degraded", {"reason": "mirror_unavailable",
                                                  "error": str(exc)[:160]})
                hits, scores = [], []
                reason = "mirror_unavailable"
        elif not hits and self.bm25 is None:
            reason = reason or "mirror_unavailable"

        if query_vec is None and not reason:
            reason = "embedding_unavailable"
        if not hits:
            base.update({"k_used": k, "k_source": k_source, "k_spec_version": version,
                         "rerank_state": rerank_state, "degraded_reason": reason,
                         "latency_ms": round((time.perf_counter() - started) * 1000, 1)})
            return base

        order = sorted(range(len(hits)), key=lambda i: (-float(scores[i]), hits[i].get("chunk_id") or ""))
        hits = [hits[i] for i in order]
        scores = [float(scores[i]) for i in order]
        n = choose_n(scores, cap=top_n)

        cite_books = {b["book_id"]: b for b in books}
        out = []
        for hit, score in zip(hits[:n], scores[:n]):
            if mode == "milvus_rerank":
                hit["rerank_score"] = round(score, 6)
            elif mode == "milvus":
                hit["vector_score"] = round(score, 6)
            book = cite_books.get(hit.get("book_id")) or {}
            meta = book.get("meta") or {}
            hit.setdefault("version", meta.get("version") or book.get("version") or "")
            hit.setdefault("author", meta.get("author") or book.get("author") or "")
            hit.setdefault("publisher", meta.get("publisher") or book.get("publisher") or "")
            hit.setdefault("filename", book.get("filename") or "")
            out.append(citations_mod.to_citation(
                hit, retrieval_mode=mode, k_used=k, n_used=n, k_source=k_source,
                rerank_state=rerank_state, kb_snapshot=snapshot))

        base.update({"citations": out, "retrieval_mode": mode, "k_used": k,
                     "n_used": len(out), "k_source": k_source, "k_spec_version": version,
                     "rerank_state": rerank_state, "degraded_reason": reason,
                     "latency_ms": round((time.perf_counter() - started) * 1000, 1)})
        if reason:
            self.audit("kb_search_degraded", {"reason": reason, "query": query[:60]})
        return base
