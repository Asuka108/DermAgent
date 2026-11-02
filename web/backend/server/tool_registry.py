#!/usr/bin/env python3
"""v68 工具注册表（v77 起为兼容门面）。

唯一真源已迁到 `tool_spec.py`：本模块只做旧视图/旧函数名的转发，避免调用方与测试断裂。
`TOOLS` 仍是 15 条 × 9 键（name/label/description/input_schema/output_schema/
requires_confirmation/source_required/risk_level/route），供 UI 目录与 v68 回归共用。

设计边界（计划 §十）：所有工具都是医生辅助；医学判断类（鉴别/用药/检查）
必须 source_required=True 且 requires_confirmation=True；调度规则见 app.py _screen。
"""

try:
    import tool_spec as _tool_spec
except ImportError:  # pragma: no cover - 包导入路径
    from server import tool_spec as _tool_spec

RISK_LOW, RISK_MED, RISK_HIGH = "low", "medium", "high"

TOOLS = _tool_spec.legacy_specs()
TOOL_REGISTRY = {t["name"]: t for t in TOOLS}

_RISK_LEVEL = {"read": "low", "write": "medium", "screen": "medium", "export": "medium"}

NEUTRAL = {
    "name": "", "label": "", "description": "",
    "input_schema": {}, "output_schema": {},
    "requires_confirmation": False, "source_required": False,
    "risk_level": RISK_LOW, "route": "",
}


def tool_for_task(task: str) -> str:
    """任务类型 → 注册表工具名（未匹配回退病例提取）。"""
    return _tool_spec.LEGACY_TASK_TOOLS.get(task, "case_extractor")


def spec(name: str) -> dict:
    """取旧协议视图：先查 15 条旧表，再按规范名/别名/route 解析，未知给中性缺省。"""
    if name in TOOL_REGISTRY:
        return TOOL_REGISTRY[name]
    canonical = _tool_spec.get(name)
    if canonical is not None:
        return {"name": canonical.name, "label": canonical.title,
                "description": canonical.description,
                "input_schema": canonical.legacy_input or canonical.input_schema,
                "output_schema": canonical.legacy_output or canonical.output_schema,
                "requires_confirmation": canonical.requires_confirmation,
                "source_required": canonical.source_required,
                "risk_level": (_RISK_LEVEL.get(canonical.risk, RISK_LOW)
                               if not canonical.legacy_risk else canonical.legacy_risk),
                "route": canonical.route}
    out = dict(NEUTRAL)
    out["name"] = name
    out["route"] = name
    return out
