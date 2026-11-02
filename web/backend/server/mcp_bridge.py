# -*- coding: utf-8 -*-
"""v77 MCP 只读桥：JSON-RPC 2.0（stateless），默认关闭。

边界（医疗安全优先）：
- 只暴露 `tool_spec.mcp_tools()`：只读、无医生确认门禁；写工具永远不可达；
- 每个病例类工具必须带 `case_id`，服务端校验病例存在；
- `DERM_MCP=0` 默认关闭；`DERM_MCP_TOKEN` 非空时要求 Bearer；
- 每次调用写审计（只记工具名/参数摘要/结果/耗时，不记病例内容）。
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import tool_exec
    import tool_spec
except ImportError:  # pragma: no cover - 包导入路径
    from server import tool_exec
    from server import tool_spec

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "dermagent", "version": "1.0.0"}

# JSON-RPC / MCP 错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
POLICY_DENIED = -32001
CASE_NOT_FOUND = -32002


def enabled() -> bool:
    raw = os.environ.get("DERM_MCP", "0")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def token_ok(header: str) -> bool:
    expected = str(os.environ.get("DERM_MCP_TOKEN", "") or "").strip()
    if not expected:
        return True
    got = str(header or "").strip()
    if got.lower().startswith("bearer "):
        got = got[7:].strip()
    return hmac_compare(got, expected)


def hmac_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def manifest() -> dict:
    return {"name": SERVER_INFO["name"], "version": SERVER_INFO["version"],
            "protocolVersion": PROTOCOL_VERSION, "tools": tool_spec.mcp_tools()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value) -> str:
    try:
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        blob = str(value)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _error(req_id, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _ok(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _tool_result(result: dict) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(result, ensure_ascii=False)}],
            "isError": not bool(result.get("ok", True))}


def _call_tool(params: dict, ctx: dict):
    audit = ctx.get("audit") or (lambda *_a, **_k: None)
    name = str((params or {}).get("name") or "")
    args = (params or {}).get("arguments")
    if not name or not isinstance(args, dict):
        return None, (INVALID_PARAMS, "tools/call 需要 name 与 arguments")
    allowed = {t["name"] for t in tool_spec.mcp_tools()}
    if name not in allowed:
        audit("mcp_policy_denied", {"tool": name, "reason": "not_exposed"})
        return None, (POLICY_DENIED, "该工具未对 MCP 暴露（只读白名单）")
    case_id = str(args.get("case_id") or "").strip()
    if not case_id:
        return None, (INVALID_PARAMS, "缺少 case_id")
    try:
        sess = ctx["load_sess"](case_id)
    except Exception as exc:  # noqa: BLE001 — app 侧用 HTTPException(404) 表达缺病例
        if getattr(exc, "status_code", None) == 404:
            return None, (CASE_NOT_FOUND, "病例不存在")
        return None, (INTERNAL_ERROR, "读取病例失败")
    if not sess:
        return None, (CASE_NOT_FOUND, "病例不存在")
    # 只读工具不需要写锁；load_sess 已由 app 在全局锁内读盘
    import agent_loop  # 触发 5 个执行器注册（幂等）
    agent_loop.ensure_executors()
    tool_ctx = {"kb_search": ctx["kb_search"], "record_labels": ctx.get("record_labels") or {}}
    t0 = time.monotonic()
    result = tool_exec.execute(name, args, sess, tool_ctx)
    duration = int((time.monotonic() - t0) * 1000)
    audit("mcp_tool_call", {"tool": name, "case_id": case_id,
                            "args_digest": _digest({k: v for k, v in args.items()
                                                    if k != "case_id"}),
                            "ok": bool(result.get("ok")), "duration_ms": duration})
    return _tool_result(result), None


def dispatch(payload, ctx: dict):
    """处理单条或批量 JSON-RPC；通知（无 id）返回 None；批量返回列表或 None。"""
    if isinstance(payload, list):
        if not payload:
            return _error(None, INVALID_REQUEST, "空批量请求")
        out = [r for r in (dispatch_one(item, ctx) for item in payload) if r is not None]
        return out or None
    return dispatch_one(payload, ctx)


def dispatch_one(req, ctx: dict):
    if not isinstance(req, dict):
        return _error(None, INVALID_REQUEST, "请求必须是 JSON 对象")
    req_id = req.get("id")
    method = req.get("method")
    is_notification = "id" not in req
    if req.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return None if is_notification else _error(req_id, INVALID_REQUEST, "非法 JSON-RPC 请求")
    params = req.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION,
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": SERVER_INFO}
    elif method in ("notifications/initialized", "initialized"):
        return None
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_spec.mcp_tools()}
    elif method == "tools/call":
        result, err = _call_tool(params, ctx)
        if err:
            return None if is_notification else _error(req_id, err[0], err[1],
                                                       {"at": _now()})
    else:
        return None if is_notification else _error(req_id, METHOD_NOT_FOUND,
                                                   f"未知方法：{method}")
    return None if is_notification else _ok(req_id, result)


def default_ctx() -> dict:
    """stdio 入口用：惰性导入 app，避免导入即启动副作用。"""
    try:
        import app as app_mod
    except ImportError:  # pragma: no cover
        from server import app as app_mod
    import agent_loop
    agent_loop.ensure_executors()
    return {"load_sess": app_mod._load_sess,
            "kb_search": app_mod._agent_kb_search,
            "record_labels": app_mod._RECORD_LABELS,
            "audit": app_mod.audit}


def serve_stdio(ctx: dict = None) -> None:
    ctx = ctx or default_ctx()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps(_error(None, PARSE_ERROR, "Parse error")) + "\n")
            sys.stdout.flush()
            continue
        try:
            resp = dispatch(payload, ctx)
        except Exception as exc:  # noqa: BLE001 — stdio 不能因单条请求退出
            (ctx.get("audit") or (lambda *_a, **_k: None))("mcp_internal_error",
                                                           {"error": str(exc)[:160]})
            resp = _error(None, INTERNAL_ERROR, "内部错误")
        if resp is None:
            continue
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover - 手工运行
    if "--stdio" not in sys.argv:
        print("用法：python -m server.mcp_bridge --stdio", file=sys.stderr)
        raise SystemExit(2)
    if not enabled():
        print("DERM_MCP=0，MCP 接口未启用", file=sys.stderr)
        raise SystemExit(1)
    serve_stdio()
