# -*- coding: utf-8 -*-
"""KB 存储层单测：书目去重/隔离、k 规格切换、SQLite 镜像 generation 替换。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server.kb.store import (  # noqa: E402
    DuplicateBook, InMemoryKBStore, SQLiteMirrorStore, book_out, new_book,
    pack_centroid, unpack_centroid,
)


def _book(**extra):
    base = {"filename": "皮肤病学.pdf", "ext": "pdf", "sha256": "a" * 64,
            "size_bytes": 1024, "original_path": "/tmp/a.pdf"}
    base.update(extra)
    return base


def test_new_book_defaults_and_truncation():
    row = new_book(_book(filename="x" * 300, ext=".PDF", title="皮肤科学"))
    assert row["book_id"].startswith("bk") and len(row["book_id"]) == 12
    assert len(row["filename"]) == 255
    assert row["ext"] == "pdf"
    assert row["dedup_key"] == "a" * 64
    assert row["status"] == "uploaded" and row["index_state"] == "none"
    assert row["current_generation"] == 0 and row["chunk_count"] == 0
    assert row["meta"] == {} and row["mineru"] == {}


def test_new_book_requires_sha256():
    with pytest.raises(ValueError):
        new_book({"filename": "x.txt"})


def test_inmemory_duplicate_and_quarantine_reuse():
    store = InMemoryKBStore()
    first = store.create_book(_book())
    with pytest.raises(DuplicateBook) as exc:
        store.create_book(_book())
    assert exc.value.existing["book_id"] == first["book_id"]

    out = store.quarantine_book(first["book_id"])
    assert out["status"] == "quarantined"
    assert out["dedup_key"] == "a" * 64 + "#" + first["book_id"]
    assert store.list_books() == []
    assert len(store.list_books(include_quarantined=True)) == 1

    again = store.create_book(_book())
    assert again["book_id"] != first["book_id"]


def test_inmemory_update_and_hard_delete():
    store = InMemoryKBStore()
    row = store.create_book(_book())
    out = store.update_book(row["book_id"], status="ready", current_generation=2,
                            chunk_count=17, meta={"clean_log": {"dropped": 3}},
                            mineru={"version": "4.1"}, title="临床皮肤病学")
    assert out["status"] == "ready" and out["chunk_count"] == 17
    assert out["meta"]["clean_log"]["dropped"] == 3
    assert out["mineru"]["version"] == "4.1"
    assert store.update_book("bkmissing", status="ready") is None
    with pytest.raises(ValueError):
        store.update_book(row["book_id"], nope=1)

    assert store.hard_delete_book(row["book_id"]) is True
    assert store.get_book(row["book_id"]) is None
    assert store.hard_delete_book(row["book_id"]) is False


def test_inmemory_k_spec_switch_and_centroids():
    store = InMemoryKBStore()
    assert store.active_k_spec() is None
    first = store.save_k_spec(
        {"artifact_version": "k-1", "global_k": 20, "labels_present": False,
         "eval": {"note": "unlabeled"}},
        [{"cluster_id": 0, "centroid": [0.1, 0.2], "k_star": 12, "sample_count": 5}])
    assert first["active"] is True and first["global_k"] == 20
    assert first["clusters"][0]["k_star"] == 12

    second = store.save_k_spec({"artifact_version": "k-2", "global_k": 30,
                                "labels_present": True}, [])
    assert store.active_k_spec()["artifact_version"] == "k-2"
    assert store.get_k_spec("k-1")["active"] is False
    assert second["clusters"] == []

    with pytest.raises(ValueError):
        store.save_k_spec({"global_k": 1}, [])


def test_centroid_pack_roundtrip():
    vec = [0.5, -1.25, 3.0]
    packed = pack_centroid(vec)
    assert len(packed) == 12
    assert unpack_centroid(packed) == vec


def test_book_out_iso_timestamps():
    out = book_out({"book_id": "bk1", "status": "ready", "size_bytes": "9",
                    "created_at": "2026-10-30 04:00:00.123", "updated_at": None})
    assert out["size_bytes"] == 9
    assert out["created_at"] == "2026-10-30 04:00:00.123"
    assert out["updated_at"] == ""


def _chunk(idx, generation, content="内容"):
    return {"chunk_id": f"c{idx}-g{generation}", "chunk_index": idx, "section": "第一章",
            "section_path": "第一章", "page": 1, "page_hint": 1, "content": content,
            "content_hash": f"h{idx}", "token_count": 10}


def test_sqlite_mirror_generation_swap(tmp_path):
    mirror = SQLiteMirrorStore(str(tmp_path / "mirror.sqlite3"))
    book = book_out(new_book(_book()))
    mirror.sync_book({**book, "status": "ready", "title": "测试书"})
    assert mirror.replace_chunks(book["book_id"], 1, [_chunk(0, 1), _chunk(1, 1)]) == 2
    assert mirror.chunk_count() == 2

    mirror.replace_chunks(book["book_id"], 2, [_chunk(0, 2, "新内容")])
    current = mirror.chunks(book["book_id"])
    assert [c["content"] for c in current] == ["新内容"]
    assert current[0]["is_current"] == 1

    mirror.mark_book(book["book_id"], "ready", generation=2, chunk_count=1)
    assert mirror.get_book(book["book_id"])["current_generation"] == 2
    assert mirror.list_books()[0]["chunk_count"] == 1

    mirror.remove_book(book["book_id"])
    assert mirror.chunks(book["book_id"]) == []
    assert mirror.list_books() == []


def test_sqlite_mirror_meta_and_health(tmp_path):
    mirror = SQLiteMirrorStore(str(tmp_path / "mirror.sqlite3"))
    assert mirror.meta_get("k_spec") is None
    mirror.meta_set("k_spec", {"artifact_version": "k-1", "global_k": 20})
    assert mirror.meta_get("k_spec")["global_k"] == 20
    assert mirror.health()["ok"] is True


def test_sqlite_mirror_read_only_after_write(tmp_path):
    path = str(tmp_path / "mirror.sqlite3")
    mirror = SQLiteMirrorStore(path)
    book = book_out(new_book(_book()))
    mirror.sync_book({**book, "status": "ready"})
    mirror.replace_chunks(book["book_id"], 1, [_chunk(0, 1)])
    ro = SQLiteMirrorStore(path)
    with pytest.raises(Exception):
        conn = ro._ro_connect()
        try:
            conn.execute("DELETE FROM mirror_chunks")
            conn.commit()
        finally:
            conn.close()
