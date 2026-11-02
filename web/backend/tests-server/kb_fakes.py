# -*- coding: utf-8 -*-
"""KB 离线测试替身：解析/向量/Milvus/重排全部进程内确定性实现。

- FakeParser 直接读上传原件（txt/md）作为 MinerU 的 Markdown 输出；
- FakeVectors 用 dict + cosine 模拟 Milvus 的 generation 语义；
- 不联网、不加载 torch、不触碰真实 MySQL/Redis/Milvus。
"""
import hashlib
import math

from server.kb.queue import MemoryQueue
from server.kb.store import InMemoryKBStore, SQLiteMirrorStore


def _vec(text: str) -> list:
    """确定性 768 维单位向量（同一文本始终同向量）。"""
    digest = hashlib.sha256(str(text or "").encode("utf-8")).digest()
    raw = [(digest[i % len(digest)] / 255.0) - 0.5 for i in range(768)]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


class FakeVectors:
    def __init__(self):
        self.rows = {}

    def upsert(self, chunks, vectors):
        for c, v in zip(chunks, vectors):
            self.rows[c["chunk_id"]] = {**c, "vector": list(v)}

    def count_generation(self, book_id, generation):
        return sum(1 for r in self.rows.values()
                   if r["book_id"] == book_id and int(r["generation"]) == int(generation))

    def delete_book(self, book_id):
        self.rows = {k: v for k, v in self.rows.items() if v["book_id"] != book_id}

    def delete_generation(self, book_id, generation):
        self.rows = {k: v for k, v in self.rows.items()
                     if not (v["book_id"] == book_id
                             and int(v["generation"]) == int(generation))}

    def generations(self, book_id):
        return sorted({int(r["generation"]) for r in self.rows.values()
                       if r["book_id"] == book_id})

    def search(self, vector, limit=200, book_id=None):
        out = []
        for row in self.rows.values():
            if book_id and row["book_id"] != book_id:
                continue
            out.append({**row, "vector_score": _cosine(vector, row["vector"])})
        out.sort(key=lambda r: (-r["vector_score"], r["chunk_id"]))
        return out[:limit]

    def health(self):
        return {"ok": True, "detail": f"fake rows={len(self.rows)}"}


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb or 1.0)


class FakeParser:
    def __init__(self):
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        with open(path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        return {"markdown": text, "raw_json": {}, "engine": "fake",
                "version": "test", "tier": "standard"}


class FakeEmbedder:
    def __init__(self):
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        return [_vec(t) for t in texts]


def setup_fake_kb(tmp_path):
    """装配一个可直接跑 上传→索引→检索 的 KBService（需调用方 configure）。"""
    store = InMemoryKBStore()
    mirror = SQLiteMirrorStore(str(tmp_path / "mirror.sqlite3"))
    vectors = FakeVectors()
    parser = FakeParser()
    embedder = FakeEmbedder()
    return {"store": store, "mirror": mirror, "vectors": vectors, "queue": MemoryQueue(),
            "parser": parser, "embedder": embedder}


def install_index_fakes(svc, parser=None, embedder=None):
    if parser is not None:
        svc.indexer._parser = parser
    if embedder is not None:
        svc.indexer._embedder = embedder
    return svc
