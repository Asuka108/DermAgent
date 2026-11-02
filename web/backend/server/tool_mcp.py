# -*- coding: utf-8 -*-
"""外部 MCP 客户端管理器（官方 mcp SDK）。

- 每个服务器一个常驻 asyncio 任务：在该任务内进入/退出传输与 ClientSession，
  避免 anyio cancel scope 跨任务错误；外部通过队列投递命令并用 Future 等待。
- 只读门禁：只有 annotations.readOnlyHint=True 且 destructiveHint 不为 True 的工具
  才进入可启用候选；写/未声明工具标 blocked，UI 置灰。
- 状态与错误可查询；配置保存后 reload_all() 热生效；不记录 env/header 明文。
"""
import asyncio
import contextlib
import json
import sys
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timezone

try:
    import tool_store
except ImportError:  # pragma: no cover - 包导入路径
    from server import tool_store

CONNECT_TIMEOUT = 15
LIST_TIMEOUT = 15
_STOP_TIMEOUT = 5

_LOCK = threading.RLock()
_LOOP = None
_THREAD = None
_LOOP_READY = threading.Event()
_SERVERS = {}
_CONFIGS = {}
_AUDIT = lambda *_a, **_k: None


def configure(audit=None) -> None:
    global _AUDIT
    if callable(audit):
        _AUDIT = audit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(event: str, detail: dict) -> None:
    try:
        _AUDIT(event, detail)
    except Exception:  # noqa: BLE001 — 审计失败不影响连接
        pass


def _fingerprint(cfg: dict) -> str:
    keys = ("transport", "command", "args", "cwd", "url", "timeout_s", "enabled",
            "secrets_enc")
    return json.dumps({k: cfg.get(k) for k in keys}, ensure_ascii=False, sort_keys=True)


class _Handle:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.queue = None
        self.task = None
        self.ready = None
        self.status = "connecting"
        self.error = ""
        self.tools = []
        self.connected_at = ""

    def public(self) -> dict:
        return {"id": self.cfg.get("id"), "name": self.cfg.get("name"),
                "transport": self.cfg.get("transport"), "enabled": True,
                "status": self.status, "error": self.error,
                "tools": [dict(t) for t in self.tools],
                "connected_at": self.connected_at,
                "timeout_s": self.cfg.get("timeout_s")}


def _annotation(tool, *names):
    annotations = getattr(tool, "annotations", None)
    for name in names:
        if isinstance(annotations, dict) and name in annotations:
            return annotations.get(name)
        value = getattr(annotations, name, None) if annotations is not None else None
        if value is not None:
            return value
    return None


def _tool_view(tool) -> dict:
    name = str(getattr(tool, "name", "") or "")
    description = str(getattr(tool, "description", "") or "")
    schema = (getattr(tool, "inputSchema", None)
              or getattr(tool, "input_schema", None) or {})
    read_only = _annotation(tool, "readOnlyHint", "read_only_hint") is True
    destructive = _annotation(tool, "destructiveHint", "destructive_hint") is True
    if read_only and not destructive:
        reason = ""
    elif destructive:
        reason = "可能修改或删除数据，外部工具只允许只读"
    else:
        reason = "未声明只读（readOnlyHint），外部工具只允许只读"
    return {"name": name, "description": description, "input_schema": schema,
            "read_only": read_only and not destructive, "blocked_reason": reason}


def _extract_result(result) -> dict:
    content = getattr(result, "content", None) or []
    texts = []
    for block in content:
        if getattr(block, "type", "") == "text":
            text = getattr(block, "text", "")
            if isinstance(text, str):
                texts.append(text)
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    text = "\n".join(texts)[:65536]
    data = {"text": text}
    structured = (getattr(result, "structuredContent", None)
                  or getattr(result, "structured_content", None))
    if structured is not None:
        data["structured"] = structured
    if is_error:
        return {"ok": False, "error": (text or "MCP 工具返回错误")[:500]}
    return {"ok": True, "data": data}


@contextlib.asynccontextmanager
async def _transport(cfg: dict, secrets: dict):
    transport = cfg.get("transport")
    if transport == "stdio":
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = {str(k): str(v) for k, v in (secrets.get("env") or {}).items()}
        async with stdio_client(StdioServerParameters(
                command=str(cfg.get("command") or ""),
                args=[str(x) for x in (cfg.get("args") or [])],
                env=env or None,
                cwd=str(cfg.get("cwd") or "") or None)) as streams:
            yield streams
        return
    headers = {str(k): str(v) for k, v in (secrets.get("headers") or {}).items()}
    if transport == "sse":
        from mcp.client.sse import sse_client
        async with sse_client(str(cfg.get("url") or ""), headers=headers or None) as streams:
            yield streams
        return
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    async with create_mcp_http_client(headers=headers or None) as client:
        async with streamable_http_client(str(cfg.get("url") or ""),
                                          http_client=client) as streams:
            yield streams


