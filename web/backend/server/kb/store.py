# -*- coding: utf-8 -*-
"""KB 存储层：书目/任务规格（MySQL）+ BM25 只读镜像（SQLite）+ 单测假实现。

职责边界：
- 书目与 k 规格走 MySQL（kb_books / kb_k_spec / kb_query_clusters）；
- 任务状态不放这里（Redis，见 kb/queue.py）；
- chunk 正文与向量不放这里（Milvus，见 kb/vector.py）；
- SQLite 镜像仅供 BM25 降级与 mock 模式只读检索，worker 事务写入。
"""
import array
import json
import os
import pathlib
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from . import config

BOOK_STATUSES = ("uploaded", "queued", "parsing", "cleaning", "chunking",
                 "embedding", "indexing", "ready", "failed", "quarantined")
INDEX_STATES = ("none", "queued", "running", "reindexing", "ready", "failed")

_TEXT_FIELDS = ("filename", "ext", "sha256", "dedup_key", "status", "index_state",
                "error_code", "error_detail", "title", "author", "publisher",
                "version", "original_path", "raw_md_path", "raw_json_path",
                "cleaned_path", "deleted_at")
_INT_FIELDS = ("size_bytes", "current_generation", "chunk_count", "retry_count")
_JSON_FIELDS = {"meta": "meta_json", "mineru": "mineru_json", "ocr_quality": "ocr_quality_json"}
_BOOK_OUT_FIELDS = ("book_id",) + _TEXT_FIELDS + _INT_FIELDS + (
    "meta", "mineru", "ocr_quality", "created_at", "updated_at", "deleted_at")


class KBStoreError(Exception):
    pass


class BookNotFound(KBStoreError):
    pass


