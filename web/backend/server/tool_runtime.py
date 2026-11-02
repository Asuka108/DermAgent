# -*- coding: utf-8 -*-
"""动态工具运行时：内置工具启停 + 外部 MCP / HTTP / Python 工具的统一目录与分发。

只读边界：
- 外部工具只允许只读：MCP 要求 readOnlyHint=True；HTTP 仅 GET/POST；Python 为只读契约；
- 写类能力仍只存在于内置工具，并沿用原医生确认门禁；
- 结果截断、审计只记摘要，不记参数内容与密钥。
"""
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import copy
from urllib.parse import urlparse

import httpx

try:
    import tool_mcp
    import tool_spec
    import tool_store
except ImportError:  # pragma: no cover - 包导入路径
    from server import tool_mcp
    from server import tool_spec
    from server import tool_store

MAX_RESULT_CHARS = 8192
PY_TIMEOUT = 15

_LOCK = threading.RLock()
_AUDIT = lambda *_a, **_k: None
_CACHE = {"tools": [], "builtin_disabled": [], "loaded": False}


def configure(audit=None) -> None:
    global _AUDIT
    if callable(audit):
        _AUDIT = audit


def _audit(event: str, detail: dict) -> None:
    try:
        _AUDIT(event, detail)
    except Exception:  # noqa: BLE001 — 审计失败不影响工具调用
        pass


def _digest(value) -> str:
    try:
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        blob = str(value)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _unique(base: str, used: set) -> str:
    base = (base or "tool")[:64]
    name = base
    index = 2
    while name in used:
        suffix = f"_{index}"
        name = base[:64 - len(suffix)] + suffix
        index += 1
    used.add(name)
    return name


def _dynamic(public_name, source, origin, description, schema, read_only,
             enabled=False, blocked_reason="", server_id="", original_name="",
             timeout_s=30, status="", file="", secrets_enc="", url="", method="GET"):
    return {"public_name": public_name, "source": source, "origin": origin,
            "description": str(description or "")[:500], "input_schema": schema or {},
            "read_only": bool(read_only), "enabled": bool(enabled) and bool(read_only),
            "blocked_reason": blocked_reason, "server_id": server_id,
            "original_name": original_name, "timeout_s": int(timeout_s or 30),
            "status": status, "_file": file, "_secrets_enc": secrets_enc,
            "_url": url, "_method": method}


def refresh() -> dict:
    cfg = tool_store.load()
    statuses = tool_mcp.status_all()
    used = set()
    tools = []
    for tool in cfg["http_tools"]:
        base = tool.get("public_name") or f"http_{tool_store.slug(tool.get('name'))}"
        public = _unique(base, used)
        tools.append(_dynamic(
            public, "http", tool.get("name"), tool.get("description"),
            tool.get("input_schema"), True, tool.get("enabled"),
            "" if tool.get("enabled") else "未启用",
            original_name=tool.get("name"), timeout_s=tool.get("timeout_s"),
            status="enabled" if tool.get("enabled") else "disabled",
            secrets_enc=tool.get("secrets_enc") or "",
            url=tool.get("url") or "", method=tool.get("method") or "GET"))
    for tool in cfg["py_tools"]:
        base = tool.get("public_name") or f"py_{tool_store.slug(tool.get('name'))}"
        public = _unique(base, used)
        tools.append(_dynamic(
            public, "py", tool.get("name"), tool.get("description"),
            tool.get("input_schema"), True, tool.get("enabled"),
            "" if tool.get("enabled") else "未启用",
            original_name=tool.get("name"), timeout_s=PY_TIMEOUT,
            status="enabled" if tool.get("enabled") else "disabled",
            file=tool.get("file") or ""))
    for server in cfg["servers"]:
        state = statuses.get(server["id"]) or {}
        removed = set(server.get("removed_tools") or [])
        states = server.get("tool_states") or {}
        for tool in sorted(state.get("tools") or [], key=lambda x: x.get("name", "")):
            name = str(tool.get("name") or "")
            if not name or name in removed:
                continue
            base = f"mcp_{tool_store.slug(server.get('name'), 'server')}_{tool_store.slug(name)}"
            public = _unique(base, used)
            read_only = bool(tool.get("read_only")) and bool((tool.get("blocked_reason") or "") == "")
            blocked = str(tool.get("blocked_reason") or "")
            enabled = (read_only and bool((states.get(name) or {}).get("enabled"))
                       and state.get("status") == "connected")
            if blocked:
                status = "blocked"
            elif state.get("status") != "connected":
                status = str(state.get("status") or "disconnected")
            else:
                status = "enabled" if enabled else "disabled"
            tools.append(_dynamic(
                public, "mcp", server.get("name"), tool.get("description"),
                tool.get("input_schema"), read_only, enabled,
                blocked or ("" if enabled else "未启用"),
                server_id=server["id"], original_name=name,
                timeout_s=server.get("timeout_s"), status=status))
    with _LOCK:
        _CACHE["tools"] = tools
        _CACHE["builtin_disabled"] = list(cfg.get("builtin_disabled") or [])
        _CACHE["loaded"] = True
    return {"tools": len(tools), "enabled": sum(1 for t in tools if t["enabled"])}