async def _run_server(handle: _Handle) -> None:
    cfg = handle.cfg
    session = None
    try:
        from mcp import ClientSession
        try:
            secrets = tool_store.server_secrets(cfg)
        except ValueError as exc:
            handle.status = "error"
            handle.error = str(exc)[:300]
            handle.ready.set_result(False)
            _audit("mcp_server_connect", {"server_id": cfg.get("id"),
                                          "name": cfg.get("name"),
                                          "ok": False, "error": handle.error})
            return
        async with _transport(cfg, secrets) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write,
                                     read_timeout_seconds=CONNECT_TIMEOUT) as sess:
                session = sess
                await asyncio.wait_for(session.initialize(), CONNECT_TIMEOUT)
                listed = await asyncio.wait_for(session.list_tools(), LIST_TIMEOUT)
                handle.tools = [_tool_view(t) for t in (getattr(listed, "tools", None) or [])]
                handle.status = "connected"
                handle.error = ""
                handle.connected_at = _now()
                handle.ready.set_result(True)
                _audit("mcp_server_connect", {
                    "server_id": cfg.get("id"), "name": cfg.get("name"),
                    "transport": cfg.get("transport"), "ok": True,
                    "tools": len(handle.tools),
                    "read_only": sum(1 for t in handle.tools if t["read_only"])})
                while True:
                    item = await handle.queue.get()
                    if item is None:
                        break
                    action, payload, fut = item
                    try:
                        if action == "call":
                            timeout = float(payload.get("timeout") or CONNECT_TIMEOUT)
                            result = await asyncio.wait_for(
                                session.call_tool(payload["name"], payload.get("args") or {},
                                                  read_timeout_seconds=timeout),
                                timeout + 2)
                            fut.set_result(_extract_result(result))
                        elif action == "list":
                            listed = await asyncio.wait_for(session.list_tools(), LIST_TIMEOUT)
                            handle.tools = [_tool_view(t)
                                            for t in (getattr(listed, "tools", None) or [])]
                            fut.set_result([dict(t) for t in handle.tools])
                        else:
                            fut.set_exception(ValueError(f"未知 MCP 操作：{action}"))
                    except Exception as exc:  # noqa: BLE001 — 单次命令失败不结束连接
                        fut.set_exception(exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — 连接失败如实记状态
        handle.status = "error"
        handle.error = _clean_error(exc)
        if handle.ready is not None and not handle.ready.done():
            handle.ready.set_result(False)
        _audit("mcp_server_connect", {"server_id": cfg.get("id"), "name": cfg.get("name"),
                                      "ok": False, "error": handle.error})
    finally:
        if session is None and handle.ready is not None and not handle.ready.done():
            handle.ready.set_result(False)
        if handle.status == "connected":
            handle.status = "stopped"
        handle.queue = None


def _clean_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    return (text or exc.__class__.__name__)[:300]


def _run_loop() -> None:
    global _LOOP
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _LOOP = loop
    _LOOP_READY.set()
    loop.run_forever()


def ensure_started() -> bool:
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive() and _LOOP is not None:
            return True
        _LOOP_READY.clear()
        _THREAD = threading.Thread(target=_run_loop, daemon=True, name="tool-mcp-loop")
        _THREAD.start()
    return _LOOP_READY.wait(5)


def _submit(handle: _Handle, action: str, payload: dict, timeout: float):
    if handle.queue is None or handle.status != "connected":
        raise RuntimeError(handle.error or "MCP 服务器未连接")
    fut = Future()
    _LOOP.call_soon_threadsafe(handle.queue.put_nowait, (action, payload, fut))
    return fut.result(timeout=timeout)


async def _spawn(cfg: dict) -> None:
    handle = _Handle(cfg)
    handle.queue = asyncio.Queue()
    handle.ready = asyncio.get_running_loop().create_future()
    _SERVERS[cfg["id"]] = handle
    handle.task = asyncio.create_task(_run_server(handle))
    try:
        await asyncio.wait_for(asyncio.shield(handle.ready), CONNECT_TIMEOUT + 2)
    except asyncio.TimeoutError:
        handle.status = "error"
        handle.error = "连接超时"
        handle.task.cancel()


async def _stop(handle: _Handle) -> None:
    if handle.queue is not None:
        handle.queue.put_nowait(None)
    if handle.task is not None:
        try:
            await asyncio.wait_for(handle.task, _STOP_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            handle.task.cancel()
    handle.status = "stopped"


def _drop(server_id: str) -> None:
    with _LOCK:
        handle = _SERVERS.pop(server_id, None)
        _CONFIGS.pop(server_id, None)
    if handle is not None and _LOOP is not None:
        try:
            asyncio.run_coroutine_threadsafe(_stop(handle), _LOOP).result(_STOP_TIMEOUT + 2)
        except Exception:  # noqa: BLE001 — 停止失败不能阻塞配置保存
            pass


def reload_all() -> dict:
    if not ensure_started():
        return {"ok": False, "error": "MCP 管理线程启动失败"}
    cfg = tool_store.load()
    desired = {s["id"]: s for s in cfg["servers"] if s.get("enabled")}
    with _LOCK:
        current = dict(_CONFIGS)
    for server_id, old in current.items():
        new = desired.get(server_id)
        if new is None or _fingerprint(old) != _fingerprint(new):
            _drop(server_id)
    for server_id, server in desired.items():
        with _LOCK:
            handle = _SERVERS.get(server_id)
            if handle is not None and _fingerprint(_CONFIGS.get(server_id, {})) == _fingerprint(server):
                continue
            _CONFIGS[server_id] = dict(server)
        try:
            asyncio.run_coroutine_threadsafe(_spawn(dict(server)), _LOOP).result(CONNECT_TIMEOUT + 4)
        except Exception as exc:  # noqa: BLE001
            with _LOCK:
                failed = _SERVERS.get(server_id)
                if failed is not None:
                    failed.status = "error"
                    failed.error = _clean_error(exc)
    return {"ok": True, "servers": status_all()}


def reload_server(server_id: str) -> dict:
    _drop(server_id)
    result = reload_all()
    return {"ok": result.get("ok", False),
            "server": status_all().get(server_id),
            "error": result.get("error", "")}


def stop_server(server_id: str) -> None:
    _drop(server_id)


def stop_all() -> None:
    for server_id in list(_SERVERS.keys()):
        _drop(server_id)


def status_all() -> dict:
    cfg = tool_store.load()
    out = {}
    for server in cfg["servers"]:
        server_id = server["id"]
        if not server.get("enabled"):
            out[server_id] = {"id": server_id, "name": server.get("name"),
                              "transport": server.get("transport"), "enabled": False,
                              "status": "disabled", "error": "", "tools": [],
                              "connected_at": "", "timeout_s": server.get("timeout_s")}
            continue
        with _LOCK:
            handle = _SERVERS.get(server_id)
            out[server_id] = handle.public() if handle is not None else {
                "id": server_id, "name": server.get("name"),
                "transport": server.get("transport"), "enabled": True,
                "status": "connecting", "error": "", "tools": [],
                "connected_at": "", "timeout_s": server.get("timeout_s")}
    return out


def status(server_id: str) -> dict:
    return status_all().get(server_id) or {}


def tools(server_id: str) -> list:
    with _LOCK:
        handle = _SERVERS.get(server_id)
    return [dict(t) for t in (handle.tools if handle is not None else [])]


def refresh_tools(server_id: str) -> list:
    with _LOCK:
        handle = _SERVERS.get(server_id)
    if handle is None:
        raise RuntimeError("MCP 服务器未连接")
    result = _submit(handle, "list", {}, LIST_TIMEOUT + 2)
    return list(result or [])


def call(server_id: str, tool_name: str, args: dict, timeout: float = None) -> dict:
    with _LOCK:
        handle = _SERVERS.get(server_id)
    if handle is None:
        return {"ok": False, "error": "MCP 服务器未连接"}
    if handle.status != "connected":
        return {"ok": False, "error": handle.error or "MCP 服务器未连接"}
    limit = float(timeout or handle.cfg.get("timeout_s") or 30)
    started = time.monotonic()
    try:
        result = _submit(handle, "call", {"name": tool_name,
                                          "args": dict(args or {}),
                                          "timeout": limit}, limit + 3)
    except TimeoutError:
        return {"ok": False, "error": f"MCP 工具调用超时（{int(limit)} 秒）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False,
                "error": f"MCP 工具调用失败：{_clean_error(exc)}",
                "duration_ms": int((time.monotonic() - started) * 1000)}
    result = dict(result or {"ok": False, "error": "MCP 工具无返回"})
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


# app.py 运行脚本与 tests-server 包导入会在同一进程产生两个模块实例；
# 统一成同一个，避免 MCP 连接管理与后台线程被复制成两份。
sys.modules.setdefault("tool_mcp", sys.modules[__name__])
sys.modules.setdefault("server.tool_mcp", sys.modules[__name__])
