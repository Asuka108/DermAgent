#!/usr/bin/env python3
"""v70：产物与可用动作派生层（服务端权威，前端只渲染不猜测）。

交互契约变更——**对话优先、结果按需打开**：
v69 的服务端导航决策会被前端「自动执行」（回复完自动切病例/3D 工作区），
本层把语义反转为「只声明、不执行」：

    session 状态 / 本轮产物  →  artifacts（有什么）  →  available_actions（能点什么）

- `artifacts`：本轮**真实生成**了什么（病例草稿 / 辅助分析 / RAG 引用 / 用药核对 /
  危险信号 / 3D 选点上下文）。为 False 的项，前端不得渲染对应按钮。
- `available_actions`：由 artifacts 派生的可点动作，前端按钮**只来自这里**，
  严禁根据回复文字猜测该打开哪个窗口。
- `message_id`：每条 assistant 回复的唯一标识；前端按钮携带它，
  保证「点某条回复的按钮只打开该条回复对应的结果」，不会串到旧病例或其他消息。
- `result_summary`：该条回复下展示的结果摘要文案（仅列真实存在的产物）。
- `authenticity`：真实 AI / 离线演示 / 模板 / 本地指令 / 无（失败）——如实标注。

失败纪律（对应验收要求 9）：LLM 失败、超时或空响应时，artifacts 一律为空，
available_actions 只给 `retry`（以及已存在的真实病例入口），绝不显示虚假的分析按钮。

仅供医生决策支持演示，不构成诊疗建议或处方。
"""

# 产物键（与前端卡片渲染一一对应；新增产物必须同时登记到 _LABELS 与 _ACTIONS）
ARTIFACT_KEYS = ("case_draft", "analysis", "rag_sources", "medication_check",
                 "risk_screening", "model_3d_context")

# 动作 → 前端按钮文案（前端只按此表取标签，不自造动作名）
ACTION_LABELS = {
    "open_case": "查看病例",
    "open_analysis": "查看完整分析",
    "open_sources": "查看 RAG 引用",
    "open_medication": "查看用药核对",
    "open_risk": "查看危险信号",
    "open_3d": "打开 3D 人体模型",
    "export_case": "导出病例",
    "retry": "重试",
}

# 产物 → 动作（顺序即按钮展示顺序）
_ACTIONS = (
    ("case_draft", "open_case"),
    ("analysis", "open_analysis"),
    ("rag_sources", "open_sources"),
    ("medication_check", "open_medication"),
    ("risk_screening", "open_risk"),
    ("model_3d_context", "open_3d"),
)

# 失败态：不产生任何产物
_FAILED_STATES = ("llm_error", "llm_unavailable")

# 诊断工作流阶段：回复中的“查看病例”是工作入口，不等同于虚构病例草稿产物。
_DIAGNOSIS_WORKFLOW_STATES = frozenset((
    "case_intake", "case_supplement", "clarifying", "awaiting_follow_up",
    "awaiting_user_input", "screening", "awaiting_review", "review", "approved",
))


def _text(value) -> str:
    return str(value or "").strip()


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def message_id_of(sess: dict) -> str:
    """当前消息标识（由回复落库时盖章，见 app._stamp_message）。"""
    return str((sess or {}).get("message_id") or "")


def authenticity_of(snap: dict) -> str:
    """结果来源如实标注：real_ai / offline_demo / template / agent_command / none。"""
    source = str(((snap or {}).get("llm") or {}).get("source") or "")
    if source == "llm":
        return "real_ai"
    if source == "demo":
        return "offline_demo"
    if source == "template":
        return "template"
    if source == "agent":
        return "agent_command"
    if (snap or {}).get("draft") or (snap or {}).get("doc"):
        return "template"          # 有产物但无 llm 元信息：按规则管线如实标注
    return "none"


def citations_of(snap: dict) -> list:
    """本轮可用的引用（RAG 真书检索 + 文书引用），去重后返回。"""
    out, seen = [], set()
    pools = [snap.get("book_citations")]
    doc = snap.get("doc") if isinstance(snap.get("doc"), dict) else {}
    pools.append(doc.get("citations"))
    record = snap.get("record") if isinstance(snap.get("record"), dict) else {}
    pools.append(record.get("citations"))
    for pool in pools:
        for c in _as_list(pool):
            key = c.get("citation_id") if isinstance(c, dict) else str(c)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(c)
    return out


def _record_fields(snap: dict) -> dict:
    record = snap.get("record") if isinstance(snap.get("record"), dict) else {}
    fields = record.get("fields")
    return fields if isinstance(fields, dict) else {}


def _record_status(snap: dict) -> str:
    record = snap.get("record") if isinstance(snap.get("record"), dict) else {}
    return str(record.get("review_status") or snap.get("record_review_status") or "")


def _confirmed(snap: dict) -> bool:
    """""医生已确认"的两个真实来源：对话审核确认（review_status=approved）
    或病例面板整单确认（record.review_status=confirmed）。任一成立即解锁导出动作。"""
    snap = snap or {}
    return str(snap.get("review_status") or "") == "approved" or _record_status(snap) == "confirmed"


