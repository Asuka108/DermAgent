#!/usr/bin/env python3
"""v69：统一导航决策层（服务端权威，前端只执行不推断）。

问题背景——v68 之前「Agent 已经回复，但界面没有自动跳转到对应窗口」的根因是：
导航链路上只有一个零散的 `open_case` 布尔标志（且只在两句指令里被置位），
`workflow_state` 与界面工作区之间**没有任何映射**，服务端从不告诉前端
「本轮结束后你该把医生带到哪里」。于是只有「新建病例」这一种情况会开面板，
doc / awaiting_review / analysis / sources 全部停在原地。

本模块把「状态 → 下一步动作 → 工作区」固化为唯一权威表：

    workflow_state / action / task_type  →  nav_state  →  next_action + workspace

**v70 语义反转（对话优先、结果按需打开）**：本层降级为「只声明、不执行」。
`resolve()` 一律返回 `auto=False`，`workspace` 只是**建议工作区**（供前端标题栏
提示与导航日志），前端**不得**据此自动切换窗口或弹面板。真正驱动界面的是
`artifacts.py` 派生的 `artifacts / available_actions`——由医生点击按钮按需打开。

- `nav_state`  跨端一致的导航态词汇（awaiting_confirmation / case_draft / …）；
- `next_action` 前端 `window.__nav` 可直接执行的动作（navigate_to_* / focus_input / …）；
- `workspace`  只允许 chat | case | 3d。对话永远是默认主窗口；sources / analysis
  是 chat 与 case 内部「滚动 + 高亮」的目标区域，不新开窗口、不遮挡对话。

纪律（对应验收要求 11/12/13）：
- 失败 / 超时 / 空响应（llm_error / llm_unavailable）→ nav_state=error、workspace=None：
  留在当前工作区显示真实错误，绝不跳空白页；
- case_id 为空时不做任何病例相关跳转（避免打开空病例或旧病例）；
- 医生的显式自然语言导航指令（返回对话 / 查看引用 / 打开人体模型…）优先级最高。

仅供医生决策支持演示，不构成诊疗建议或处方。
"""

WORKSPACES = ("chat", "case", "3d")

# —— 规范导航态词汇（前端日志与验收共用；用户验收词汇的落地名） ——
NAV_STATES = (
    "idle", "chat_reply", "awaiting_confirmation", "collecting_information",
    "awaiting_user_input", "case_draft", "awaiting_review", "doctor_modified",
    "confirmed", "rejected", "tool_3d_active", "rag_searching", "rag_completed",
    "analysis_ready", "error",
)

# nav_state → 导航规则。
# workspace=None 表示「留在当前工作区」（错误态绝不跳转）；
# focus_input/scroll/highlight/export_unlocked 是随动作一起下发的界面意图。
NAV_RULES = {
    "idle": {"workspace": None, "next_action": "stay", "reason": "空闲：保持当前工作区"},
    "awaiting_confirmation": {"workspace": "chat", "next_action": "focus_input", "focus_input": True,
                              "reason": "等待医生确认计划：回对话窗口并聚焦输入框"},
    "collecting_information": {"workspace": "chat", "next_action": "focus_input", "focus_input": True,
                               "reason": "信息不足需追问：回对话窗口并聚焦输入框"},
    "awaiting_user_input": {"workspace": "chat", "next_action": "focus_input", "focus_input": True,
                            "reason": "等待医生输入：回对话窗口并聚焦输入框"},
    "chat_reply": {"workspace": "chat", "next_action": "navigate_to_chat",
                   "reason": "Agent 自然语言回复：停留/回到对话窗口"},
    "rag_searching": {"workspace": "chat", "next_action": "scroll_to_latest", "scroll": True,
                      "reason": "知识库检索中：对话区显示检索状态，不跳离"},
    "rag_completed": {"workspace": "chat", "next_action": "scroll_to_latest", "scroll": True,
                      "sources": True, "reason": "检索完成：停留对话，提供「查看引用」入口"},
    "analysis_ready": {"workspace": "chat", "next_action": "navigate_to_analysis", "scroll": True,
                       "sources": True,
                       "reason": "分析结果就绪：对话区滚动到最新结果，提供引用/完整病例入口"},
    "case_draft": {"workspace": "case", "next_action": "navigate_to_case",
                   "reason": "病例草稿：打开病例工作区"},
    "awaiting_review": {"workspace": "case", "next_action": "highlight_case_fields", "scroll": True,
                        "highlight": True,
                        "reason": "病例待医生审核：打开病例工作区并高亮待确认字段"},
    "doctor_modified": {"workspace": "case", "next_action": "highlight_case_fields", "scroll": True,
                        "highlight": True,
                        "reason": "医生已修改字段：保持病例工作区并高亮修改项"},
    "confirmed": {"workspace": "case", "next_action": "navigate_to_case", "scroll": True,
                  "export_unlocked": True,
                  "reason": "病例已确认：打开病例工作区并解锁导出"},
    "rejected": {"workspace": "case", "next_action": "navigate_to_case",
                 "reason": "病例已拒绝：停留在病例工作区（导出不可用）"},
    "tool_3d_active": {"workspace": "3d", "next_action": "navigate_to_3d",
                       "reason": "3D 选点工具激活：切到 3D 面板，选完回对话"},
    "error": {"workspace": None, "next_action": "stay", "error": True,
              "reason": "请求失败/超时/空响应：留在当前工作区显示真实错误，不跳转"},
}

