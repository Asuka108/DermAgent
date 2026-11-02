# -*- coding: utf-8 -*-
"""KB 向量化/重排/向量库适配单测：前缀区分、768 维校验、诚实降级。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server.kb import config, embed, rerank  # noqa: E402
from server.kb.vector import KBVectorStore  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    embed.reset()
    rerank.reset()
    yield
    embed.reset()
    rerank.reset()


class FakeRag:
    def __init__(self, dim=768, ok=True, installed=True):
        self.dim = dim
        self.ok = ok
        self.installed = installed
        self.calls = []
        self.ensured = 0

    def is_installed(self, short):
        return self.installed

    def ensure_specific(self, short):
        self.ensured += 1
        if not self.ok:
            return {"ok": False, "detail": "模型未安装（server/data/models/x）"}
        return {"ok": True, "detail": "loaded", "dim": self.dim}

    def embed_specific(self, texts, short, *, prefix="", timeout_ms=0):
        self.calls.append({"texts": list(texts), "prefix": prefix, "model": short})
        return [[0.01] * self.dim for _ in texts]


def test_embed_documents_no_prefix_and_query_prefix(monkeypatch):
    fake = FakeRag()
    monkeypatch.setattr(embed, "_rag_embed", lambda: fake)

    docs = embed.embed_documents(["段落一", "段落二"])
    assert len(docs) == 2 and all(len(v) == 768 for v in docs)
    assert fake.calls[-1]["prefix"] == ""
    assert fake.calls[-1]["texts"] == ["段落一", "段落二"]

    vec = embed.embed_query("湿疹怎么治")
    assert len(vec) == 768
    assert fake.calls[-1]["prefix"] == config.embed_query_prefix()
    assert fake.calls[-1]["texts"] == ["湿疹怎么治"]
    assert fake.ensured == 1
    assert embed.ready() and embed.status()["state"] == "ready"


def test_embed_rejects_wrong_dim(monkeypatch):
    fake = FakeRag(dim=1024)
    monkeypatch.setattr(embed, "_rag_embed", lambda: fake)
    assert embed.embed_documents(["x"]) is None
    assert embed.status()["state"] == "error"


def test_embed_unavailable_is_honest(monkeypatch):
    fake = FakeRag(ok=False)
    monkeypatch.setattr(embed, "_rag_embed", lambda: fake)
    assert embed.embed_query("x") is None
    assert embed.status()["state"] == "not_installed"
    assert embed.health()["ok"] is False


def test_embed_empty_inputs(monkeypatch):
    fake = FakeRag()
    monkeypatch.setattr(embed, "_rag_embed", lambda: fake)
    assert embed.embed_documents([]) == []
    assert embed.embed_query("   ") is None
    assert fake.calls == []


def test_rerank_sigmoid_bounds():
    assert rerank.sigmoid(0) == 0.5
    assert 0.0 < rerank.sigmoid(-100) < 0.01
    assert 0.99 < rerank.sigmoid(100) <= 1.0


class FakeCrossEncoder:
    def __init__(self, outputs=None, error=None):
        self.outputs = outputs or []
        self.error = error
        self.pairs = []

    def predict(self, pairs, batch_size=8, show_progress_bar=False):
        self.pairs = pairs
        if self.error:
            raise RuntimeError(self.error)
        return self.outputs


def test_rerank_score_applies_sigmoid(monkeypatch):
    model = FakeCrossEncoder([[-1.0], [0.0], [2.0]])
    rerank._model = model
    rerank._state = {"state": "ready", "detail": ""}
    scores = rerank.score("湿疹", ["a", "b", "c"])
    assert scores[1] == 0.5
    assert scores[0] < scores[1] < scores[2]
    assert model.pairs == [["湿疹", "a"], ["湿疹", "b"], ["湿疹", "c"]]
    assert rerank.ready() is True


def test_rerank_error_marks_state(monkeypatch):
    monkeypatch.setattr(rerank, "ensure", lambda: {"ok": True, "state": "ready", "detail": ""})
    rerank._model = FakeCrossEncoder(error="boom")
    rerank._state = {"state": "ready", "detail": ""}
    assert rerank.score("q", ["t"]) is None
    assert rerank.status()["state"] == "error"


def test_rerank_missing_is_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "rerank_dir", lambda: str(tmp_path / "missing"))
    monkeypatch.setattr(rerank.importlib.util, "find_spec", lambda name: object())
    out = rerank.ensure()
    assert out["ok"] is False and out["state"] == "not_installed"
    assert rerank.health()["ok"] is False


def test_vector_store_rejects_bad_batches_without_milvus():
    store = KBVectorStore(uri="http://127.0.0.1:1", collection="x")
    with pytest.raises(ValueError):
        store.upsert([{"chunk_id": "a"}], [])
    with pytest.raises(ValueError):
        store.upsert([{"chunk_id": "a"}], [[0.0] * 767])
    assert store.upsert([], []) == 0


def test_vector_store_health_without_pymilvus():
    import importlib.util
    if importlib.util.find_spec("pymilvus") is not None:
        pytest.skip("pymilvus 已安装，健康检查会尝试真实连接")
    store = KBVectorStore(uri="http://127.0.0.1:1", collection="x")
    out = store.health()
    assert out["ok"] is False and out["detail"]


def test_vector_store_upsert_is_column_major():
    """pymilvus ORM upsert 只收列式数据（14 列，等长）；行式会被服务端拒绝。"""
    class _Col:
        def __init__(self):
            self.data = None

        def upsert(self, data):
            self.data = data

    store = KBVectorStore(uri="http://127.0.0.1:1", collection="x")
    col = _Col()
    store._collection = lambda: col
    chunks = [
        {"chunk_id": "a", "book_id": "b1", "generation": 1, "chunk_index": 0,
         "content": "甲", "book_title": "书"},
        {"chunk_id": "b", "book_id": "b1", "generation": 1, "chunk_index": 1,
         "content": "乙", "book_title": "书"},
    ]
    assert store.upsert(chunks, [[0.1] * 768, [0.2] * 768]) == 2
    assert len(col.data) == 14
    assert all(len(column) == 2 for column in col.data)
    assert col.data[0] == ["a", "b"]
    assert len(col.data[-1][0]) == 768


def test_vector_store_count_has_no_pagination_and_flushes():
    """Milvus 2.6 禁止 count(*) + limit；计数前必须 flush 才能看到刚写入的数据。"""
    class _Col:
        def __init__(self):
            self.flushed = 0
            self.kwargs = None

        def flush(self):
            self.flushed += 1

        def query(self, **kwargs):
            self.kwargs = kwargs
            return [{"count(*)": 3}]

    store = KBVectorStore(uri="http://127.0.0.1:1", collection="x")
    col = _Col()
    store._collection = lambda: col
    assert store.count_generation("b1", 2) == 3
    assert col.flushed == 1
    assert "limit" not in col.kwargs
    assert 'generation == 2' in col.kwargs["expr"]


def test_vector_store_delete_flushes():
    class _Col:
        def __init__(self):
            self.deleted = []
            self.flushed = 0

        def delete(self, expr):
            self.deleted.append(expr)

        def flush(self):
            self.flushed += 1

    store = KBVectorStore(uri="http://127.0.0.1:1", collection="x")
    col = _Col()
    store._collection = lambda: col
    store.delete_book("b1")
    store.delete_generation("b1", 2)
    assert col.flushed == 2
    assert 'book_id == "b1"' in col.deleted[0]
    assert "generation == 2" in col.deleted[1]
