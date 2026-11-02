# -*- coding: utf-8 -*-
"""BM25 降级检索：只读 SQLite 镜像，中文 bigram + 拉丁词（Okapi k1=1.5 b=0.75）。"""
import re


def tokens(text: str) -> list:
    out = []
    for word in re.split(r"[\s,，。；;：:！!？?\.\-—()（）\[\]【】《》\"']+", (text or "").lower()):
        if not word:
            continue
        if re.fullmatch(r"[a-z0-9]+", word):
            out.append(word)
        else:
            out.extend(word[i:i + 2] for i in range(len(word) - 1))
    return out


def bm25_scores(query: str, docs: list) -> dict:
    """docs: [(chunk_id, tokens)] → {chunk_id: score}。"""
    q = tokens(query)
    if not q or not docs:
        return {}
    k1, b = 1.5, 0.75
    avgdl = sum(len(t) for _, t in docs) / max(len(docs), 1)
    df = {}
    for _, toks in docs:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1
    n = len(docs)
    scores = {}
    for cid, toks in docs:
        tf = {}
        for term in toks:
            tf[term] = tf.get(term, 0) + 1
        dl = len(toks)
        score = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = max(0.01, (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scores[cid] = round(score, 4)
    return scores


class BM25Index:
    """镜像只读检索；book_ids 限定 ready 书目，镜像异常由调用方降级为空结果。"""

    def __init__(self, mirror, token_cache_limit: int = 200000):
        self.mirror = mirror
        self._cache: dict[str, list] = {}
        self._cache_limit = token_cache_limit

    def _tokens_for(self, row: dict) -> list:
        key = row.get("chunk_id") or ""
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        toks = tokens(row.get("content") or "")
        if len(self._cache) < self._cache_limit:
            self._cache[key] = toks
        return toks

    def health(self) -> dict:
        return self.mirror.health()

    def search(self, query: str, limit: int, book_ids=None) -> list:
        books = {b["book_id"]: b for b in self.mirror.list_books()}
        allowed = set(book_ids) if book_ids is not None else set(books)
        rows = [r for r in self.mirror.chunks(current_only=True) if r["book_id"] in allowed]
        if not rows:
            return []
        docs = [(r["chunk_id"], self._tokens_for(r)) for r in rows]
        scores = bm25_scores(query, docs)
        by_id = {r["chunk_id"]: r for r in rows}
        top = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, int(limit))]
        out = []
        for cid, score in top:
            row = by_id[cid]
            book = books.get(row["book_id"]) or {}
            out.append({
                "chunk_id": row["chunk_id"], "book_id": row["book_id"],
                "generation": int(row.get("generation") or 0),
                "chunk_index": int(row.get("chunk_index") or 0),
                "section": row.get("section") or "", "section_path": row.get("section_path") or "",
                "page": row.get("page"), "page_hint": row.get("page_hint"),
                "content": row.get("content") or "", "content_hash": row.get("content_hash") or "",
                "anchor_hash": row.get("anchor_hash") or "",
                "token_count": int(row.get("token_count") or 0),
                "book_title": book.get("title") or book.get("filename") or "",
                "vector_score": None, "bm25_score": score,
            })
        return out