def build(sess: dict, snap: dict) -> dict:
    """核心：由会话与本轮快照派生 artifacts / available_actions / 摘要 / 真实性。"""
    snap = snap if isinstance(snap, dict) else {}
    sess = sess if isinstance(sess, dict) else {}
    wf = str(snap.get("workflow_state") or "")
    failed = wf in _FAILED_STATES
    draft = snap.get("draft") if isinstance(snap.get("draft"), dict) else {}
    doc = snap.get("doc") if isinstance(snap.get("doc"), dict) else {}
    fields = _record_fields(snap)
    record_ready = bool(snap.get("record_ready"))
    citations = citations_of(snap)
    # 本轮结果卡承载的内容类型（report=分析报告 / doc=文书 / closed=拒绝回执——
    # 这些卡面上真实展示着分析/引用/核对内容，按钮随卡走）。
    action = str(((snap.get("nav") or {}).get("action")) or "")
    carries = action in ("report", "doc", "closed")
    # 本轮差异产物（app._stamp_message 结算）：确认卡/指令轮靠它声明「本轮新产出了什么」
    turn = snap.get("turn_products")
    touched = set(turn or []) if isinstance(turn, list) else set()
    has = lambda k: k in touched

    if failed:
        artifacts = {k: False for k in ARTIFACT_KEYS}
        # 失败时只留真实错误与重试；已有真实病例（或已初始化的病例单）仍可打开，
        # 其余一律不给分析类按钮。
        actions = ["retry"]
        if snap.get("case_id") and (record_ready or wf in _DIAGNOSIS_WORKFLOW_STATES):
            actions.insert(0, "open_case")
        if record_ready and _confirmed(snap):
            actions.append("export_case")
        return {"artifacts": artifacts, "available_actions": actions,
                "result_summary": [], "authenticity": authenticity_of(snap)}

    red_flags = _as_list(snap.get("red_flags"))
    med_checks = _as_list(draft.get("medication_checks"))
    draft_conds = _as_list(draft.get("suspected_conditions"))
    regions = _as_list(snap.get("selected_body_regions"))

    # 派生规则：**本轮差异** 或 **本轮结果卡实际承载** 才算本轮产物。
    # - 纯闲聊/指令轮（action=reply 且无差异）：不因病例里存着上一轮的字段而虚报；
    # - 确认重放/刷新恢复轮（action=report/doc）：结果卡真实展示着分析/引用/核对内容，
    #   按钮随卡走，医生仍可「查看完整分析」。
    presence = {
        "case_draft": bool(record_ready),
        "analysis": bool(draft_conds or draft.get("symptom_assessment")) or bool(doc.get("content")),
        "rag_sources": bool(citations),
        "medication_check": bool(med_checks) or bool(_text(fields.get("medication_check"))),
        "risk_screening": bool(red_flags) or bool(_text(fields.get("danger_signals")))
                          or bool(draft.get("red_flag_escalation")),
        "model_3d_context": bool(regions),
    }
    artifacts = {
        "case_draft": has("case") or (carries and presence["case_draft"]),
        "analysis": has("analysis") or (carries and presence["analysis"]),
        "rag_sources": has("rag_sources") or (carries and presence["rag_sources"]),
        "medication_check": has("medication_check") or (carries and presence["medication_check"]),
        "risk_screening": has("risk_screening") or (carries and presence["risk_screening"]),
        "model_3d_context": presence["model_3d_context"],
    }

    actions = [action for key, action in _ACTIONS if artifacts.get(key)]
    # 诊断流程的每条对话回复都要提供继续工作的入口：打开病例单不是声明
    # “本轮生成了病例草稿”，因此不改变 artifacts/turn_products 的真实性。
    if snap.get("case_id") and wf in _DIAGNOSIS_WORKFLOW_STATES and "open_case" not in actions:
        actions.insert(0, "open_case")
    # 3D 是可选辅助工具入口：只要该回复已关联病例就允许打开（对话前/后都能开），
    # 与「本轮是否用过 3D 数据」（artifacts.model_3d_context）是两件事。
    # 病例采集阶段的 3D 入口固定放在病例单内，回复卡不再重复显示入口。
    if snap.get("case_id") and snap.get("workflow_state") != "case_intake" and "open_3d" not in actions:
        actions.append("open_3d")
    if _confirmed(snap) and "export_case" not in actions:
        actions.append("export_case")
    if not any(artifacts.values()):
        # 纯自然语言回复：保留真实工作入口（病例/3D），但不造病例草稿、分析等产物按钮。
        actions = [a for a in actions if a in ("open_case", "open_3d")]

    summary = []
    if artifacts["case_draft"]:
        summary.append("已生成病例草稿")
    if artifacts["analysis"]:
        summary.append("已完成辅助分析")
    if artifacts["rag_sources"]:
        summary.append(f"已找到 {len(citations)} 条知识库引用")
    if artifacts["medication_check"]:
        summary.append("已完成用药安全核对")
    if artifacts["risk_screening"]:
        n = len([f for f in red_flags if _text(f)]) or 1
        summary.append(f"发现 {n} 条需要医生关注的危险信号")
    if artifacts["model_3d_context"]:
        summary.append(f"已关联 3D 选点部位 {len(regions)} 处")

    return {"artifacts": artifacts, "available_actions": actions,
            "result_summary": summary, "authenticity": authenticity_of(snap)}


def map_payload() -> dict:
    """只读导出（GET /api/artifacts/map）：前端按钮文案与测试共用同一权威来源。"""
    return {"artifact_keys": list(ARTIFACT_KEYS),
            "action_labels": dict(ACTION_LABELS),
            "artifact_actions": [list(p) for p in _ACTIONS],
            "failed_states": list(_FAILED_STATES)}