def _ensure_loaded() -> None:
    with _LOCK:
        loaded = _CACHE.get("loaded")
    if not loaded:
        refresh()


def _tools() -> list:
    _ensure_loaded()
    with _LOCK:
        return [dict(t) for t in _CACHE["tools"]]


def builtin_disabled() -> set:
    _ensure_loaded()
    with _LOCK:
        return set(_CACHE["builtin_disabled"])


def get(public_name: str) -> dict:
    name = str(public_name or "").strip()
    for tool in _tools():
        if tool["public_name"] == name:
            return tool
    return {}


def schemas() -> list:
    refresh()
    out = []
    for tool in _tools():
        if not tool["enabled"] or not tool["read_only"]:
            continue
        out.append({"type": "function", "function": {
            "name": tool["public_name"],
            "description": (f"[外部工具 · {tool['source']} · {tool['origin']}] "
                            + tool["description"])[:1024],
            "parameters": tool["input_schema"] or {"type": "object", "properties": {}}}})
    return out


def names() -> set:
    refresh()
    return {t["public_name"] for t in _tools() if t["enabled"] and t["read_only"]}


def _cap(result: dict) -> dict:
    try:
        blob = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = str(result)
    if len(blob) <= MAX_RESULT_CHARS:
        return result
    return {"ok": bool(result.get("ok", True)), "truncated": True,
            "data": {"text": blob[:MAX_RESULT_CHARS] + "…（已截断）"}}


def execute(name: str, args: dict, ctx: dict = None, test: bool = False) -> dict:
    tool = get(name)
    if not tool:
        return {"ok": False, "error": f"未知工具：{name}"}
    if not tool["read_only"]:
        return {"ok": False, "error": tool.get("blocked_reason") or "外部工具只允许只读"}
    if not tool["enabled"] and not test:
        return {"ok": False, "error": tool.get("blocked_reason") or "工具未启用"}
    err = tool_spec.validate_schema(args or {}, tool.get("input_schema") or {})
    if err:
        return {"ok": False, "error": "args_invalid", "detail": err}
    started = time.monotonic()
    try:
        if tool["source"] == "mcp":
            result = tool_mcp.call(tool["server_id"], tool["original_name"],
                                   args or {}, tool.get("timeout_s"))
        elif tool["source"] == "http":
            result = _call_http(tool, args or {})
        else:
            result = _call_python(tool, args or {})
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 — 外部工具异常不能让回合 500
        result = {"ok": False, "error": f"工具执行失败：{str(exc)[:200]}"}
    result = _cap(result if isinstance(result, dict) else {"ok": False, "error": "工具返回格式非法"})
    duration = int((time.monotonic() - started) * 1000)
    _audit("tool_test" if test else "tool_call", {
        "tool": name, "source": tool["source"], "origin": tool["origin"],
        "ok": bool(result.get("ok")), "duration_ms": duration,
        "args_digest": _digest(args or {})})
    result.setdefault("source", tool["source"])
    result.setdefault("duration_ms", duration)
    return result


def _query_params(args: dict) -> dict:
    out = {}
    for key, value in (args or {}).items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif isinstance(value, list):
            out[key] = [str(x) for x in value]
        else:
            out[key] = json.dumps(value, ensure_ascii=False)
    return out


def _guard_host(url: str) -> None:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("HTTP 工具地址缺少主机名")
    if host in ("169.254.169.254", "metadata.google.internal") or host.startswith("169.254."):
        raise ValueError("HTTP 工具地址指向云元数据服务，已拒绝")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if parsed.scheme != "https":
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError("HTTP 工具不允许访问私网/本机地址（本机调试请用 http://localhost）")


def _call_http(tool: dict, args: dict) -> dict:
    url = str(tool.get("_url") or "")
    _guard_host(url)
    headers = {}
    if tool.get("_secrets_enc"):
        headers = tool_store.http_secrets(
            {"secrets_enc": tool["_secrets_enc"]}).get("headers") or {}
    method = str(tool.get("_method") or "GET").upper()
    timeout = float(tool.get("timeout_s") or 15)
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, verify=True) as client:
            if method == "GET":
                resp = client.get(url, params=_query_params(args), headers=headers)
            else:
                resp = client.post(url, json=args, headers=headers)
    except httpx.TimeoutException:
        return {"ok": False, "error": f"HTTP 工具超时（{int(timeout)} 秒）"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"HTTP 工具请求失败：{str(exc)[:200]}"}
    if 300 <= resp.status_code < 400:
        return {"ok": False, "error": "HTTP 工具返回重定向，已拒绝跟随"}
    if resp.status_code >= 400:
        return {"ok": False, "error": f"HTTP {resp.status_code}：{resp.text[:300]}"}
    text = resp.text[:65536]
    try:
        data = resp.json()
    except ValueError:
        data = {"text": text}
    return {"ok": True, "status": resp.status_code, "data": data}


