# -*- coding: utf-8 -*-
"""索引编排：parse → clean → chunk → embed → upsert → 计数校验 → generation 切换。

reindex 期间旧 generation 保持可检索；新代写入并校验后才翻转 current_generation，
最后清理旧代。任何阶段失败抛 IndexFailure，由 worker 决定重试或落 failed。
"""
import json
import os
import re
import shutil

from . import chunk as chunk_mod
from . import clean as clean_mod
from . import config
from . import embed as embed_mod
from . import parse as parse_mod

_BOOK_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_RETRYABLE = {"milvus_unavailable", "model_unavailable", "mirror_unavailable",
              "mysql_unavailable", "redis_unavailable", "mineru_service_unavailable",
              "mineru_timeout"}
_BATCH = 32


class IndexFailure(Exception):
    def __init__(self, code: str, detail: str = "", retryable: bool | None = None):
        super().__init__(code)
        self.code = str(code)[:48]
        self.detail = str(detail or "")[:300]
        self.retryable = (self.code in _RETRYABLE) if retryable is None else bool(retryable)


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _safe_rmtree(path: str) -> None:
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def _write_images(md_dir: str, images: dict) -> None:
    base = os.path.abspath(md_dir)
    for rel, blob in (images or {}).items():
        target = os.path.abspath(os.path.join(base, str(rel)))
        if not target.startswith(base + os.sep) or not isinstance(blob, bytes):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(blob)