# 服务端 workflow_state → 规范导航态
_STATE_TO_NAV = {
    "planning": "awaiting_confirmation",
    "case_intake": "case_draft",
    "case_supplement": "collecting_information",
    "clarifying": "collecting_information",
    "chat_reply": "chat_reply",
    "screening": "collecting_information",
    "doc_ready": "analysis_ready",
    "awaiting_review": "awaiting_review",
    "escalated": "awaiting_review",
    "approved": "confirmed",
    "rejected": "rejected",
    "closed": "rejected",
    "llm_error": "error",
    "llm_unavailable": "error",
    "tool_3d_active": "tool_3d_active",
    "rag_searching": "rag_searching",
    "rag_completed": "rag_completed",
}

# 医生显式导航指令（自然语言 → 动作/工作区/是否聚焦输入框）
INTENT_ACTIONS = {
    "navigate_to_chat": ("navigate_to_chat", "chat", False),
    "navigate_to_case": ("navigate_to_case", "case", False),
    "navigate_to_3d": ("navigate_to_3d", "3d", False),
    "navigate_to_sources": ("navigate_to_sources", "case", False),
    "navigate_to_analysis": ("navigate_to_analysis", "chat", False),
    "focus_input": ("focus_input", "chat", True),
}

# 需要高亮「待医生确认字段」的导航态
_HIGHLIGHT_STATES = ("awaiting_review", "doctor_modified", "confirmed")


def action_of(snap: dict) -> str:
    """本轮响应类型。与客户端 agent_client._render 的优先级保持一致
    （report > closed > llm_unavailable > llm_error > reply > doc > review）。"""
    snap = snap or {}
    if snap.get("report"):
        return "report"
    if snap.get("workflow_state") == "rejected" or snap.get("phase") == "closed":
        return "closed"
    if snap.get("workflow_state") == "llm_unavailable":
        return "llm_unavailable"
    if snap.get("workflow_state") == "llm_error":
        return "llm_error"
    if snap.get("reply"):
        return "reply"
    if snap.get("doc"):
        return "doc"
    return "review"


def nav_state_of(snap: dict) -> str:
    """快照 → 规范导航态。顺序即优先级（显式开窗 > 失败态 > 已确认 > 待审核 > 产物）。"""
    snap = snap or {}
    wf = str(snap.get("workflow_state") or "")
    if snap.get("open_case"):
        return "case_draft"          # 医生明确要求打开/新建病例
    if wf in ("llm_error", "llm_unavailable"):
        return "error"
    if wf == "approved" or snap.get("report"):
        return "confirmed"
    if wf in ("rejected", "closed"):
        return "rejected"
    if wf in ("awaiting_review", "escalated") or snap.get("draft"):
        # 医生已手动改过字段 → 导航态区分出来（前端高亮修改项而不是全新草稿）
        return "doctor_modified" if snap.get("record_review_status") == "doctor_modified" \
            else "awaiting_review"
    if wf == "doc_ready" or snap.get("doc"):
        return "analysis_ready"
    if wf in _STATE_TO_NAV:
        return _STATE_TO_NAV[wf]
    return "collecting_information"


