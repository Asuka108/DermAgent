# -*- coding: utf-8 -*-
"""服务层单测：上传/入队/索引/重建/隔离/恢复/硬删/备份与降级。"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server.kb import configure, reset  # noqa: E402
from server.kb import search as search_mod  # noqa: E402
from server.kb.queue import MemoryQueue  # noqa: E402
from server.kb.store import InMemoryKBStore, SQLiteMirrorStore  # noqa: E402

DOC = """# 第一章 红斑狼疮

红斑狼疮的诊断标准包括抗核抗体阳性与皮肤活检。系统性红斑狼疮需要长期随访和免疫指标监测。

## 第一段

患者应避免日晒，并在医生指导下使用防晒措施。治疗方向需医生核对。
"""


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
            dot = sum(a * b for a, b in zip(vector, row["vector"]))
            na = math.sqrt(sum(a * a for a in vector))
            nb = math.sqrt(sum(b * b for b in row["vector"]))
            out.append({**row, "vector_score": dot / (na * nb or 1.0)})
        out.sort(key=lambda r: (-r["vector_score"], r["chunk_id"]))
        return out[:limit]

    def health(self):
        return {"ok": True, "detail": f"fake rows={len(self.rows)}"}


class FakeParser:
    def __init__(self, text=DOC):
        self.text = text
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        return {"markdown": self.text, "raw_json": {}, "engine": "fake",
                "version": "0.1", "tier": "standard"}


def _setup(tmp_path, *, queue=True):
    store = InMemoryKBStore()
    mirror = SQLiteMirrorStore(str(tmp_path / "mirror.sqlite3"))
    vectors = FakeVectors()
    q = MemoryQueue() if queue else None
    events = []
    svc = configure(store=store, mirror=mirror, vectors=vectors, queue=q,
                    audit=lambda e, d="": events.append(e))
    parser = FakeParser()
    svc.indexer._parser = parser
    svc.indexer._embedder = lambda texts: [[0.5] * 768 for _ in texts]
    return svc, store, mirror, vectors, parser, events


@pytest.fixture(autouse=True)
def _isolate_kb():
    reset()
    yield
    reset()


def _upload(svc):
    return svc.upload("皮肤科.txt", DOC.encode("utf-8"))


def _indexed(tmp_path):
    svc, store, mirror, vectors, parser, events = _setup(tmp_path)
    book = _upload(svc)
    assert book["ok"], book
    queued = svc.index(book["book_id"])
    assert queued["ok"], queued
    assert svc.worker.run_once(timeout=0)
    got = store.get_book(book["book_id"])
    assert got["status"] == "ready"
    return svc, store, mirror, vectors, parser, events, book["book_id"]


# —— 上传 ——

def test_upload_dedup_and_rejects(tmp_path):
    svc, *_ = _setup(tmp_path)
    first = _upload(svc)
    assert first["ok"] and first["status"] == "uploaded"
    assert os.path.isfile(os.path.join(
        os.path.dirname(svc._get_book(first["book_id"])["original_path"]),
        "original.txt"))
    dup = _upload(svc)
    assert dup["ok"] is False and dup["reason"] == "duplicate_sha256"
    assert dup["duplicate_of"] == first["book_id"]
    assert svc.upload("bad.exe", b"x")["reason"] == "format_unsupported:exe"
    assert svc.upload("book.pdf", b"x" * 10)["ok"] is True


def test_upload_requires_mysql(tmp_path):
    store = InMemoryKBStore()
    svc = configure(store=None, mirror=SQLiteMirrorStore(str(tmp_path / "m.sqlite3")),
                    vectors=FakeVectors(), queue=MemoryQueue())
    assert svc.upload("a.txt", b"text")["reason"] == "mysql_unavailable"


# —— 索引 ——

def test_index_to_ready_and_search(tmp_path, monkeypatch):
    svc, store, mirror, vectors, parser, events, book_id = _indexed(tmp_path)
    book = store.get_book(book_id)
    assert book["current_generation"] == 1 and book["chunk_count"] >= 1
    assert book["index_state"] == "ready"
    assert parser.calls == 1
    assert mirror.chunk_count() == book["chunk_count"]
    assert vectors.count_generation(book_id, 1) == book["chunk_count"]
    assert any(r["chunk_id"].startswith(book_id + "-g1-") for r in vectors.rows.values())

    monkeypatch.setattr(search_mod.embed_mod, "embed_query", lambda q: [0.5] * 768)
    monkeypatch.setattr(search_mod.embed_mod, "status", lambda: {"state": "ready"})
    monkeypatch.setattr(search_mod.rerank_mod, "score", lambda q, texts: [0.9] * len(texts))
    monkeypatch.setattr(search_mod.rerank_mod, "status", lambda: {"state": "ready"})
    out = svc.search("红斑狼疮诊断")
    assert out["retrieval_mode"] == "milvus_rerank"
    assert out["citations"] and out["citations"][0]["book_id"] == book_id
    assert out["citations"][0]["section_path"].startswith("第一章")
    assert out["total_indexed_books"] == 1


def test_reindex_keeps_ready_and_swaps_generation(tmp_path):
    svc, store, mirror, vectors, parser, events, book_id = _indexed(tmp_path)
    svc.indexer._parser = FakeParser(DOC + "\n\n## 新增段落\n\n补充说明内容。\n")
    queued = svc.index(book_id, reindex=True)
    assert queued["kind"] == "reindex" and queued["generation"] == 2
    mid = store.get_book(book_id)
    assert mid["status"] == "ready" and mid["index_state"] == "queued"
    assert svc.worker.run_once(timeout=0)
    book = store.get_book(book_id)
    assert book["status"] == "ready" and book["current_generation"] == 2
    assert vectors.count_generation(book_id, 1) == 0
    assert vectors.count_generation(book_id, 2) == book["chunk_count"]
    assert mirror.chunk_count() == book["chunk_count"]


def test_index_requires_queue(tmp_path):
    svc, *_ = _setup(tmp_path, queue=False)
    book = _upload(svc)
    assert svc.index(book["book_id"])["reason"] == "queue_unavailable"


# —— 隔离 / 恢复 / 删除 ——

def test_quarantine_restore_and_delete(tmp_path):
    svc, store, mirror, vectors, parser, events, book_id = _indexed(tmp_path)
    calls = parser.calls
    assert svc.quarantine(book_id)["status"] == "quarantined"
    assert store.get_book(book_id)["status"] == "quarantined"
    assert mirror.chunk_count() == 0
    assert svc.search("红斑狼疮")["degraded_reason"] == "no_ready_books"

    restored = svc.restore(book_id)
    assert restored["ok"] and restored["status"] == "queued"
    assert svc.worker.run_once(timeout=0)
    book = store.get_book(book_id)
    assert book["status"] == "ready" and book["current_generation"] == 2
    assert parser.calls == calls          # 恢复复用清洗文本，不再调 MinerU
    assert vectors.count_generation(book_id, 2) == book["chunk_count"]

    assert svc.delete(book_id)["ok"] is True
    assert store.get_book(book_id) is None
    assert mirror.get_book(book_id) is None
    from server.kb import config as kb_config
    assert not os.path.isdir(kb_config.upload_dir(book_id))


def test_restore_requires_cleaned_text(tmp_path):
    svc, store, mirror, vectors, parser, events, book_id = _indexed(tmp_path)
    svc.quarantine(book_id)
    book = store.get_book(book_id)
    store.update_book(book_id, cleaned_path=book["cleaned_path"] + ".missing")
    assert svc.restore(book_id)["reason"] == "cleaned_missing"


# —— 备份 ——

def test_backup_v2_and_restore(tmp_path):
    svc, store, mirror, vectors, parser, events, book_id = _indexed(tmp_path)
    backup = svc.backup()
    assert backup["ok"] and backup["version"] == 2
    assert backup["books"] and backup["books"][0]["cleaned_text"].strip()
    assert "向量" not in backup["note"] or "不含向量" in backup["note"]

    store2 = InMemoryKBStore()
    mirror2 = SQLiteMirrorStore(str(tmp_path / "mirror2.sqlite3"))
    vectors2 = FakeVectors()
    svc2 = configure(store=store2, mirror=mirror2, vectors=vectors2, queue=MemoryQueue())
    svc2.indexer._embedder = lambda texts: [[0.5] * 768 for _ in texts]
    out = svc2.restore_backup(backup)
    assert out["ok"] and out["restored"] and out["needs_reindex"] is True
    assert svc2.worker.run_once(timeout=0)
    row = store2.list_books()[0]
    assert row["status"] == "ready" and row["current_generation"] == 1

    again = svc2.restore_backup(backup)
    assert again["ok"] and again["skipped"]


# —— 健康 ——

def test_health_components_honest(tmp_path):
    svc, *_ = _setup(tmp_path)
    health = svc.health()
    assert set(health["components"]) >= {"mysql", "mirror", "milvus", "redis",
                                         "embedder", "reranker", "mineru"}
    assert health["components"]["mysql"]["ok"] is True
    assert health["components"]["mineru"]["ok"] is False
    assert "worker" in health