class Indexer:
    def __init__(self, store, mirror=None, vectors=None, audit=None,
                 parser=None, cleaner=None, chunker=None, embedder=None):
        self.store = store
        self.mirror = mirror
        self.vectors = vectors
        self._audit_cb = audit or (lambda event, detail="": None)
        self._parser = parser or (lambda path: parse_mod.parse_document(path))
        self._cleaner = cleaner or clean_mod.clean_markdown
        self._chunker = chunker or chunk_mod.chunk_markdown
        self._embedder = embedder or embed_mod.embed_documents

    def audit(self, event: str, detail) -> None:
        try:
            self._audit_cb(event, detail)
        except Exception:  # noqa: BLE001
            pass

    def _chunk_options(self) -> dict:
        """任务执行时读取 RAG 偏好：切分方式 / overlap 对之后的导入与重建生效。"""
        try:
            import rag as rag_mod
        except ImportError:  # 以 server 包方式导入
            from server import rag as rag_mod
        prefs = rag_mod.get_prefs()
        if prefs.get("chunk_mode_error"):
            raise IndexFailure("bad_chunk_mode", prefs["chunk_mode_error"], retryable=False)
        if prefs.get("chunk_overlap_error"):
            raise IndexFailure("bad_chunk_overlap", prefs["chunk_overlap_error"], retryable=False)
        limits = prefs.get("chunk_limits") or {}
        return {
            "mode": prefs.get("chunk_mode") or "section",
            "target": int(limits.get("target") or config.chunk_target()),
            "overlap": int(prefs.get("chunk_overlap") or 0),
            "max_chars": int(limits.get("max_chars") or config.chunk_max()),
        }

    # —— 单本书索引 ——
    def run(self, book: dict, reuse_cleaned: bool = False, reindex: bool = False,
            progress=None) -> dict:
        book_id = str(book.get("book_id") or "")
        if not _BOOK_ID.match(book_id):
            raise IndexFailure("bad_book_id", book_id, retryable=False)
        generation = int(book.get("current_generation") or 0) + 1

        def tick(phase: str, pct: int, message: str = ""):
            if progress:
                try:
                    progress(phase, pct, message)
                except Exception:  # noqa: BLE001
                    pass
            if phase == "ready":
                fields = {"status": "ready", "index_state": "ready"}
            elif reindex:
                fields = {"index_state": "reindexing"}
            else:
                fields = {"status": phase, "index_state": "running"}
            try:
                self.store.update_book(book_id, **fields)
            except Exception as exc:  # noqa: BLE001
                raise IndexFailure("mysql_unavailable", str(exc), retryable=True) from exc

        clean_log = {}
        mineru_meta = dict(book.get("mineru") or {})
        raw_paths = {"raw_md": book.get("raw_md_path"), "raw_json": book.get("raw_json_path")}
        if reuse_cleaned:
            cleaned_path = (book.get("cleaned_path")
                            or config.cleaned_file(book_id, max(1, generation - 1)))
            if not os.path.isfile(cleaned_path):
                raise IndexFailure("cleaned_missing", cleaned_path, retryable=False)
            with open(cleaned_path, encoding="utf-8") as fh:
                cleaned_text = fh.read()
            tick("chunking", 40, "复用已清洗文本")
        else:
            original = book.get("original_path") or ""
            if not original or not os.path.isfile(original):
                raise IndexFailure("corrupt_file", f"原文件缺失: {original}", retryable=False)
            tick("parsing", 10, "MinerU 解析")
            try:
                parsed = self._parser(original)
            except parse_mod.ParseError as exc:
                raise IndexFailure(exc.code, exc.detail,
                                   retryable=exc.code in _RETRYABLE) from exc
            md_dir = config.mineru_gen_dir(book_id, generation)
            raw_md = os.path.join(md_dir, "raw.md")
            raw_json = os.path.join(md_dir, "raw.json")
            try:
                _atomic_write(raw_md, parsed.get("markdown") or "")
                _atomic_write(raw_json, json.dumps(parsed.get("raw_json") or {},
                                                   ensure_ascii=False, default=str))
                _write_images(md_dir, parsed.get("images") or {})
            except OSError as exc:
                raise IndexFailure("io_error", str(exc), retryable=True) from exc
            raw_paths = {"raw_md": raw_md, "raw_json": raw_json}
            mineru_meta = {"engine": parsed.get("engine") or "mineru",
                           "version": parsed.get("version") or "",
                           "tier": parsed.get("tier") or ""}

            tick("cleaning", 25, "标准清洗")
            cleaned = self._cleaner(parsed.get("markdown") or "")
            cleaned_text = cleaned.get("text") or ""
            clean_log = cleaned.get("log") or {}
            if not cleaned_text.strip():
                raise IndexFailure("empty_text", "清洗后无正文", retryable=False)
            cleaned_path = config.cleaned_file(book_id, generation)
            try:
                _atomic_write(cleaned_path, cleaned_text)
            except OSError as exc:
                raise IndexFailure("io_error", str(exc), retryable=True) from exc

        tick("chunking", 35, "段落切分")
        title = str(book.get("title") or os.path.splitext(book.get("filename") or "")[0])[:200]
        chunk_opts = self._chunk_options()
        chunks = self._chunker(cleaned_text, book_id, title=title, generation=generation,
                               mode=chunk_opts["mode"], target=chunk_opts["target"],
                               overlap=chunk_opts["overlap"], max_chars=chunk_opts["max_chars"])
        if not chunks:
            raise IndexFailure("empty_text", "切分后无 chunk", retryable=False)
        for row in chunks:
            row["generation"] = generation
        total = len(chunks)
        chunk_meta = {
            "chunk_mode": chunk_opts["mode"],
            "chunk_target": chunk_mod.effective_target(
                chunk_opts["mode"], chunk_opts["target"], chunk_opts["max_chars"]),
            "chunk_overlap": chunk_opts["overlap"],
            "chunk_max": chunk_opts["max_chars"],
            "chunk_count": total,
        }

        if self.vectors is None:
            raise IndexFailure("milvus_unavailable", "未配置向量库", retryable=True)
        tick("embedding", 40, f"向量化 0/{total}")
        for start in range(0, total, _BATCH):
            part = chunks[start:start + _BATCH]
            vecs = self._embedder([c["content"] for c in part])
            if vecs is None:
                raise IndexFailure("model_unavailable",
                                   f"embedding 不可用（{config.embed_model()}）",
                                   retryable=True)
            if len(vecs) != len(part) or any(len(v) != embed_mod.DIM for v in vecs):
                raise IndexFailure("model_unavailable", "向量数量或维度不匹配", retryable=True)
            try:
                self.vectors.upsert(part, vecs)
            except Exception as exc:  # noqa: BLE001
                raise IndexFailure("milvus_unavailable", str(exc), retryable=True) from exc
            done = start + len(part)
            tick("indexing", 40 + int(50 * done / total), f"写入向量 {done}/{total}")

        try:
            written = self.vectors.count_generation(book_id, generation)
        except Exception as exc:  # noqa: BLE001
            raise IndexFailure("milvus_unavailable", f"计数校验失败: {exc}",
                               retryable=True) from exc
        if written < total:
            raise IndexFailure("milvus_count_mismatch",
                               f"期望 {total}，实际 {written}", retryable=True)

        if self.mirror is not None:
            try:
                self.mirror.sync_book({**book, "status": "ready",
                                       "current_generation": generation,
                                       "chunk_count": total, "title": title,
                                       "meta": {**(book.get("meta") or {}),
                                                **chunk_meta, "clean_log": clean_log}})
                self.mirror.replace_chunks(book_id, generation, chunks)
            except Exception as exc:  # noqa: BLE001
                raise IndexFailure("mirror_unavailable", str(exc), retryable=True) from exc

        meta = {**(book.get("meta") or {}), **chunk_meta, "clean_log": clean_log}
        try:
            self.store.update_book(
                book_id, status="ready", index_state="ready",
                current_generation=generation, chunk_count=total, retry_count=0,
                error_code=None, error_detail=None, title=title,
                cleaned_path=cleaned_path, raw_md_path=raw_paths["raw_md"],
                raw_json_path=raw_paths["raw_json"], meta=meta,
                mineru={**mineru_meta, "generation": generation},
                ocr_quality=book.get("ocr_quality") or {})
        except Exception as exc:  # noqa: BLE001
            raise IndexFailure("mysql_unavailable", str(exc), retryable=True) from exc

        try:
            for old in self.vectors.generations(book_id):
                if old != generation:
                    self.vectors.delete_generation(book_id, old)
        except Exception as exc:  # noqa: BLE001 — 旧代残留由检索按最大代过滤
            self.audit("kb_generation_cleanup_failed",
                       {"book_id": book_id, "generation": generation, "error": str(exc)[:160]})

        tick("ready", 100, "索引完成")
        self.audit("kb_index_done", {"book_id": book_id, "generation": generation,
                                     "chunks": total})
        return {"ok": True, "book_id": book_id, "generation": generation,
                "chunk_count": total, "chunks": len(chunks)}

    # —— 隔离 / 硬删 ——
    def quarantine(self, book: dict) -> dict:
        book_id = str(book.get("book_id") or "")
        if not _BOOK_ID.match(book_id):
            raise IndexFailure("bad_book_id", book_id, retryable=False)
        if self.vectors is not None:
            try:
                self.vectors.delete_book(book_id)
            except Exception as exc:  # noqa: BLE001 — 检索侧按 ready 书目过滤
                self.audit("kb_quarantine_vectors_failed",
                           {"book_id": book_id, "error": str(exc)[:160]})
        if self.mirror is not None:
            try:
                self.mirror.remove_book(book_id)
            except Exception as exc:  # noqa: BLE001
                self.audit("kb_quarantine_mirror_failed",
                           {"book_id": book_id, "error": str(exc)[:160]})
        row = self.store.quarantine_book(book_id)
        self.audit("kb_quarantine", {"book_id": book_id})
        return row

    def delete(self, book: dict) -> dict:
        book_id = str(book.get("book_id") or "")
        if not _BOOK_ID.match(book_id):
            raise IndexFailure("bad_book_id", book_id, retryable=False)
        if self.vectors is not None:
            try:
                self.vectors.delete_book(book_id)
            except Exception as exc:  # noqa: BLE001
                raise IndexFailure("milvus_unavailable", str(exc), retryable=True) from exc
        if self.mirror is not None:
            try:
                self.mirror.remove_book(book_id)
            except Exception as exc:  # noqa: BLE001
                raise IndexFailure("mirror_unavailable", str(exc), retryable=True) from exc
        _safe_rmtree(config.upload_dir(book_id))
        _safe_rmtree(os.path.join(config.mineru_dir(), book_id))
        _safe_rmtree(os.path.join(config.cleaned_dir(), book_id))
        if not self.store.hard_delete_book(book_id):
            raise IndexFailure("book_not_found", book_id, retryable=False)
        self.audit("kb_hard_delete", {"book_id": book_id})
        return {"ok": True, "book_id": book_id}