def highlight_fields(sess: dict, nav_state: str) -> list:
    """待医生确认的字段键（有内容且未被医生确认）——前端 casePanel 据此高亮。
    医生确认过的字段不再打扰；空字段由面板自身的完备度提示负责。"""
    if nav_state not in _HIGHLIGHT_STATES:
        return []
    rec = (sess or {}).get("record") or {}
    fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
    meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
    out = []
    for key, value in fields.items():
        if not str(value or "").strip():
            continue
        src = str((meta.get(key) or {}).get("source") or "")
        if src == "doctor_confirmed":
            continue
        out.append(key)
    return out[:8]


def tool_calls(snap: dict, limit: int = 3) -> list:
    """本轮（末尾若干条）工具调用名——导航日志字段之一，便于回溯「哪次工具导致跳转」。"""
    hist = snap.get("tool_history")
    if not isinstance(hist, list):
        return []
    names = []
    for entry in hist[-limit:]:
        if isinstance(entry, dict) and entry.get("tool"):
            names.append(str(entry["tool"]))
    return names


def resolve(sess: dict, snap: dict | None = None) -> dict:
    """核心：给定会话与快照，产出前端唯一需要的导航决策。

    返回字段即前端 `window.__nav` 的入参与导航日志（13 字段）来源：
    nav_state / next_action / workspace / case_id / session_id / workflow_state /
    task_type / tool_calls / focus_input / scroll_to_latest / highlight_fields /
    export_unlocked / reason。
    """
    snap = snap if isinstance(snap, dict) else {}
    sess = sess if isinstance(sess, dict) else {}
    case_id = str(snap.get("case_id") or sess.get("case_id") or "")
    nav_state = nav_state_of(snap)
    rule = dict(NAV_RULES.get(nav_state) or NAV_RULES["idle"])
    next_action = rule.get("next_action", "stay")
    workspace = rule.get("workspace")
    focus_input = bool(rule.get("focus_input"))
    fields = highlight_fields(sess, nav_state)

    # 医生显式导航指令优先（「返回对话」「查看刚才的引用」「打开人体模型」…）
    intent = str(snap.get("nav_intent") or "")   # 已由 _snapshot 一次性消费，不再回读会话
    if intent in INTENT_ACTIONS:
        next_action, workspace, focus_input = INTENT_ACTIONS[intent]
        rule["reason"] = "医生显式导航指令：" + intent
        fields = []

    # 无病例不跳病例工作区（防止打开空病例或上一个病例）
    if workspace == "case" and not case_id:
        workspace = None
        next_action = "focus_input" if nav_state in ("collecting_information",
                                                     "awaiting_confirmation") else "stay"
        fields = []

    scroll = bool(rule.get("scroll")) or next_action in (
        "navigate_to_case", "navigate_to_analysis", "navigate_to_sources", "highlight_case_fields")
    return {
        "nav_state": nav_state,
        "next_action": next_action,
        "workspace": workspace,
        "focus_input": focus_input,
        "scroll_to_latest": scroll,
        "highlight_fields": fields,
        "export_unlocked": bool(rule.get("export_unlocked")),
        "sources_available": bool(rule.get("sources")),
        "case_id": case_id,
        "session_id": case_id,          # 桌面端会话与病例一一对应；单列便于跨端对齐
        "workflow_state": str(snap.get("workflow_state") or ""),
        "task_type": str(snap.get("task_type") or ""),
        "action": action_of(snap),
        "tool_calls": tool_calls(snap),
        "reason": str(rule.get("reason") or ""),
        # v70：显式声明「本决策仅供参考，不自动执行」——前端 __nav 收到后只更新
        # 状态/标题/日志，绝不自动切工作区或弹窗（要求 1/4：所有工作窗口由医生点击打开）。
        "auto": False,
    }


def map_payload() -> dict:
    """映射表只读导出（GET /api/nav/map）——前端兜底表与测试共用同一权威来源。"""
    return {"workspaces": list(WORKSPACES), "nav_states": list(NAV_STATES),
            "rules": {k: dict(v) for k, v in NAV_RULES.items()},
            "state_to_nav": dict(_STATE_TO_NAV),
            "intents": {k: list(v) for k, v in INTENT_ACTIONS.items()},
            "highlight_states": list(_HIGHLIGHT_STATES)}
