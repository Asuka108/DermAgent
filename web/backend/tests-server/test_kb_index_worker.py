# -*- coding: utf-8 -*-
"""KB 队列/索引/worker 单测：幂等入队、generation 切换、重试与诚实失败。"""
import hashlib
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server.kb import config  # noqa: E402
from server.kb.index import Indexer, IndexFailure  # noqa: E402
from server.kb.parse import ParseError  # noqa: E402
from server.kb.queue import MemoryQueue  # noqa: E402
from server.kb.store import InMemoryKBStore, SQLiteMirrorStore  # noqa: E402
from server.kb.worker import KBWorker  # noqa: E402


# —— 队列 ——

def test_queue_enqueue_is_idempotent_until_terminal():
    queue = MemoryQueue()
    first = queue.enqueue("bk1", "index")
    assert queue.enqueue("bk1", "index") == first
    assert queue.get_job(first)["state"] == "queued"

    job_id = queue.claim(timeout=0)
    assert job_id == first
    job = queue.get_job(job_id)
    assert job["state"] == "running" and job["attempts"] == 1
    assert queue.enqueue("bk1", "index") == first

    queue.finish(job_id, True)
    assert queue.get_job(job_id)["state"] == "done"
    second = queue.enqueue("bk1", "index")
    assert second != first


def test_queue_retry_delay_and_promote():
    queue = MemoryQueue()
    job_id = queue.enqueue("bk1", "index")
    queue.claim(timeout=0)
    queue.retry(job_id, delay=60)
    assert queue.get_job(job_id)["state"] == "queued"
    assert queue.claim(timeout=0) is None
    assert queue.promote_delayed(now=0) == 0
    assert queue.promote_delayed(now=10 ** 12) == 1
    assert queue.claim(timeout=0) == job_id


def test_queue_reclaim_processing_after_crash():
    queue = MemoryQueue()
    job_id = queue.enqueue("bk1", "index")
    queue.claim(timeout=0)
    assert queue.reclaim() == 1
    job = queue.get_job(job_id)
    assert job["state"] == "queued"
    assert queue.claim(timeout=0) == job_id


def test_queue_progress_and_lease():
    queue = MemoryQueue()
    queue.set_progress("bk1", "parsing", 15, "解析中")
    assert queue.progress("bk1") == {"phase": "parsing", "pct": 15, "message": "解析中"}
    assert queue.acquire_lease("bk1", "job-a") is True
    assert queue.acquire_lease("bk1", "job-b") is False
    queue.release_lease("bk1")
    assert queue.acquire_lease("bk1", "job-b") is True


# —— 索引 ——

class FakeVectors:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    def upsert(self, chunks, vectors):
        for chunk, vec in zip(chunks, vectors):
            self.rows[chunk["chunk_id"]] = {**chunk, "vector": vec}
        return len(chunks)

    def count_generation(self, book_id, generation):
        return sum(1 for r in self.rows.values()
                   if r["book_id"] == book_id and r["generation"] == generation)

    def generations(self, book_id):
        return sorted({r["generation"] for r in self.rows.values() if r["book_id"] == book_id})

    def delete_generation(self, book_id, generation):
        for cid in [c for c, r in self.rows.items()
                    if r["book_id"] == book_id and r["generation"] == generation]:
            self.rows.pop(cid, None)

    def delete_book(self, book_id):
        for cid in [c for c, r in self.rows.items() if r["book_id"] == book_id]:
            self.rows.pop(cid, None)

    def health(self):
        return {"ok": True, "latency_ms": 0, "detail": "fake"}


def fake_embed(texts):
    out = []
    for text in texts:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        vec = [digest[i % 32] / 255.0 - 0.5 for i in range(768)]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        out.append([x / norm for x in vec])
    return out


def direct_parser(path):
    with open(path, encoding="utf-8") as fh:
        return {"markdown": fh.read(), "raw_json": {}, "engine": "direct",
                "version": "", "tier": "direct"}


