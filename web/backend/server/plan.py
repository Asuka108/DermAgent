# -*- coding: utf-8 -*-
"""v76 显式计划对象：可展示、可确认、可取消、执行期工具白名单。

旧字段 `planned_task` / `plan_confirmed` / `reply.plan` 继续写入（向后兼容），
本模块只维护新增的 `sess["plan_object"]`，不改变既有计划门控的判定。
"""
import uuid
from datetime import datetime, timezone

try:
    import tool_spec as _tool_spec
except ImportError:  # pragma: no cover - 包导入路径
    from server import tool_spec as _tool_spec

SCHEMA_VERSION = "1.0"

# v77：标签/风险/任务映射全部来自 tool_spec 单一真源，本模块只保留旧导出名。
TOOL_RISK = {s.name: s.risk for s in _tool_spec.all() if s.expose_llm}
TOOL_STEP_LABELS = {s.name: s.step_label or s.title
                    for s in _tool_spec.all() if s.expose_llm}
TASK_TOOL_MAP = _tool_spec.task_tool_map()
DEFAULT_TOOLS = tuple(_tool_spec.for_task("__default__"))

ACTIVE_STATES = ("approved", "executing", "completed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def tools_for(task: str) -> list:
    return list(TASK_TOOL_MAP.get(str(task or ""), DEFAULT_TOOLS))


def _steps(tools: list) -> list:
    return [{"id": i + 1, "tool": tool, "label": TOOL_STEP_LABELS.get(tool, tool),
             "risk": TOOL_RISK.get(tool, "read"), "status": "pending",
             "started_at": None, "ended_at": None, "error": ""}
            for i, tool in enumerate(tools)]


def current(sess: dict) -> dict:
    plan = sess.get("plan_object")
    return plan if isinstance(plan, dict) else {}


def create(sess: dict, task: str, missing=None, action: str = "",
           source: str = "rules", turn_id: str = "") -> dict:
    prev = current(sess)
    version = 1
    if prev and prev.get("task_type") == task:
        version = int(prev.get("version") or 0) + 1
    tools = tools_for(task)
    plan = {"schema_version": SCHEMA_VERSION,
            "plan_id": "pln_" + uuid.uuid4().hex[:12],
            "turn_id": str(turn_id or ""),
            "version": version,
            "task_type": task,
            "action": str(action or ""),
            "state": "proposed",
            "created_at": _now(), "approved_at": None, "approved_by": None,
            "steps": _steps(tools), "allowed_tools": tools,
            "missing": list(missing or []), "source": source}
    sess["plan_object"] = plan
    return plan


def approve(sess: dict, by: str = "doctor") -> dict:
    plan = current(sess)
    if not plan or plan.get("state") not in ("proposed", "approved"):
        return plan
    plan["state"] = "approved"
    plan["approved_at"] = _now()
    plan["approved_by"] = by
    return plan


def cancel(sess: dict) -> dict:
    plan = current(sess)
    if plan and plan.get("state") in ("proposed", "approved", "executing"):
        plan["state"] = "cancelled"
        plan["ended_at"] = _now()
    return plan


def supersede(sess: dict) -> dict:
    plan = current(sess)
    if plan and plan.get("state") in ("proposed", "approved", "executing"):
        plan["state"] = "superseded"
        plan["ended_at"] = _now()
    return plan


def allowed_tools(sess: dict):
    """执行期白名单；无计划对象或计划未确认时返回 None（旧会话不限制）。"""
    plan = current(sess)
    if not plan or plan.get("state") not in ACTIVE_STATES:
        return None
    tools = plan.get("allowed_tools")
    return list(tools) if isinstance(tools, list) and tools else None


def check_tool(sess: dict, tool: str) -> bool:
    allowed = allowed_tools(sess)
    return True if allowed is None else tool in allowed


def record_trace(sess: dict, tool_trace: list) -> dict:
    """把本轮真实工具轨迹回填到计划步骤（只更新匹配的 pending 步骤）。"""
    plan = current(sess)
    if not plan or plan.get("state") not in ("approved", "executing", "completed"):
        return plan
    if plan.get("state") == "approved":
        plan["state"] = "executing"
        plan["started_at"] = _now()
    steps = plan.get("steps") or []
    for trace in tool_trace or []:
        if not isinstance(trace, dict):
            continue
        tool = str(trace.get("tool") or "")
        for step in steps:
            if step.get("tool") != tool or step.get("status") != "pending":
                continue
            status = trace.get("status") or ("ok" if trace.get("ok") else "error")
            step["status"] = "done" if status == "ok" else ("failed" if status != "denied" else "skipped")
            step["started_at"] = trace.get("at")
            step["ended_at"] = trace.get("at")
            step["error"] = str(trace.get("error") or "")[:160]
            break
    return plan


def complete(sess: dict, ok: bool = True) -> dict:
    plan = current(sess)
    if not plan or plan.get("state") not in ("approved", "executing"):
        return plan
    for step in plan.get("steps") or []:
        if step.get("status") == "pending":
            step["status"] = "skipped"
    plan["state"] = "completed" if ok else "failed"
    plan["ended_at"] = _now()
    return plan