def _call_python(tool: dict, args: dict) -> dict:
    path = tool_store.path_for_py_tool({"file": tool.get("_file") or ""})
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tool_py_runner.py")
    if not os.path.isfile(runner):
        return {"ok": False, "error": "Python 工具运行器缺失"}
    workdir = tempfile.mkdtemp(prefix="derm-tool-")
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
           if key in os.environ}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    try:
        proc = subprocess.run(
            [sys.executable, runner, path], input=json.dumps({"args": args}, ensure_ascii=False),
            capture_output=True, text=True, timeout=PY_TIMEOUT, cwd=workdir, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Python 工具超时（{PY_TIMEOUT} 秒）"}
    except OSError as exc:
        return {"ok": False, "error": f"Python 工具启动失败：{str(exc)[:200]}"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()[:300]
        return {"ok": False, "error": f"Python 工具退出码 {proc.returncode}：{detail}"}
    raw = (proc.stdout or "").strip()
    if not raw or len(raw) > 65536:
        return {"ok": False, "error": "Python 工具输出为空或超过 64KB"}
    try:
        result = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "Python 工具输出不是合法 JSON"}
    return result if isinstance(result, dict) else {"ok": False, "error": "Python 工具返回值必须是对象"}


def catalog() -> list:
    refresh()
    out = []
    disabled = builtin_disabled()
    for spec in tool_spec.catalog():
        item = copy.deepcopy(spec)
        item.update({"source": "builtin", "builtin": True,
                     "enabled": spec.get("name") not in disabled,
                     "read_only": spec.get("risk") == "read",
                     "deletable": False, "testable": False,
                     "status": "available" if spec.get("available") else "declared"})
        out.append(item)
    with _LOCK:
        dynamic = [dict(t) for t in _CACHE["tools"]]
    for tool in dynamic:
        out.append({
            "name": tool["public_name"], "title": tool["origin"] or tool["public_name"],
            "description": tool["description"], "input_schema": tool["input_schema"],
            "risk": "read", "source": tool["source"], "builtin": False,
            "enabled": tool["enabled"], "read_only": tool["read_only"],
            "deletable": True, "testable": True, "status": tool["status"],
            "error": tool["blocked_reason"], "origin": tool["origin"],
            "server_id": tool["server_id"], "original_name": tool["original_name"],
            "available": tool["enabled"], "executor": "",
            "expose_llm": True, "expose_mcp": False,
        })
    return out


def servers_view() -> list:
    refresh()
    public = tool_store.public_config()
    statuses = tool_mcp.status_all()
    with _LOCK:
        dynamic = [dict(t) for t in _CACHE["tools"]]
    for server in public["servers"]:
        state = statuses.get(server["id"]) or {}
        server["status"] = state.get("status", "disabled")
        server["error"] = state.get("error", "")
        server["connected_at"] = state.get("connected_at", "")
        server["tools"] = [
            {"public_name": t["public_name"], "name": t["original_name"],
             "description": t["description"], "input_schema": t["input_schema"],
             "enabled": t["enabled"], "read_only": t["read_only"],
             "status": t["status"], "error": t["blocked_reason"]}
            for t in dynamic if t["source"] == "mcp" and t["server_id"] == server["id"]]
    return public["servers"]


def http_tools_view() -> list:
    refresh()
    public = tool_store.public_config()
    with _LOCK:
        dynamic = {t["original_name"]: t for t in _CACHE["tools"] if t["source"] == "http"}
    for tool in public["http_tools"]:
        extra = dynamic.get(tool.get("name")) or {}
        tool["public_name"] = extra.get("public_name") or tool.get("public_name")
        tool["read_only"] = True
        tool["status"] = extra.get("status") or ("enabled" if tool.get("enabled") else "disabled")
    return public["http_tools"]


def py_tools_view() -> list:
    refresh()
    public = tool_store.public_config()
    with _LOCK:
        dynamic = {t["original_name"]: t for t in _CACHE["tools"] if t["source"] == "py"}
    for tool in public["py_tools"]:
        extra = dynamic.get(tool.get("name")) or {}
        tool["public_name"] = extra.get("public_name") or tool.get("public_name")
        tool["read_only"] = True
        tool["status"] = extra.get("status") or ("enabled" if tool.get("enabled") else "disabled")
    return public["py_tools"]


def reset() -> None:
    with _LOCK:
        _CACHE["tools"] = []
        _CACHE["builtin_disabled"] = []
        _CACHE["loaded"] = False


# app.py 运行脚本与 tests-server 包导入会在同一进程产生两个模块实例；
# 统一成同一个，避免动态工具缓存被复制成两份。
sys.modules.setdefault("tool_runtime", sys.modules[__name__])
sys.modules.setdefault("server.tool_runtime", sys.modules[__name__])
