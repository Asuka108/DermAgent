# -*- coding: utf-8 -*-
"""KB 服务层：上传/入队/状态/隔离/恢复/硬删/检索/备份 + 组件装配与健康。

写入路径依赖 MySQL/Redis/Milvus；缺失时按组件如实降级：
- MySQL 不可用 → 目录读 SQLite 镜像，写操作返回 mysql_unavailable；
- Redis 不可用 → 入队返回 queue_unavailable；
- Milvus/embedding/reranker 缺失 → 检索侧降级（见 search.py），不伪造引用。
"""
import hashlib
import os
import re
import shutil
import uuid
from datetime import datetime, timezone

from . import config
from . import embed as embed_mod
from . import parse as parse_mod
from . import rerank as rerank_mod
from . import search as search_mod
from .index import IndexFailure, Indexer
from .queue import MemoryQueue, QueueUnavailable, RedisQueue
from .store import DuplicateBook, InMemoryKBStore, MySQLKBStore, SQLiteMirrorStore
from .vector import KBVectorStore
from .worker import KBWorker

_MAX_FILENAME = 200
_BOOK_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _err(reason: str, **extra) -> dict:
    return {"ok": False, "reason": reason, **extra}


def _mirror_ok(mirror) -> bool:
    return mirror is not None


class KBService:
    def __init__(self, store=None, mirror=None, vectors=None, queue=None,
                 audit=None, autostart_worker: bool = False):
        self.store = store
        self.mirror = mirror
        self.vectors = vectors
        self.queue = queue
        self._audit_cb = audit or (lambda event, detail="": None)
        self.searcher = search_mod.KBSearch(store=store, mirror=mirror, vectors=vectors,
                                            queue=queue, audit=self.audit)
        self.indexer = Indexer(store=store, mirror=mirror, vectors=vectors, audit=self.audit)
        self.worker = (KBWorker(queue=queue, indexer=self.indexer, store=store, audit=self.audit)
                       if queue is not None else None)
        self.autostart_worker = bool(autostart_worker)

    # —— 基础 ——
    def audit(self, event: str, detail) -> None:
        try:
            self._audit_cb(event, detail)
        except Exception:  # noqa: BLE001
            pass

    def start_worker(self) -> bool:
        if not (self.worker and self.autostart_worker):
            return False
        try:
            self.worker.start()
            return True
        except Exception as exc:  # noqa: BLE001 — 队列不可用不阻塞服务启动
            self.audit("kb_worker_start_failed", {"error": str(exc)[:160]})
            return False

    def stop(self) -> None:
        if self.worker is not None:
            try:
                self.worker.stop()
            except Exception:  # noqa: BLE001
                pass

    def _get_book(self, book_id: str):
        if not _BOOK_ID.match(str(book_id or "")):
            return None
        if self.store is not None:
            try:
                return self.store.get_book(book_id)
            except Exception:  # noqa: BLE001 — MySQL 不可用走镜像目录
                pass
        if self.mirror is not None:
            try:
                row = self.mirror.get_book(book_id)
                return self._mirror_book_out(row) if row else None
            except Exception:  # noqa: BLE001
                return None
        return None

    @staticmethod
    def _mirror_book_out(row: dict) -> dict:
        out = {k: row.get(k) for k in (
            "book_id", "filename", "title", "status", "current_generation",
            "chunk_count", "updated_at", "meta_json")}
        out["chunk_count"] = int(row.get("chunk_count") or 0)
        out["current_generation"] = int(row.get("current_generation") or 0)
        try:
            import json
            out["meta"] = json.loads(row.get("meta_json") or "{}")
        except (TypeError, ValueError):
            out["meta"] = {}
        out.setdefault("index_state", "ready" if out.get("status") == "ready" else "none")
        return out

    @staticmethod
    def _decorate(book: dict, progress: dict) -> dict:
        out = dict(book)
        out["chunks"] = int(out.get("chunk_count") or 0)
        out["size"] = int(out.get("size_bytes") or 0)
        out["error"] = out.get("error_code")
        out["sha256_short"] = str(out.get("sha256") or "")[:12]
        out["meta_incomplete"] = bool((out.get("meta") or {}).get("format_incomplete"))
        out["progress"] = progress or {}
        return out

    def _progress(self, book_id: str) -> dict:
        if self.queue is None:
            return {}
        try:
            return self.queue.progress(book_id)
        except Exception:  # noqa: BLE001
            return {}

    # —— 目录 ——
    def list_books(self, include_quarantined: bool = True) -> list:
        rows = []
        if self.store is not None:
            try:
                rows = self.store.list_books(include_quarantined=include_quarantined)
            except Exception:  # noqa: BLE001
                rows = []
        if not rows and self.mirror is not None:
            try:
                rows = [self._mirror_book_out(r) for r in self.mirror.list_books(status="")]
            except Exception:  # noqa: BLE001
                rows = []
        if not include_quarantined:
            rows = [b for b in rows if b.get("status") != "quarantined"]
        out = [self._decorate(b, self._progress(b.get("book_id") or "")) for b in rows]
        out.sort(key=lambda b: b.get("created_at") or b.get("updated_at") or "", reverse=True)
        return out

    def books_payload(self) -> dict:
        books = self.list_books(include_quarantined=True)
        spec = self.k_spec()
        return {
            "ok": True,
            "books": books,
            "embedding_state": embed_mod.health().get("state", "unknown"),
            "rerank_state": rerank_mod.health().get("state", "unknown"),
            "mineru_state": (parse_mod.available().get("state")
                             or ("ready" if parse_mod.available().get("ok")
                                 else "not_installed")),
            "k_spec_version": spec.get("artifact_version") or "",
            "kb_snapshot": search_mod._snapshot([b for b in books if b.get("status") == "ready"]),
        }

    def k_spec(self) -> dict:
        if self.store is not None:
            try:
                spec = self.store.active_k_spec()
                if spec:
                    return spec
            except Exception:  # noqa: BLE001
                pass
        if self.mirror is not None:
            try:
                spec = self.mirror.meta_get("k_spec")
                return spec if isinstance(spec, dict) else {}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    # —— 上传 / 索引 ——
    def upload(self, filename: str, data: bytes, meta_note: str = "") -> dict:
        if not config.enabled():
            return _err("kb_disabled")
        filename = os.path.basename(str(filename or ""))[:_MAX_FILENAME]
        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        if ext not in config.supported_formats():
            return _err(f"format_unsupported:{ext}" if ext else "format_unsupported")
        if len(data) > config.max_book_bytes():
            return _err("file_too_large", max_bytes=config.max_book_bytes())
        if self.store is None:
            return _err("mysql_unavailable")
        digest = _sha256(data)
        try:
            existing = self.store.find_by_dedup(digest)
        except Exception as exc:  # noqa: BLE001
            return _err("mysql_unavailable", detail=str(exc)[:160])
        if existing:
            return {"ok": False, "reason": "duplicate_sha256",
                    "duplicate_of": existing.get("book_id"),
                    "book_id": existing.get("book_id"), "status": existing.get("status"),
                    "sha256": digest}

        book_id = "bk" + uuid.uuid4().hex[:10]
        original = os.path.join(config.upload_dir(book_id), f"original.{ext}")
        try:
            os.makedirs(os.path.dirname(original), exist_ok=True)
            tmp = original + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, original)
        except OSError as exc:
            shutil.rmtree(config.upload_dir(book_id), ignore_errors=True)
            return _err("io_error", detail=str(exc)[:160])

        title = os.path.splitext(filename)[0][:200]
        try:
            book = self.store.create_book({
                "book_id": book_id, "filename": filename, "ext": ext,
                "sha256": digest, "dedup_key": digest, "size_bytes": len(data),
                "status": "uploaded", "index_state": "none",
                "title": title, "original_path": original,
                "meta": {"meta_note": str(meta_note or "")[:200],
                         "format_incomplete": True}})
        except DuplicateBook as exc:
            shutil.rmtree(config.upload_dir(book_id), ignore_errors=True)
            return {"ok": False, "reason": "duplicate_sha256",
                    "duplicate_of": exc.existing.get("book_id"),
                    "book_id": exc.existing.get("book_id"), "sha256": digest}
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(config.upload_dir(book_id), ignore_errors=True)
            return _err("mysql_unavailable", detail=str(exc)[:160])
        self.audit("kb_upload", {"book_id": book_id, "filename": filename[:80],
                                "size": len(data)})
        return {"ok": True, "book_id": book_id, "status": book.get("status") or "uploaded",
                "sha256": digest, "filename": filename, "size_bytes": len(data)}

    def index(self, book_id: str, reindex: bool = False, reuse_cleaned: bool = False) -> dict:
        book = self._get_book(book_id)
        if not book:
            return {"ok": False, "reason": "not_found"}
        if self.queue is None:
            return _err("queue_unavailable")
        if book.get("status") == "quarantined":
            return _err("quarantined")
        kind = "reindex" if reindex or book.get("status") == "ready" else "index"
        generation = int(book.get("current_generation") or 0) + 1
        try:
            job_id = self.queue.enqueue(book_id, kind=kind, generation=generation,
                                        reuse_cleaned=reuse_cleaned)
        except QueueUnavailable as exc:
            return _err("queue_unavailable", detail=str(exc)[:160])
        except Exception as exc:  # noqa: BLE001
            return _err("queue_unavailable", detail=str(exc)[:160])
        fields = {"index_state": "queued", "error_code": None, "error_detail": None}
        if kind != "reindex":
            fields["status"] = "queued"
        try:
            self.store.update_book(book_id, **fields)
        except Exception:  # noqa: BLE001 — 入队成功但状态回写失败，轮询 job 仍可追踪
            self.audit("kb_index_state_write_failed", {"book_id": book_id})
        self.audit("kb_index_queued", {"book_id": book_id, "job_id": job_id,
                                      "kind": kind, "generation": generation})
        return {"ok": True, "book_id": book_id, "job_id": job_id, "kind": kind,
                "status": "queued", "generation": generation}

    def job(self, job_id: str) -> dict:
        if self.queue is None:
            return _err("queue_unavailable")
        try:
            job = self.queue.get_job(job_id)
        except Exception as exc:  # noqa: BLE001
            return _err("queue_unavailable", detail=str(exc)[:160])
        if not job:
            return {"ok": False, "reason": "not_found"}
        return {"ok": True, **job}

    def status(self, book_id: str) -> dict:
        book = self._get_book(book_id)
        if not book:
            return {"ok": False, "reason": "not_found"}
        return {"ok": True, "book": self._decorate(book, self._progress(book_id)),
                "progress": self._progress(book_id)}

    # —— 生命周期 ——
    def quarantine(self, book_id: str) -> dict:
        book = self._get_book(book_id)
        if not book:
            return {"ok": False, "reason": "not_found"}
        try:
            row = self.indexer.quarantine(book)
        except IndexFailure as exc:
            return {"ok": False, "reason": exc.code, "detail": exc.detail,
                    "retryable": exc.retryable}
        if not row:
            return {"ok": False, "reason": "not_found"}
        return {"ok": True, "book_id": book_id, "status": "quarantined"}

    def restore(self, book_id: str) -> dict:
        book = self._get_book(book_id)
        if not book:
            return {"ok": False, "reason": "not_found"}
        if book.get("status") != "quarantined":
            return {"ok": False, "reason": "not_quarantined"}
        if self.queue is None:
            return _err("queue_unavailable")
        cleaned = book.get("cleaned_path")
        if not cleaned or not os.path.isfile(cleaned):
            return {"ok": False, "reason": "cleaned_missing",
                    "detail": "缺少清洗文本，无法免解析恢复"}
        try:
            job_id = self.queue.enqueue(book_id, kind="restore",
                                        generation=int(book.get("current_generation") or 0) + 1,
                                        reuse_cleaned=True)
        except Exception as exc:  # noqa: BLE001
            return _err("queue_unavailable", detail=str(exc)[:160])
        try:
            self.store.update_book(book_id, status="queued", index_state="queued",
                                   dedup_key=book.get("sha256"),
                                   deleted_at=None, error_code=None, error_detail=None)
        except Exception:  # noqa: BLE001
            return _err("mysql_unavailable")
        self.audit("kb_restore_queued", {"book_id": book_id, "job_id": job_id})
        return {"ok": True, "book_id": book_id, "job_id": job_id, "status": "queued"}

    def delete(self, book_id: str) -> dict:
        book = self._get_book(book_id)
        if not book:
            return {"ok": False, "reason": "not_found"}
        try:
            return self.indexer.delete(book)
        except IndexFailure as exc:
            return {"ok": False, "reason": exc.code, "detail": exc.detail,
                    "retryable": exc.retryable}

    # —— 检索 ——
    def search(self, query: str, top_n: int = None) -> dict:
        if not config.enabled():
            return {"ok": False, "reason": "kb_disabled", "citations": [],
                    "retrieval_mode": "bm25", "k_used": 0, "n_used": 0,
                    "k_source": "default", "rerank_state": "skipped",
                    "degraded_reason": "kb_disabled"}
        return self.searcher.search(query, top_n=top_n)

    # —— 备份（v2：书目 + 清洗文本，不含向量） ——
    def backup(self) -> dict:
        books = []
        for book in self.list_books(include_quarantined=True):
            text = ""
            path = book.get("cleaned_path")
            if path and os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                except OSError:
                    text = ""
            books.append({
                "book_id": book.get("book_id"), "filename": book.get("filename"),
                "ext": book.get("ext"), "sha256": book.get("sha256"),
                "size_bytes": book.get("size_bytes"), "status": book.get("status"),
                "current_generation": book.get("current_generation"),
                "title": book.get("title"), "author": book.get("author"),
                "publisher": book.get("publisher"), "version": book.get("version"),
                "meta": book.get("meta") or {}, "mineru": book.get("mineru") or {},
                "cleaned_text": text,
            })
        return {"ok": True, "version": 2, "exported_at": _now(),
                "kb_snapshot": search_mod._snapshot(
                    [b for b in books if b.get("status") == "ready"]),
                "note": "备份含书目与清洗文本，不含向量；恢复后需重新索引。",
                "books": books}

    def restore_backup(self, data: dict) -> dict:
        if not isinstance(data, dict) or int(data.get("version") or 0) != 2 \
                or not isinstance(data.get("books"), list):
            return {"ok": False, "reason": "invalid_backup"}
        if self.store is None:
            return _err("mysql_unavailable")
        if self.queue is None:
            return _err("queue_unavailable")
        restored, skipped, failed = [], [], []
        for item in data["books"]:
            if not isinstance(item, dict):
                continue
            sha = str(item.get("sha256") or "")
            try:
                existing = self.store.find_by_dedup(sha) if sha else None
            except Exception as exc:  # noqa: BLE001
                return _err("mysql_unavailable", detail=str(exc)[:160])
            if existing:
                skipped.append({"sha256": sha[:12], "book_id": existing.get("book_id")})
                continue
            book_id = "bk" + uuid.uuid4().hex[:10]
            cleaned = config.cleaned_file(book_id, 1)
            try:
                os.makedirs(os.path.dirname(cleaned), exist_ok=True)
                with open(cleaned + ".tmp", "w", encoding="utf-8") as fh:
                    fh.write(str(item.get("cleaned_text") or ""))
                os.replace(cleaned + ".tmp", cleaned)
                book = self.store.create_book({
                    "book_id": book_id, "filename": item.get("filename") or "",
                    "ext": item.get("ext") or "", "sha256": sha, "dedup_key": sha,
                    "size_bytes": int(item.get("size_bytes") or 0),
                    "status": "queued", "index_state": "queued",
                    "title": item.get("title"), "author": item.get("author"),
                    "publisher": item.get("publisher"), "version": item.get("version"),
                    "meta": item.get("meta") or {}, "mineru": item.get("mineru") or {},
                    "cleaned_path": cleaned})
                self.queue.enqueue(book_id, kind="index", generation=1, reuse_cleaned=True)
                restored.append(book["book_id"])
            except Exception as exc:  # noqa: BLE001
                failed.append({"sha256": sha[:12], "reason": str(exc)[:120]})
        self.audit("kb_backup_restore", {"restored": len(restored),
                                         "skipped": len(skipped), "failed": len(failed)})
        return {"ok": True, "restored": restored, "skipped": skipped,
                "failed": failed, "needs_reindex": True,
                "note": "已恢复书目与清洗文本并重新入队；向量需重新生成。"}

    # —— 健康 ——
    def health(self) -> dict:
        def probe(name, fn):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "detail": f"{exc.__class__.__name__}: {exc}"[:160]}

        components = {
            "mysql": probe("mysql", self.store.health) if self.store is not None
            else {"ok": False, "detail": "未配置 MySQL"},
            "mirror": probe("mirror", self.mirror.health) if self.mirror is not None
            else {"ok": False, "detail": "未配置 SQLite 镜像"},
            "milvus": probe("milvus", self.vectors.health) if self.vectors is not None
            else {"ok": False, "detail": "未配置 Milvus"},
            "redis": probe("redis", self.queue.health) if self.queue is not None
            else {"ok": False, "detail": "未配置 Redis"},
            "embedder": probe("embedder", embed_mod.health),
            "reranker": probe("reranker", rerank_mod.health),
            "mineru": parse_mod.available(),
        }
        components["mineru"].setdefault("state", "ready" if components["mineru"].get("ok")
                                        else "not_installed")
        worker = self.worker.health() if self.worker is not None else {"ok": False,
                                                                      "thread": False}
        return {"ok": bool(components["mirror"].get("ok") or components["mysql"].get("ok")),
                "components": components, "worker": worker}


# —— 装配 ——

def build_service(audit=None) -> KBService:
    backend = config.backend()
    store, mirror, vectors, queue = None, None, None, None
    try:
        mirror = SQLiteMirrorStore()
    except Exception:  # noqa: BLE001 — 镜像不可写不阻塞启动，检索会如实降级
        mirror = None
    if backend in ("fake", "memory"):
        store, queue = InMemoryKBStore(), MemoryQueue()
    elif backend == "sqlite":
        pass
    else:
        store, vectors, queue = MySQLKBStore(), KBVectorStore(), RedisQueue()
    return KBService(store=store, mirror=mirror, vectors=vectors, queue=queue,
                     audit=audit, autostart_worker=config.autostart_worker())
