# -*- coding: utf-8 -*-
"""v77 工具执行绑定：把 ToolSpec.executor 字符串映射到真实函数。

- 由 agent_loop（Agent 5 工具）与 app（只读执行器）分别注册，避免 app ↔ 工具循环循环依赖；
- 执行前统一走 tool_spec 校验；异常一律转 {ok:false,error}，由运行时回注模型；
- MCP 只调用只读执行器，写工具不在此暴露。
"""
import threading
import sys

try:
    import tool_spec
except ImportError:  # pragma: no cover - 包导入路径
    from server import tool_spec

_LOCK = threading.RLock()
_EXECUTORS = {}


def register(key: str, fn) -> None:
    """注册/覆盖执行器；key 与 ToolSpec.executor 对应。"""
    with _LOCK:
        _EXECUTORS[str(key)] = fn


def registered() -> dict:
    with _LOCK:
        return dict(_EXECUTORS)


def execute(name: str, args: dict, sess: dict, ctx: dict, validate: bool = True) -> dict:
    spec = tool_spec.get(name)
    if spec is None:
        return _dynamic_execute(name, args)
    if not spec.executor:
        return {"ok": False, "error": f"工具仅声明未接入执行器：{spec.name}"}
    fn = registered().get(spec.executor)
    if fn is None:
        return {"ok": False, "error": f"执行器未注册：{spec.executor}"}
    if validate:
        err = tool_spec.validate_schema(args or {}, spec.input_schema)
        if err:
            return {"ok": False, "error": "args_invalid", "detail": err}
    try:
        result = fn(args or {}, sess or {}, ctx or {})
    except Exception as exc:  # noqa: BLE001 — 工具异常不让回合 500
        return {"ok": False, "error": str(exc)[:200]}
    if not isinstance(result, dict):
        return {"ok": False, "error": "工具返回格式非法"}
    return result


def _dynamic_execute(name: str, args: dict) -> dict:
    """未命中内置 ToolSpec 时回落动态工具（外部 MCP / HTTP / Python）。"""
    try:
        import tool_runtime
    except ImportError:  # pragma: no cover - 包导入路径
        from server import tool_runtime
    try:
        return tool_runtime.execute(name, args or {})
    except Exception as exc:  # noqa: BLE001 — 动态工具异常不让回合 500
        return {"ok": False, "error": str(exc)[:200]}


# app.py 会把 server 目录插入 sys.path；同一进程里 `tool_exec` 与 `server.tool_exec`
# 必须指向同一模块，否则执行器注册表会被复制成两份（MCP 与 Agent 各看一份）。
sys.modules.setdefault("tool_exec", sys.modules[__name__])
sys.modules.setdefault("server.tool_exec", sys.modules[__name__])
