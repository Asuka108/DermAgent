# -*- coding: utf-8 -*-
"""桌面端知识库客户端：只经本机服务端 HTTP 访问 KB，不直接读写 books.json。

- server 模式：上传/索引/检索/备份全部走 server/app.py（自动拉起由 main.py 负责）；
- mock 模式：仅以只读方式打开 SQLite 镜像展示书目；上传/索引明确返回 server_unavailable，
  绝不伪造索引结果。
"""
import base64
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8765"
DEFAULT_TIMEOUT = 15


def _server_data_dir() -> str:
    explicit = os.environ.get("DERM_DATA_DIR")
    if explicit:
        return explicit
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "backend", "server", "data")


class KBClient:
    def __init__(self, base_url: str = "", mode: str = "server", timeout: int = DEFAULT_TIMEOUT):
        self.base_url = (base_url or DEFAULT_BASE).rstrip("/")
        self.mode = mode if mode in ("server", "mock") else "server"
        self.timeout = max(2, int(timeout))

    # —— 传输 ——
    def _send(self, req: urllib.request.Request, timeout: int = None) -> dict:
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(payload) if payload.strip() else {}
                return parsed if isinstance(parsed, dict) else {"ok": True, "data": parsed}
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                if isinstance(payload, dict):
                    payload.setdefault("ok", False)
                    return payload
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "reason": f"http_{exc.code}"}
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return {"ok": False, "reason": "server_unavailable"}

    def _req(self, method: str, path: str, body=None, timeout: int = None) -> dict:
        if self.mode != "server":
            return {"ok": False, "reason": "server_unavailable"}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data is not None else {})
        return self._send(req, timeout)

    # —— 工具管理（v79）——
    def tools_catalog(self) -> dict:
        return self._req("GET", "/api/tools")

    def set_builtin_enabled(self, name: str, enabled: bool) -> dict:
        return self._req("PATCH", "/api/tools/builtin/" + urllib.parse.quote(str(name), safe=""),
                         {"enabled": bool(enabled)})

    def list_servers(self) -> dict:
        return self._req("GET", "/api/tools/servers")

    def save_server(self, payload: dict, server_id: str = "") -> dict:
        if server_id:
            return self._req("PUT", "/api/tools/servers/" + urllib.parse.quote(server_id, safe=""),
                             payload, timeout=25)
        return self._req("POST", "/api/tools/servers", payload, timeout=25)

    def delete_server(self, server_id: str) -> dict:
        return self._req("DELETE", "/api/tools/servers/" + urllib.parse.quote(server_id, safe=""))

    def test_server(self, server_id: str) -> dict:
        return self._req("POST", "/api/tools/servers/" + urllib.parse.quote(server_id, safe="") + "/test",
                         {}, timeout=40)

    def set_mcp_tool(self, server_id: str, tool_name: str, enabled: bool) -> dict:
        return self._req("PATCH", "/api/tools/servers/" + urllib.parse.quote(server_id, safe="") +
                         "/tools/" + urllib.parse.quote(str(tool_name), safe=""),
                         {"enabled": bool(enabled)})

    def delete_mcp_tool(self, server_id: str, tool_name: str) -> dict:
        return self._req("DELETE", "/api/tools/servers/" + urllib.parse.quote(server_id, safe="") +
                         "/tools/" + urllib.parse.quote(str(tool_name), safe=""))

    def import_mcp_json(self, payload: dict) -> dict:
        return self._req("POST", "/api/tools/import/mcp", payload, timeout=30)

    def list_http_tools(self) -> dict:
        return self._req("GET", "/api/tools/http")

    def save_http_tool(self, payload: dict) -> dict:
        return self._req("POST", "/api/tools/http", payload)

    def delete_http_tool(self, tool_id: str) -> dict:
        return self._req("DELETE", "/api/tools/http/" + urllib.parse.quote(tool_id, safe=""))

    def list_py_tools(self) -> dict:
        return self._req("GET", "/api/tools/python")

    def import_py_tool(self, filename: str, data: bytes) -> dict:
        # base64 JSON：服务端不强制 python-multipart，缺少该依赖也能启动
        return self._req("POST", "/api/tools/import/python",
                         {"filename": os.path.basename(filename),
                          "content_b64": base64.b64encode(data).decode("ascii")},
                         timeout=30)

    def set_dynamic_enabled(self, name: str, enabled: bool) -> dict:
        return self._req("PATCH", "/api/tools/dynamic/" + urllib.parse.quote(str(name), safe=""),
                         {"enabled": bool(enabled)})

    def delete_dynamic_tool(self, name: str) -> dict:
        return self._req("DELETE", "/api/tools/dynamic/" + urllib.parse.quote(str(name), safe=""))

    def test_tool(self, name: str, args: dict) -> dict:
        return self._req("POST", "/api/tools/test", {"name": name, "args": args or {}}, timeout=60)

    def tool_audit(self, limit: int = 50) -> dict:
        return self._req("GET", f"/api/tools/audit?limit={int(limit)}")


    # —— 书目 ——
    def list_books(self) -> dict:
        if self.mode != "server":
            return self._mirror_books()
        return self._req("GET", "/api/kb/books")

    def _mirror_books(self) -> dict:
        path = os.path.join(_server_data_dir(), "kb", "mirror.sqlite3")
        if not os.path.isfile(path):
            return {"ok": False, "reason": "mirror_unavailable", "books": [],
                    "read_only": True}
        try:
            conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT book_id, filename, title, status, current_generation,"
                    " chunk_count, meta_json, updated_at FROM mirror_books"
                    " ORDER BY updated_at DESC").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            return {"ok": False, "reason": "mirror_unavailable", "detail": str(exc)[:120],
                    "books": []}
        books = []
        for row in rows:
            try:
                meta = json.loads(row["meta_json"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            books.append({
                "book_id": row["book_id"], "filename": row["filename"],
                "title": row["title"] or os.path.splitext(row["filename"] or "")[0],
                "status": row["status"], "current_generation": row["current_generation"],
                "chunk_count": row["chunk_count"], "chunks": row["chunk_count"],
                "meta_incomplete": bool(meta.get("format_incomplete")),
                "updated_at": row["updated_at"], "updated_label": str(row["updated_at"] or "")[:19].replace("T", " "),
            })
        return {"ok": True, "books": books, "read_only": True, "kb_snapshot": "",
                "embedding_state": "unknown", "rerank_state": "unknown",
                "mineru_state": "unknown", "degraded_reason": "desktop_mock_mirror"}

    # —— 上传 / 索引 ——
    def upload(self, filename: str, data: bytes, meta_note: str = "") -> dict:
        import base64
        return self._req("POST", "/api/kb/books", {
            "filename": filename,
            "content_base64": base64.b64encode(data).decode("ascii"),
            "meta_note": meta_note,
        }, timeout=120)

    def index(self, book_id: str, reindex: bool = False) -> dict:
        path = "/reindex" if reindex else "/index"
        return self._req("POST", f"/api/kb/books/{book_id}{path}")

    def status(self, book_id: str) -> dict:
        return self._req("GET", f"/api/kb/books/{book_id}/status")

    def job(self, job_id: str) -> dict:
        return self._req("GET", f"/api/kb/jobs/{job_id}")

    # —— 生命周期 ——
    def delete(self, book_id: str) -> dict:
        return self._req("DELETE", f"/api/kb/books/{book_id}")

    def delete_hard(self, book_id: str) -> dict:
        return self._req("DELETE", f"/api/kb/books/{book_id}/hard")

    def restore(self, book_id: str) -> dict:
        return self._req("POST", f"/api/kb/books/{book_id}/restore")

    # —— 检索 / 备份 / 健康 ——
    def search(self, query: str, top_n: int = None) -> dict:
        suffix = f"?q={urllib.parse.quote(str(query or ''))}"
        if top_n:
            suffix += f"&top_k={int(top_n)}"
        return self._req("GET", "/api/kb/search" + suffix)

    def backup(self) -> dict:
        return self._req("GET", "/api/kb/backup", timeout=60)

    def restore_backup(self, data: dict) -> dict:
        return self._req("POST", "/api/kb/restore", {"data": data}, timeout=60)

    def health(self) -> dict:
        return self._req("GET", "/api/kb/health")

    def embedding_state(self) -> str:
        comp = (self.health().get("components") or {}).get("embedder") or {}
        return comp.get("state") or ("ready" if comp.get("ok") else "unknown")

    # —— 设置页扩展（全部复用既有 HTTP 端点，不另建存储） ——
    def embedding_info(self) -> dict:
        return self._req("GET", "/api/kb/embedding")

    def embedding_install(self, short: str) -> dict:
        return self._req("POST", "/api/kb/embedding/install", {"model": short}, timeout=30)

    def embedding_activate(self, short: str) -> dict:
        return self._req("POST", "/api/kb/embedding/activate", {"model": short})

    def prefs(self) -> dict:
        return self._req("GET", "/api/rag/prefs")

    def set_prefs(self, strength: str, chunk_mode: str, chunk_overlap: int) -> dict:
        return self._req("POST", "/api/rag/prefs",
                         {"strength": strength, "chunk_mode": chunk_mode,
                          "chunk_overlap": chunk_overlap})

    def rebuild(self) -> dict:
        return self._req("POST", "/api/kb/rebuild", timeout=600)

    def import_folder(self) -> dict:
        return self._req("POST", "/api/kb/import-folder", timeout=120)