def _setup(tmp_path, parser=None, embedder=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = InMemoryKBStore()
    src = tmp_path / "book.txt"
    src.write_text("# 湿疹\n\n" + "皮肤瘙痒、红斑反复发作。" * 40, encoding="utf-8")
    book = store.create_book({
        "filename": "book.txt", "ext": "txt", "sha256": "b" * 64,
        "size_bytes": src.stat().st_size, "original_path": str(src)})
    vectors = FakeVectors()
    mirror = SQLiteMirrorStore(str(tmp_path / "mirror.sqlite3"))
    indexer = Indexer(store, mirror=mirror, vectors=vectors,
                      parser=parser or direct_parser, embedder=embedder or fake_embed)
    return store, book, vectors, mirror, indexer


def test_indexer_initial_and_reindex_generation(tmp_path):
    store, book, vectors, mirror, indexer = _setup(tmp_path)
    out = indexer.run(book)
    assert out["generation"] == 1 and out["chunk_count"] > 0
    ready = store.get_book(book["book_id"])
    assert ready["status"] == "ready" and ready["current_generation"] == 1
    assert ready["chunk_count"] == out["chunk_count"]
    assert vectors.generations(book["book_id"]) == [1]
    assert mirror.chunk_count() == out["chunk_count"]
    assert os.path.isfile(ready["cleaned_path"])
    assert ready["meta"]["clean_log"]

    out2 = indexer.run(ready, reindex=True)
    assert out2["generation"] == 2
    assert vectors.generations(book["book_id"]) == [2]
    assert mirror.chunk_count() == out2["chunk_count"]
    assert store.get_book(book["book_id"])["current_generation"] == 2


def test_indexer_failures_are_classified(tmp_path):
    def empty_parser(path):
        return {"markdown": "", "raw_json": {}, "engine": "mineru"}

    store, book, vectors, mirror, indexer = _setup(tmp_path, parser=empty_parser)
    with pytest.raises(IndexFailure) as exc:
        indexer.run(book)
    assert exc.value.code == "empty_text" and exc.value.retryable is False
    assert store.get_book(book["book_id"])["status"] == "cleaning"

    store2, book2, _v, _m, indexer2 = _setup(tmp_path / "b", embedder=lambda texts: None)
    with pytest.raises(IndexFailure) as exc2:
        indexer2.run(book2)
    assert exc2.value.code == "model_unavailable" and exc2.value.retryable is True

    def missing_mineru(path):
        raise ParseError("mineru_unavailable", "not_installed")

    store3, book3, _v3, _m3, indexer3 = _setup(tmp_path / "c", parser=missing_mineru)
    with pytest.raises(IndexFailure) as exc3:
        indexer3.run(book3)
    assert exc3.value.code == "mineru_unavailable" and exc3.value.retryable is False


# —— worker ——

def _worker(tmp_path, **kwargs):
    store, book, vectors, mirror, indexer = _setup(tmp_path, **kwargs)
    queue = MemoryQueue()
    worker = KBWorker(queue, indexer, store, leader_required=False)
    return store, book, queue, worker


def test_worker_indexes_book_to_ready(tmp_path):
    store, book, queue, worker = _worker(tmp_path)
    job_id = queue.enqueue(book["book_id"], "index")
    assert worker.run_once() is True
    assert queue.get_job(job_id)["state"] == "done"
    assert store.get_book(book["book_id"])["status"] == "ready"


def test_worker_mineru_missing_fails_without_retry(tmp_path):
    def missing(path):
        raise ParseError("mineru_unavailable", "not_installed")

    store, book, queue, worker = _worker(tmp_path, parser=missing)
    job_id = queue.enqueue(book["book_id"], "index")
    worker.run_once()
    job = queue.get_job(job_id)
    assert job["state"] == "failed" and job["error_code"] == "mineru_unavailable"
    row = store.get_book(book["book_id"])
    assert row["status"] == "failed" and row["error_code"] == "mineru_unavailable"


def test_worker_retryable_then_success(tmp_path):
    calls = {"n": 0}

    def flaky(texts):
        calls["n"] += 1
        return None if calls["n"] == 1 else fake_embed(texts)

    store, book, queue, worker = _worker(tmp_path, embedder=flaky)
    job_id = queue.enqueue(book["book_id"], "index")
    worker.run_once()
    job = queue.get_job(job_id)
    assert job["state"] == "queued" and job["error_code"] == "model_unavailable"
    assert store.get_book(book["book_id"])["status"] == "queued"

    assert queue.promote_delayed(now=10 ** 12) == 1
    worker.run_once()
    assert queue.get_job(job_id)["state"] == "done"
    assert store.get_book(book["book_id"])["status"] == "ready"
    assert calls["n"] == 2


def test_worker_stops_after_max_attempts(tmp_path):
    def broken(texts):
        return None

    store, book, queue, worker = _worker(tmp_path, embedder=broken)
    job_id = queue.enqueue(book["book_id"], "index")
    for _ in range(config.job_max_attempts()):
        worker.run_once()
        queue.promote_delayed(now=10 ** 12)
    job = queue.get_job(job_id)
    assert job["state"] == "failed" and job["attempts"] == config.job_max_attempts()
    assert job["error_code"] == "model_unavailable"
    assert store.get_book(book["book_id"])["status"] == "failed"


def test_indexer_persists_cloud_images_without_path_escape(tmp_path):
    def cloud_parser(path):
        return {"markdown": "# 湿疹\n\n" + "皮肤瘙痒、红斑反复发作。" * 40,
                "raw_json": {"content_list": []},
                "images": {"images/a.jpg": b"img", "../escape.jpg": b"bad"},
                "engine": "mineru_cloud", "version": "v4", "tier": "standard"}

    store, book, _v, _m, indexer = _setup(tmp_path, parser=cloud_parser)
    indexer.run(book)
    ready = store.get_book(book["book_id"])
    base = os.path.dirname(ready["raw_md_path"])
    with open(os.path.join(base, "images", "a.jpg"), "rb") as fh:
        assert fh.read() == b"img"
    assert not os.path.exists(os.path.join(base, "..", "escape.jpg"))
    assert ready["mineru"]["engine"] == "mineru_cloud"


# —— 切分偏好进入实际管线 ——

def _write_prefs(monkeypatch, tmp_path, payload):
    import json

    import rag

    path = tmp_path / "prefs.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rag, "PREFS_FILE", str(path))
    return rag


def test_indexer_reads_chunk_prefs_and_records_meta(tmp_path, monkeypatch):
    _write_prefs(monkeypatch, tmp_path,
                 {"strength": "mid", "chunk_mode": "sentence", "chunk_overlap": 20})
    store, book, _v, _m, indexer = _setup(tmp_path)
    out = indexer.run(book)
    meta = store.get_book(book["book_id"])["meta"]
    assert meta["chunk_mode"] == "sentence"
    assert meta["chunk_overlap"] == 20
    assert meta["chunk_target"] == config.chunk_target()
    assert meta["chunk_max"] == config.chunk_max()
    assert meta["chunk_count"] == out["chunk_count"] > 0


def test_indexer_fails_honestly_on_invalid_stored_overlap(tmp_path, monkeypatch):
    _write_prefs(monkeypatch, tmp_path, {"chunk_mode": "section", "chunk_overlap": 999})
    store, book, _v, _m, indexer = _setup(tmp_path)
    with pytest.raises(IndexFailure) as exc:
        indexer.run(book)
    assert exc.value.code == "bad_chunk_overlap"
    assert exc.value.retryable is False
    assert "999" in exc.value.detail or "overlap" in exc.value.detail