class DuplicateBook(KBStoreError):
    def __init__(self, existing: dict):
        super().__init__("duplicate book")
        self.existing = existing


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _dt_str(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _json_load(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_dump(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def book_out(row: dict) -> dict:
    """把存储行归一化成 API/服务层统一格式（JSON 列展开为 dict，时间转 ISO）。"""
    out = {}
    for key in _BOOK_OUT_FIELDS:
        if key == "meta":
            out[key] = _json_load(row.get("meta_json"), {})
        elif key == "mineru":
            out[key] = _json_load(row.get("mineru_json"), {})
        elif key == "ocr_quality":
            out[key] = _json_load(row.get("ocr_quality_json"), {})
        elif key in _INT_FIELDS:
            out[key] = int(row.get(key) or 0)
        elif key in ("created_at", "updated_at", "deleted_at"):
            out[key] = _dt_str(row.get(key))
        else:
            out[key] = row.get(key)
    return out


def new_book(book: dict) -> dict:
    """补默认值并生成 book_id；调用方负责 sha256/dedup_key 等必需字段。"""
    now = _now()
    row = {
        "book_id": str(book.get("book_id") or ("bk" + uuid.uuid4().hex[:10]))[:16],
        "filename": str(book.get("filename") or "")[:255],
        "ext": str(book.get("ext") or "").lower().lstrip(".")[:10],
        "sha256": str(book.get("sha256") or "")[:64],
        "dedup_key": str(book.get("dedup_key") or book.get("sha256") or "")[:96],
        "size_bytes": int(book.get("size_bytes") or 0),
        "status": str(book.get("status") or "uploaded")[:24],
        "index_state": str(book.get("index_state") or "none")[:24],
        "current_generation": int(book.get("current_generation") or 0),
        "chunk_count": int(book.get("chunk_count") or 0),
        "retry_count": int(book.get("retry_count") or 0),
        "error_code": book.get("error_code"),
        "error_detail": book.get("error_detail"),
        "title": book.get("title"),
        "author": book.get("author"),
        "publisher": book.get("publisher"),
        "version": book.get("version"),
        "meta": book.get("meta") or {},
        "mineru": book.get("mineru") or {},
        "ocr_quality": book.get("ocr_quality") or {},
        "original_path": book.get("original_path"),
        "raw_md_path": book.get("raw_md_path"),
        "raw_json_path": book.get("raw_json_path"),
        "cleaned_path": book.get("cleaned_path"),
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }
    if not row["sha256"]:
        raise ValueError("sha256 required")
    return row


_TEXT_LIMITS = {
    "filename": 255, "ext": 10, "sha256": 64, "dedup_key": 96, "status": 24,
    "index_state": 24, "error_code": 48, "error_detail": 500, "title": 200,
    "author": 100, "publisher": 100, "version": 50,
    "original_path": 512, "raw_md_path": 512, "raw_json_path": 512, "cleaned_path": 512,
    "deleted_at": 32,
}


def _update_pairs(fields: dict) -> tuple:
    cols, vals = [], []
    for key, value in (fields or {}).items():
        if key in _JSON_FIELDS:
            cols.append(_JSON_FIELDS[key])
            vals.append(_json_dump(value))
        elif key in _INT_FIELDS:
            cols.append(key)
            vals.append(int(value or 0))
        elif key in _TEXT_FIELDS:
            cols.append(key)
            vals.append(None if value is None else str(value)[:_TEXT_LIMITS[key]])
        else:
            raise ValueError(f"unknown book field: {key}")
    return cols, vals


def pack_centroid(vec) -> bytes:
    arr = array.array("f", [float(x) for x in vec])
    if sys.byteorder != "little":
        arr.byteswap()
    return arr.tobytes()


def unpack_centroid(blob) -> list:
    arr = array.array("f")
    arr.frombytes(bytes(blob or b""))
    if sys.byteorder != "little":
        arr.byteswap()
    return list(arr)


def kspec_out(row: dict, clusters=None) -> dict:
    out = {
        "artifact_version": row.get("artifact_version"),
        "global_k": int(row.get("global_k") or 0),
        "labels_present": bool(int(row.get("labels_present") or 0)),
        "active": bool(int(row.get("active") or 0)),
        "eval": _json_load(row.get("eval_json"), {}),
        "created_at": _dt_str(row.get("created_at")),
    }
    if clusters is not None:
        out["clusters"] = [{
            "cluster_id": int(c.get("cluster_id") or 0),
            "centroid": (c.get("centroid") if isinstance(c.get("centroid"), list)
                         else unpack_centroid(c.get("centroid"))),
            "k_star": int(c.get("k_star") or 0),
            "sample_count": int(c.get("sample_count") or 0),
        } for c in clusters]
    return out


class KBStore:
    """协议：所有方法失败抛异常，由调用方决定降级/审计。"""

    def health(self) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def create_book(self, book: dict) -> dict:
        raise NotImplementedError

    def get_book(self, book_id: str) -> dict | None:
        raise NotImplementedError

    def find_by_dedup(self, dedup_key: str) -> dict | None:
        raise NotImplementedError

    def list_books(self, include_quarantined: bool = False) -> list:
        raise NotImplementedError

    def update_book(self, book_id: str, **fields) -> dict | None:
        raise NotImplementedError

    def quarantine_book(self, book_id: str) -> dict | None:
        raise NotImplementedError

    def hard_delete_book(self, book_id: str) -> bool:
        raise NotImplementedError

    def active_k_spec(self) -> dict | None:
        raise NotImplementedError

    def get_k_spec(self, version: str) -> dict | None:
        raise NotImplementedError

    def save_k_spec(self, spec: dict, clusters: list) -> dict:
        raise NotImplementedError


class InMemoryKBStore(KBStore):
    """单测/离线用：与 MySQL 实现共享 book_out/new_book 语义。"""

    def __init__(self):
        self.books: dict[str, dict] = {}
        self.specs: dict[str, dict] = {}
        self.clusters: dict[str, list] = {}
        self._lock = threading.Lock()

    def health(self):
        return {"ok": True, "latency_ms": 0, "detail": "memory"}

    @staticmethod
    def _raw(book: dict) -> dict:
        row = dict(book)
        row["meta_json"] = row.pop("meta", {})
        row["mineru_json"] = row.pop("mineru", {})
        row["ocr_quality_json"] = row.pop("ocr_quality", {})
        return row

    def create_book(self, book):
        row = new_book(book)
        with self._lock:
            if any(b.get("dedup_key") == row["dedup_key"] for b in self.books.values()):
                existing = next(b for b in self.books.values() if b.get("dedup_key") == row["dedup_key"])
                raise DuplicateBook(book_out(existing))
            self.books[row["book_id"]] = self._raw(row)
        return book_out(self.books[row["book_id"]])

    def get_book(self, book_id):
        row = self.books.get(book_id)
        return book_out(row) if row else None

    def find_by_dedup(self, dedup_key):
        row = next((b for b in self.books.values() if b.get("dedup_key") == dedup_key), None)
        return book_out(row) if row else None

    def list_books(self, include_quarantined=False):
        rows = [b for b in self.books.values()
                if include_quarantined or b.get("status") != "quarantined"]
        rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return [book_out(r) for r in rows]

    def update_book(self, book_id, **fields):
        row = self.books.get(book_id)
        if not row:
            return None
        cols, vals = _update_pairs(fields)
        for col, val in zip(cols, vals):
            if col in _JSON_FIELDS.values():
                row[col] = _json_load(val, {})
            else:
                row[col] = val
        row["updated_at"] = _now()
        return book_out(row)

    def quarantine_book(self, book_id):
        row = self.books.get(book_id)
        if not row:
            return None
        now = _now()
        row.update(status="quarantined", index_state="none",
                   dedup_key=f"{row.get('sha256')}#{book_id}",
                   deleted_at=now, updated_at=now)
        return book_out(row)

    def hard_delete_book(self, book_id):
        return self.books.pop(book_id, None) is not None

    def active_k_spec(self):
        for version, row in self.specs.items():
            if int(row.get("active") or 0):
                return kspec_out(row, self.clusters.get(version))
        return None

    def get_k_spec(self, version):
        row = self.specs.get(version)
        return kspec_out(row, self.clusters.get(version)) if row else None

    def save_k_spec(self, spec, clusters):
        version = str(spec.get("artifact_version") or "").strip()
        if not version:
            raise ValueError("artifact_version required")
        with self._lock:
            for row in self.specs.values():
                row["active"] = 0
            row = {
                "artifact_version": version,
                "global_k": int(spec.get("global_k") or 0),
                "labels_present": int(bool(spec.get("labels_present"))),
                "active": 1,
                "eval_json": spec.get("eval") or spec.get("eval_json") or {},
                "created_at": spec.get("created_at") or _now(),
            }
            self.specs[version] = row
            self.clusters[version] = [
                {"cluster_id": int(c.get("cluster_id") or 0),
                 "centroid": list(c.get("centroid") or []),
                 "k_star": int(c.get("k_star") or 0),
                 "sample_count": int(c.get("sample_count") or 0)}
                for c in (clusters or [])]
        return kspec_out(row, self.clusters[version])


class MySQLKBStore(KBStore):
    """MySQL 实现：库名沿用 DERM_MYSQL_DB（默认 dermagent）。

    schema 采用懒初始化：MySQL 未安装/未启动时不阻塞服务启动，由 health()
    与调用方降级到 SQLite 镜像。
    """

    def __init__(self, settings: dict | None = None):
        self.settings = dict(settings or config.mysql_settings())
        self.db = self.settings.get("database") or "dermagent"
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def _connect(self, with_db: bool = True):
        import pymysql
        from pymysql.cursors import DictCursor
        kw = {k: v for k, v in self.settings.items() if v not in ("", None)}
        kw.pop("database", None)
        return pymysql.connect(
            database=self.db if with_db else None,
            charset="utf8mb4", autocommit=False, cursorclass=DictCursor,
            connect_timeout=2, read_timeout=5, write_timeout=5,
            init_command="SET time_zone='+00:00'",
            **kw)

    def _ensure_schema(self):
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with open(config.schema_file(), encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if not ln.strip().startswith("--")]
            ddl = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
            conn = self._connect(with_db=False)
            try:
                with conn.cursor() as cur:
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.db}` "
                                "DEFAULT CHARACTER SET utf8mb4")
                    cur.execute(f"USE `{self.db}`")
                    for stmt in ddl:
                        cur.execute(stmt)
                conn.commit()
            finally:
                conn.close()
            self._schema_ready = True

    def _conn(self):
        self._ensure_schema()
        return self._connect()

    def health(self):
        t0 = time.perf_counter()
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                conn.close()
            return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": "mysql"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": str(exc)[:160]}

    # —— 书目 ——
    def _row(self, cur, book_id):
        cur.execute("SELECT * FROM kb_books WHERE book_id=%s", (book_id,))
        return cur.fetchone()

    def create_book(self, book):
        row = new_book(book)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kb_books (book_id, filename, ext, sha256, dedup_key, size_bytes,"
                    " status, index_state, current_generation, chunk_count, retry_count,"
                    " error_code, error_detail, title, author, publisher, version,"
                    " meta_json, mineru_json, ocr_quality_json, original_path, raw_md_path,"
                    " raw_json_path, cleaned_path, created_at, updated_at)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                    " %s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (row["book_id"], row["filename"], row["ext"], row["sha256"], row["dedup_key"],
                     row["size_bytes"], row["status"], row["index_state"],
                     row["current_generation"], row["chunk_count"], row["retry_count"],
                     row["error_code"], row["error_detail"], row["title"], row["author"],
                     row["publisher"], row["version"], _json_dump(row["meta"]),
                     _json_dump(row["mineru"]), _json_dump(row["ocr_quality"]),
                     row["original_path"], row["raw_md_path"], row["raw_json_path"],
                     row["cleaned_path"], row["created_at"], row["updated_at"]))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            if exc.__class__.__name__ == "IntegrityError":
                existing = self.find_by_dedup(row["dedup_key"])
                if existing:
                    raise DuplicateBook(existing) from exc
            raise
        finally:
            conn.close()
        return self.get_book(row["book_id"])

    def get_book(self, book_id):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                row = self._row(cur, book_id)
            return book_out(row) if row else None
        finally:
            conn.close()

    def find_by_dedup(self, dedup_key):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM kb_books WHERE dedup_key=%s LIMIT 1", (dedup_key,))
                row = cur.fetchone()
            return book_out(row) if row else None
        finally:
            conn.close()

    def list_books(self, include_quarantined=False):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                if include_quarantined:
                    cur.execute("SELECT * FROM kb_books ORDER BY updated_at DESC LIMIT 500")
                else:
                    cur.execute("SELECT * FROM kb_books WHERE status<>'quarantined'"
                                " ORDER BY updated_at DESC LIMIT 500")
                rows = cur.fetchall()
            return [book_out(r) for r in rows]
        finally:
            conn.close()

    def update_book(self, book_id, **fields):
        cols, vals = _update_pairs(fields)
        if not cols:
            return self.get_book(book_id)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                sql = ("UPDATE kb_books SET " + ", ".join(f"{c}=%s" for c in cols)
                       + ", updated_at=%s WHERE book_id=%s")
                cur.execute(sql, tuple(vals) + (_now(), book_id))
            conn.commit()
        finally:
            conn.close()
        return self.get_book(book_id)

    def quarantine_book(self, book_id):
        now = _now()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE kb_books SET status='quarantined', index_state='none',"
                    " dedup_key=CONCAT(sha256,'#',book_id), deleted_at=%s, updated_at=%s"
                    " WHERE book_id=%s", (now, now, book_id))
            conn.commit()
        finally:
            conn.close()
        return self.get_book(book_id)

    def hard_delete_book(self, book_id):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kb_books WHERE book_id=%s", (book_id,))
                removed = cur.rowcount
            conn.commit()
            return bool(removed)
        finally:
            conn.close()

    # —— k 规格 ——
    def _clusters(self, cur, version):
        cur.execute("SELECT * FROM kb_query_clusters WHERE artifact_version=%s"
                    " ORDER BY cluster_id", (version,))
        return cur.fetchall()

    def active_k_spec(self):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM kb_k_spec WHERE active=1"
                            " ORDER BY created_at DESC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    return None
                return kspec_out(row, self._clusters(cur, row["artifact_version"]))
        finally:
            conn.close()

    def get_k_spec(self, version):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM kb_k_spec WHERE artifact_version=%s", (version,))
                row = cur.fetchone()
                if not row:
                    return None
                return kspec_out(row, self._clusters(cur, version))
        finally:
            conn.close()

    def save_k_spec(self, spec, clusters):
        version = str(spec.get("artifact_version") or "").strip()
        if not version:
            raise ValueError("artifact_version required")
        created = spec.get("created_at") or _now()
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE kb_k_spec SET active=0 WHERE active=1")
                cur.execute(
                    "INSERT INTO kb_k_spec (artifact_version, global_k, labels_present,"
                    " active, eval_json, created_at) VALUES (%s,%s,%s,1,%s,%s)"
                    " ON DUPLICATE KEY UPDATE global_k=VALUES(global_k),"
                    " labels_present=VALUES(labels_present), active=1,"
                    " eval_json=VALUES(eval_json), created_at=VALUES(created_at)",
                    (version, int(spec.get("global_k") or 0),
                     1 if spec.get("labels_present") else 0,
                     _json_dump(spec.get("eval") or spec.get("eval_json") or {}), created))
                cur.execute("DELETE FROM kb_query_clusters WHERE artifact_version=%s",
                            (version,))
                for cluster in clusters or []:
                    centroid = cluster.get("centroid") or []
                    blob = centroid if isinstance(centroid, (bytes, bytearray)) else pack_centroid(centroid)
                    cur.execute(
                        "INSERT INTO kb_query_clusters (artifact_version, cluster_id, centroid,"
                        " k_star, sample_count) VALUES (%s,%s,%s,%s,%s)",
                        (version, int(cluster.get("cluster_id") or 0), bytes(blob),
                         int(cluster.get("k_star") or 0),
                         int(cluster.get("sample_count") or 0)))
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_k_spec(version)


_MIRROR_DDL = """
CREATE TABLE IF NOT EXISTS mirror_books (
  book_id            TEXT PRIMARY KEY,
  filename           TEXT NOT NULL,
  title              TEXT,
  status             TEXT NOT NULL,
  current_generation INTEGER NOT NULL DEFAULT 0,
  chunk_count        INTEGER NOT NULL DEFAULT 0,
  meta_json          TEXT,
  updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mirror_chunks (
  chunk_id      TEXT PRIMARY KEY,
  book_id       TEXT NOT NULL,
  generation    INTEGER NOT NULL,
  is_current    INTEGER NOT NULL DEFAULT 1,
  chunk_index   INTEGER NOT NULL,
  section       TEXT,
  section_path  TEXT,
  page          INTEGER,
  page_hint     INTEGER,
  content       TEXT NOT NULL,
  content_hash  TEXT,
  anchor_hash   TEXT,
  token_count   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mirror_chunks_book ON mirror_chunks(book_id, generation);
CREATE TABLE IF NOT EXISTS mirror_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


class SQLiteMirrorStore:
    """BM25 只读镜像：worker 事务写入，检索以 mode=ro 打开。"""

    def __init__(self, path: str | None = None):
        self.path = path or config.mirror_path()
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _ro_connect(self):
        uri = pathlib.Path(os.path.abspath(self.path)).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        conn = self._connect()
        try:
            conn.executescript(_MIRROR_DDL)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(mirror_chunks)")}
            if "anchor_hash" not in cols:
                conn.execute("ALTER TABLE mirror_chunks ADD COLUMN anchor_hash TEXT")
            conn.commit()
        finally:
            conn.close()

    def health(self):
        t0 = time.perf_counter()
        try:
            conn = self._ro_connect()
            try:
                count = conn.execute("SELECT COUNT(*) FROM mirror_chunks").fetchone()[0]
            finally:
                conn.close()
            return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": f"sqlite chunks={count}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": str(exc)[:160]}

    def sync_book(self, book: dict) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO mirror_books (book_id, filename, title, status,"
                " current_generation, chunk_count, meta_json, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(book_id) DO UPDATE SET filename=excluded.filename,"
                " title=excluded.title, status=excluded.status,"
                " current_generation=excluded.current_generation,"
                " chunk_count=excluded.chunk_count, meta_json=excluded.meta_json,"
                " updated_at=excluded.updated_at",
                (book["book_id"], book.get("filename") or "", book.get("title"),
                 book.get("status") or "ready", int(book.get("current_generation") or 0),
                 int(book.get("chunk_count") or 0), _json_dump(book.get("meta") or {}),
                 _dt_str(book.get("updated_at")) or _now()))
            conn.commit()
        finally:
            conn.close()

    def replace_chunks(self, book_id: str, generation: int, chunks: list) -> int:
        """整代替换：写入后只把该代标为 is_current。"""
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM mirror_chunks WHERE book_id=? AND generation=?",
                             (book_id, int(generation)))
                conn.executemany(
                    "INSERT INTO mirror_chunks (chunk_id, book_id, generation, is_current,"
                    " chunk_index, section, section_path, page, page_hint, content,"
                    " content_hash, anchor_hash, token_count) VALUES (?,?,?,0,?,?,?,?,?,?,?,?,?)",
                    [(c["chunk_id"], book_id, int(generation), int(c.get("chunk_index") or 0),
                      c.get("section") or "", c.get("section_path") or "",
                      c.get("page"), c.get("page_hint"), c.get("content") or "",
                      c.get("content_hash") or "", c.get("anchor_hash") or "",
                      int(c.get("token_count") or 0))
                     for c in chunks])
                conn.execute("UPDATE mirror_chunks SET is_current=0 WHERE book_id=?", (book_id,))
                conn.execute("UPDATE mirror_chunks SET is_current=1 WHERE book_id=? AND generation=?",
                             (book_id, int(generation)))
                conn.execute("DELETE FROM mirror_chunks WHERE book_id=? AND generation<>?",
                             (book_id, int(generation)))
            return len(chunks)
        finally:
            conn.close()

    def mark_book(self, book_id: str, status: str, generation: int = 0,
                  chunk_count: int | None = None) -> None:
        conn = self._connect()
        try:
            with conn:
                if chunk_count is None:
                    conn.execute(
                        "UPDATE mirror_books SET status=?, current_generation=?, updated_at=?"
                        " WHERE book_id=?", (status, int(generation), _now(), book_id))
                else:
                    conn.execute(
                        "UPDATE mirror_books SET status=?, current_generation=?, chunk_count=?,"
                        " updated_at=? WHERE book_id=?",
                        (status, int(generation), int(chunk_count), _now(), book_id))
        finally:
            conn.close()

    def remove_book(self, book_id: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM mirror_chunks WHERE book_id=?", (book_id,))
                conn.execute("DELETE FROM mirror_books WHERE book_id=?", (book_id,))
        finally:
            conn.close()

    def get_book(self, book_id: str):
        conn = self._ro_connect()
        try:
            row = conn.execute("SELECT * FROM mirror_books WHERE book_id=?", (book_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_books(self, status: str = "ready") -> list:
        conn = self._ro_connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM mirror_books WHERE status=?"
                                    " ORDER BY updated_at DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM mirror_books"
                                    " ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def chunks(self, book_id: str | None = None, current_only: bool = True) -> list:
        conn = self._ro_connect()
        try:
            sql = "SELECT * FROM mirror_chunks"
            cond, args = [], []
            if book_id:
                cond.append("book_id=?")
                args.append(book_id)
            if current_only:
                cond.append("is_current=1")
            if cond:
                sql += " WHERE " + " AND ".join(cond)
            sql += " ORDER BY book_id, chunk_index"
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def chunk_count(self) -> int:
        conn = self._ro_connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM mirror_chunks WHERE is_current=1")
                       .fetchone()[0])
        finally:
            conn.close()

    def meta_get(self, key: str, default=None):
        conn = self._ro_connect()
        try:
            row = conn.execute("SELECT value FROM mirror_meta WHERE key=?", (key,)).fetchone()
            return _json_load(row["value"], default) if row else default
        finally:
            conn.close()

    def meta_set(self, key: str, value) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("INSERT INTO mirror_meta (key, value) VALUES (?,?)"
                             " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, _json_dump(value)))
        finally:
            conn.close()
