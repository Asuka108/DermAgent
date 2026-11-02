# -*- coding: utf-8 -*-
"""检索链路单测：n 断崖、kselect 阈值、引用契约、BM25 镜像、编排降级。"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server.kb import bm25 as bm25_mod  # noqa: E402
from server.kb import citations as cite_mod  # noqa: E402
from server.kb import config as kb_config  # noqa: E402
from server.kb import kselect  # noqa: E402
from server.kb import search as search_mod  # noqa: E402
from server.kb.search import KBSearch, choose_n  # noqa: E402
from server.kb.store import InMemoryKBStore, SQLiteMirrorStore  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_kselect():
    kselect.configure(None)
    yield
    kselect.configure(None)


# —— n 断崖 ——

def test_choose_n_empty_and_small():
    assert choose_n([]) == 0
    assert choose_n([0.9]) == 1
    assert choose_n([0.9, 0.1]) == 2
    assert choose_n([0.9, 0.5, 0.1]) == 3


def test_choose_n_cliff_and_bounds():
    # 0.8 → 0.2 相对落差 75%，断在 i=3 → n=4
    assert choose_n([0.95, 0.9, 0.85, 0.8, 0.2, 0.19, 0.18]) == 4
    # 单点断崖落在 i=0，仍保底 3
    assert choose_n([1.0, 0.2, 0.1, 0.05]) == 3
    # 无断崖取 n_max=8
    assert choose_n([1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55]) == 8


def test_choose_n_tie_earliest_and_cap(monkeypatch):
    # 关闭相对阈值、固定绝对阈值 0.125：三个等距落差并列，取最早位置 i=2 → n=3
    monkeypatch.setattr(kb_config, "gap_rel", lambda: 10.0)
    monkeypatch.setattr(kb_config, "gap_abs", lambda: 0.125)
    assert choose_n([0.875, 0.75, 0.625, 0.5, 0.375, 0.25]) == 3
    monkeypatch.setattr(kb_config, "gap_rel", lambda: 0.30)
    assert choose_n([0.9, 0.8, 0.7, 0.6, 0.2, 0.1, 0.05, 0.0], cap=2) == 2
    assert choose_n([0.9, 0.8, 0.1], cap=5) == 3


# —— kselect ——

def _centroid(sim: float) -> list:
    return [sim, math.sqrt(max(0.0, 1.0 - sim * sim))]


def _spec(store, *, labels=True, global_k=12, clusters=None):
    store.save_k_spec(
        {"artifact_version": "kv-test", "global_k": global_k, "labels_present": labels},
        clusters or [])


def test_kselect_default_without_labels():
    store = InMemoryKBStore()
    _spec(store, labels=False, global_k=15)
    kselect.configure(store=store)
    assert kselect.choose([1.0, 0.0]) == {"k": 20, "source": "default",
                                          "version": "kv-test"}


def test_kselect_no_spec_is_default():
    kselect.configure(store=InMemoryKBStore())
    assert kselect.choose(None) == {"k": 20, "source": "default", "version": ""}


def test_kselect_cluster_threshold_and_samples(monkeypatch):
    # [3,4] 对 [1,0] 的 cosine 恰为 0.6，验证 >= 边界
    monkeypatch.setattr(kb_config, "cluster_min_sim", lambda: 0.6)
    cluster = {"cluster_id": 0, "centroid": [3.0, 4.0], "k_star": 9,
               "sample_count": 3}
    store = InMemoryKBStore()
    _spec(store, clusters=[cluster])
    kselect.configure(store=store)
    assert kselect.choose([1.0, 0.0]) == {"k": 9, "source": "cluster",
                                          "version": "kv-test"}

    store2 = InMemoryKBStore()
    _spec(store2, clusters=[{**cluster, "centroid": [1.0, 2.0]}])
    kselect.configure(store=store2)
    assert kselect.choose([1.0, 0.0]) == {"k": 12, "source": "global",
                                          "version": "kv-test"}

    store3 = InMemoryKBStore()
    _spec(store3, clusters=[{**cluster, "sample_count": 2}])
    kselect.configure(store=store3)
    assert kselect.choose([1.0, 0.0])["source"] == "global"


def test_kselect_global_and_clamp():
    store = InMemoryKBStore()
    _spec(store, global_k=99, clusters=[{"cluster_id": 0, "centroid": _centroid(0.9),
                                         "k_star": 99, "sample_count": 5}])
    kselect.configure(store=store)
    assert kselect.choose([1.0, 0.0]) == {"k": kb_config.k_max(), "source": "cluster",
                                          "version": "kv-test"}
    assert kselect.choose(None)["k"] == kb_config.k_max()


# —— 引用契约 ——

def test_sanitize_query_strips_identity_and_caps_length():
    raw = "患者姓名：张三 电话13800138000 身份证110101199001011234 红斑狼疮怎么诊断"
    out = cite_mod.sanitize_query(raw)
    assert "张三" not in out and "13800138000" not in out
    assert "110101199001011234" not in out
    assert "红斑狼疮" in out
    assert len(cite_mod.sanitize_query("诊" * 400)) == kb_config.query_max_chars()


def test_citation_id_prefers_anchor_and_is_stable():
    a = {"book_id": "bk0000000001", "anchor_hash": "abcdef1234567890",
         "content_hash": "deadbeef", "chunk_index": 1}
    b = dict(a, chunk_index=99)
    assert cite_mod.citation_id(a) == cite_mod.citation_id(b) == "RB-bk0000000001-abcdef12"
    assert cite_mod.citation_id({"book_id": "bk1", "content_hash": "0011223344"}) == "RB-bk1-00112233"
    assert cite_mod.citation_id({"book_id": "bk1"}).endswith("unknown")


def test_to_citation_keeps_legacy_fields_and_meta():
    hit = {"book_id": "bk1", "book_title": "皮肤科", "section": "红斑狼疮",
           "section_path": "第二章 > 红斑狼疮", "page": 42, "content": "正文" * 10,
           "content_hash": "h1", "anchor_hash": "a1", "vector_score": 0.81,
           "rerank_score": 0.93, "filename": "skin.pdf"}
    out = cite_mod.to_citation(hit, retrieval_mode="milvus_rerank", k_used=20,
                               n_used=3, k_source="global", rerank_state="ready",
                               kb_snapshot="local-books@1books@x")
    assert out["citation_id"] == "RB-bk1-a1"
    assert out["source_type"] == "local_book"
    assert out["retrieval_score"] == 0.93
    for key in ("quoted_text", "local_file", "book_title", "page", "chunk_hash",
                "vector_score", "rerank_score", "retrieval_mode", "k_used",
                "n_used", "k_source", "rerank_state", "kb_snapshot"):
        assert key in out
    assert out["k_used"] == 20 and out["n_used"] == 3


def test_validate_ids_whitelist():
    cites = [{"citation_id": "RB-a-1"}, {"citation_id": "RB-b-2"}]
    assert cite_mod.validate_ids(["RB-a-1", "RB-x-9", "SYN-1"], cites) == ["RB-a-1"]
    assert cite_mod.allowed_ids(cites) == {"RB-a-1", "RB-b-2"}


def test_build_context_truncates_per_item_and_total():
    cited = [{"citation_id": f"RB-b-{i}", "book_title": "书", "section_path": "一章",
              "page": 42, "quoted_text": "字" * 400, "rerank_score": 0.87}
             for i in range(3)]
    out = cite_mod.build_context(cited[:2])
    assert out["used"] == 2 and out["truncated"] is False
    assert "rerank=0.87" in out["text"] and "p.42" in out["text"]
    assert "…" in out["text"]
    assert len(out["text"].splitlines()[0]) <= kb_config.citation_chars() + 120
    assert out["chars"] <= kb_config.context_chars()

    tight = cite_mod.build_context(cited, total_chars=500)
    assert tight["truncated"] is True and tight["used"] == 1
    none = cite_mod.build_context(cited, total_chars=10)
    assert none["used"] == 0 and none["truncated"] is True


# —— BM25 镜像 ——

def _mirror_book(mirror, book_id, title, content):
    mirror.sync_book({"book_id": book_id, "filename": f"{book_id}.pdf",
                      "title": title, "status": "ready",
                      "current_generation": 1, "chunk_count": 1})
    mirror.replace_chunks(book_id, 1, [
        {"chunk_id": f"{book_id}-g1-00000", "chunk_index": 0,
         "section_path": "第一章", "content": content,
         "content_hash": "h-" + book_id, "anchor_hash": "a-" + book_id,
         "token_count": len(content), "page": 1}])


def test_bm25_mirror_search_and_book_filter(tmp_path):
    mirror = SQLiteMirrorStore(str(tmp_path / "mirror.sqlite3"))
    _mirror_book(mirror, "bk0000000001", "皮肤科", "红斑狼疮的诊断标准包括抗核抗体阳性")
    _mirror_book(mirror, "bk0000000002", "内科", "高血压患者应低盐饮食并监测血压")
    index = bm25_mod.BM25Index(mirror)

    hits = index.search("高血压 低盐饮食", 5)
    assert hits and hits[0]["book_id"] == "bk0000000002"
    assert hits[0]["bm25_score"] > 0 and hits[0]["vector_score"] is None
    assert hits[0]["book_title"] == "内科"

    assert index.search("高血压 低盐饮食", 5, book_ids={"bk0000000001"}) == []
    assert index.search("红斑狼疮", 5)[0]["book_id"] == "bk0000000001"
    assert mirror.health()["ok"] is True


# —— 检索编排 ——

class FakeVectors:
    def __init__(self, hits=None, error=None):
        self.hits = [dict(h) for h in (hits or [])]
        self.error = error
        self.calls = []

    def search(self, vector, limit=200, book_id=None):
        self.calls.append((vector, limit, book_id))
        if self.error:
            raise self.error
        return [dict(h) for h in self.hits][:limit]


def _hit(index, score, gen=1):
    return {"chunk_id": f"bk0000000001-g{gen}-0000{index}", "book_id": "bk0000000001",
            "generation": gen, "chunk_index": index, "section_path": "第二章 红斑狼疮",
            "section": "红斑狼疮", "page": 40 + index, "page_hint": None,
            "content": f"第{index}段正文：红斑狼疮的诊断标准", "content_hash": f"c{index}",
            "anchor_hash": f"anchor{index:02d}", "token_count": 20,
            "book_title": "皮肤病学", "vector_score": score}


def _setup(tmp_path, *, vectors=True, broken_mirror=False):
    root = tmp_path / "kb"
    root.mkdir(parents=True, exist_ok=True)
    mirror = SQLiteMirrorStore(str(root / "mirror.sqlite3"))
    store = InMemoryKBStore()
    book = store.create_book({
        "book_id": "bk0000000001", "filename": "皮肤科.pdf", "ext": "pdf",
        "sha256": "b" * 64, "size_bytes": 10, "status": "ready",
        "index_state": "ready", "current_generation": 1, "chunk_count": 2,
        "title": "皮肤病学", "meta": {"author": "主编", "version": "第9版"},
    })
    mirror.sync_book({"book_id": book["book_id"], "filename": "皮肤科.pdf",
                      "title": "皮肤病学", "status": "ready", "current_generation": 1,
                      "chunk_count": 2, "meta": {"author": "主编"}})
    mirror.replace_chunks(book["book_id"], 1, [
        {"chunk_id": "bk0000000001-g1-00000", "chunk_index": 0,
         "section_path": "第二章 红斑狼疮",
         "content": "红斑狼疮的诊断标准包括抗核抗体阳性与皮肤活检",
         "content_hash": "c0", "anchor_hash": "anchor00", "token_count": 24, "page": 40},
        {"chunk_id": "bk0000000001-g1-00001", "chunk_index": 1,
         "section_path": "第二章 红斑狼疮",
         "content": "系统性红斑狼疮需要长期随访和免疫指标监测",
         "content_hash": "c1", "anchor_hash": "anchor01", "token_count": 22, "page": 41},
    ])
    vec = None
    if vectors:
        vec = FakeVectors([_hit(i, s) for i, s in enumerate((0.91, 0.88, 0.80, 0.30))])
    mirror_arg = mirror
    if broken_mirror:
        mirror_arg = _BrokenMirror(mirror)
    return store, mirror_arg, vec


class _BrokenMirror:
    def __init__(self, inner):
        self.inner = inner

    def list_books(self, status="ready"):
        return self.inner.list_books(status)

    def chunks(self, *args, **kwargs):
        raise RuntimeError("mirror unavailable")

    def health(self):
        return {"ok": False, "detail": "broken"}


@pytest.fixture
def stub_models(monkeypatch):
    monkeypatch.setattr(search_mod.embed_mod, "embed_query",
                        lambda q: [0.0] * 767 + [1.0])
    monkeypatch.setattr(search_mod.embed_mod, "status", lambda: {"state": "ready"})
    monkeypatch.setattr(search_mod.rerank_mod, "score",
                        lambda q, texts: [0.9, 0.8, 0.7, 0.2][:len(texts)])
    monkeypatch.setattr(search_mod.rerank_mod, "status", lambda: {"state": "ready"})


def test_search_milvus_rerank_cliff(tmp_path, stub_models):
    store, mirror, vec = _setup(tmp_path)
    events = []
    kb = KBSearch(store=store, mirror=mirror, vectors=vec,
                  audit=lambda e, d="": events.append(e))
    out = kb.search("红斑狼疮怎么诊断？")
    assert out["retrieval_mode"] == "milvus_rerank"
    assert out["k_used"] == 20 and out["k_source"] == "default"
    assert out["n_used"] == 3 and len(out["citations"]) == 3
    assert out["rerank_state"] == "ready" and out["degraded_reason"] == ""
    first = out["citations"][0]
    assert first["citation_id"].startswith("RB-bk0000000001-")
    assert first["book_title"] == "皮肤病学" and first["page"] == 40
    assert first["rerank_score"] == 0.9 and first["retrieval_score"] == 0.9
    assert first["k_used"] == 20 and first["n_used"] == 3
    assert out["kb_snapshot"].startswith("local-books@1books@")
    assert out["total_indexed_books"] == 1


def test_search_rerank_missing_falls_back_to_vector_scores(tmp_path, monkeypatch):
    store, mirror, vec = _setup(tmp_path)
    monkeypatch.setattr(search_mod.embed_mod, "embed_query",
                        lambda q: [0.0] * 767 + [1.0])
    monkeypatch.setattr(search_mod.embed_mod, "status", lambda: {"state": "ready"})
    monkeypatch.setattr(search_mod.rerank_mod, "score", lambda q, texts: None)
    monkeypatch.setattr(search_mod.rerank_mod, "status",
                        lambda: {"state": "not_installed"})
    out = KBSearch(store=store, mirror=mirror, vectors=vec).search("红斑狼疮")
    assert out["retrieval_mode"] == "milvus"
    assert out["rerank_state"] == "not_installed"
    # 向量分 0.91/0.88/0.80/0.30：最大落差在 0.80 → 0.30，n=3
    assert out["n_used"] == 3
    assert out["citations"][0]["vector_score"] == 0.91
    assert out["citations"][0]["rerank_score"] is None


def test_search_milvus_failure_degrades_to_bm25(tmp_path, stub_models):
    store, mirror, vec = _setup(tmp_path)
    vec.error = RuntimeError("milvus down")
    events = []
    out = KBSearch(store=store, mirror=mirror, vectors=vec,
                   audit=lambda e, d="": events.append(e)).search("红斑狼疮诊断标准")
    assert out["retrieval_mode"] == "bm25" and out["rerank_state"] == "skipped"
    assert out["degraded_reason"] == "milvus_unavailable"
    assert out["citations"] and out["citations"][0]["book_id"] == "bk0000000001"
    assert "kb_search_degraded" in events


def test_search_embedding_unavailable_still_bm25(tmp_path, monkeypatch):
    store, mirror, vec = _setup(tmp_path)
    monkeypatch.setattr(search_mod.embed_mod, "embed_query", lambda q: None)
    monkeypatch.setattr(search_mod.embed_mod, "status",
                        lambda: {"state": "not_installed"})
    out = KBSearch(store=store, mirror=mirror, vectors=vec).search("红斑狼疮诊断标准")
    assert out["retrieval_mode"] == "bm25"
    assert out["degraded_reason"] == "embedding_unavailable"
    assert out["citations"]


def test_search_stale_generation_is_dropped(tmp_path, stub_models):
    store, mirror, _ = _setup(tmp_path)
    stale = FakeVectors([_hit(i, s, gen=0) for i, s in enumerate((0.9, 0.8))])
    out = KBSearch(store=store, mirror=mirror, vectors=stale).search("红斑狼疮诊断标准")
    assert out["retrieval_mode"] == "bm25"
    assert out["degraded_reason"] == "milvus_empty"


def test_search_mirror_failure_returns_empty_not_fake(tmp_path, stub_models):
    store, mirror, _ = _setup(tmp_path, vectors=False, broken_mirror=True)
    out = KBSearch(store=store, mirror=mirror, vectors=None).search("红斑狼疮")
    assert out["citations"] == []
    assert out["degraded_reason"] == "mirror_unavailable"


def test_search_empty_query_and_no_books(tmp_path, stub_models):
    store, mirror, vec = _setup(tmp_path)
    kb = KBSearch(store=store, mirror=mirror, vectors=vec)
    assert kb.search("   ")["degraded_reason"] == "empty_query"
    empty = KBSearch(store=InMemoryKBStore(), mirror=mirror, vectors=vec)
    out = empty.search("红斑狼疮")
    assert out["degraded_reason"] == "no_ready_books" and out["citations"] == []


def test_search_top_n_cap(tmp_path, stub_models):
    store, mirror, vec = _setup(tmp_path)
    out = KBSearch(store=store, mirror=mirror, vectors=vec).search("红斑狼疮", top_n=2)
    assert out["n_used"] == 2 and len(out["citations"]) == 2
