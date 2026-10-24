#!/usr/bin/env python3
"""肤智（DermAgent）可信智能体服务层（参赛计划 §4 可信服务层 MVP）。

- 病例会话状态机持久化（case_id 跨端续办：Web / Android(PWA) / Windows 共用同一会话）；
- 规则管线（derm_core 单一权威实现）：必填校验 / 红旗分流 / 检索快照 / 引用白名单 / v1.1 契约；
- 受控模型适配：LLM Key 仅在服务端 env（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）；
  v58 防伪装：自定义病例 LLM 未配置/失败 → 不生成任何医疗内容（workflow_state=llm_unavailable），
  只有 demo 病例回退规则稿（明确标注的离线演示数据），客户端永不见密钥；
- 操作审计 JSONL；端点：/api/cases（创建/列表）、/api/cases/{id}（续办快照）、
  /api/cases/{id}/supplement、/api/cases/{id}/review、静态托管 webui/ 与 client/web/。

仅供医生决策支持演示，不构成诊疗建议或处方。
"""

import hashlib
import json
import os
import re
import ssl
import sys
import time
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

try:
    import llm_config as llm_config_mod  # noqa: E402
except ImportError:
    from server import llm_config as llm_config_mod  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from tool_registry import TOOL_REGISTRY, spec, tool_for_task  # noqa: E402  # v68 工具统一协议
    import nav as nav_mod  # noqa: E402  # v69 统一导航决策层
    import artifacts as artifacts_mod  # noqa: E402  # v70 产物/动作派生层
except ImportError:  # 以 server.app 包方式导入（tests-server）
    from server.tool_registry import TOOL_REGISTRY, spec, tool_for_task  # noqa: E402
    from server import nav as nav_mod  # noqa: E402
    from server import artifacts as artifacts_mod  # noqa: E402

from derm_core import (  # noqa: E402
    DEMO_CASES, REQUIRED_FIELDS, SIGNATURE_NOTE, build_case_from_text, build_draft,
    build_review_question, classify_review, compute_missing, detect_red_flags, merge_reply,
    normalize_llm_draft, render_report, retrieve_citations,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # rag 等服务端本地模块随 app 部署目录可导入
# v63：DERM_DATA_DIR 允许测试隔离数据目录（tests-server conftest 指到临时目录），
# 防止 pytest 直写真实 server/data/ —— 曾发生 test_delete_all_cases 清空医生真实病例。
DATA = os.environ.get("DERM_DATA_DIR") or os.path.join(HERE, "data")
SESSIONS_FILE = os.path.join(DATA, "sessions.json")
AUDIT_FILE = os.path.join(DATA, "audit.jsonl")
MAX_ASK_ROUNDS = 2

app = FastAPI(title="肤智 DermAgent 可信服务层", version="1.1")
_lock = threading.RLock()  # 可重入：review/supplement 在持锁期内调用 _store_sess

# v71：web 界面以 file://（null origin）加载，批量导入大书必须绕开 QWebChannel
# （单消息 >~1MB 会丢数据）直接 fetch 本机服务端；CORS 放行不引入新暴露面
# （API 本就对局域网可直连，CORS 仅约束浏览器跨源读）。
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _pna_preflight(request, call_next):
    """v71：Chromium 对 file://→localhost 强制 Private Network Access 预检（请求头
    Access-Control-Request-Private-Network）；旧版 CORSMiddleware 不认识会打成 400，
    fetch 直接失败。此处在 CORS 层之前短路返回带 PNA 放行的预检响应。"""
    if (request.method == "OPTIONS"
            and str(request.headers.get("access-control-request-private-network", "")).lower() == "true"):
        origin = request.headers.get("origin", "*")
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Private-Network": "true",
            "Access-Control-Max-Age": "600",
        })
    return await call_next(request)


def audit(event: str, detail="") -> None:
    os.makedirs(DATA, exist_ok=True)
    detail = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event,
                             "detail": detail[:500]}, ensure_ascii=False) + "\n")


# —— 会话存储（MVP：JSON 落盘，线程安全；远期替换结构化数据库） ——

def _load_sessions() -> dict:
    try:
        with open(SESSIONS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sessions(sessions: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    with open(SESSIONS_FILE, "w", encoding="utf-8") as fh:
        json.dump(sessions, fh, ensure_ascii=False, indent=2)


# v64-P2：Agent 状态机（快照透传 agent_state，前端可显示当前阶段）
# idle/thinking 为客户端本地状态；服务端映射 workflow_state → agent_state
_AGENT_STATES = {
    "screening": "thinking", "llm_unavailable": "error", "llm_error": "error",
    "planning": "planning", "clarifying": "waiting_user", "chat_reply": "responding",
    "doc_ready": "completed", "awaiting_review": "waiting_user",
    "approved": "completed", "rejected": "closed", "closed": "closed",
}


def _snapshot(sess: dict) -> dict:
    """跨端续办快照：任意端 GET 同一 case_id 得到同一状态/结果/审核版本。
    v69：快照末尾挂权威导航决策（nav / next_action）——前端只执行不推断。"""
    draft = sess.get("draft") or {}
    # v64：.get() 兜底——畸形会话数据（如外部注入缺 red_flags）不该 500，宁可快照缺字段
    out = {"case_id": sess["case_id"], "demo": sess.get("demo"), "phase": sess["phase"],
            "case": sess.get("case") or {}, "missing": sess.get("missing") or [],
            "red_flags": sess.get("red_flags") or [],
            "ask_count": sess.get("ask_count", 0), "draft": sess.get("draft"),
            "review_status": sess.get("review_status", "pending"),
            # 阶段6：审核历史摘要与医生最终判断（与 AI 建议分开传输，feedback 截断防快照膨胀）
            "review_history": [{"at": h.get("at"), "decision": h.get("decision"),
                                "action": h.get("action"), "feedback": (h.get("feedback") or "")[:80],
                                "result_version": h.get("result_version")}
                               for h in sess.get("review_history", [])],
            "doctor_final": sess.get("doctor_final"),
            "final_assist": sess.get("final_assist"),
            "book_citations": sess.get("book_citations", []),
            "workflow_state": sess.get("workflow_state"), "question": sess.get("question"),
            "agent_state": _AGENT_STATES.get(sess.get("workflow_state"), "responding"),
            "report": sess.get("report"), "message": sess.get("message"),
            # v59 Agent 化：任务类型（本轮路由结果）与文书产物（doc_ready 时的结构化文书）
            "task_type": sess.get("task_type") or "case_write",
            "doc": sess.get("doc"),
            # v65：门诊病历单就绪标记（病例窗口入口状态；完整内容走 /record 端点）
            "record_ready": bool(isinstance(sess.get("record"), dict)
                                 and any(v for v in sess["record"].get("fields", {}).values())),
            "record_review_status": (sess.get("record") or {}).get("review_status", ""),
            # 元数据由权威服务层统一返回，三端只读渲染，禁止客户端自行派生版本。
            "result_version": sess.get("result_version", "r0"),
            # v58 真实性元信息：结果来源（llm=真实分析 / demo=离线演示 / none=LLM 不可用）与失败原因
            "llm": sess.get("llm_meta") or {},
            # v64 Agent 对话：自然语言回复（smalltalk/clarify）与按需工具调用记录；会话状态对象
            "reply": sess.get("reply"),
            # v68 §十一.4：结构化上下文摘要（规则式生成；医生可查看；较早对话的压缩表示）
            "context_summary": sess.get("context_summary"),
            # v69：显式导航指令一次性消费——随快照下发后即从会话状态移除，
            # 刷新/续办时不会拿旧指令再跳一次窗口。
            "nav_intent": sess.pop("nav_intent", None),
            # v73：设置动作（如切换界面语言）——写入后随本轮所有快照下发，落盘前消费
            "ui_action": sess.get("_ui_action"),
            # v70：本条 assistant 回复的唯一标识（前端工作按钮据此绑定本条结果，不串旧消息）
            "message_id": artifacts_mod.message_id_of(sess),
            # v70：本轮真实产出的产物键（None = 非回复路径，按病例范围派生）
            "turn_products": sess.get("turn_products"),
            # v70：历史各条回复自己的产物/按钮（前端渲染历史卡按钮区，不串消息）
            "msg_artifacts": dict(list((sess.get("msg_artifacts") or {}).items())[-12:]),
            "selected_body_regions": sess.get("selected_body_regions") or [],
            "attachments": sess.get("attachments") or [],
            "tool_history": sess.get("tool_history") or [],
            # v64-P1 连续对话：对话历史随快照透传（重启恢复/前端渲染；截断防膨胀）
            "messages": (sess.get("messages") or [])[-12:],
            "model_version": draft.get("model_version"),
            "knowledge_snapshot": draft.get("knowledge_snapshot"),
            "safety_policy_version": draft.get("safety_policy_version"),
            "updated_at": sess.get("updated_at")}
    return _apply_nav(out, sess)


def _apply_nav(snap: dict, sess: dict) -> dict:
    """v69：把权威导航决策挂到快照上（nav + next_action）。
    路由返回快照之后才置位的标志（如 open_case）需再调一次本函数重算导航。

    v70 起 nav 降级为「只声明、不执行」：前端不再据它自动切工作区，
    真正驱动界面的是随之派生的 artifacts / available_actions（见 _apply_artifacts）。"""
    decision = nav_mod.resolve(sess, snap)
    snap["nav"] = decision
    snap["next_action"] = decision["next_action"]
    return _apply_artifacts(snap, sess)


def _apply_artifacts(snap: dict, sess: dict) -> dict:
    """v70：挂载「本轮真实产物 → 可点动作 → 结果摘要 → 真实性」。

    前端按钮**只**来自 available_actions，严禁按回复文字猜测该打开哪个窗口；
    LLM 失败/超时时 artifacts 全空、actions 只剩 retry，绝不显示虚假的分析或病例按钮。
    """
    try:
        snap.update(artifacts_mod.build(sess, snap))
    except Exception:                     # 派生失败也不该让请求 500：宁可少按钮，不可假按钮
        snap.setdefault("artifacts", {k: False for k in artifacts_mod.ARTIFACT_KEYS})
        snap.setdefault("available_actions", [])
        snap.setdefault("result_summary", [])
        snap.setdefault("authenticity", "none")
    return snap


def _turn_fingerprint(sess: dict) -> dict:
    """v70：本轮产物指纹（值 = (非空条数, 内容哈希)）。

    为什么需要它：artifacts 若只看「病例里有没有字段」，那么医生只说一句「你好」
    也会显示「已生成病例草稿」——把上一轮的产物冒充成本轮结果（验收要求 7/9 禁止）。
    这里在每次回复盖章时对产物取指纹并与上一轮比对，只有**本轮真的新增/变更**的
    产物才算本轮产物：对话类回合自然只剩工具入口，不产生任何虚假按钮。
    """
    def h(value) -> str:
        return hashlib.md5(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      default=str).encode("utf-8")).hexdigest()

    rec = sess.get("record") if isinstance(sess.get("record"), dict) else {}
    fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
    draft = sess.get("draft") if isinstance(sess.get("draft"), dict) else {}
    doc = sess.get("doc") if isinstance(sess.get("doc"), dict) else {}
    fields_used = {k: str(v) for k, v in fields.items() if str(v or "").strip()}
    conds = [c for c in (draft.get("suspected_conditions") or []) if c]
    assessment = str(draft.get("symptom_assessment") or "")
    red_flag_escalation = str(draft.get("red_flag_escalation") or "")
    doc_text = str(doc.get("content") or "")
    meds = [m for m in (draft.get("medication_checks") or []) if m]
    risks = [f for f in (sess.get("red_flags") or []) if str(f or "").strip()]
    cites = [c for c in (sess.get("book_citations") or []) if c]
    analysis = {"conds": conds, "assessment": assessment,
                "escalation": red_flag_escalation, "doc": doc_text[:400]}
    # 值一律用 [count, hash] 列表（JSON 原生）：指纹要随会话落盘、跨请求比对，
    # 若用 tuple 存盘读回会变 list，导致「与上一轮相同」误判为「本轮新产出」。
    return {
        "case": [len(fields_used), h(fields_used)],
        "analysis": [len(conds) + (1 if assessment else 0) + (1 if doc_text else 0), h(analysis)],
        "rag_sources": [len(cites), h(cites)],
        "medication_check": [len(meds), h(meds)],
        "risk_screening": [len(risks), h(risks)],
    }


def _rebuild_snapshot(sess: dict, prev_snap: dict | None = None) -> dict:
    """v70：回复盖章后重建快照（带上本轮 message_id 与 artifacts）。

    nav_intent 是一次性消费——_screen 内部快照已把它从会话弹出；本轮若带显式
    导航指令，重建前必须回注，否则「返回对话」等指令会被重建吞掉（退化成
    focus_input）。prev_snap 传 _screen 返回的快照以取回该意图。"""
    intent = (prev_snap or {}).get("nav_intent")
    snap = _snapshot(sess)
    if intent and not snap.get("nav_intent"):
        snap["nav_intent"] = intent
        _apply_nav(snap, sess)          # 意图在场后重算导航决策
    return snap


def _persist_msg_artifacts(sess: dict, snap: dict) -> None:
    """v70：把本轮 artifacts 随 message_id 存档（近 12 条）。
    前端渲染历史消息的按钮区时取本条消息自己的产物，而不是整会话最后状态——
    「点某条回复的按钮，只打开该条回复对应的结果」（验收要求 5）。"""
    mid = str(snap.get("message_id") or "")
    if not mid:
        return
    store = sess.setdefault("msg_artifacts", {})
    store[mid] = {"artifacts": snap.get("artifacts") or {},
                  "available_actions": snap.get("available_actions") or [],
                  "result_summary": snap.get("result_summary") or [],
                  "authenticity": snap.get("authenticity") or "none"}
    sess["msg_artifacts"] = dict(list(store.items())[-12:])


def _stamp_message(sess: dict) -> str:
    """v70：给本轮 assistant 回复盖章唯一 message_id，并结算「本轮真实产物」。

    要求 5：点击某条回复下方的按钮，只能打开该条回复对应的病例/分析/引用/工具结果，
    因此每条回复都必须有独立标识，并随快照下发到前端按钮。
    """
    n = int(sess.get("_msg_seq", 0) or 0) + 1
    sess["_msg_seq"] = n
    mid = f"msg_{sess.get('case_id') or 'case'}_{n:04d}"
    sess["message_id"] = mid
    fp = _turn_fingerprint(sess)
    prev = sess.get("_fp") if isinstance(sess.get("_fp"), dict) else {}
    sess["_fp"] = fp
    # 只有「非空 且 与上一轮不同」才算本轮产出
    sess["turn_products"] = [k for k, v in fp.items() if v[0] > 0 and prev.get(k) != v]
    return mid




def _record_nav(sess: dict) -> dict:
    """v69：病例面板写操作返回的导航决策（与对话轮复用同一权威 resolver）。
    医生保存 → doctor_modified（保持病例工作区并高亮修改项）；确认 → confirmed（解锁导出）。"""
    return _apply_nav(_snapshot(sess), sess)["nav"]


def _bump_result_version(sess: dict) -> None:
    """病例每次产生新权威结果时递增版本，供跨端一致性断言（验收卡通用断言）。"""
    current = str(sess.get("result_version", "r0"))
    try:
        n = int(current.removeprefix("r"))
    except ValueError:
        n = 0
    sess["result_version"] = f"r{n + 1}"


# —— v59：自然语言任务路由（Agent 化核心） ——
# 任务类型与触发关键词（顺序即优先级：编辑 > 具体文书 > 写病例；都不匹配 → case_write）。
# 设计边界：规则分类只决定「走哪条管线」，不解析医学内容；LLM 不可用时各管线的
# 降级路径互不相同（文书=本地模板整理 / 医学判断=零内容），防线沿用 v58 契约。
TASK_PATTERNS = [
    # v72 设置指令放最前（「把模型改成 X」含「改成」，不能被 case_edit 抢走）
    ("settings_control", ("修改设置", "更改设置", "改一下设置", "设置修改", "切换连接", "激活连接",
                          "换连接", "换线路", "切换模型", "换模型", "模型改成", "模型设为", "模型换成",
                          "思考改成", "思考调", "思考等级",
                          "语言改成", "语言设为", "语言换成", "语言切换", "切换语言", "切换到英文",
                          "切换到中文", "改成英文", "改成中文", "设为英文", "设为中文", "界面语言")),
    # v73：对话内手动压缩上下文（按钮与输入框指令同路）
    ("context_compress", ("压缩上下文", "压缩记忆", "压缩对话", "整理上下文", "上下文压缩")),
    # 具体目标优先匹配；完整分析必须使用明确口令，不能被“病例/病历”泛词触发。
    ("case_edit", ("改成", "改写", "重写", "修改病例", "编辑病例", "补充到病例", "更新病例", "修订",
                   "重新整理", "根据修改重新整理")),
    ("case_compare", ("只对比病例", "单独对比病例", "对比病例", "病例对比", "和历史病例对比", "与历史病例对比", "跟上次比较")),
    ("image_analyze", ("只分析图片", "单独分析图片", "分析图片", "分析照片", "图片分析", "照片分析", "分析这张图", "看看这张照片")),
    ("report_export", ("只导出报告", "单独导出报告", "导出报告", "生成报告", "导出病历文件", "导出门诊记录文件")),
    ("privacy_deidentify", ("只做脱敏", "单独脱敏", "隐私脱敏", "数据脱敏", "病例脱敏")),
    ("body_region_3d", ("只选身体部位", "单独选部位", "打开人体模型", "打开3D", "打开3d", "人体模型选点", "去选部位")),
    ("medical_kb_search", ("只查知识库", "单独查知识库", "检索知识库", "搜索知识库", "查医学资料", "查文献")),
    ("red_flag_screen", ("只查危险信号", "单独查危险信号", "危险信号筛查", "红旗筛查", "筛查红旗")),
    ("differential", ("只做鉴别诊断", "单独做鉴别诊断", "只做鉴别", "单独鉴别", "鉴别诊断", "鉴别")),
    ("treatment_ref", ("只做治疗方向", "单独做治疗方向", "只看治疗", "只做治疗参考", "治疗方向", "治疗参考", "治疗建议参考")),
    ("medication_review", ("只查用药", "单独查用药", "只做用药核对", "单独用药核对", "用药核对", "用药清单", "药物核对", "用药审查")),
    ("auxiliary", ("只做检查建议", "单独做检查建议", "只查检查", "检查建议", "辅助检查建议")),
    ("patient_edu", ("只做患者教育", "单独做患者教育", "只做健康教育", "健康教育", "患者能看懂", "患者教育", "通俗解释")),
    ("history", ("只写现病史", "单独写现病史", "现病史",)),
    ("lesion_desc", ("只写皮损描述", "单独写皮损描述", "皮损描述", "皮损记录", "皮损情况", "皮损表现")),
    ("referral", ("只写转诊", "单独写转诊", "转诊摘要", "转诊")),
    ("followup", ("只写随访", "单独写随访", "随访计划", "随访记录", "随访")),
    ("summary", ("只写病例摘要", "单独写病例摘要", "病例摘要", "病历摘要", "摘要")),
    ("case_organizer", ("只整理病历", "单独整理病历", "只写病历", "单独写病历", "帮我写病历", "整理一份病历", "病例整理")),
    ("complete_analysis", ("确认提交", "开始完整分析", "开始诊断辅助", "进行完整分析", "完整病例分析")),
    ("case_write", ("写一份", "写病例", "写个病例", "写一版", "门诊病历", "病例模板", "病历模板",
                    "进入诊断", "开始诊断", "辅助评估", "建立病例", "建立评估病例")),
]
# 医学判断类：LLM 不可用 → llm_unavailable（绝不本地编造医学内容）
MEDICAL_DOC_TASKS = {"differential", "treatment_ref", "medication_review", "patient_edu", "auxiliary"}
# v67 最终 RAG：医学判断任务检索本地书籍时的主题词（与病例症状拼接为查询）
_DOC_SEARCH_HINTS = {"differential": "鉴别诊断", "treatment_ref": "治疗 参考方案",
                     "medication_review": "用药 禁忌 注意事项", "patient_edu": "患者教育 恢复 注意",
                     "auxiliary": "辅助检查 检查目的 适用条件"}
# 文书整理类：LLM 不可用 → 本地模板整理（只重组用户已提供的字段，缺的一律「未提供」）
TEMPLATE_DOC_TASKS = {"history", "lesion_desc", "summary", "referral", "followup"}
DOC_TASKS = MEDICAL_DOC_TASKS | TEMPLATE_DOC_TASKS

TASK_LABELS = {
    "complete_analysis": "完整病例分析", "settings_control": "程序设置", "context_compress": "上下文压缩",
    "case_organizer": "病历整理",
    "medical_kb_search": "知识库检索", "red_flag_screen": "危险信号筛查", "auxiliary": "检查建议", "case_edit": "病例编辑", "history": "现病史整理",
    "lesion_desc": "皮损描述", "differential": "鉴别分析", "treatment_ref": "治疗参考",
    "medication_review": "用药核对", "patient_edu": "患者健康教育", "referral": "转诊摘要",
    "followup": "随访记录", "summary": "病例摘要", "report_export": "报告导出",
    "case_compare": "病例对比", "image_analyze": "图片分析",   # v68 新工具
}


# v64：闲聊/能力询问——只回复不调工具。工作类任务词优先级更高（「你好，帮我写病例」→ case_write）
_GREET_EXACT = {"hi", "hello", "hey", "你好", "您好", "在吗", "在么", "嗨", "hello？", "hi？"}
_SMALLTALK_KEYS = ("你是谁", "你能做什么", "你能干什么", "你叫什么", "介绍你能", "介绍下你",
                   "你会做什么", "你会什么", "你有什么功能", "有什么功能", "你能帮我做什么",
                   "怎么使用", "这是什么软件", "你负责什么")


def _is_smalltalk(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t.lower() in _GREET_EXACT:
        return True
    return any(k in t for k in _SMALLTALK_KEYS)


# v64-P2：计划确认——医生回复「确认」等短语视为同意执行；其他输入一律按补充/修正处理
# （精确匹配防误伤：「好的，另外患者还有头痛」是补充而非确认）。
_CORE_CONFIRMS = ("确认", "确认执行", "同意", "没问题", "可以了", "开始吧", "执行吧",
                  "就这样", "好的", "可以开始", "继续执行", "ok", "go")


def _is_confirm(text: str) -> bool:
    t = (text or "").strip().lower().rstrip("。，！!？?~ ")
    return t in _CORE_CONFIRMS


def _classify_task(text: str) -> str:
    """自然语言指令 → 任务类型（纯规则，离线可用；LLM 分类会引入不可控依赖）。"""
    t = (text or "").strip()
    if not t:
        return "case_write"
    for task, keys in TASK_PATTERNS:
        if any(k in t for k in keys):
            # 「只写现病史，不要生成诊断」这类指令里可能同时出现「鉴别/治疗」否定词——
            # 以显式任务动词为准：句首/开头出现「写/整理/改成」+ 目标名词时优先匹配该目标。
            return task
    if re.match(r"^对比\s*[0-9a-fA-F]{6,12}$", t):
        return "case_compare"   # v68：带病例 ID 的精确对比指令
    if _is_smalltalk(t):
        return "smalltalk"
    return "case_write"


# v67：对话驱动控制指令——医生在任意阶段用自然语言改变任务/暂停/撤销，
# Agent 按指令切换而不是强制继续原步骤。全部零 LLM 依赖（离线即时响应）。
_AGENT_COMMANDS = {
    "skip_3d": ("跳过3D", "跳过3d", "不用3D", "不用3d", "不使用3D", "不使用3d",
                "不用人体模型", "不用模型选点", "先不选部位"),
    "add_image": ("补充图片", "添加图片", "补充一张图片", "添加一张图片",
                  "补充照片", "添加照片", "上传图片", "上传照片"),
    "reask": ("重新询问患者", "重新追问", "重新梳理缺失", "问我还缺什么", "还缺什么信息"),
    "undo_last": ("撤销刚才的信息", "撤销刚才补充", "撤销上一条信息", "撤销刚才那条"),
    "save_case": ("保存当前病例", "保存病例草稿", "保存当前草稿", "把病例保存", "先保存病例"),
    # v69：显式界面导航指令（零 LLM，即时切换工作区）。词表排在 skip_3d 之后，
    # 避免「不用人体模型」被 open_model 抢先匹配。
    "back_to_chat": ("返回对话", "回到对话", "返回聊天", "回到聊天", "回到主对话", "回到对话框"),
    "show_sources": ("查看刚才的引用", "查看引用", "刚才的引用", "引用详情", "看引用", "查看文献引用"),
    "open_model": ("打开人体模型", "打开3D", "打开3d", "打开三维模型", "打开模型选点",
                   "人体模型选点", "去选部位", "打开人体图"),
    "open_analysis": ("打开分析结果", "查看分析结果", "查看完整分析", "打开结果窗口"),
}


# v69：显式导航指令 → (前端动作, Agent 回复文案)
_NAV_COMMANDS = {
    "back_to_chat": ("navigate_to_chat",
                     "已返回对话窗口。对话始终是主工作区，病例、3D 与引用面板的内容都不会丢失。"),
    "show_sources": ("navigate_to_sources",
                     "已为您打开当前病例的引用资料区（含资料名/版本/章节与引用片段）。"
                     "引用只来自本地医学知识库，未覆盖时如实标注，不编造。"),
    "open_model": ("navigate_to_3d",
                   "已打开 3D 人体模型。它是可选辅助工具：选中部位会自动写入当前病例草稿的"
                   "「身体部位」（来源标注为 3D 模型选择），选完点「完成并返回对话」即可继续。"),
    "open_analysis": ("navigate_to_analysis",
                      "已定位到最近一次分析结果。可继续在对话中让我整理病例、做鉴别方向或用药核对。"),
}


def _is_diagnosis_entry(text: str) -> bool:
    s = (text or "").strip().lower()
    return s in {"进入诊断", "开始诊断", "进行诊断", "新建诊断", "开始一次诊断"}


def _classify_agent_command(text: str) -> str:
    """对话控制指令识别（精确短语匹配，避免误伤病例描述文本）。"""
    t = (text or "").strip()
    if not t or len(t) > 60:
        return ""
    for cmd, keys in _AGENT_COMMANDS.items():
        if any(k in t for k in keys):
            return cmd
    return ""


def _route_agent_command(sess: dict, cmd: str) -> dict:
    """执行对话控制指令：全部以 agent 身份即时回复，不调 LLM、不动已有草稿/文书。"""
    now = datetime.now(timezone.utc).isoformat()
    reply_map = {
        "skip_3d": "好的，已跳过 3D 人体模型。它是可选辅助工具，不选部位完全不影响评估流程——"
                   "您直接用文字描述身体部位（如「左手背」）即可，病例中会标注来源为「医生输入」。",
        "add_image": "好的，可以补充图片。请点击输入框上方的「📎 图片附件」按钮选择照片"
                     "（png/jpg/webp），随下一条消息一起提交；我会在病例「辅助检查」中引用。",
        "save_case": None,   # 特殊处理
        "reask": None,
        "undo_last": None,
    }
    if cmd == "save_case":
        _sync_record(sess)
        _store_sess(sess)
        sess["reply"] = {"content": "已保存当前病例草稿（右侧「皮肤科门诊病历」面板可查看与继续编辑）。"
                                    "保存的是草稿状态，导出正式病例仍需医生确认。",
                         "generated_by": "agent", "model": "", "task_type": "case_edit",
                         "plan": False, "missing": sess.get("missing") or [], "disclaimer": "", "at": now}
        sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "agent-cmd",
                            "task_type": "case_edit", "error": "", "at": now}
        sess.update({"phase": "awaiting_review", "workflow_state": "awaiting_review"})
        sess["message"] = ""
        _bump_result_version(sess)
        return _snapshot(sess)
    if cmd == "reask":
        sess["plan_confirmed"] = False   # 允许重新出计划/追问
        sess.pop("planned_task", None)
        return _route_clarify(sess)
    if cmd == "undo_last":
        prev = sess.get("case_prev")
        if not isinstance(prev, dict) or not prev:
            content = "没有可撤销的对话补充信息（医生手动修改过的字段不受影响）。"
        else:
            sess["case"] = dict(prev)
            sess["missing"] = compute_missing(sess["case"])
            _sync_record(sess)
            _store_sess(sess)
            content = ("已撤销最近一次对话补充的信息，病例草稿已回退到补充前状态；"
                       "医生手动修改/确认过的字段未被覆盖。需要恢复的话请重新描述。")
        sess["reply"] = {"content": content, "generated_by": "agent", "model": "",
                         "task_type": "case_edit", "plan": False,
                         "missing": sess.get("missing") or [], "disclaimer": "", "at": now}
        sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "agent-cmd",
                            "task_type": "case_edit", "error": "", "at": now}
        sess.update({"phase": "awaiting_review", "workflow_state": "awaiting_review"})
        sess["message"] = ""
        _bump_result_version(sess)
        return _snapshot(sess)
    if cmd in _NAV_COMMANDS:
        # v69：显式导航指令——只切工作区 + 回一句说明，不动草稿/文书，不调 LLM
        nav_action, nav_content = _NAV_COMMANDS[cmd]
        sess["nav_intent"] = nav_action
        sess["reply"] = {"content": nav_content, "generated_by": "agent", "model": "",
                         "task_type": sess.get("task_type") or "case_write", "plan": False,
                         "missing": sess.get("missing") or [], "disclaimer": "", "at": now}
        sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "agent-cmd",
                            "task_type": sess.get("task_type") or "case_write", "error": "", "at": now}
        sess.pop("llm_error", None)
        sess["message"] = ""
        _bump_result_version(sess)
        return _snapshot(sess)
    content = reply_map.get(cmd) or "指令已收到。"
    sess["reply"] = {"content": content, "generated_by": "agent", "model": "",
                     "task_type": sess.get("task_type") or "case_write", "plan": False,
                     "missing": sess.get("missing") or [], "disclaimer": "", "at": now}
    sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "agent-cmd",
                        "task_type": sess.get("task_type") or "case_write", "error": "", "at": now}
    sess.pop("llm_error", None)
    sess["message"] = ""
    _bump_result_version(sess)
    return _snapshot(sess)


def _llm_ready() -> tuple:
    """返回活动连接快照；空配置时按旧环境变量兼容回退。"""
    cfg = load_llm_config()
    base_url = (cfg.get("base_url") or os.environ.get("LLM_BASE_URL", "")).strip().rstrip("/")
    api_key = cfg.get("api_key") or os.environ.get("LLM_API_KEY", "").strip()
    model = cfg.get("model") or os.environ.get("LLM_MODEL", "").strip()
    return base_url, api_key, model, cfg.get("thinking_level", "")


def _snapshot_llm_unavailable(sess: dict, err: str, message: str, retryable: bool = False) -> dict:
    """v58 防伪装快照：LLM 不可用 → 不生成任何疾病/药物/治疗方案内容。
    v64：retryable 区分「未配置」（须先配置连接，不重试）与「已配置但请求失败/超时」
    （网关抖动常见，进对话流错误卡并给「重试」按钮，workflow_state=llm_error）。"""
    sess["draft"] = None
    sess["doc"] = None
    sess["llm_error"] = err
    sess["llm_meta"] = {"ok": False, "source": "none", "model": "", "error": err,
                        "task_type": sess.get("task_type") or "case_write",
                        "at": datetime.now(timezone.utc).isoformat()}
    wf = "llm_error" if retryable else "llm_unavailable"
    sess.update({"phase": wf, "workflow_state": wf})
    sess["question"] = ""
    sess["message"] = (message + "，可点击对话中的「重试」再次发送") if retryable else message
    _bump_result_version(sess)
    return _snapshot(sess)


# v64：纯任务指令字符（无任何真实病例描述）——「请帮我写一份病例」应澄清，
# 「右前臂红斑瘙痒7天」这类描述性文本必然含表外字符，直接进入草稿生成
_CASE_DIRECTIVE_CHARS = set(
    "请帮忙我给可以能不能想要需准备开始先再然后写一份个病例门诊皮肤科记录草稿模板谢谢好了新的"
    "，。！？、；：,!?;: 　-—·（）()「」『』\"'")


def _is_directive_only(text: str) -> bool:
    t = (text or "").strip()
    return not t or set(t) <= _CASE_DIRECTIVE_CHARS


# v65：医生在对话里要求打开/新建病例窗口——Agent 决定开窗（响应带 open_case 标志，
# 客户端 MainWindow._on_chat_done 据此自动打开独立病例窗口；任务路由不受影响）。
_OPEN_CASE_KEYS = ("病例窗口", "病历窗口", "打开病例", "打开病历单", "新建一份病例", "新开一份病例",
                   "新建门诊病历", "建一份病例", "开一份病例", "写一份门诊病历", "写份门诊病历",
                   "新建一份门诊病历", "帮我写一份门诊病历", "新建一个病例", "打开门诊病历")


def _wants_case_window(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(k in t for k in _OPEN_CASE_KEYS)


def _case_has_content(case: dict) -> bool:
    """病例是否已有真实内容：结构化资料/任一字段被提取即有；symptoms 兜底整段时看是否纯指令。"""
    if case.get("clinic_payload"):
        return True
    if any(str(case.get(k) or "").strip() for k in
           ("chief_complaint", "body_region", "duration", "triggers", "past_history",
            "allergies", "current_medications")):
        return True
    sym = str(case.get("symptoms") or "").strip()
    return bool(sym) and not _is_directive_only(sym)


def _tool_traced(sess: dict, snap: dict, tool: str) -> dict:
    """v64：Agent 按需调用工具——真实发生的工具调用记录进 tool_history（前端可展开过程消息）。
    v68（计划 §五.1/§七.后端4）：统一协议元数据（risk_level/确认/来源要求）+ 任务目标 +
    输入/输出摘要 + RAG 来源 + 病例版本，全部可追踪到 case_id；失败调用不伪装成功。"""
    reg = spec(tool)
    if tool not in TOOL_REGISTRY:   # v68：传入是 route 名（如 case_writer）→ 反查注册表条目取中文标签
        by_route = next((t for t in TOOL_REGISTRY.values()
                         if t.get("route") == tool
                         or (str(t.get("route", "")).startswith("doc_task:")
                             and str(t["route"]).split(":")[-1] == tool)), None)
        if by_route:
            reg = by_route
    case_now = sess.get("case") or {}
    in_digest = "；".join([str(case_now.get(k) or "").strip() for k in
                           ("chief_complaint", "symptoms", "body_region") if str(case_now.get(k) or "").strip()])
    doc = snap.get("doc") or {}
    out_digest = ""
    if doc.get("content"):
        out_digest = str(doc.get("task_type") or sess.get("task_type") or "") + "：" + str(doc["content"])[:100]
    elif snap.get("draft"):
        out_digest = "分析草稿：" + str((snap["draft"] or {}).get("model_version") or "awaiting_review")
    elif snap.get("reply", None) is not None:
        out_digest = str((snap.get("reply") or {}).get("content") or "")[:100]
    cites = []
    for c in (doc.get("citations") or [])[:5]:
        if isinstance(c, dict) and c.get("citation_id"):
            cites.append(c["citation_id"])
    if not cites and isinstance(snap.get("book_citations"), list):
        cites = [c.get("citation_id") for c in snap["book_citations"][:5] if isinstance(c, dict)]
    entry = {"tool": tool, "label": reg.get("label", tool),
             "risk_level": reg.get("risk_level", "low"),
             "source_required": reg.get("source_required", False),
             "requires_confirmation": reg.get("requires_confirmation", False),
             "confirmed": bool(sess.get("plan_confirmed")),   # 本轮执行是否经过医生确认（计划卡）
             "case_version": sess.get("result_version", "r0"),
             "goal": str(sess.get("task_text") or "")[:80],
             "input_digest": in_digest[:120],
             "output_digest": out_digest[:140],
             "sources": cites,
             "task": sess.get("task_type") or "case_write",
             "at": datetime.now(timezone.utc).isoformat()}
    hist = sess.setdefault("tool_history", [])
    hist.append(entry)
    sess["tool_history"] = hist[-20:]
    snap["tool_history"] = sess["tool_history"]   # _snapshot 生成于路由内部，补齐过程字段
    audit("tool_call", {"case_id": sess.get("case_id"), "tool": tool,
                        "risk": entry["risk_level"], "confirmed": entry["confirmed"],
                        "goal": entry["goal"], "sources": cites,
                        "case_version": entry["case_version"]})
    return snap


# v64-P2：各任务的「准备执行的动作」描述（计划确认卡第二行）
_PLAN_ACTIONS = {
    "case_write": "建立皮肤科辅助评估病例：整理已识别信息 → 检索知识库 → 生成医疗辅助评估（非确定性诊断），供您审核",
    "complete_analysis": "执行完整病例分析：红旗筛查 → 知识库检索 → 鉴别、治疗方向与用药参考草稿（供医生审核）",
    "case_organizer": "只整理当前病例字段为门诊病历文本；不执行鉴别诊断、治疗方向或用药分析",
    "privacy_deidentify": "生成当前病例的本地脱敏预览，不修改原病例内容",
    "body_region_3d": "打开 3D 人体模型供您选择身体部位，不执行病例分析",
    "medical_kb_search": "仅检索本地医学知识库并返回引用资料，不生成医学判断",
    "red_flag_screen": "仅筛查当前病例危险信号与升级提示，不生成诊断、治疗或用药结果",
    "auxiliary": "仅生成建议检查及其目的/适用条件，供医生核对",
    "case_edit": "按您的要求修订当前病例草稿（结构化校验后重新呈现）",
    "report_export": "汇总当前病例与草稿，本地组装导出报告（不经过 LLM 重新生成）",
    "history": "检索本地书籍依据并生成现病史整理文书",
    "lesion_desc": "检索本地书籍依据并生成皮损描述文书",
    "summary": "检索本地书籍依据并生成病例摘要",
    "referral": "检索本地书籍依据并生成转诊摘要",
    "followup": "检索本地书籍依据并生成随访记录",
    "differential": "检索本地医学书籍并生成鉴别分析参考（真实引用标注）",
    "treatment_ref": "检索本地医学书籍并生成治疗参考（供医生决策，不做处方）",
    "medication_review": "检索本地医学书籍并生成用药核对清单（须医生确认）",
    "patient_edu": "检索本地医学书籍并生成患者健康教育材料",
}


# v67：辅助评估最低信息条件——缺这三项时确认卡明示，Agent 对话逐项追问；齐了直接进入下一阶段
_MIN_ANALYSIS_FIELDS = ("symptoms", "body_region", "duration")
_TASK_REQUIRED_KEYS = {
    "case_organizer": ("symptoms", "body_region", "duration"),
    "differential": _MIN_ANALYSIS_FIELDS, "treatment_ref": _MIN_ANALYSIS_FIELDS,
    "medication_review": ("symptoms",), "auxiliary": _MIN_ANALYSIS_FIELDS,
    "red_flag_screen": ("symptoms",), "medical_kb_search": ("symptoms",),
}
_TASK_ALLOWED_TOOLS = {
    "case_organizer": ("case_extractor", "dynamic_clarifier", "case_organizer"),
    "history": ("case_extractor", "dynamic_clarifier", "case_organizer"),
    "lesion_desc": ("case_extractor", "dynamic_clarifier", "case_organizer"),
    "summary": ("case_extractor", "dynamic_clarifier", "case_organizer"),
    "referral": ("case_extractor", "dynamic_clarifier", "case_organizer"),
    "followup": ("case_extractor", "dynamic_clarifier", "followup_planner"),
    "differential": ("case_extractor", "dynamic_clarifier", "medical_kb_search", "differential_assistant"),
    "treatment_ref": ("case_extractor", "dynamic_clarifier", "medical_kb_search", "exam_advisor"),
    "medication_review": ("case_extractor", "dynamic_clarifier", "medical_kb_search", "medication_safety_checker"),
    "auxiliary": ("case_extractor", "dynamic_clarifier", "medical_kb_search", "exam_advisor"),
    "patient_edu": ("case_extractor", "patient_communicator"),
    "image_analyze": ("image_analyzer",), "case_compare": ("case_comparator",),
    "report_export": ("case_exporter",), "privacy_deidentify": ("privacy_deidentifier",),
    "body_region_3d": ("body_region_3d",), "medical_kb_search": ("medical_kb_search",),
    "red_flag_screen": ("red_flag_screen",),
    "complete_analysis": ("case_extractor", "dynamic_clarifier", "red_flag_screen", "medical_kb_search", "case_organizer", "differential_assistant", "exam_advisor", "medication_safety_checker"),
    "case_write": ("case_extractor", "dynamic_clarifier"),
}


def _plan_gate(sess: dict, task: str) -> bool:
    """v64-P2：明确工作要求 → Agent 先说明理解到的任务与准备执行的动作，等医生确认再执行。
    - demo 离线演示不拦（保持演示流程原样）
    - 同一任务已确认（plan_confirmed=True）→ 直接执行（后续补充/微调不再反复确认）
    - 任务类型变更（如病例写作 → 鉴别分析）→ 重新出计划
    - 待确认期间医生发来补充/修正 → 更新理解后再次出计划（不执行）
    返回 True 表示本轮已出计划（不执行工作）。"""
    if sess.get("demo"):
        return False
    if task != (sess.get("planned_task") or ""):
        sess["planned_task"] = task
        sess["plan_confirmed"] = False
    if sess.get("plan_confirmed"):
        # v67 需求3：确认时仍缺最低分析条件（症状/身体区域/病程）且尚无草稿 → 转入逐项追问，
        # 不带着关键缺口直接出评估；追问一轮后医生再确认则放行（草稿如实标注缺失）。
        if task == "case_write" and sess.get("draft") is None and not sess.get("demo") \
                and not sess.get("clarify_min_gate"):
            case_now = sess.get("case") or {}
            lacking_min = [lb for key, lb, _ in REQUIRED_FIELDS
                           if key in _MIN_ANALYSIS_FIELDS
                           and not str(case_now.get(key) or "").strip()]
            if lacking_min:
                sess["clarify_min_gate"] = True   # 追问一轮后（补充或再确认）即放行，不反复拦截
                now = datetime.now(timezone.utc).isoformat()
                sess["reply"] = {
                    "content": "\n".join([
                        "要开始辅助评估，还需要您补充：" + "、".join(lacking_min) + "。",
                        "可以直接用一句话描述（例如「部位在左手背，持续两周」），我会把信息并入病例后自动继续；",
                        "也可以回复「确认」，我先按现有信息生成医疗辅助评估，缺失部分会在评估中如实标注、不编造。",
                    ]),
                    "generated_by": "agent", "model": "", "task_type": "case_write",
                    "plan": False, "missing": sess.get("missing") or [], "disclaimer": "", "at": now}
                sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "agent-plan",
                                    "task_type": "case_write", "error": "", "at": now}
                sess.pop("llm_error", None)
                sess.update({"phase": "clarifying", "workflow_state": "clarifying"})
                sess["message"] = ""
                _bump_result_version(sess)
                return True
        return False
    text = str(sess.get("task_text") or "").strip()
    understanding = text[:140] if text else "整理当前病例并生成内容"
    # v67：确认卡带信息盘点——已识别到什么、还缺什么；症状/身体区域/病程是最低分析条件，
    # 缺这三项时明确说明（医生可选择补充后开始，或确认后由 Agent 在对话中逐项追问）。
    case = sess.get("case") or {}
    known = [(label, str(case.get(key) or "")) for key, label, _ in REQUIRED_FIELDS
             if str(case.get(key) or "").strip()]
    known_txt = "；".join(f"{lb}：{v[:40]}" for lb, v in known[:4]) or "（暂无）"
    missing = sess.get("missing") or []
    if task != "complete_analysis" and task in _TASK_REQUIRED_KEYS:
        target_missing = _task_missing(sess, task)
        if target_missing:
            missing = target_missing
    min_labels = [label for key, label, _ in REQUIRED_FIELDS if key in _MIN_ANALYSIS_FIELDS]
    lacking_min = [lb for lb in min_labels if lb in missing]
    lacking_txt = "；".join(missing[:5]) if missing else "（无）"
    # 初步 RAG（v67 第一阶段）：计划期就检索本地知识库，辅助判断追问重点与后续评估方向。
    # 结果缓存（同一任务只检索一次）；失败/未装知识库 → 如实说明，绝不影响计划流程。
    rag_line = ""
    rag_task_key = sess.get("rag_prelim_task")
    if rag_task_key != task and task not in ("privacy_deidentify", "body_region_3d", "red_flag_screen"):
        q = "；".join(str(case.get(k) or "") for k in ("chief_complaint", "symptoms", "body_region") if case.get(k))[:120]
        if q.strip():
            try:
                cites = kb_search(q).get("citations", [])
                sess["rag_prelim"] = {"at": datetime.now(timezone.utc).isoformat(),
                                      "citations": [{"citation_id": c.get("citation_id"),
                                                     "book_title": c.get("book_title"),
                                                     "author": c.get("author"),
                                                     "publisher": c.get("publisher"),
                                                     "section": c.get("section"),
                                                     "page_hint": c.get("page_hint"),
                                                     "quoted_text": str(c.get("quoted_text") or "")[:160],
                                                     "retrieved_at": datetime.now(timezone.utc).isoformat()}
                                                    for c in cites]}
            except Exception as exc:  # noqa: BLE001
                audit("rag_prelim_failed", str(exc)[:120])
                sess["rag_prelim"] = {"at": datetime.now(timezone.utc).isoformat(), "citations": []}
            sess["rag_prelim_task"] = task
    prelim = (sess.get("rag_prelim") or {}).get("citations") or []
    if prelim:
        names = "；".join(f"{c['book_title']}·{c['section']}" for c in prelim[:3] if c.get("book_title"))
        rag_line = "📚 初步知识库检索：" + (f"已找到 {len(prelim)} 条相关资料（{names}），将用于后续辅助评估与引用。" if names else f"已找到 {len(prelim)} 条资料。")
    else:
        rag_line = "📚 初步知识库检索：暂未检索到直接相关资料（评估中将如实说明，不编造引用）。"
    lines = [
        "我理解到的任务：「" + TASK_LABELS.get(task, task) + "」",
        "准备执行的动作：" + _PLAN_ACTIONS.get(task, "整理病例信息并生成目标内容，供您审核"),
        "本次允许调用：" + ("、".join(_TASK_ALLOWED_TOOLS.get(task, ())) or "目标工具"),
        "本次不会调用：" + ("完整病例分析、诊断、治疗或用药" if task not in ("complete_analysis", "case_write") else "超出完整分析范围的工具"),
        "已识别到：" + known_txt,
        "本轮补充/指令：" + (text[:160] if text else "（无）"),
        "还缺少：" + lacking_txt,
    ]
    if lacking_min:
        lines.append("其中「" + "、".join(lacking_min) + "」是开始辅助评估的最低信息要求；您可以现在补充，或先确认，我会再向您追问。")
    if rag_line:
        lines.append(rag_line)
    lines += ["", "是否开始？回复「确认」执行；也可以先补充或修正病例信息，我会更新理解后再等你确认。"]
    sess["reply"] = {"content": "\n".join(lines), "generated_by": "agent", "model": "",
                     "task_type": task, "plan": True, "missing": sess.get("missing") or [],
                     "disclaimer": "", "at": datetime.now(timezone.utc).isoformat()}
    sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "agent-plan",
                        "task_type": task, "error": "",
                        "at": datetime.now(timezone.utc).isoformat()}
    sess.pop("llm_error", None)
    sess.update({"phase": "planning", "workflow_state": "planning"})
    sess["message"] = ""
    _bump_result_version(sess)
    return True


def _screen(sess: dict) -> dict:
    """v59：任务路由入口（原「完整性检查→强制追问」固定流程已删除）。
    按任务类型分流：闲聊回复 / 病例分析 / 病例编辑 / 文书与医学判断。
    v64：Agent 先回复再决定工具——闲聊只回复零工具；信息为空的开放写作请求先澄清。"""
    case = sess["case"]
    sess["red_flags"] = detect_red_flags(case)
    sess["missing"] = compute_missing(case)
    sess.pop("question", None)
    sess.pop("reply", None)   # v64-P2：清上一轮回复卡（计划/澄清/闲聊）——工作产物不得被 stale reply 遮蔽
    sess.pop("nav_intent", None)   # v69：显式导航指令只对本轮生效（防陈旧指令重复跳转）
    task = sess.get("task_type") or "case_write"
    cmd = _classify_agent_command(str(sess.get("task_text") or ""))
    if cmd and task not in ("smalltalk",):
        return _route_agent_command(sess, cmd)             # v67：对话控制指令即时切换/暂停
    if task == "smalltalk":
        return _route_smalltalk(sess)                      # 只回复，零工具
    if task == "settings_control":
        return _route_settings(sess)   # v72：设置修改即时执行（低风险、非医疗、零 LLM 依赖）
    if task == "context_compress":
        return _route_context_compress(sess)   # v73：手动压缩上下文（规则式摘要，如实标注）
    # case_write 且医生还没给出任何真实病例描述（非 demo）→ 先澄清需要哪些信息（v64）。
    # 顺序：先澄清、后计划确认——信息都没有，计划无从谈起；补充后进入计划确认环节。
    if task == "case_write" and sess.get("draft") is None and not sess.get("demo") \
            and not _case_has_content(case):
        return _route_clarify(sess)
    if _plan_gate(sess, task):                             # v64-P2：先计划确认，等医生回复再执行
        return _snapshot(sess)
    missing = _task_missing(sess, task)
    # 目标任务的缺失项已在计划卡中提示；保持既有 handler 的安全降级语义，
    # 不把单独医学任务误转成通用 clarifying 状态。
    if task != "complete_analysis" and missing and task in ("case_organizer",):
        return _route_task_clarify(sess, task, missing)
    if task == "medical_kb_search":
        return _tool_traced(sess, _route_kb_search(sess), "medical_kb_search")
    if task == "red_flag_screen":
        return _tool_traced(sess, _route_red_flag_screen(sess), "red_flag_screen")
    if task == "privacy_deidentify":
        return _tool_traced(sess, _route_privacy_deidentify(sess), "privacy_deidentifier")
    if task == "body_region_3d":
        return _tool_traced(sess, _route_body_region_3d(sess), "body_region_3d")
    if task == "case_organizer":
        return _tool_traced(sess, _route_case_organizer(sess), "case_organizer")
    if task == "complete_analysis" and sess.get("plan_confirmed"):
        task = "case_write"
        sess["task_type"] = task
    if task == "case_edit":
        return _tool_traced(sess, _route_case_edit(sess), "case_editor")
    if task == "report_export":
        return _tool_traced(sess, _route_report_export(sess), "report_export")
    if task == "image_analyze":
        return _tool_traced(sess, _route_image_analysis(sess), "image_analyzer")
    if task == "case_compare":
        return _tool_traced(sess, _route_case_compare(sess), "case_comparator")
    if task in DOC_TASKS:
        # v68：工具名按注册表协议映射（medication_review 此前误标 case_writer）
        return _tool_traced(sess, _route_doc_task(sess, task), tool_for_task(task))
    return _tool_traced(sess, _analyze_case(sess), "case_writer")


def _agent_reply(sess: dict, content: str, task_type: str, card=None, disclaimer: str = "") -> dict:
    """v68：零 LLM 工具回复卡通用包装（与 _route_agent_command 同契约，不动已有 draft/doc）。"""
    now = datetime.now(timezone.utc).isoformat()
    reply = {"content": content, "generated_by": "agent", "model": "", "task_type": task_type,
             "plan": False, "missing": sess.get("missing") or [], "disclaimer": disclaimer, "at": now}
    if card:
        reply["card"] = card
    sess["reply"] = reply
    sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "agent-cmd",
                        "task_type": task_type, "error": "", "at": now}
    sess.pop("llm_error", None)
    sess["message"] = ""
    _bump_result_version(sess)
    return _snapshot(sess)


def _route_kb_search(sess: dict) -> dict:
    """独立知识库检索：只返回引用，不生成诊断/治疗/用药内容。"""
    case = sess.get("case") or {}
    query = str(sess.get("task_text") or "").strip()
    query = re.sub(r"^(只|仅|单独)?(查|检索|搜索)(知识库|医学资料|文献)[:：]?", "", query).strip()
    query = query or "；".join(str(case.get(k) or "") for k in ("chief_complaint", "symptoms", "body_region") if case.get(k))
    citations = _stamp_citations(_agent_kb_search(query[:160], 5)) if query else []
    content = "知识库检索结果（仅返回资料，不生成医学判断）：\n" + ("\n".join(
        f"- [{c.get('citation_id')}] {c.get('document_title') or c.get('book_title') or '资料'}：{c.get('page_or_section') or c.get('section') or ''}"
        for c in citations) if citations else "- 未检索到相关资料（不编造引用）")
    return _agent_reply(sess, content, "medical_kb_search", card={"type": "kb_search", "citations": citations},
                        disclaimer="仅为本地知识库资料检索，不构成诊断、治疗或用药建议。")


def _route_red_flag_screen(sess: dict) -> dict:
    flags = detect_red_flags(sess.get("case") or {})
    content = "危险信号筛查（仅筛查，不生成诊断/治疗/用药）：\n" + ("\n".join(f"- {x}" for x in flags) if flags else "- 当前规则未检出危险信号；仍需医生结合实际判断。")
    return _agent_reply(sess, content, "red_flag_screen", card={"type": "red_flag_screen", "red_flags": flags},
                        disclaimer="规则筛查仅供医生参考，不能替代临床评估。")


def _route_privacy_deidentify(sess: dict) -> dict:
    """只生成脱敏预览；原始病例不改，身份字段只在返回预览中替换。"""
    case = sess.get("case") or {}
    masked = dict(case)
    masked_fields = []
    for key in ("name", "patient_name", "phone", "mobile", "id_number", "identity_card", "address", "no"):
        if key in masked and str(masked.get(key) or "").strip():
            masked[key] = "[已脱敏]"
            masked_fields.append(key)
    patient = masked.get("clinic_payload", {}).get("patient") if isinstance(masked.get("clinic_payload"), dict) else None
    if isinstance(patient, dict):
        cp = dict(masked["clinic_payload"]); cp["patient"] = dict(patient)
        for key in ("name", "no", "phone", "mobile", "id_number", "address"):
            if key in cp["patient"] and str(cp["patient"].get(key) or "").strip():
                cp["patient"][key] = "[已脱敏]"; masked_fields.append("clinic_payload.patient." + key)
        masked["clinic_payload"] = cp
    return _agent_reply(sess, "已生成脱敏预览，原病例未修改。\n脱敏字段：" + ("、".join(masked_fields) if masked_fields else "未发现需替换的直接身份字段"),
                        "privacy_deidentify", card={"type": "privacy_deidentify", "deidentified": masked, "masked_fields": masked_fields},
                        disclaimer="这是脱敏预览；发送或导出前请由医生复核。")


def _task_missing(sess: dict, task: str) -> list:
    """按独立目标计算缺失字段；不会把目标任务升级为完整病例分析。"""
    case = sess.get("case") or {}
    labels = {key: label for key, label, *_ in REQUIRED_FIELDS}
    return [labels.get(key, key) for key in _TASK_REQUIRED_KEYS.get(task, ())
            if not str(case.get(key) or "").strip()]


def _route_task_clarify(sess: dict, task: str, missing: list) -> dict:
    label = TASK_LABELS.get(task, task)
    allowed = "、".join(_TASK_ALLOWED_TOOLS.get(task, ()))
    sess["reply"] = {"content": (
        f"要执行「{label}」，还需要补充：" + "、".join(missing) + "。\n"
        "请只补充这些信息；补充后我会重新展示本任务计划，不会自动进入完整病例分析。\n"
        f"本任务允许工具：{allowed or '当前功能'}。"),
        "generated_by": "agent", "model": "", "task_type": task, "plan": False,
        "missing": missing, "disclaimer": "", "at": datetime.now(timezone.utc).isoformat()}
    sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "task-clarify",
                        "task_type": task, "error": "", "at": datetime.now(timezone.utc).isoformat()}
    sess.update({"phase": "clarifying", "workflow_state": "clarifying", "task_type": task,
                 "planned_task": task, "plan_confirmed": False})
    sess["message"] = ""
    _bump_result_version(sess)
    return _snapshot(sess)


def _route_case_organizer(sess: dict) -> dict:
    """独立病历整理：仅输出文书，不触发完整诊断链。"""
    doc = _template_doc(sess, "summary")
    doc["task_type"] = "case_organizer"
    doc["content"] = doc["content"].replace("病例摘要（整理）", "门诊病历整理")
    sess["doc"] = doc
    sess["reply"] = None
    sess["llm_meta"] = {"ok": True, "source": "template", "model": "", "generated_by": "case-organizer",
                        "task_type": "case_organizer", "error": "", "at": datetime.now(timezone.utc).isoformat()}
    sess.update({"phase": "doc_ready", "workflow_state": "doc_ready"})
    _sync_record(sess)
    _bump_result_version(sess)
    return _snapshot(sess)


def _route_body_region_3d(sess: dict) -> dict:
    sess["nav_intent"] = "navigate_to_3d"
    return _agent_reply(sess, "已准备打开 3D 人体模型。请选择身体部位，完成后返回对话；本次不会执行病例分析。",
                        "body_region_3d", card={"type": "body_region_3d"})
def _route_image_analysis(sess: dict) -> dict:
    """v68 图片分析工具（计划 §三.3，注册表 image_analyzer）：质量提示 + 可见特征协作采集。
    边界：仅供辅助不等于诊断；不默认执行（医生明确发起才分析）；图片不上传任何外部服务，
    只基于附件元数据与医生勾选特征工作；零 LLM 依赖——特征判断来自医生勾选，AI 不代看。"""
    atts = [a for a in (sess.get("attachments") or []) if isinstance(a, dict)]
    if not atts:
        return _agent_reply(
            sess, "当前会话还没有图片附件。请点击输入框上方的「📎 图片附件」按钮上传皮肤照片"
                  "（png/jpg/webp）随消息提交，然后再回复「分析图片」。",
            "image_analyze")
    names, hints = [], []
    for a in atts[-3:]:
        name = str(a.get("name") or a.get("filename") or "图片")
        names.append(name)
        try:
            size = int(a.get("size") or a.get("bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        if 0 < size < 20 * 1024:
            hints.append(f"「{name}」文件较小（约 {max(1, size // 1024)}KB），分辨率可能不足，特征判断可能受影响")
        if not str(a.get("taken_at") or a.get("shot_at") or "").strip():
            hints.append(f"「{name}」未记录拍摄时间/部位，建议在特征说明中补充")
    if not hints:
        hints.append("附件元信息完整；照片实际质量（光线/范围/清晰度）请医生结合原图判断。")
    features = ["红斑", "丘疹", "水疱", "脓疱", "鳞屑", "结痂", "渗出", "糜烂",
                "苔藓样变", "边界清楚", "边界不清", "对称分布", "单侧分布", "线状排列", "群集分布"]
    return _agent_reply(
        sess, "图片分析（辅助工具，仅供辅助，不等于诊断）——\n"
              "质量提示：" + "；".join(hints) + "。\n"
              "请在勾选卡中选出照片可见的皮损特征，或直接输入补充描述（如「分布：手背为主，伴瘙痒」）；"
              "提交后特征将写入病例「症状/皮损表现」，来源标注为医生输入。"
              "需要 AI 结合特征做进一步分析时，请再发起鉴别分析任务。",
        "image_analyze",
        card={"type": "image_analysis", "quality_hints": hints,
              "features": features, "attachments": names},
        disclaimer="图片分析仅供辅助，不等于诊断；特征以医生勾选为准。")


def _route_case_compare(sess: dict) -> dict:
    """v68 病例对比工具（计划 §四.2，注册表 case_comparator）：当前 vs 历史病例客观字段对比。
    只做字段级 变化/新增/缺失 对比（规则式零伪造）；改善/恶化等语义判断留给医生；零 LLM。"""
    text = str(sess.get("task_text") or "")
    m = re.search(r"([0-9a-f]{8,12})", text.lower())
    if not m:   # v68：确认轮 text=「确认」→ 回看上一轮真实指令（医生已给 ID，不应再让他选一次）
        m = re.search(r"([0-9a-f]{8,12})", str(sess.get("prev_task_text") or "").lower())
    sessions = _load_sessions()
    target = ""
    if m:
        key = m.group(1)
        target = next((cid for cid in sessions
                       if cid != sess["case_id"] and cid.startswith(key)), "")
    else:
        candidates = [cid for cid in sessions if cid != sess["case_id"]]
        if len(candidates) == 1:
            target = candidates[0]   # 仅一个历史病例时直接对比
    if not target:
        others = sorted((s for cid, s in sessions.items() if cid != sess["case_id"]),
                        key=lambda s: s.get("updated_at", ""), reverse=True)[:5]
        if not others:
            return _agent_reply(sess, "当前没有其他历史病例可对比。新建并填写病例后，再回复「对比病例」。",
                                "case_compare")
        lines = ["- {} ｜ {} ｜ 更新 {}".format(
            s.get("case_id"), str(s.get("label") or "")[:24],
            str(s.get("updated_at") or "")[:16].replace("T", " ")) for s in others]
        return _agent_reply(sess, "请选择要对比的历史病例，回复「对比 <病例ID>」：\n" + "\n".join(lines),
                            "case_compare")
    cur_case = sess.get("case") or {}
    hist_case = sessions[target].get("case") or {}
    changed, added, missing = [], [], []
    for key, label, *_ in REQUIRED_FIELDS:
        cv = str(cur_case.get(key) or "").strip()
        hv = str(hist_case.get(key) or "").strip()
        if cv and hv and cv != hv:
            changed.append(f"{label}：{hv[:60]} → {cv[:60]}")
        elif cv and not hv:
            added.append(f"{label}：{cv[:80]}")
        elif hv and not cv:
            missing.append(f"{label}：历史病例有、当前未提供")
    md = ("## 病例对比（客观字段级）\n\n"
          f"当前病例 `{sess['case_id']}` vs 历史病例 `{target}`\n\n"
          "### 变化（历史 → 当前）\n" + ("\n".join(f"- {x}" for x in changed) if changed else "- 无变化字段") +
          "\n\n### 当前新增\n" + ("\n".join(f"- {x}" for x in added) if added else "- 无") +
          "\n\n### 当前缺失（历史有）\n" + ("\n".join(f"- {x}" for x in missing) if missing else "- 无") +
          "\n\n---\n改善/恶化等临床语义判断由医生做出；本对比只呈现字段客观差异。")
    sess["doc"] = {"task_type": "case_compare", "content": md, "generated_by": "local",
                   "citations": [], "version": 1,
                   "disclaimer": "客观字段对比，不构成改善/恶化判断；临床结论以医生审核为准。",
                   "at": datetime.now(timezone.utc).isoformat()}
    sess["reply"] = None   # v68：清掉 plan gate 的确认卡残留，避免 reply 优先级压住本轮 doc
    sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "agent-cmd",
                        "task_type": "case_compare", "error": "", "at": datetime.now(timezone.utc).isoformat()}
    sess.pop("llm_error", None)
    sess["message"] = ""
    sess.update({"phase": "doc_ready", "workflow_state": "doc_ready"})
    _bump_result_version(sess)
    return _snapshot(sess)


def _route_report_export(sess: dict) -> dict:
    """报告导出：只在医生明确要求时执行；本地组装既有草稿/文书（不经过 LLM，不产生新医学内容）。
    无可导出内容 → 不生成空报告，明确提示先完成病例。"""
    draft = sess.get("draft") or {}
    doc_prev = sess.get("doc") or {}
    if not draft and not doc_prev:
        sess["doc"] = None
        sess["llm_meta"] = {"ok": False, "source": "none", "model": "", "error": "no_content",
                            "task_type": "report_export", "at": datetime.now(timezone.utc).isoformat()}
        sess.update({"phase": "screening", "workflow_state": "screening"})
        sess["message"] = "暂无可导出的病例内容，请先完成病例写作或整理"
        _bump_result_version(sess)
        return _snapshot(sess)
    case = sess.get("case") or {}
    rows = [
        ("主诉", case.get("chief_complaint")), ("症状", case.get("symptoms")),
        ("病程", case.get("duration")), ("诱因", case.get("trigger")),
        ("既往史", case.get("past_history")), ("过敏史", case.get("allergy")),
        ("当前用药", case.get("medication")), ("身体区域", case.get("body_region")),
    ]
    lines = ["## 病例报告（本地导出）", ""]
    lines += [f"- {label}：{(str(v).strip() if str(v or '').strip() else '未提供')}" for label, v in rows]
    if draft.get("case_summary"):
        lines += ["", "### AI 草稿摘要（医生审核状态：" + str(draft.get("review_status") or sess.get("review_status") or "pending") + "）",
                  str(draft.get("case_summary"))]
    if doc_prev.get("content"):
        lines += ["", "### 最近文书", str(doc_prev.get("content"))]
    lines += ["", "> 本报告由本地导出既有内容组装，未经 LLM 重新生成；正式病历须经医生签署。"]
    sess["doc"] = {"task_type": "report_export", "content": "\n".join(lines),
                   "generated_by": "local-export", "citations": [],
                   "disclaimer": "本地导出内容，正式病历须经医生签署",
                   "at": datetime.now(timezone.utc).isoformat()}
    sess["llm_meta"] = {"ok": True, "source": "template", "model": "", "generated_by": "local-export",
                        "task_type": "report_export", "error": "",
                        "at": datetime.now(timezone.utc).isoformat()}
    sess.update({"phase": "doc_ready", "workflow_state": "doc_ready"})
    sess["message"] = ""
    _bump_result_version(sess)
    return _snapshot(sess)


SMALLTALK_SYSTEM = (
    "你是 DermAgent（肤智），面向皮肤科医生的对话式病历助手。"
    "能力：根据医生口述撰写门诊病历草稿、整理现病史/皮损描述/病例摘要、鉴别分析参考、用药核对、"
    "患者健康教育、转诊与随访文书、按病例检索本地医学书籍依据；支持可选的 3D 人体模型点选身体部位作为病例上下文。"
    "要求：用自然、简短的中文对话回复；不诊断、不处方、不编造医学结论；"
    "如果医生的意图是处理病例，简述你将怎么做并请医生直接描述病例信息。当前消息如果是问候或询问能力，直接介绍并给出示例指令。"
)


def _reply_unavailable(sess: dict) -> dict:
    """v64 防伪装：回复类任务遇 LLM 不可用——只说无法回复，零医疗内容、零工具结果。"""
    sess["reply"] = None
    sess["doc"] = None
    sess["llm_meta"] = {"ok": False, "source": "none", "model": "", "error": "not_configured",
                        "task_type": sess.get("task_type") or "smalltalk",
                        "at": datetime.now(timezone.utc).isoformat()}
    sess.update({"phase": "llm_unavailable", "workflow_state": "llm_unavailable"})
    sess["message"] = "当前未连接 LLM，无法回复"
    _bump_result_version(sess)
    return _snapshot(sess)


# —— v72：对话内修改程序设置（低风险、非医疗、即时生效、零 LLM 依赖） ——
_EFFORT_ZH = {"低": "low", "中": "medium", "高": "high", "low": "low", "medium": "medium", "high": "high"}

# —— v73：跨请求上下文记忆（无病历的临时轮次，如设置指令）——滚动窗口约 200k 字符 ——
import collections as _collections
_MEMORY_MAX_CHARS = 200_000
_EPHEMERAL_MEMORY = _collections.deque(maxlen=2000)   # 容量上限兜底；主约束是 200k 字符预算触发自动压缩
_EPHEMERAL_CHARS = 0


def _mem_compress() -> None:
    """v73：到达 200k 预算自动压缩——较旧一半压成要点摘要（医生指令保留原文片段，
    助手回复留首句），最近一半原样保留。规则式、零 LLM 成本、确定性。"""
    global _EPHEMERAL_CHARS
    items = list(_EPHEMERAL_MEMORY)
    if len(items) < 6:
        return
    cut = len(items) // 2
    old, recent = items[:cut], items[cut:]
    bullets = ["医生：" + m["content"][:150] if m["role"] == "user" else "助手：" + m["content"][:60]
               for m in old]
    summary = "【早前临时轮次压缩摘要（自动生成）】\n" + "\n".join(bullets)
    while len(summary) + sum(len(m["content"]) for m in recent) > _MEMORY_MAX_CHARS and len(bullets) > 2:
        bullets.pop(0)
        summary = "【早前临时轮次压缩摘要（自动生成）】\n" + "\n".join(bullets)
    _EPHEMERAL_MEMORY.clear()
    _EPHEMERAL_MEMORY.append({"role": "assistant", "content": summary})
    _EPHEMERAL_MEMORY.extend(recent)
    _EPHEMERAL_CHARS = len(summary) + sum(len(m["content"]) for m in recent)
    audit("ephemeral_memory_compressed", {"chars": _EPHEMERAL_CHARS})


def _mem_append(role: str, content) -> None:
    global _EPHEMERAL_CHARS
    c = str(content or "")[:4000]
    if not c:
        return
    _EPHEMERAL_MEMORY.append({"role": role, "content": c})
    _EPHEMERAL_CHARS += len(c)
    if _EPHEMERAL_CHARS > _MEMORY_MAX_CHARS:
        _mem_compress()                       # v73：到 200k 自动压缩而不是直接丢弃
    while _EPHEMERAL_MEMORY and _EPHEMERAL_CHARS > _MEMORY_MAX_CHARS:
        _EPHEMERAL_CHARS -= len(_EPHEMERAL_MEMORY[0]["content"])
        _EPHEMERAL_MEMORY.popleft()


def _mem_dump() -> str:
    return "\n".join(("医生：" if m["role"] == "user" else "助手：") + m["content"]
                     for m in _EPHEMERAL_MEMORY)


# 指代最近临时轮次的延续指令（如改完语言后说「改回来」）
_MEM_CONT_RE = re.compile(r"^(改回来|改回去|换回来|换回去|切回来|切回去|还原|撤销|恢复(之前|原来|刚才)?(的)?(设置|语言|模型|连接)?|继续|再改|还是英文|还是中文|不要英文|不要中文)[!！。.~\s]{0,3}$")


def _settings_payload(act: dict, **overrides) -> dict:
    """用活动连接当前公开字段构造 save_connection 载荷（不含 api_key → 原 key 保留）。"""
    p = {"id": act.get("id"), "name": act.get("name"), "provider": act.get("provider"),
         "base_url": act.get("base_url"), "model": act.get("model"),
         "thinking_level": act.get("thinking_level", "")}
    p.update(overrides)
    return p


_SETTINGS_SYSTEM = (
    "你是 DermAgent 的设置助理。把医生的自然语言指令解析成一次程序设置更改。"
    '只输出 JSON 本体（无代码块、无解释），格式：'
    '{"action":"activate_connection|set_model|set_thinking|set_language|none",'
    '"value":"<字符串>","reply":"<给医生的简短中文确认或追问>"}。'
    "可识别的设置："
    "①activate_connection 切换活动 LLM 连接（value=连接名或ID，只能从可用列表选，不得编造）；"
    "②set_model 改活动连接的模型（value=模型名，如 glm-4.5 / deepseek-chat）；"
    "③set_thinking 思考等级（value=low|medium|high）；"
    "④set_language 界面语言（value=en|zh）。"
    "指令目标不明确 → action=clarify（reply 里追问一句）；"
    "要求更改程序未提供的设置（如超时/主题等）→ action=none（reply 说明该项请在 ⚙ 设置页查看或暂不支持）。"
    "reply 用简体中文。"
)


def _route_settings(sess: dict) -> dict:
    """v73：医生在对话里改程序设置——LLM 自己理解任意表述并路由到设置动作；
    服务端只校验并执行。LLM 不可用/解析失败 → 降级关键词规则（离线可用，绝不编造）。"""
    import llm_config as _lc
    text = str(sess.get("task_text") or "")
    sess["doc"] = None
    base, key, model, _eff0 = _llm_ready()
    conns = _lc.load_config()["connections"]
    act = _lc.active_connection() or {}
    ui_action = None
    done = ""
    if base and key and model:
        try:
            prompt = "\n".join([
                (f"最近无病历轮次的对话（用于理解「改回来」等指代）：\n{_mem_dump()}\n\n" if _EPHEMERAL_MEMORY else "")
                + f"医生指令：{text}",
                f"可用连接：{[c.get('name') or c['id'] for c in conns] or '（无）'}",
                f"当前活动连接：{act.get('name') or '（无）'}；当前模型：{act.get('model') or '（未设置）'}；"
                f"当前思考等级：{act.get('thinking_level') or '（默认）'}",
            ])
            parsed = _extract_json(_chat(base, key, model, prompt, "low", system=_SETTINGS_SYSTEM))
            # v73-fix：真实来源标注——LLM 路由成功时回复卡如实显示「真实 LLM 回复」
            sess["llm_meta"] = {"ok": True, "source": "llm", "model": model, "generated_by": "settings-llm"}
            action = str(parsed.get("action") or "").strip()
            value = str(parsed.get("value") or "").strip()
            reply = str(parsed.get("reply") or "").strip()
            if action == "activate_connection":
                target = next((c for c in conns if value and (
                    value == (c.get("name") or "") or value in (c.get("name") or "")
                    or c["id"].startswith(value))), None)
                if target is None:
                    sess["reply"] = {"content": reply or (f"没有找到匹配「{value}」的连接。"
                                                          f"可用：{'、'.join(c.get('name') or c['id'] for c in conns)}")}
                    return _snapshot(sess)
                _lc.activate(target["id"])
                done = reply or f"已切换活动连接：{target.get('name') or target['id']}（模型 {target.get('model') or '未设置'}）。"
            elif action == "set_model":
                if not act.get("id"):
                    sess["reply"] = {"content": "当前没有活动连接。请先在 ⚙ 设置页创建并激活一个 LLM 连接。"}
                    return _snapshot(sess)
                _lc.save_connection(_settings_payload(act, model=value), connection_id=act["id"])
                done = reply or f"已把活动连接「{act.get('name')}」的模型改为 {value}。"
            elif action == "set_thinking":
                eff2 = value.lower()
                if eff2 not in ("low", "medium", "high"):
                    raise ValueError(f"bad effort: {eff2}")
                _lc.save_connection(_settings_payload(act, thinking_level=eff2), connection_id=act["id"])
                done = reply or f"已把活动连接「{act.get('name')}」的思考等级改为 {eff2}。"
            elif action == "set_language":
                lang = "en" if value.lower() in ("en", "english", "英文") else "zh"
                ui_action = {"type": "set_language", "value": lang}
                done = reply or f"已把界面语言切换为{'英文 English' if lang == 'en' else '中文'}。"
            elif action == "clarify":
                sess["reply"] = {"content": reply or "请再说明一下想修改哪项设置。"}
                return _snapshot(sess)
            elif action == "none":
                sess["reply"] = {"content": reply or "该项设置暂不支持在对话中修改，请点击左下角 ⚙ 打开设置页。"}
                return _snapshot(sess)
        except Exception as exc:  # noqa: BLE001 — LLM 路由失败 → 降级关键词规则
            audit("settings_llm_route_failed", str(exc)[:120])
            done = ""
    if not done:
        return _route_settings_regex(sess)   # 离线/解析失败降级
    audit("settings_via_chat", {"case_id": sess.get("case_id"), "change": done[:80]})
    sess["reply"] = {"content": done + " 已即时生效，无需重启。"}
    if ui_action:
        sess["_ui_action"] = ui_action   # v73：随本请求快照下发（_snapshot 读取，_store_sess 消费）
    return _snapshot(sess)


def _route_settings_regex(sess: dict) -> dict:
    """v73：设置修改的关键词降级路径（LLM 不可用时离线可用）。"""
    import llm_config as _lc
    # v73-fix：如实标注本地规则来源（来源卡显示非 AI 生成，不冒充 LLM 反馈）
    sess["llm_meta"] = {"ok": False, "source": "none", "model": "", "error": "llm_unavailable_fallback"}
    text = str(sess.get("task_text") or "")
    act = _lc.active_connection() or {}
    names = lambda: "、".join(c.get("name") or c["id"] for c in _lc.load_config()["connections"])
    done = ""
    ui_action = None
    # ⓪ 界面语言
    m = re.search(r"(?:把)?(?:界面)?语言(?:改成|设为|换成|切换到|切换为|设置为)\s*(英文|中文|english|chinese|en|zh)", text, re.I)
    if m:
        lang = "en" if m.group(1).lower() in ("英文", "english", "en") else "zh"
        ui_action = {"type": "set_language", "value": lang}
        done = f"已把界面语言切换为{'英文 English' if lang == 'en' else '中文'}。"
    # ① 切换活动连接（按名称包含或 ID 前缀匹配）
    m = re.search(r"(?:切换|换到|换用|激活|使用)(?:到|用)?\s*(?:连接|线路|配置)\s*[:：]?\s*([^\s，。,；;]{1,24})", text)
    if m:
        key = m.group(1).strip()
        target = next((c for c in _lc.load_config()["connections"]
                       if key in (c.get("name") or "") or c["id"].startswith(key)), None)
        if target is None:
            sess["reply"] = {"content": f"没有找到名称或 ID 匹配「{key}」的连接。当前已有连接：{names()}。"}
            return _snapshot(sess)
        _lc.activate(target["id"])
        done = f"已切换活动连接：{target.get('name') or target['id']}（模型 {target.get('model') or '未设置'}）。"
    else:
        # ② / ③ 需要活动连接（语言等不依赖连接的指令已完成时不再拦截）
        if not done and not act.get("id"):
            sess["reply"] = {"content": "当前没有活动连接。请先在 ⚙ 设置页创建并激活一个 LLM 连接，或对我说「切换连接 XX」。"}
            return _snapshot(sess)
        m = re.search(r"思考(?:等级)?(?:改成|设为|调成|调为|设置为|改为)\s*(低|中|高|low|medium|high)", text, re.I)
        if m and act.get("id"):
            eff = _EFFORT_ZH[m.group(1).lower()]
            _lc.save_connection(_settings_payload(act, thinking_level=eff), connection_id=act["id"])
            done = f"已把活动连接「{act.get('name')}」的思考等级改为 {eff}。"
        else:
            m = re.search(r"模型(?:改成|设为|换成|设置为|改为|调为)\s*([\w.\-/]{1,64})", text)
            if m and act.get("id"):
                _lc.save_connection(_settings_payload(act, model=m.group(1)), connection_id=act["id"])
                done = f"已把活动连接「{act.get('name')}」的模型改为 {m.group(1)}。"
    if not done:
        sess["reply"] = {"content":
            "可以在对话里直接修改的设置：\n"
            "· 切换连接：如「切换连接 DeepSeek」\n"
            "· 改模型：如「把模型改成 glm-4.5」\n"
            "· 思考等级：如「思考改成低」\n"
            "· 界面语言：如「把语言改成英文」\n"
            "其余设置请点击左下角 ⚙ 打开设置页修改。"}
        return _snapshot(sess)
    audit("settings_via_chat", {"case_id": sess.get("case_id"), "change": done[:80]})
    sess["reply"] = {"content": done + " 已即时生效，无需重启。"}
    if ui_action:
        sess["_ui_action"] = ui_action
    return _snapshot(sess)


def _route_context_compress(sess: dict) -> dict:
    """v73：对话内「压缩上下文」——强制生成本会话结构化摘要（规则式，如实标注非 AI）。
    原始对话记录保留；摘要作为后续 LLM 调用的压缩上下文（对话增长自动触发同机制）。"""
    sess["doc"] = None
    forced = _maybe_summarize(sess, force=True)
    n = len(sess.get("messages") or [])
    sess["llm_meta"] = {"ok": False, "source": "none", "model": "", "error": "local_summary"}
    sess["reply"] = {"content":
        (f"已压缩当前对话上下文：{n} 条消息已整理为结构化摘要（病例字段/待确认项/风险/工具与引用来源）。"
         f"较早对话在后续 LLM 调用中以摘要参与，原始记录保留。"
         if forced else
         "当前对话较短，暂无可压缩内容——上下文会随对话增长自动压缩（无需手动）。")}
    audit("context_compress_manual", {"case_id": sess.get("case_id"), "messages": n})
    return _snapshot(sess)


def _route_smalltalk(sess: dict) -> dict:
    """v64：闲聊/能力询问——Agent 直接自然语言回复，不进病例工作流、不调用任何工具。"""
    base_url, api_key, model, effort = _llm_ready()
    sess["doc"] = None
    if not (base_url and api_key and model):
        return _reply_unavailable(sess)
    tail = (sess.get("messages") or [])[-6:]   # 近几轮上下文（含本轮用户消息）
    ctx = "\n".join(f"{'医生' if m.get('role') == 'user' else '助手'}：{m.get('content', '')}"
                    for m in tail).strip()
    try:
        text = _chat(base_url, api_key, model, ctx or sess.get("task_text") or "你好",
                     effort, system=SMALLTALK_SYSTEM)
    except ConnectionError as exc:
        # v64：能走到这里说明 LLM 已配置——属「已配置但请求失败/超时」（网关抖动常见），
        # 进对话流错误卡并给重试；未配置在上方 _reply_unavailable 分支处理。
        sess["reply"] = None
        sess["llm_meta"] = {"ok": False, "source": "none", "model": "", "error": str(exc)[:160],
                            "task_type": "smalltalk", "at": datetime.now(timezone.utc).isoformat()}
        sess.update({"phase": "llm_error", "workflow_state": "llm_error"})
        sess["message"] = "LLM 请求失败（网关不可达或超时），可点击对话中的「重试」再次发送"
        _bump_result_version(sess)
        return _snapshot(sess)
    sess["reply"] = {"content": text, "generated_by": "llm", "model": model,
                     "task_type": "smalltalk",
                     "disclaimer": "AI 生成内容仅供医生决策支持，不构成诊断或处方",
                     "at": datetime.now(timezone.utc).isoformat()}
    sess["llm_meta"] = {"ok": True, "source": "llm", "model": model, "generated_by": "llm",
                        "task_type": "smalltalk", "error": "",
                        "at": datetime.now(timezone.utc).isoformat()}
    sess.pop("llm_error", None)
    sess.update({"phase": "chat_reply", "workflow_state": "chat_reply"})
    sess["message"] = ""
    _bump_result_version(sess)
    return _snapshot(sess)


def _route_clarify(sess: dict) -> dict:
    """v64：开放病例写作请求且病例信息几乎全空 → Agent 先澄清（本地确定性清单，不空转 LLM）。
    医生补充后消息经 chat 并入病例，字段齐全自动进入草稿生成。"""
    missing = sess.get("missing") or []
    lines = ["好的，我来帮您写病例。目前还没有收到病例信息，请直接描述（一条或分几条发送均可）：", ""]
    lines += [f"- {lbl}" for lbl in missing]
    lines += ["", "也可以点开「人体模型」选择部位，作为病例的身体区域上下文；"
                  "信息齐全后我会生成待您审核的门诊病历草稿，不会把未知内容写成「无」。"]
    sess["reply"] = {"content": "\n".join(lines), "generated_by": "agent", "model": "",
                     "task_type": "case_write", "missing": missing,
                     "disclaimer": "", "at": datetime.now(timezone.utc).isoformat()}
    sess["llm_meta"] = {"ok": True, "source": "agent", "model": "", "generated_by": "agent-clarify",
                        "task_type": "case_write", "error": "",
                        "at": datetime.now(timezone.utc).isoformat()}
    sess.pop("llm_error", None)
    sess.update({"phase": "clarifying", "workflow_state": "clarifying"})
    sess["message"] = ""
    _bump_result_version(sess)
    return _snapshot(sess)


def _analyze_case(sess: dict) -> dict:
    """病例分析管线（case_write/general）：检索 → LLM 草稿；缺失字段如实进草稿，不再追问。"""
    case, red_flags, missing = sess["case"], sess["red_flags"], sess["missing"]
    citations = _stamp_citations(retrieve_citations(case))
    # §16.1：医生生成辅助建议时自动全库检索本地书籍（只回 ready；KB 故障/未装不阻断主流程）
    book_citations = []
    try:
        from rag import search as kb_search
        kb_q = "；".join(str(v) for v in (case.values() if isinstance(case, dict) else []) if v)
        book_citations = kb_search(kb_q).get("citations", [])
    except Exception as exc:  # noqa: BLE001
        audit("kb_search_failed", str(exc)[:120])
    book_citations = _stamp_citations(book_citations)
    draft, llm_err = sess["_make_draft"](case, citations, red_flags, missing, demo=sess.get("demo") or "")
    if draft is None:
        # v58 防伪装：LLM 不可用 → 不生成任何疾病/药物/治疗方案内容，明确进入 llm_unavailable
        # v64：已配置但请求失败/超时 → llm_error（对话流错误卡 + 重试）；未配置 → llm_unavailable
        _rb, _rk, _rm, _ = _llm_ready()
        return _snapshot_llm_unavailable(sess, llm_err, "当前未连接 LLM，无法生成真实 AI 医疗建议",
                                         retryable=bool(_rb and _rm))
    if book_citations:
        draft["book_citations"] = book_citations      # 与合成 KB 引用分开列示（引用契约不混淆）
    sess["book_citations"] = book_citations
    generated_by = str(draft.get("generated_by") or "")
    sess["llm_meta"] = {"ok": True, "model": draft.get("model_version", ""),
                        # demo 病例的规则稿 = 明确标注的离线演示数据，不冒充真实 LLM 结果
                        "source": "demo" if (sess.get("demo") and generated_by.startswith("rule")) else "llm",
                        "generated_by": generated_by,
                        "task_type": sess.get("task_type") or "case_write", "error": "",
                        "at": datetime.now(timezone.utc).isoformat()}
    sess.pop("llm_error", None)
    sess["doc"] = None
    sess.update({"phase": "awaiting_review", "workflow_state": draft["workflow_state"], "draft": draft})
    _sync_record(sess)
    _bump_result_version(sess)
    sess["question"] = build_review_question(draft, red_flags)
    return _snapshot(sess)


def _route_doc_task(sess: dict, task: str) -> dict:
    """文书/医学判断任务：按需检索 → LLM 生成；降级路径分两类——
    文书整理类（现病史/摘要/转诊等）LLM 不可用 → 本地模板整理（明确标注，只重组已提供字段）；
    医学判断类（鉴别/治疗/用药/宣教）LLM 不可用 → llm_unavailable（零医疗内容）。"""
    need_citations = task in MEDICAL_DOC_TASKS
    citations = _stamp_citations(retrieve_citations(sess["case"])) if need_citations else []
    if need_citations:
        # v67 最终 RAG（第二阶段）：资料收集完成后按症状+任务主题检索本地书籍，
        # 与合成 KB 引用合并（citation_id 去重）；KB 故障/未装不阻断文书流程，引用如实缺失。
        try:
            from rag import search as kb_search
            case = sess.get("case") or {}
            q_parts = [str(case.get(k) or "") for k in ("chief_complaint", "symptoms", "body_region")]
            q = "；".join([p for p in q_parts if p.strip()] + [_DOC_SEARCH_HINTS.get(task, "")])[:160]
            if q.strip():
                seen = {c.get("citation_id") for c in citations}
                citations += [c for c in _stamp_citations(kb_search(q).get("citations", []))
                              if c.get("citation_id") not in seen]
        except Exception as exc:  # noqa: BLE001
            audit("kb_search_failed", str(exc)[:120])
    doc, err = None, ""
    base_url, api_key, model, effort = _llm_ready()
    if base_url and model:
        doc, err = _make_doc_llm(sess, task, citations, base_url, api_key, model, effort)
    if doc is not None:
        sess["llm_meta"] = {"ok": True, "source": "llm", "model": model,
                            "generated_by": "llm", "task_type": task, "error": "",
                            "at": datetime.now(timezone.utc).isoformat()}
        sess.pop("llm_error", None)
    elif task in TEMPLATE_DOC_TASKS:
        doc = _template_doc(sess, task)
        sess["llm_meta"] = {"ok": True, "source": "template", "model": "",
                            "generated_by": "local-template", "task_type": task,
                            "error": err[:160],
                            "at": datetime.now(timezone.utc).isoformat()}
    else:
        # v64：base_url+model 已配置 → 请求失败/超时可重试；未配置 → 引导配置（不重试）
        return _snapshot_llm_unavailable(sess, err or "未配置 LLM 连接（设置 → LLM 与模型）",
                                         "当前未连接 LLM，无法生成真实 AI 医疗建议",
                                         retryable=bool(base_url and model))
    sess["doc"] = doc   # 文书任务不动已有草稿（draft 原样保留）
    sess.update({"phase": "doc_ready", "workflow_state": "doc_ready"})
    sess["message"] = ""
    _sync_record(sess)
    _bump_result_version(sess)
    return _snapshot(sess)


def _route_case_edit(sess: dict) -> dict:
    """病例编辑：读取当前病例与对话上下文 → LLM 重写草稿（结构化校验复用）。
    LLM 不可用时：已有 AI 草稿 → llm_unavailable（绝不本地篡改 AI 医疗内容）；
    无草稿（纯字段病例）→ 本地模板重排（明确标注）。
    v67：医生确认「根据修改重新整理」→ 本轮允许 AI 更新其手动修改字段（需求 8 闭环）。"""
    sess["allow_ai_override"] = True
    base_url, api_key, model, effort = _llm_ready()
    citations = retrieve_citations(sess["case"])
    if base_url and model:
        draft, err = _make_edit_llm(sess, citations, base_url, api_key, model, effort)
        if draft is not None:
            sess["llm_meta"] = {"ok": True, "source": "llm", "model": model,
                                "generated_by": str(draft.get("generated_by") or "llm-edited"),
                                "task_type": "case_edit", "error": "",
                                "at": datetime.now(timezone.utc).isoformat()}
            sess.pop("llm_error", None)
            sess["doc"] = None
            sess.update({"phase": "awaiting_review", "workflow_state": draft["workflow_state"], "draft": draft})
            _sync_record(sess)
            _bump_result_version(sess)
            sess["question"] = build_review_question(draft, sess["red_flags"])
            return _snapshot(sess)
        return _snapshot_llm_unavailable(sess, err, "当前未连接 LLM，无法生成真实 AI 医疗建议",
                                         retryable=True)   # 已配置但请求失败/超时 → 可重试
    prev = sess.get("draft")
    if prev and str(prev.get("generated_by") or "").startswith("llm"):
        return _snapshot_llm_unavailable(sess, "未配置 LLM 连接（设置 → LLM 与模型）",
                                         "当前未连接 LLM，无法生成真实 AI 医疗建议")
    doc = _template_doc(sess, "summary")
    doc["task_type"] = "case_edit"
    doc["content"] = ("（本地模板整理：LLM 未连接，仅按字段重排，不做任何医学改写）\n\n" + doc["content"])
    sess["llm_meta"] = {"ok": True, "source": "template", "model": "", "generated_by": "local-template",
                        "task_type": "case_edit", "error": "",
                        "at": datetime.now(timezone.utc).isoformat()}
    sess["doc"] = doc
    sess.update({"phase": "doc_ready", "workflow_state": "doc_ready"})
    _sync_record(sess)
    _bump_result_version(sess)
    return _snapshot(sess)


def _empty_record() -> dict:
    """v65：门诊病历单空模板——17 字段全空（渲染端显示「未提供/待补充」）。"""
    now = datetime.now(timezone.utc).isoformat()
    return {"fields": {k: "" for k, _ in RECORD_FIELDS}, "meta": {},
            "review_status": "draft", "confirmed_at": None,
            "citations": [], "created_at": now, "updated_at": now}


def _parse_doc_sections(content: str) -> dict:
    """doc markdown（## / ### 标题分节）→ {record 字段: 段落文本}。
    标题按 _DOC_SECTION_MAP 关键词命中（先命中先得）；无标题正文不强行归类。"""
    out, cur_field, buf = {}, None, []

    def flush():
        if cur_field and buf and out.get(cur_field) is None:
            out[cur_field] = "\n".join(buf).strip()

    for raw in str(content or "").splitlines():
        line = raw.strip()
        m = re.match(r"^#{1,4}\s*(.+)$", line)
        if m:
            flush()
            cur_field, buf = None, []
            title = m.group(1).strip()
            for field, keys in _DOC_SECTION_MAP:
                if any(k in title for k in keys):
                    cur_field = field
                    break
            continue
        if cur_field:
            buf.append(raw.rstrip())
    flush()
    return {k: v for k, v in out.items() if v}


def _patient_from_clinic(sess: dict) -> str:
    """患者基本信息行：来自医生资料 patient 白名单（客户端发送前已脱敏为类别占位）。
    只拼真实提供的子项；全缺 → 空串（渲染端显示「未提供」）。"""
    p = ((sess.get("case") or {}).get("clinic_payload") or {}).get("patient") or {}
    if not isinstance(p, dict):
        return ""
    parts = []
    if p.get("name"):
        parts.append(f"姓名：{p['name']}")
    if p.get("sex"):
        parts.append(f"性别：{p['sex']}")
    if p.get("age"):
        parts.append(f"年龄：{p['age']}")
    if p.get("no"):
        parts.append(f"门诊号：{p['no']}")
    if p.get("date"):
        parts.append(f"就诊日期：{p['date']}")
    return "；".join(parts)


def _present_illness_from_case(case: dict) -> str:
    """无 doc 时的现病史兜底：只重组已提供字段（缺失字段不出现，不写「无/否认」）。"""
    parts = []
    for key, label in (("symptoms", "症状"), ("duration", "病程"), ("triggers", "诱因"), ("trend", "变化趋势")):
        v = str(case.get(key) or "").strip()
        if v:
            parts.append(f"{label}：{v}")
    return "；".join(parts)


def _sections_from_draft(draft: dict) -> dict:
    """AI 分析草稿（awaiting_review 管线）→ 判断/核对/随访字段（措辞受 prompt 硬约束）。
    v67：detect_red_flags 结果落风险提示字段。"""
    out = {}
    flags = [str(f) for f in (draft.get("red_flags")
                              or (draft.get("red_flag_escalation") or {}).get("reasons") or [])
             if str(f).strip()]
    if flags:
        out["danger_signals"] = "危险信号（自动检测，请医生核对）：\n" + "\n".join(
            f"- {f}" for f in flags[:6]
        )
    conds = draft.get("suspected_conditions") or []
    if conds:
        lines = []
        for c in conds[:4]:
            name = str(c.get("name") or "").strip()
            conf = str(c.get("confidence") or "unknown")
            if not name:
                continue
            ev = ""
            for e in (c.get("supporting_evidence") or []):
                if isinstance(e, dict) and str(e.get("statement") or "").strip():
                    ev = str(e["statement"]).strip()
                    break
            lines.append(f"- {name}（依据程度：{conf}，建议医生核对）" + (f"：{ev}" if ev else ""))
        if lines:
            out["impression"] = "鉴别方向（AI 草稿，按可能性排序，待医生核对）：\n" + "\n".join(lines)
    exams = [e for e in (draft.get("recommended_exams") or []) if isinstance(e, dict)]
    exam_lines = []
    for e in exams[:5]:
        exam = str(e.get("exam") or "").strip()
        if not exam:
            continue
        reason = str(e.get("reason") or "").strip()
        exam_lines.append(f"- {exam}" + (f"（{reason}）" if reason else ""))
    if exam_lines:
        out["auxiliary_exam"] = "建议检查（AI 草稿，待医生核对执行）：\n" + "\n".join(exam_lines)
    tref = [t for t in (draft.get("treatment_reference") or []) if isinstance(t, dict)]
    tref_lines = []
    for t in tref[:5]:
        approach = str(t.get("approach") or "").strip()
        if not approach:
            continue
        goal = str(t.get("indication_or_goal") or "").strip()
        basis = str(t.get("basis") or "").strip()
        refer = str(t.get("when_to_refer") or "").strip()
        extra = "；".join(x for x in (goal, f"依据：{basis}" if basis else "", f"转诊：{refer}" if refer else "") if x)
        tref_lines.append(f"- {approach}" + (f"（{extra}）" if extra else ""))
    if tref_lines:
        out["treatment_ref"] = "治疗方向（AI 辅助参考，非处方，待医生核定）：\n" + "\n".join(tref_lines)
    med_recs = [m for m in (draft.get("medication_recommendations") or []) if isinstance(m, dict)]
    med_rec_lines = []
    for m in med_recs[:6]:
        name = str(m.get("generic_name") or "").strip() or "药物名称待核实"
        purpose = str(m.get("purpose") or "").strip()
        duration = str(m.get("reference_duration") or "").strip() or "参考时长待核实"
        checks = "、".join(str(x).strip() for x in (m.get("contraindications_or_checks") or []) if str(x).strip())
        med_rec_lines.append(f"- {name}｜用途：{purpose or '待核实'}｜参考使用时长：{duration}"
                             + (f"｜核对：{checks}" if checks else ""))
    if med_rec_lines:
        out["medication_recommendation"] = (
            "用药参考（非处方，药名与时长均须医生结合病情核对后决定）：\n" + "\n".join(med_rec_lines))
    rp = draft.get("recovery_period") or {}
    course = str(rp.get("typical_course") or "").strip()
    infl = [str(x).strip() for x in (rp.get("influence_factors") or []) if str(x).strip()]
    if course:
        out["follow_up"] = "恢复周期参考：" + course + \
            ("（影响因素：" + "；".join(infl[:4]) + "）" if infl else "")
    checks = draft.get("medication_checks") or []
    if checks:
        lines = ["- {}: 需核对 {}".format(str(m.get("generic_name") or "").strip() or "药物",
                                          str(m.get("items_to_verify") or "").strip())
                 for m in checks[:5] if isinstance(m, dict)]
        if lines:
            out["medication_check"] = "用药核对项（仅核对清单，非处方）：\n" + "\n".join(lines)
    steps = draft.get("recommended_next_steps") or []
    if steps:
        lines = ["- {}".format(str(s.get("action") or "").strip()) for s in steps[:5] if isinstance(s, dict)]
        if lines:
            step_txt = "建议下一步（待医生确认）：\n" + "\n".join(lines)
            # 恢复周期与建议下一步同属随访字段：合并且各自带小标题
            out["follow_up"] = (out.get("follow_up", "") + "\n\n" + step_txt).strip()
    return out


def _sync_record(sess: dict) -> None:
    """v65：AI 产物（draft/doc）→ 病历单 record。医生已确认（doctor_confirmed）的字段
    永不被 AI 覆盖；其余字段以最新产物为准。
    v66：来源细分——医生口述/资料录入=user_input、3D 选点=model_3d、带引用的 AI 分析=kb_ref。
    身体部位来自 3D 选点（可选工具，未选留空）；患者基本信息来自资料白名单。"""
    # v66：字段整理类任务正文无分节标题（如「只修改现病史」生成一段现病史），
    # 按任务类型直接落入对应字段；有分节标题的整份文书仍走 _parse_doc_sections。
    _doc = sess.get("doc") if isinstance(sess.get("doc"), dict) else {}
    _task_field = _DOC_TASK_FIELD.get(_doc.get("task_type") or "")
    _task_content = str(_doc.get("content") or "").strip() if _task_field else ""
    rec = sess.get("record")
    if not isinstance(rec, dict) or not isinstance(rec.get("fields"), dict):
        rec = _empty_record()
        sess["record"] = rec
    now = datetime.now(timezone.utc).isoformat()
    fields, meta = rec["fields"], rec.setdefault("meta", {})
    cites = (sess.get("doc") or {}).get("citations") or (sess.get("draft") or {}).get("citations") or []

    protected = {"doctor_confirmed", "doctor_modified"}
    if sess.get("allow_ai_override"):
        # v67：医生确认「根据修改重新整理」后，本轮允许 AI 更新其手动修改字段（一次性授权）
        protected.discard("doctor_modified")
        sess["ai_override_used"] = True

    def put(field: str, value: str, source: str = "ai_draft"):
        value = str(value or "").strip()
        if not value or meta.get(field, {}).get("source") in protected:
            return
        if fields.get(field) == value:
            return
        fields[field] = value
        meta[field] = {"source": source, "at": now}

    case = sess.get("case") or {}
    put("patient", _patient_from_clinic(sess), "user_input")
    put("chief_complaint", str(case.get("chief_complaint") or ""), "user_input")
    put("past_history", str(case.get("past_history") or ""), "user_input")
    put("allergy_history", str(case.get("allergies") or ""), "user_input")
    put("medication_history", str(case.get("current_medications") or ""), "user_input")
    if not (meta.get("present_illness", {}).get("source") == "doctor_confirmed"):
        doc_pi = _parse_doc_sections((sess.get("doc") or {}).get("content") or "").get("present_illness")
        if not doc_pi and _task_field == "present_illness":
            doc_pi = _task_content
        if doc_pi:
            put("present_illness", doc_pi, "ai_draft")
        else:
            put("present_illness", _present_illness_from_case(case), "user_input")
    draft = sess.get("draft") if isinstance(sess.get("draft"), dict) else {}
    sections = _parse_doc_sections((sess.get("doc") or {}).get("content") or "")
    if _task_field and _task_field != "present_illness" and not sections.get(_task_field):
        sections[_task_field] = _task_content
    sections.update({k: v for k, v in _sections_from_draft(draft).items() if not sections.get(k)})
    for field in ("skin_lesions", "physical_exam", "auxiliary_exam", "impression",
                  "treatment_ref", "medication_check", "follow_up", "personal_history",
                  "danger_signals"):
        src = "kb_ref" if (cites and field in ("impression", "treatment_ref", "medication_check")) else "ai_draft"
        put(field, sections.get(field, ""), src)
    regions = [str(r) for r in (sess.get("selected_body_regions") or []) if str(r).strip()]
    if regions:
        # v66：3D 选点是数据联动字段，不受医生确认锁限制（confirm 端点同此例外）
        val = "；".join(regions)
        if fields.get("body_regions") != val:
            fields["body_regions"] = val
            meta["body_regions"] = {"source": "model_3d", "at": now}
    if cites and not rec.get("citations"):
        rec["citations"] = cites
    rec["updated_at"] = now
    if sess.pop("ai_override_used", False):
        sess.pop("allow_ai_override", None)  # 一次性授权：本轮同步后恢复保护


def _stamp_citations(cites: list) -> list:
    """v67 需求6：所有 RAG 引用统一带检索时间（retrieved_at），前端展示引用详情。"""
    at = datetime.now(timezone.utc).isoformat()
    out = []
    for c in cites or []:
        if not isinstance(c, dict):
            continue
        d = dict(c)
        d.setdefault("retrieved_at", at)
        out.append(d)
    return out


def _record_status(rec: dict) -> tuple:
    """v66：病例单显示状态（派生自 meta，review_status 是数据层）。
    返回 (status_key, 中文标签)。优先级：已拒绝 > 已确认 > 医生已修改 > AI 草稿 > 待医生审核。"""
    if rec.get("rejected"):
        return "rejected", "已拒绝"
    if rec.get("review_status") == "confirmed":
        return "confirmed", "已确认"
    sources = [m.get("source") for m in (rec.get("meta") or {}).values()]
    if "doctor_modified" in sources:
        return "doctor_modified", "医生已修改"
    if "ai_draft" in sources or "kb_ref" in sources:
        return "ai_draft", "AI 草稿"
    return "pending_review", "待医生审核"


def _record_snapshot(sess: dict) -> dict:
    """病例单读快照：record 不存在时返回空模板（医生可直接手动填写）。
    v66：附派生状态 status/status_label（AI 草稿/待医生审核/医生已修改/已确认/已拒绝）。"""
    rec = sess.get("record") if isinstance(sess.get("record"), dict) else _empty_record()
    out = dict(rec)
    out.setdefault("fields", {k: "" for k, _ in RECORD_FIELDS})
    out["case_id"] = sess["case_id"]
    out["label"] = sess.get("label", "")
    out["phase"] = sess.get("phase", "")
    out["llm_meta"] = sess.get("llm_meta") or {}
    key, label = _record_status(rec)
    out["status"] = key
    out["status_label"] = label
    return out


_RECORD_SOURCE_LABELS = {"ai_draft": "AI 草稿", "doctor_modified": "医生修改", "doctor_confirmed": "医生确认",
                         "user_input": "用户输入", "model_3d": "3D 模型选择", "kb_ref": "医学知识库引用"}


def _record_markdown(sess: dict) -> str:
    """v65/v66：导出 Markdown——编号/创建/更新/状态/17 字段（附内容来源）/引用/免责声明。
    仅医生确认（review_status=confirmed）后可导出（端点门禁）。字段缺失显示「未提供」，绝不补「无/否认」。"""
    rec = _record_snapshot(sess)
    fields = rec["fields"]
    lines = ["# 皮肤科门诊病历", ""]
    lines.append(f"- 病例编号：{sess['case_id']}")
    lines.append(f"- 创建时间：{str(rec.get('created_at') or '')[:19].replace('T', ' ')}")
    lines.append(f"- 更新时间：{str(rec.get('updated_at') or '')[:19].replace('T', ' ')}")
    lines.append(f"- 审核状态：{rec.get('status_label') or '医生审核中'}（最终审核版本，医生已确认）")
    lines.append("")
    for key, label in RECORD_FIELDS:
        val = str(fields.get(key) or "").strip()
        if key == "body_regions" and not val:
            val = "未使用 3D 部位选择"
        src = (rec.get("meta") or {}).get(key, {}).get("source")
        src_note = f"（来源：{_RECORD_SOURCE_LABELS.get(src, 'AI 草稿')}）" if src else ""
        lines.append(f"## {label}")
        lines.append("")
        lines.append((val if val else "未提供") + (f"\n\n> {src_note}" if src_note else ""))
        lines.append("")
    cites = rec.get("citations") or []
    if cites:
        lines.append("## 引用（AI 生成内容的资料来源）")
        lines.append("")
        for c in cites:
            if not isinstance(c, dict):
                continue
            lines.append(f"- [{c.get('citation_id')}] {c.get('document_title')} · {c.get('document_version', '')}"
                         f" · {c.get('source_type', '')} — {str(c.get('quoted_text') or '')[:80]}")
        lines.append("")
    lines.append("> 免责声明：本病历中标注「AI 草稿」的内容仅供医生审核参考，不能替代医生诊断与签名；"
                 "AI 不自动确诊、不开具处方，用药仅列核对项；缺失信息如实标注「未提供」。")
    return "\n".join(lines)


# 文书任务的本地模板：只组装用户已提供的字段；缺失一律「未提供」，绝不写「无/否认」，
# 不含任何疾病/药物/治疗/引用（那是 LLM 管线的事）。
_TEMPLATE_SECTIONS = {
    "history": ("现病史（整理）", ["chief_complaint", "symptoms", "duration", "trigger", "trend"]),
    "lesion_desc": ("皮损描述（整理）", ["body_region", "symptoms", "duration", "trend"]),
    "summary": ("病例摘要（整理）", ["chief_complaint", "symptoms", "duration", "past_history",
                                     "allergy", "medication"]),
    "referral": ("转诊摘要（整理）", ["chief_complaint", "symptoms", "duration", "past_history", "medication"]),
    "followup": ("随访记录（整理）", ["chief_complaint", "symptoms", "duration", "medication"]),
}
_FIELD_LABELS = {"body_region": "身体区域", "chief_complaint": "主诉", "symptoms": "症状",
                 "duration": "病程", "trigger": "诱因", "trend": "演变趋势",
                 "past_history": "既往史", "allergy": "过敏史", "medication": "当前用药"}


def _template_doc(sess: dict, task: str) -> dict:
    case = sess.get("case") or {}
    title, keys = _TEMPLATE_SECTIONS.get(task, ("病例整理", ["symptoms"]))
    lines = [f"## {title}", ""]
    lines.append("- 整理方式：本地模板整理（非 AI 分析；LLM 未连接或本任务为纯文字整理）")
    for k in keys:
        v = str(case.get(k) or "").strip()
        label = _FIELD_LABELS.get(k, k)
        lines.append(f"- {label}：{v if v else '未提供'}")
    prov = [str(x) for x in (sess.get("missing") or [])]
    if prov:
        lines.append("")
        lines.append("- 待补充信息（未知字段如实列出，不推断为「无」）：" + "、".join(prov))
    lines.append("")
    lines.append("> 本内容为本地模板整理，不构成医学建议；医疗判断须由医生完成。")
    return {"task_type": task, "content": "\n".join(lines), "generated_by": "local-template",
            "citations": [], "disclaimer": "本地模板整理，不是真实 AI 分析，不可用于医疗判断",
            "at": datetime.now(timezone.utc).isoformat()}


# v68 上下文管理（计划 §十一）：预算与阈值（字符近似估算，1 汉字 ≈ 1 token，为输出留足空间）
CTX_BUDGET_CHARS = 6000          # 对话/工具/RAG 可裁层总预算
_SUMMARY_MSG_THRESHOLD = 30      # §十一.8：连续 30 轮对话触发摘要
_SUMMARY_CHARS_THRESHOLD = 9000  # 或消息总字符超限触发


def _maybe_summarize(sess: dict, force: bool = False) -> bool:
    """v68（计划 §十一.4）：对话达到轮数/字符阈值 → 自动生成结构化摘要（规则式，零 LLM 零伪造）。
    原始对话记录保留；摘要只作为后续 LLM 调用的压缩上下文。摘要标记信息来源：
    confirmed_facts 全部来自医生输入/确认字段；AI 推断不写入。
    v73：force=True 时忽略阈值（对话内「压缩上下文」手动触发）。"""
    msgs = sess.get("messages") or []
    total = sum(len(str(m.get("content") or "")) for m in msgs)
    if not force and len(msgs) < _SUMMARY_MSG_THRESHOLD and total < _SUMMARY_CHARS_THRESHOLD:
        return False
    case = sess.get("case") or {}
    rec = sess.get("record") or {}
    meta = rec.get("meta") or {}
    doctor_edits = sorted(f for f, m in meta.items()
                          if isinstance(m, dict) and m.get("source") in ("doctor_modified", "doctor_confirmed"))
    tool_results = [{"tool": t.get("tool"), "goal": str(t.get("goal") or "")[:60],
                     "digest": str(t.get("output_digest") or "")[:80]}
                    for t in (sess.get("tool_history") or [])[-5:] if isinstance(t, dict)]
    rag_ids = [c.get("citation_id") for c in (sess.get("book_citations") or [])
               if isinstance(c, dict) and c.get("citation_id")][:8]
    sess["context_summary"] = {
        "confirmed_facts": {k: str(case.get(k) or "").strip()[:200]
                            for k, _, _ in REQUIRED_FIELDS if str(case.get(k) or "").strip()},
        "unconfirmed_items": [str(x) for x in (sess.get("missing") or [])],
        "allergy_and_risk_flags": {"allergies": str(case.get("allergies") or "").strip(),
                                   "red_flags": [str(x) for x in (sess.get("red_flags") or [])]},
        "doctor_edits": doctor_edits,
        "tool_results": tool_results,
        "rag_sources": rag_ids,
        "current_task": str(sess.get("task_type") or ""),
        "next_action": "按医生下一条指令继续；缺失项见 unconfirmed_items（不推断为「无」）。",
        "at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "local-summary",   # 信息来源标记：规则式摘要，非 AI 生成
    }
    sess["ctx_just_summarized"] = True
    audit("context_summarized", {"case_id": sess.get("case_id"), "messages": len(msgs), "chars": total})
    return True


def _build_context(sess: dict) -> str:
    """v68（计划 §十一.1/§十一.3）：五层分层上下文文本——供文书/编辑任务 LLM 提示词。
    裁剪优先级（从不可裁到先裁）：病例字段 → 过敏史/危险信号/医生修改（永不裁剪）→
    当前任务与待办 → 病例摘要（触发过自动摘要时替代较早对话）→ 最近对话 → 工具结果与 RAG 来源。
    任何裁剪不得删除过敏史、危险信号、医生修改内容和已确认病例字段。"""
    case = sess.get("case") or {}
    parts = []
    # L1 病例结构化字段（不可裁）
    parts.append("【病例字段】" + json.dumps(case, ensure_ascii=False))
    # L2 安全层（不可裁）
    allergy = str(case.get("allergies") or "").strip()
    flags = [str(f) for f in (sess.get("red_flags") or [])]
    rec = sess.get("record") or {}
    doc_edits = sorted(f for f, m in ((rec.get("meta") or {}).items())
                       if isinstance(m, dict) and m.get("source") in ("doctor_modified", "doctor_confirmed"))
    sec2 = []
    if allergy:
        sec2.append("过敏史：" + allergy)
    if flags:
        sec2.append("危险信号：" + "；".join(flags[:6]))
    if doc_edits:
        sec2.append("医生修改/确认过的字段（不得覆盖）：" + "、".join(doc_edits))
    parts.append("【安全上下文（永不裁剪）】" + ("；".join(sec2) if sec2 else "（暂无）"))
    # L3 当前任务与待办
    parts.append("【当前任务】" + str(sess.get("task_type") or "") + "：" + str(sess.get("task_text") or "")[:120]
                 + "；缺失项：" + ("、".join(str(x) for x in (sess.get("missing") or [])) or "无"))
    # L4 病例摘要（存在则替代较早对话）
    summary = sess.get("context_summary")
    if summary:
        parts.append("【病例摘要（较早对话已压缩，以此为准）】" + json.dumps(summary, ensure_ascii=False))
    # L5/L6 可裁层：最近对话 + 工具结果 + RAG 来源（预算内从最早开始裁）
    msgs = [m for m in (sess.get("messages") or []) if str(m.get("content") or "").strip()]
    tools = [t for t in (sess.get("tool_history") or []) if isinstance(t, dict)]
    rags = [c.get("citation_id") for c in (sess.get("book_citations") or [])
            if isinstance(c, dict) and c.get("citation_id")]
    budget = CTX_BUDGET_CHARS
    dialog_lines = []
    for m in reversed(msgs[-20:]):   # 最多回看 20 条，从最新往旧装配
        line = f"{'医生' if m.get('role') == 'user' else '助手'}：{str(m.get('content'))[:220]}"
        if budget - len(line) < 400 and dialog_lines:   # 预算保护：至少保留最新一条
            break
        budget -= len(line)
        dialog_lines.insert(0, line)
    tool_lines = [f"- {t.get('tool')}（{t.get('risk_level', 'low')}）：{str(t.get('goal') or '')[:40]}"
                  f" → {str(t.get('output_digest') or '')[:60]}" for t in reversed(tools[-5:])]
    part56 = ""
    if dialog_lines:
        part56 += "【最近对话】\n" + "\n".join(dialog_lines)
    if tool_lines:
        part56 += "\n【工具执行记录】\n" + "\n".join(tool_lines)
    if rags:
        part56 += "\n【RAG 引用来源】" + "、".join(rags[:8])
    if part56:
        parts.append(part56)
    return "\n\n".join(parts)


def _doc_prompt(sess: dict, task: str, citations: list) -> str:
    """文书/判断任务的 LLM 提示词：任务指令 + 病例 + 对话上下文 + 引用白名单 + 措辞硬约束。"""
    case = sess.get("case") or {}
    ctx = _build_context(sess)   # v68：五层分层上下文（病例/安全层/任务/摘要/对话+工具+RAG，预算内裁剪）
    cites = json.dumps(citations, ensure_ascii=False)
    ids = [c["citation_id"] for c in citations] or []
    common = ("只输出 JSON 本体：{\"content\": \"<markdown 正文>\"}。硬约束："
              "措辞用「疑似/考虑/待排/需要排除/建议医生核对」，不得自动诊断、不得开处方/剂量/疗程、"
              "不得编造病史/检查结果/用药史；未知信息写「未提供」，不得写「无/否认」；简体中文。")
    task_spec = {
        "history": "任务：只整理现病史（主诉/症状/病程/诱因/演变一段连贯叙述）。不要输出诊断、鉴别、治疗、用药任何内容。",
        "lesion_desc": "任务：整理皮损描述（部位/形态/分布/病程演变），供医生病历记录引用。不要输出诊断与治疗。",
        "differential": ("任务：基于知识库检索结果列出需要鉴别的疾病方向与判断依据；每个方向必须给"
                         " citation_id（只能用白名单）；知识库未覆盖的方向不得编造，明确写「知识库未覆盖」。"),
        "treatment_ref": "任务：给出治疗参考方向（类别级，非处方），逐项标注「建议医生核对」；重要结论给 citation_id（只能用白名单）。",
        "medication_review": ("任务：生成用药核对清单——每项药物必须包含适应证核对、禁忌核对、过敏核对、"
                              "相互作用核对四个核对项（markdown 表格），并标注「需医生确认后执行」；"
                              "重要结论给 citation_id（只能用白名单）。"),
        "patient_edu": ("任务：写患者能看懂的健康教育说明（通俗语言、日常护理与就医提示）；"
                        "必须包含「本说明仅供健康教育参考，具体诊疗以主治医生意见为准」；不要给处方。"),
        "referral": "任务：整理转诊摘要（转出原因/病情要点/已提供资料清单），只重组已提供信息。",
        "followup": "任务：整理随访记录（随访要点/当前用药/观察指标），只重组已提供信息。",
        "summary": "任务：整理病例摘要（结构化要点），只重组已提供信息。",
    }[task]
    return "\n\n".join([
        "你是皮肤科医疗文书助手，在医生监督下整理病历文书。只基于医生提供的信息与知识库检索结果，"
        "绝不编造。" + common,
        f"任务：{task_spec}",
        f"病例字段：{json.dumps(case, ensure_ascii=False)}",
        f"分层上下文（按层裁剪；过敏史/危险信号/医生修改不可覆盖）：\n{ctx or '（无）'}",
        f"citation_id 白名单：{ids or '（空——知识库未覆盖，医学结论不得编造引用）'}",
        f"检索片段：{cites if citations else '（空）'}",
    ])


def _sanitize_doc_citations(content: str, citations: list) -> str:
    """引用校验（citation_validator）：正文里出现的 citation_id 必须在检索白名单内，
    违规引用就地剔除并标注——重要医学结论必须可溯源，编造引用零容忍。"""
    allowed = {c["citation_id"] for c in citations or []}

    def repl(m):
        cid = m.group(0)
        return cid if cid in allowed else "〔引用未通过校验，已移除〕"

    cleaned = re.sub(r"\b(?:CIT|DOC|KB)-[A-Za-z0-9\-]+", repl, content)
    if cleaned != content:
        cleaned += "\n\n> ⚠️ 部分引用未通过白名单校验，已移除。"
    return cleaned


def _make_doc_llm(sess: dict, task: str, citations: list, base_url, api_key, model, effort):
    """LLM 文书生成：非法返回修复一次；彻底失败返回 (None, err) 由路由决定降级路径。"""
    def _once():
        text = _chat(base_url, api_key, model, _doc_prompt(sess, task, citations), reasoning_effort=effort)
        parsed = _extract_json(text)
        content = str(parsed.get("content") or "").strip()
        if not content:
            raise ValueError("content 为空")
        return _sanitize_doc_citations(content, citations)

    try:
        return {"task_type": task, "content": _once(), "generated_by": "llm",
                "citations": citations, "model_version": model,
                "disclaimer": "AI 生成文书草稿，供医生审核；不构成诊断或处方",
                "at": datetime.now(timezone.utc).isoformat()}, ""
    except (ValueError, ConnectionError) as exc:
        try:
            content = _once()
            return {"task_type": task, "content": content, "generated_by": "llm-repaired",
                    "citations": citations, "model_version": model,
                    "disclaimer": "AI 生成文书草稿（原始输出经一次受限修复），供医生审核",
                    "at": datetime.now(timezone.utc).isoformat()}, ""
        except (ValueError, ConnectionError) as exc2:
            return None, f"LLM 请求失败：{str(exc2)[:160]}"


def _make_edit_llm(sess: dict, citations: list, base_url, api_key, model, effort):
    """病例编辑：把当前草稿/字段按医生指令重写为新的结构化草稿（复用 normalize 校验）。"""
    prev = sess.get("draft")
    instruction = str(sess.get("task_text") or "")
    missing = sess.get("missing") or []
    prompt = "\n\n".join([
        "你是皮肤科循证分析引擎。医生要求在现有草稿基础上编辑病例，只输出 JSON 本体（schema_version=1.1，"
        "snake_case），字段结构与首次分析完全一致（workflow_state/case_summary/suspected_conditions/"
        "symptom_assessment/medication_checks/medication_recommendations/treatment_reference/severity/"
        "recommended_next_steps/uncertainties/citations/disclaimer）。",
        "硬约束：只执行医生的编辑指令；未提及的部分原样保留；措辞「待排/可能/建议医生核对」；"
        "引用只能使用白名单 citation_id；未知信息保持未知，不得写成「无/否认」；禁止剂量、频次、起停医嘱；"
        "药物建议仅作医生审核参考，保留通用名、用途与参考使用时长，并强制 requires_doctor_confirmation=true；简体中文。",
        f"医生编辑指令：{instruction[:400]}",
        f"当前草稿：{json.dumps(prev, ensure_ascii=False) if prev else '（无——请按病例字段生成）'}",
        f"病例字段：{json.dumps(sess.get('case') or {}, ensure_ascii=False)}",
        f"citation_id 白名单：{[c['citation_id'] for c in citations] or '（空——不得编造引用）'}",
        f"缺失字段：{'；'.join(missing) or '无'}",
    ])
    try:
        text = _chat(base_url, api_key, model, prompt, reasoning_effort=effort)
        try:
            parsed = _extract_json(text)
        except ValueError:
            repair = _chat(base_url, api_key, model,
                           "请修复以下内容为合法 JSON 并只输出 JSON 本体：\n" + text, reasoning_effort=effort)
            parsed = _extract_json(repair)
        draft = normalize_llm_draft(parsed, citations, sess["red_flags"], missing, model, "llm-edited")
        draft["edited_from"] = "previous_draft" if prev else "case_fields"
        return draft, ""
    except (ValueError, ConnectionError) as exc:
        return None, f"LLM 请求失败：{str(exc)[:160]}"


LLM_CONFIG_FILE = llm_config_mod.CONFIG_FILE


def load_llm_config() -> dict:
    """兼容旧调用方：读取活动配置；测试和旧调用可覆盖 LLM_CONFIG_FILE。"""
    # 旧测试/部署会替换模块级路径；同步到配置模块后仍走统一迁移解析。
    if llm_config_mod.CONFIG_FILE != LLM_CONFIG_FILE:
        llm_config_mod.CONFIG_FILE = LLM_CONFIG_FILE
    c = llm_config_mod.active_connection()
    if c:
        return {"base_url": c["base_url"], "api_key": c["api_key"], "model": c["model"],
                "thinking_level": c.get("thinking_level", ""), "connection_id": c["id"],
                "provider": c.get("provider", "custom"), "name": c.get("name", "")}
    return {"base_url": "", "api_key": "", "model": "", "thinking_level": ""}
    if c:
        return {"base_url": c["base_url"], "api_key": c["api_key"], "model": c["model"],
                "thinking_level": c.get("thinking_level", ""), "connection_id": c["id"],
                "provider": c.get("provider", "custom"), "name": c.get("name", "")}
    return {"base_url": "", "api_key": "", "model": "", "thinking_level": ""}


def _make_draft_server(case, citations, red_flags, missing, demo: str = ""):
    """受控模型适配（v58 防伪装改造）：返回 (draft | None, 错误原因)。

    LLM 可用 → 生成 + 修复一次 + 归一化，generated_by 标记真实来源；
    自定义病例 LLM 未配置/请求失败/非法输出 → (None, 原因)——绝不生成规则稿冒充 AI 建议；
    demo 病例例外：规则稿是明确标注的离线演示数据（前端渲染离线横幅），不伪装成真实分析。

    LLM 连接配置来源优先级：设置面板写入的 data/llm_config.json（医生本机自填，Key 不入日志/审计）
    → 环境变量 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（逐字段回退）。"""
    cfg = load_llm_config()
    base_url = (cfg["base_url"] or os.environ.get("LLM_BASE_URL", "")).strip().rstrip("/")
    api_key = cfg["api_key"] or os.environ.get("LLM_API_KEY", "").strip()
    model = cfg["model"] or os.environ.get("LLM_MODEL", "").strip()
    effort = cfg.get("thinking_level", "")          # v47：思考等级（空=不发参数，行为与旧版一致）
    if not (base_url and model):
        if demo:
            return build_draft(case, citations, red_flags, missing), ""
        return None, "未配置 LLM 连接（设置 → LLM 与模型）"
    try:
        text = _chat(base_url, api_key, model, _analyze_prompt(case, citations, red_flags, missing),
                     reasoning_effort=effort)
        try:
            return normalize_llm_draft(_extract_json(text), citations, red_flags, missing, model, "llm"), ""
        except ValueError:
            repair = _chat(base_url, api_key, model,
                           "请修复以下内容为合法 JSON 并只输出 JSON 本体：\n" + text,
                           reasoning_effort=effort)
            draft = normalize_llm_draft(_extract_json(repair), citations, red_flags, missing, model, "llm-repaired")
            draft.setdefault("uncertainties", []).append("原始模型输出为非法 JSON，经一次受限修复后通过结构化校验")
            return draft, ""
    except (ValueError, ConnectionError) as exc:
        if demo:
            return build_draft(case, citations, red_flags, missing,
                               review_note=f"模型调用失败（{str(exc)[:120]}）：离线演示数据"), ""
        return None, f"LLM 请求失败：{str(exc)[:160]}"


def _analyze_prompt(case, citations, red_flags, missing) -> str:
    from derm_core import DISCLAIMER  # noqa: F401
    return "\n\n".join([
        "你是皮肤科循证分析引擎，基于病例与知识库检索结果生成待审核草稿。只输出 JSON 本体"
        "（schema_version=1.1，snake_case），字段：workflow_state(awaiting_review|escalated)、"
        "case_summary{selected_body_region,symptom_summary,data_completeness},"
        "suspected_conditions[{name,rank,confidence(low|medium|high|unknown),supporting_evidence[{statement,citation_ids[]}],"
        "missing_information[],red_flags[]}],symptom_assessment[{symptom,status(present|absent|unknown|not_asked),basis,"
        "citation_ids[]}],medication_checks[{generic_name,purpose,items_to_verify,requires_doctor_confirmation:true,"
        "citation_ids[]}],medication_recommendations[{generic_name,purpose,reference_duration,usage_notes,"
        "contraindications_or_checks[],requires_doctor_confirmation:true,citation_ids[]}],severity{level,basis[],citation_ids[]},"
        "recommended_next_steps[{action,urgency,citation_ids[]}],recommended_exams[{exam,reason,citation_ids[]}],"
        "treatment_reference[{approach,indication_or_goal,basis,when_to_refer,citation_ids[]}],"
        "recovery_period{typical_course,influence_factors[],citation_ids[]},"
        "uncertainties[],citations[{citation_id,document_title,document_version,source_type,page_or_section,quoted_text}],"
        "disclaimer。硬约束：鉴别诊断措辞「待排/可能」，拿不准 confidence=unknown；suspected_conditions 按可能性"
        "从高到低排序，可能性较高的排前面，每条尽量给 supporting_evidence（判断依据）；引用只能使用检索白名单中的 "
        "citation_id，禁止编造；status=unknown/not_asked 不得写成 absent；禁止剂量、频次、起停医嘱；"
        "药物建议仅作医生审核参考，必须精确到具体药品通用名（如「莫匹罗星软膏」「氯雷他定」），"
        "禁止只写药物类别词；每条含用途与参考使用时长；参考时长优先用知识库依据，无依据时给该适应证常见疗程并标「待医生核实」；"
        "所有药物条目 requires_doctor_confirmation=true；recommended_exams=建议检查、treatment_reference=治疗方向（非处方）、"
        "medication_recommendations=用药参考（非处方）、recovery_period=恢复周期及影响因素；"
        "知识库与病例未覆盖的部分如实留空数组/空串，不得编造；简体中文。",
        f"病例：{json.dumps(case, ensure_ascii=False)}",
        f"citation_id 白名单：{[c['citation_id'] for c in citations] or '（空——知识库未覆盖，不得给鉴别诊断）'}",
        f"检索片段：{json.dumps(citations, ensure_ascii=False)}",
        f"红旗：{'；'.join(red_flags) or '无'}；缺失：{'；'.join(missing) or '无'}",
    ])


class _LlmTimeout(ConnectionError):
    """LLM 读超时（v73）：与可重试的网络抖动区分——慢思考模型重试大概率仍超时，
    直接落 llm_error 错误卡，不让用户白等两轮超时。"""


def _chat(base_url, api_key, model, user: str, reasoning_effort: str = "", system: str = "") -> str:
    """OpenAI 兼容 /chat/completions 调用。

    v47：reasoning_effort（low/medium/high）非空时注入请求体；网关不支持该字段
    返回 400/404/422 时自动去掉参数重试一次——思考等级是尽力而为的增强，不得
    让草稿生成因此失败（失败不变性）。
    v59：所有失败（HTTP/网络）最终都归一为 ConnectionError，调用方落 llm_unavailable
    防线，绝不 500 冒泡；外层对瞬时抖动（网关间歇性 unknown provider / 5xx）做一次
    退避重试——生成请求无副作用，重试安全。
    v64：system 可选（smalltalk 等对话任务的 Agent 设定）；非流式解析。"""

    def _post(effort: str):
        msgs = ([{"role": "system", "content": system}] if system else []) \
            + [{"role": "user", "content": user}]
        payload = {"model": model, "temperature": 0, "stream": False, "messages": msgs}
        if effort:
            payload["reasoning_effort"] = effort
        req = urllib.request.Request(f"{base_url}/chat/completions",
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json; charset=utf-8",
                                              # 浏览器式 UA：网关（Cloudflare）拦截 Python-urllib 默认签名（403/1010）
                                              "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                                              **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
                                     method="POST")
        # LLM 请求必须在有限时间内收敛；避免前端永久停留在“正在思考”。
        # 单次请求 75 秒（v71：诊断草稿长提示词在网关慢载下 45s 不够，曾双超时落 llm_error），
        # 外层仅做一次快速重试。
        # v73：思考型模型（glm-5.3 等）先思考再输出，草稿大提示词 75s 不够 → 提到 150s；
        # 超时不再退避重试（重试大概率仍超时，白等一轮），仅网络抖动类错误保留一次重试。
        with urllib.request.urlopen(req, timeout=150, context=_http_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _http_fail(exc: urllib.error.HTTPError) -> ConnectionError:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:200]
        except Exception:  # noqa: BLE001
            pass
        return ConnectionError(f"HTTP {exc.code}：{detail}")

    def _once() -> str:
        try:
            data = _post(reasoning_effort)
        except urllib.error.HTTPError as exc:
            if reasoning_effort and exc.code in (400, 404, 422):
                try:
                    data = _post("")            # 参数不被支持 → 去掉重试一次，草稿照常生成
                except urllib.error.HTTPError as exc2:
                    raise _http_fail(exc2) from exc2
                except (urllib.error.URLError, TimeoutError, OSError) as exc2:
                    raise ConnectionError(f"网络不可达：{exc2}") from exc2
            else:
                raise _http_fail(exc) from exc
        except TimeoutError as exc:             # 读超时：慢思考模型，重试大概率仍超时
            raise _LlmTimeout(f"网络不可达：{exc}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ConnectionError(f"网络不可达：{exc}") from exc
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")

    try:
        return _once()
    except _LlmTimeout:
        raise                                    # 超时不重试：直接落错误卡，把等待时间还给用户
    except ConnectionError:
        time.sleep(1.5)                         # 退避后重试一次；仍失败由调用方落防线
        return _once()


def _extract_json(text: str) -> dict:
    if not text:
        raise ValueError("空输出")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    if start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError("输出中不含合法 JSON")


sess_make_draft = _make_draft_server


# —— API 契约 ——

class StartBody(BaseModel):
    text: str = ""
    demo: str = ""
    # 医生端结构化资料（阶段3/4 脱敏载荷：hx/labs/report/meds/allergy/past/tx/imaging）。
    # 仅在自定义病例时并入 case["clinic_payload"]，随病例 JSON 进入 LLM 提示词；
    # 规则引擎路径不读取该键，demo 病例不并入，保持既有结构不受污染。
    clinic: dict = {}
    # v64 会话状态对象：3D 选点（可选上下文）与附件元信息随消息入会话，切换模型/重启不丢
    regions: list = []
    attachments: list = []


class SupplementBody(BaseModel):
    reply: str


class ChatBody(BaseModel):
    """v59 Agent 对话：同病例下继续自然语言指令（任务识别在服务端，客户端不判断）。"""
    text: str
    clinic: dict = {}
    regions: list = []          # v64：可选 3D 选点上下文（增量并入会话）
    attachments: list = []      # v64：附件元信息（会话状态对象）


class ReviewBody(BaseModel):
    feedback: str
    # 阶段6：action="regenerate" 表示医生要求重新生成整份建议（其余留空，按 feedback 分类）。
    action: str = ""


# —— 阶段7：安全闭环（病例资料白名单 / 3D refinement 边界 / 影像适配器注册） ——

# 资料白名单：clinic_payload 仅接受以下键，未知键丢弃并审计（防未声明数据进入服务端）
CLINIC_ALLOWED_KEYS = {"hx", "labs", "report", "meds", "allergy", "past", "tx", "imaging", "deidentified", "patient"}
# v56 病历单患者信息子白名单（姓名/门诊号在客户端发送前已脱敏为类别占位；这里只放行声明过的键与字符串值）
PATIENT_ALLOWED_KEYS = {"name", "sex", "age", "no", "date"}

# 影像适配器注册表：能力与边界声明。影像分析永不与 3D refinement 混用同一通道；
# ai_enabled=False → 影像不自动送模型，仅医生端本地校验/展示，是否分析由医生在端上决定。
IMAGING_ADAPTERS = {
    "local_upload": {"accept": ["png", "jpg", "jpeg", "webp"], "max_bytes": 10 * 1024 * 1024,
                     "ai_enabled": False,
                     "boundary": "影像仅在医生端本地校验与展示，不自动送模型分析；上传通道与 3D refinement 分离"},
}

# 3D refinement 只接受几何摘要；出现任一键即拒绝（与客户端桥接校验同名单，双端兜底）
REFINE_FORBIDDEN_KEYS = {"screenshot", "image", "glb", "model_data", "bytes", "base64"}
REFINE_MAX_REGIONS = 64


# —— v65：皮肤科门诊病历单（病例窗口数据模型） ——
# 17 个字段对齐真实医院病历单结构；字段值缺失留空字符串（渲染端显示「未提供/待补充」，
# 服务端绝不自动填「无/否认」——阴性描述只能由医生或真实病史给出）。
RECORD_FIELDS = [
    ("patient", "患者基本信息"),
    ("chief_complaint", "主诉"),
    ("present_illness", "现病史"),
    ("past_history", "既往史"),
    ("personal_history", "个人史"),
    ("allergy_history", "过敏史"),
    ("medication_history", "用药史"),
    ("physical_exam", "体格检查"),
    ("skin_lesions", "皮损描述"),
    ("auxiliary_exam", "辅助检查"),
    ("body_regions", "身体部位"),
    ("impression", "初步判断/鉴别方向"),
    ("treatment_ref", "治疗参考"),
    ("medication_check", "用药核对"),
    ("follow_up", "随访与恢复情况"),
    ("danger_signals", "风险提示"),
    ("doctor_review", "医生审核意见"),
    ("notes", "备注"),
]
_RECORD_LABELS = dict(RECORD_FIELDS)
_RECORD_SOURCES = {"ai_draft", "doctor_modified", "doctor_confirmed",
                   "user_input", "model_3d", "kb_ref"}   # v66：来源细分（面板徽章着色）

# doc markdown 标题关键词 → record 字段（LLM 按模板分节生成；未匹配标题不强塞）
# v66：字段整理类任务 → 直接收录的字段（正文即该字段内容；整份文书任务不在表内）
_DOC_TASK_FIELD = {
    "history": "present_illness", "lesion_desc": "skin_lesions",
    "differential": "impression", "treatment_ref": "treatment_ref",
    "medication_review": "medication_check", "patient_edu": "follow_up",
    "followup": "follow_up",
}

_DOC_SECTION_MAP = [
    ("chief_complaint", ("主诉",)),
    ("present_illness", ("现病史",)),
    ("past_history", ("既往史", "病史")),
    ("personal_history", ("个人史",)),
    ("allergy_history", ("过敏史", "过敏")),
    ("medication_history", ("用药史", "当前用药", "用药")),
    ("physical_exam", ("体格检查", "查体")),
    ("skin_lesions", ("皮损",)),
    ("auxiliary_exam", ("辅助检查", "检验", "检查结果")),
    ("impression", ("鉴别", "初步判断", "印象", "分析")),
    ("treatment_ref", ("治疗", "处理建议", "处理")),
    ("medication_check", ("用药核对", "用药审查")),
    ("follow_up", ("随访", "恢复", "观察指标", "患者教育", "恢复周期")),
    ("danger_signals", ("危险信号", "风险提示", "红旗", "就医提示", "就医等级")),
]


def _merge_clinic_payload(sess: dict, clinic) -> None:
    """v73-fix：病历单（clinic payload）随消息提交 → 真正并入病例/病历字段（只填空，绝不覆盖）。
    此前 chat 端点丢弃 clinic——Agent get_case_status 读到的永远是空病历单，
    只能本地话术回复「病历单是空的」。并入后 LLM 基于真实内容思考反馈。"""
    if not isinstance(clinic, dict) or not clinic:
        return
    g = lambda k: str(clinic.get(k) or "").strip()
    patient = clinic.get("patient") if isinstance(clinic.get("patient"), dict) else {}
    # ① 病例结构化字段（分析管线/missing 计算读取；仅填空）
    case = sess.setdefault("case", {})
    for ck, cv in (("symptoms", g("hx")), ("past_history", g("past")),
                   ("allergies", g("allergy")), ("current_medications", g("meds"))):
        if cv and not str(case.get(ck) or "").strip():
            case[ck] = cv
    if patient and not str(case.get("patient_info") or "").strip():
        info = "；".join(f"{k}：{patient[k]}" for k in ("name", "sex", "age", "no") if patient.get(k))
        if info:
            case["patient_info"] = info
    # ② 门诊病历单记录字段（Agent get_case_status 的唯一数据源；仅填空）
    rec = sess.get("record")
    if isinstance(rec, dict) and isinstance(rec.get("fields"), dict):
        fields, meta = rec["fields"], rec.setdefault("meta", {})
        labs_txt = "；".join(f"{l.get('name')}={l.get('value')}{(' ' + l['unit']) if isinstance(l, dict) and l.get('unit') else ''}"
                             for l in (clinic.get("labs") or []) if isinstance(l, dict) and (l.get("name") or l.get("value")))
        aux = "；".join(x for x in (g("report"), labs_txt) if x)
        fills = {"present_illness": g("hx"), "past_history": g("past"),
                 "allergy_history": g("allergy"), "medication_history": g("meds"),
                 "auxiliary_exam": aux}
        if patient:
            fills["patient"] = "；".join(f"{k}：{patient[k]}" for k in ("name", "sex", "age", "no", "date") if patient.get(k))
        now = datetime.now(timezone.utc).isoformat()
        protected = {"doctor_confirmed", "doctor_modified"}   # 医生确认/手改过的字段绝不覆盖
        for key, val in fills.items():
            if not val:
                continue
            cur_src = (meta.get(key) or {}).get("source", "")
            if cur_src in protected:
                continue
            fields[key] = val
            meta[key] = {"source": "clinic_form", "at": now}


def _filter_clinic_payload(clinic) -> dict:
    """资料白名单过滤：只保留声明过的键；未知键丢弃并审计。patient 为嵌套白名单。"""
    if not isinstance(clinic, dict):
        return {}
    dropped = [k for k in clinic if k not in CLINIC_ALLOWED_KEYS]
    if dropped:
        audit("clinic_payload_keys_dropped", dropped)
    clean = {k: v for k, v in clinic.items() if k in CLINIC_ALLOWED_KEYS}
    if isinstance(clean.get("patient"), dict):
        p = {pk: pv for pk, pv in clean["patient"].items()
             if pk in PATIENT_ALLOWED_KEYS and isinstance(pv, str)}
        if p:
            clean["patient"] = p
        else:
            del clean["patient"]   # 子键全被滤掉 → 整键不出现，不伪造空对象
    else:
        clean.pop("patient", None)   # 缺失/非字典一律不出现，与其他键「缺失即不出现」一致
    return clean


@app.post("/api/anatomy/refine")
def anatomy_refine(body: dict):
    """3D refinement：只接受几何摘要（区域坐标/面片索引），拒绝截图/影像/模型字节。
    与影像分析边界分离：本端点永不处理任何位图数据；不影响病例状态机。"""
    if not isinstance(body, dict) or not isinstance(body.get("regions"), list):
        audit("refine_rejected", "invalid request")
        raise HTTPException(status_code=400, detail="invalid refinement request")
    forbidden = sorted(REFINE_FORBIDDEN_KEYS & set(body.keys()))
    if forbidden:
        audit("refine_forbidden_keys_rejected", forbidden)
        raise HTTPException(status_code=400,
                            detail="refinement request may contain geometry summary only: " + ",".join(forbidden))
    regions = body["regions"][:REFINE_MAX_REGIONS]
    audit("refine_accepted", {"regions": len(regions), "model_key": str(body.get("model_key", ""))[:60]})
    return {"ok": True, "boundary": "geometry-summary-only", "received": len(regions),
            "model_key": str(body.get("model_key", ""))[:60],
            "imaging_boundary": "separate",
            "note": "几何精细化仅基于摘要；影像分析必须走影像适配器通道，两者不混合"}


@app.get("/api/imaging/adapters")
def imaging_adapters():
    """影像适配器注册表：客户端据此对齐白名单/上限；服务端不收位图，仅声明能力边界。"""
    audit("imaging_adapters_queried", None)
    return {"adapters": IMAGING_ADAPTERS, "ai_enabled_default": False}


@app.post("/api/cases")
def start_case(body: StartBody):
    demo = body.demo or None
    text = body.text or ""
    # v59 Agent 化：首条消息即任务指令——服务端识别任务类型（3D 选点等上下文已并入 text），
    # 原始指令保留给编辑类任务复用；对话历史从首条消息开始累计。
    task_type = _classify_task(text)
    # v73：指代最近临时轮次的延续指令（「改回来」等）→ 继续按设置处理（带记忆）
    if not demo and _EPHEMERAL_MEMORY and _MEM_CONT_RE.match(text.strip()):
        task_type = "settings_control"
    # v73-fix：设置/压缩指令不创建病历——就地执行动作并返回临时回复快照（不入病历库、不产生假草稿按钮）
    if not demo and task_type in ("settings_control", "context_compress"):
        _mem_append("user", text)
        sess = {"case_id": "", "demo": "", "label": "设置", "case": {}, "missing": [],
                "red_flags": [], "ask_count": 0, "phase": "chat_reply", "workflow_state": "chat_reply",
                "task_type": task_type, "task_text": text, "doc": None, "messages": [],
                "draft": None, "question": "", "report": "", "message": "",
                "selected_body_regions": [], "attachments": body.attachments or [], "tool_history": [],
                "review_status": "pending", "result_version": "r0",
                "updated_at": datetime.now(timezone.utc).isoformat()}
        if task_type == "context_compress":
            _mem_compress()   # v73：压缩跨请求上下文记忆
            sess["llm_meta"] = {"ok": False, "source": "none", "model": "", "error": "local_summary"}
            sess["reply"] = {"content": "已压缩跨请求上下文记忆：早前临时轮次已转为要点摘要（最近对话原样保留）。"}
            snap = _snapshot(sess)
        else:
            try:
                snap = _route_settings(sess)
            except Exception as exc:  # noqa: BLE001 — 设置执行失败也不 500，如实回复
                audit("settings_no_case_failed", str(exc)[:120])
                sess["llm_meta"] = {"ok": False, "source": "none", "model": "", "error": "settings_failed"}
                sess["reply"] = {"content": "设置执行失败，请稍后重试或到 ⚙ 设置页手动修改。"}
                snap = _snapshot(sess)
        snap["case_id"] = ""
        snap["task_type"] = "settings_control"
        snap["workflow_state"] = "chat_reply"
        audit("settings_no_case", {"text": text[:60]})
        _mem_append("assistant", (snap.get("reply") or {}).get("content") or "")
        return snap
    if not demo and task_type != "smalltalk":
        from derm_core import match_demo_case
        demo = match_demo_case(text)   # v64：闲聊不触发演示病例（「你好」不该变成病例）
    if demo and demo in DEMO_CASES:
        case = dict(DEMO_CASES[demo]["case"])
        label = DEMO_CASES[demo]["label"]
    else:
        # “进行诊断”是流程指令，不是患者症状；诊断入口必须创建空病例。
        case = {} if _is_diagnosis_entry(text) else build_case_from_text(text)
        # 侧栏标题 = 用户首条消息（压平空白、限长）；闲聊仍显示「对话」
        label = ("对话" if task_type == "smalltalk"
                 else re.sub(r"\s+", " ", text).strip()[:50] or "新病例")
        if isinstance(body.clinic, dict) and body.clinic:
            case["clinic_payload"] = _filter_clinic_payload(body.clinic)   # 阶段7：资料白名单
    case_id = uuid.uuid4().hex[:12]
    sess = {"case_id": case_id, "demo": demo or "", "label": label, "case": case, "missing": [],
            "red_flags": [], "ask_count": 0, "phase": "screening", "workflow_state": "screening",
            "task_type": task_type, "task_text": text, "doc": None, "messages": [],
            # 侧栏显示用首条消息（旧会话无此字段时列表接口回退 task_text/label）
            "initial_text": re.sub(r"\s+", " ", text).strip()[:80],
            "draft": None, "question": "", "report": "", "message": "",
            # v64 会话状态对象：连续对话上下文与可选上下文随会话持久化
            "selected_body_regions": [str(r) for r in (body.regions or []) if str(r).strip()],
            "attachments": body.attachments or [], "tool_history": [],
            "review_status": "pending", "result_version": "r0", "_make_draft": sess_make_draft,
            "updated_at": datetime.now(timezone.utc).isoformat()}
    # v64-P1：首条消息也进对话历史（重启恢复最近会话时第一轮不缺席）
    sess.setdefault("messages", []).append({"role": "user", "content": text[:500],
                                            "at": datetime.now(timezone.utc).isoformat()})
    # 诊断入口采用分阶段流程：先填写病例，不在首轮直接触发 RAG/LLM 诊断。
    kickoff_done = False
    if _is_diagnosis_entry(text):
        sess.update({"phase": "case_intake", "workflow_state": "case_intake",
                     "question": "请先填写病例信息，并在病例单的身体部位字段中选择（可选）3D 人体位置。完成后回复‘病历填写完成’。"})
        # v71：Agent 开工回合——「开始诊断」即由 Agent 启动病历填写（对话式开场与聚焦追问），
        # 不再是本地固定文案；LLM 未配置/调用失败 → None → 落回固定文案（断网降级不变）。
        _kick_base, _kick_key, _kick_model, _kick_eff = _llm_ready()
        snap = None
        if _kick_base and _kick_model:
            try:
                import agent_loop
                snap = agent_loop.run_agent_turn(sess, text, _agent_ctx(text))
            except Exception as exc:  # noqa: BLE001 — 开工失败落回固定文案，不阻塞建案
                audit("agent_kickoff_failed", str(exc)[:160])
                snap = None
            if snap is not None:
                kickoff_done = True
                audit("chat_agent", {"case_id": case_id, "text": text[:120], "stage": "kickoff",
                                     "tools": [t.get("tool") for t in (sess.get("tool_history") or [])[-6:]]})
        if snap is None:
            snap = {"case_id": case_id, "reply": {"content":
                "已进入诊断。请先填写病例单：患者基本信息、主诉、现病史、既往史、过敏史、用药史和皮损描述。\n"
                "身体部位可直接填写，也可以在病例单中打开 3D 人体模型选择。完成后回复‘病历填写完成’，我再进行情况补充。"},
                "workflow_state": "case_intake", "task_type": "diagnosis"}
    else:
        # v73-fix：首条消息也强制走 LLM（Agent 思考后真实反馈）——乱输入不再触发本地计划卡。
        # LLM 不可用/调用失败时才降级规则管线（降级结果如实标注非 AI 生成）。
        snap = None
        _base_a, _key_a, _model_a, _eff_a = _llm_ready()
        if _base_a and _model_a and not _classify_agent_command(text):
            try:
                import agent_loop
                snap = agent_loop.run_agent_turn(sess, text, _agent_ctx(text))
            except Exception as exc:  # noqa: BLE001 — Agent 失败落回规则管线，不阻塞建案
                audit("agent_first_turn_failed", str(exc)[:120])
                snap = None
        if snap is not None:
            kickoff_done = True             # Agent 回合内已完成盖章/重建/落盘（与开工回合一致）
            audit("chat_agent", {"case_id": case_id, "text": text[:120], "stage": "first_turn",
                                 "tools": [t.get("tool") for t in (sess.get("tool_history") or [])[-6:]]})
        else:
            snap = _screen(sess)
    # v65：会话创建即初始化门诊病历单实体（3D 选点/患者信息/主诉就位）——
    # LLM 未连接或尚未生成任何 AI 内容时，医生也能直接在病例窗口手动填写保存。
    _merge_clinic_payload(sess, body.clinic)   # v73-fix：首条消息携带的病历单内容并入病例字段
    _sync_record(sess)
    if not kickoff_done:           # agent 开工回合内已完成盖章/重建/落盘，勿重复盖章
        _stamp_message(sess)       # v70：本轮回复盖章（前端工作按钮绑定此 id）
        snap = _rebuild_snapshot(sess, snap)
    if _is_diagnosis_entry(text):
        if not kickoff_done:
            snap["reply"] = {"content":
                "已进入诊断。请先填写病例单：患者基本信息、主诉、现病史、既往史、过敏史、用药史和皮损描述。\n"
                "身体部位可直接填写，也可以在病例单中打开 3D 人体模型选择。完成后回复‘病历填写完成’，我再进行情况补充。"}
            snap["workflow_state"] = "case_intake"
        snap.setdefault("case_id", case_id)
        # 诊断工作流的第一项工作就是填写病例，首轮回复后直接打开病例工作区。
        snap["open_case"] = True
        _apply_nav(snap, sess)
    _persist_msg_artifacts(sess, snap)
    if _wants_case_window(text):   # v65：首条消息即要求开病例窗口（如「新建一份病例」）→ 开窗标志
        snap["open_case"] = True
        _apply_nav(snap, sess)     # v69：开窗标志置位后重算导航（否则仍停在对话工作区）
    with _lock:
        first_reply = ((snap.get("reply") or {}).get("content")
                       or (snap.get("doc") or {}).get("content")
                       or (snap.get("draft") or {}).get("case_summary") or "")
        sess.setdefault("messages", []).append({"role": "assistant", "content": str(first_reply)[:300],
                                                "message_id": sess.get("message_id") or "",
                                                "at": datetime.now(timezone.utc).isoformat()})
        sessions = _load_sessions()
        sess.pop("_make_draft", None)
        sess["updated_at"] = datetime.now(timezone.utc).isoformat()
        sessions[case_id] = sess
        _save_sessions(sessions)
    audit("case_started", {"case_id": case_id, "demo": demo, "label": label,
                           "clinic": bool(isinstance(body.clinic, dict) and body.clinic)})
    return snap


@app.get("/api/cases")
def list_cases():
    sessions = _load_sessions()
    return [{"case_id": s["case_id"], "label": s.get("label"),
             # 侧栏标题优先首条消息；旧会话回退 task_text / label
             "initial_text": (s.get("initial_text") or s.get("task_text")
                              or s.get("label") or "").strip(),
             "phase": s["phase"],
             "workflow_state": s.get("workflow_state"), "review_status": s.get("review_status", "pending"),
             "updated_at": s.get("updated_at")}
            for s in sorted(sessions.values(), key=lambda x: x.get("updated_at", ""), reverse=True)]


@app.get("/api/records")
def list_records():
    """v73 病历库：全部已产生病历（对话产生/诊断产生/手动填写），按更新时间倒序。
    只要病历单任一字段有内容即入库名录；正文随病例会话保存，点击续办查看。"""
    out = []
    sessions = _load_sessions()
    for s in sorted(sessions.values(), key=lambda x: x.get("updated_at", ""), reverse=True):
        rec = s.get("record") if isinstance(s.get("record"), dict) else {}
        fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
        filled = [k for k, v in fields.items() if str(v or "").strip()]
        # v75-fix：对话里「进行诊断/开始诊断」创建的病历单（case_intake）即使字段尚空也必须
        # 出现在历史病历——否则医生刚建完病历去历史病历里找不到。纯闲聊会话仍不入库。
        if not filled and s.get("workflow_state") != "case_intake" and s.get("phase") != "case_intake":
            continue
        def _brief(v, n: int = 46) -> str:
            return " ".join(str(v or "").split())[:n]

        pat_raw = _brief(fields.get("patient"), 400)
        _ms = re.search(r"(?:sex|性别)[：:]\s*([男女])", pat_raw)
        _ma = re.search(r"(?:age|年龄)[：:]\s*(\d{1,3})", pat_raw)
        pat = "·".join(x for x in ((_ms.group(1) + "性") if _ms else "",
                                   (_ma.group(1) + "岁") if _ma else "") if x) or pat_raw[:26]
        chief = _brief(fields.get("chief_complaint")) or _brief(fields.get("present_illness"))
        summary = " ｜ ".join(x for x in (pat, chief, _brief(fields.get("impression"))) if x)
        out.append({
            "case_id": s["case_id"],
            "title": (s.get("initial_text") or s.get("task_text") or s.get("label") or "新病例").strip()[:50],
            "updated_at": s.get("updated_at"),
            "review_status": rec.get("review_status") or s.get("review_status", "pending"),
            "filled_count": len(filled),
            "summary": summary,   # v75：病历内容关键信息（患者·主诉/现病史·初步判断），供列表直接浏览
        })
    return out


def _load_sess(case_id: str) -> dict:
    sess = _load_sessions().get(case_id)
    if not sess:
        raise HTTPException(404, f"病例不存在：{case_id}")
    sess["_make_draft"] = sess_make_draft
    return sess


def _store_sess(sess: dict):
    sess.pop("_make_draft", None)
    sess.pop("_ui_action", None)   # v73：设置动作一次性消费（已随本轮快照下发）
    sess["updated_at"] = datetime.now(timezone.utc).isoformat()
    with _lock:
        sessions = _load_sessions()
        sessions[sess["case_id"]] = sess
        _save_sessions(sessions)


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    return _snapshot(_load_sess(case_id))


@app.delete("/api/cases/{case_id}")
def delete_case(case_id: str):
    """v60：医生端删除历史病例（含全部会话状态与草稿）——列表与续办立即失效。

    病例数据仅存本机 sessions.json；删除为医生主动清理演示数据，硬删并审计。
    演示病例（demo 内置）与普通病例同等可删——重建演示只需重新触发触发词。"""
    with _lock:
        sessions = _load_sessions()
        sess = sessions.pop(case_id, None)
        if sess is None:
            raise HTTPException(404, f"病例不存在：{case_id}")
        _save_sessions(sessions)
    audit("case_delete", {"case_id": case_id, "label": sess.get("label"),
                          "workflow_state": sess.get("workflow_state")})
    return {"ok": True, "case_id": case_id, "deleted": True}


@app.delete("/api/cases")
def delete_all_cases():
    """v62：医生端清空全部历史病例——面板底部「删除全部」批量硬删，逐条计审计。"""
    with _lock:
        sessions = _load_sessions()
        removed = list(sessions.keys())
        sessions.clear()
        _save_sessions(sessions)
    for cid in removed:                     # 逐条落审计，保留每条病例的删除痕迹
        audit("case_delete", {"case_id": cid})
    audit("case_delete_all", {"count": len(removed)})
    return {"ok": True, "deleted": len(removed)}


@app.post("/api/cases/{case_id}/supplement")
def supplement(case_id: str, body: SupplementBody):
    with _lock:
        sess = _load_sess(case_id)
        sess["case_prev"] = json.loads(json.dumps(sess.get("case") or {}))
        sess["case"] = merge_reply(sess["case"], body.reply)
        sess["ask_count"] += 1
        sess["phase"] = "screening"
        snap = _screen(sess)
        _stamp_message(sess)       # v70：每轮回复独立 message_id（_screen 后结算产物）
        snap = _rebuild_snapshot(sess, snap)
        _persist_msg_artifacts(sess, snap)
        _store_sess(sess)
    audit("supplement", {"case_id": case_id, "reply": body.reply[:120]})
    return snap


# —— v71：Agent 工具循环桥接（LLM 自主决策 + 工具调用；工具内保留既有纪律） ——

# 医生确认语义分两档（比 _CORE_CONFIRMS 宽：覆盖「填完了」等自然说法）：
# - advance：病历填写完成/继续（「填完了」「可以了」…）——不含「开始诊断」
#   （按需求它是"启动工作"，不是阶段确认，否则首条消息会直接跳完整个流程）；
# - diagnose：确认提交/直接出诊断（「确认提交」「直接出」「生成诊断」…）。
# 生成 AI 辅助诊断的硬门槛在 agent_loop 工具侧还有一道服务端校验，这里只是开关。
_AGENT_ADVANCE_KEYS = ("确认", "提交", "完成", "填好", "填完", "好了", "下一步",
                       "继续", "可以了", "没问题", "就这样", "ok", "go")
_AGENT_DIAGNOSE_KEYS = ("确认提交", "提交诊断", "确认出", "直接出", "生成诊断",
                        "出诊断", "开始分析", "就这样出")


def _agent_save_fields(sess: dict, fields: dict) -> dict:
    """Agent 工具：医生口述信息 → 病历单字段。复用 save_record 的纪律：
    白名单拒绝未知字段；doctor_confirmed/doctor_modified 不覆盖（allow_ai_override 除外）；
    版本历史留痕；record→case 映射同步（保证分析输入一致）。"""
    incoming = {k: str(v or "").strip() for k, v in (fields or {}).items() if str(v or "").strip()}
    if not incoming:
        return {"ok": False, "updated": [], "rejected": [], "note": "无可写入内容"}
    unknown = [k for k in incoming if k not in _RECORD_LABELS]
    rec = sess.get("record") if isinstance(sess.get("record"), dict) else _empty_record()
    sess["record"] = rec
    now = datetime.now(timezone.utc).isoformat()
    history = rec.setdefault("history", [])
    protected = {"doctor_confirmed", "doctor_modified"}
    allow_override = bool(sess.get("allow_ai_override"))
    updated, rejected, notes = [], [], []
    for key, value in incoming.items():
        if key in unknown:
            rejected.append(key)
            notes.append(f"{key}:未知字段")
            continue
        if (rec["meta"].get(key) or {}).get("source") in protected and not allow_override:
            rejected.append(key)
            notes.append(f"{_RECORD_LABELS.get(key, key)}:医生已确认/修改，AI 不覆盖")
            continue
        before = str(rec["fields"].get(key) or "")
        if before != value:
            history.append({"field": key, "before": before, "after": value, "at": now, "by": "agent_llm"})
            if len(history) > 50:
                del history[:-50]
        rec["fields"][key] = value
        rec["meta"][key] = {"source": "user_input", "at": now}   # v66 语义：医生口述 = user_input
        updated.append(key)
    rec["rejected"] = False
    rec["updated_at"] = now
    # record→case 映射（与 save_record 相同，保证后续分析/检索输入同步）
    case = sess.setdefault("case", {})
    mapping = {"chief_complaint": "chief_complaint", "past_history": "past_history",
               "allergy_history": "allergies", "medication_history": "current_medications",
               "skin_lesions": "symptoms", "body_regions": "body_region"}
    for record_key, case_key in mapping.items():
        if record_key in updated:
            case[case_key] = str(rec["fields"].get(record_key) or "")
    if "present_illness" in updated:
        case["present_illness"] = str(rec["fields"].get("present_illness") or "")
    for field in ("notes", "physical_exam", "auxiliary_exam", "danger_signals"):
        if field in updated:
            case[field] = str(rec["fields"].get(field) or "")
    audit("agent_save_fields", {"case_id": sess.get("case_id"), "fields": updated[:20],
                                "rejected": rejected[:10]})
    return {"ok": bool(updated), "updated": updated, "rejected": rejected,
            "note": "；".join(notes)[:200]}


def _agent_complete_intake(sess: dict) -> dict:
    """Agent 工具：病历填写阶段完成（与 record/confirm 首次确认同一转换）。
    clarifying（开放写作澄清）也视为可完成——医生说「填完了」时同样进入情况补充。"""
    wf = sess.get("workflow_state") or sess.get("phase")
    if wf not in ("case_intake", "clarifying"):
        return {"ok": False, "error": f"当前阶段为 {wf}，无需执行病历完成"}
    sess.update({"phase": "case_supplement", "workflow_state": "case_supplement",
                 "question": "请补充症状变化、病程、诱因、伴随症状、接触史、图片或检查资料；"
                             "完成后告诉我，我再进行检索与辅助诊断。"})
    rec = sess.get("record") if isinstance(sess.get("record"), dict) else _empty_record()
    sess["record"] = rec
    rec["review_status"] = "draft"
    rec["updated_at"] = datetime.now(timezone.utc).isoformat()
    audit("record_intake_complete", {"case_id": sess.get("case_id"), "via": "agent_tool"})
    return {"ok": True, "phase": "case_supplement",
            "message": "已进入情况补充阶段"}


def _agent_run_screen(sess: dict) -> dict:
    """Agent 工具：完成情况补充 → screening → _screen（RAG 检索 + 草稿生成）。
    与关键词路径完全同一管线（红旗/引用/降级纪律不变）；医生确认已在工具侧校验。"""
    wf = sess.get("workflow_state") or sess.get("phase")
    if wf not in ("case_supplement", "awaiting_follow_up", "clarifying",
                  "awaiting_user_input", "awaiting_review"):
        return {"ok": False, "error": f"当前阶段为 {wf}，不能执行辅助分析"}
    sess["case_prev"] = json.loads(json.dumps(sess.get("case") or {}))
    sess.update({"phase": "screening", "workflow_state": "screening", "question": "",
                 "task_type": "case_write", "planned_task": "case_write",
                 "plan_confirmed": True, "clarify_min_gate": True})
    # 医生已明确确认提交 → 跳过机械追问上限直接出稿；「追问到理解充分」由 Agent 对话负责
    try:
        sess["ask_count"] = max(int(sess.get("ask_count", 0) or 0), MAX_ASK_ROUNDS)
    except (TypeError, ValueError):
        sess["ask_count"] = MAX_ASK_ROUNDS
    snap = _screen(sess)
    draft = snap.get("draft") if isinstance(snap.get("draft"), dict) else {}
    audit("agent_run_screen", {"case_id": sess.get("case_id"),
                               "draft": bool(draft), "missing": len(snap.get("missing") or [])})
    return {"ok": True, "phase": sess.get("workflow_state"),
            "missing": snap.get("missing") or [], "red_flags": snap.get("red_flags") or [],
            "draft_generated": bool(draft),
            "blocking_reason": str(draft.get("blocking_reason") or ""),
            "draft_workflow_state": draft.get("workflow_state") or "",
            "case_summary": str((draft.get("case_summary") or {}))[:300] if isinstance(draft.get("case_summary"), dict) else str(draft.get("case_summary") or "")[:300],
            "suspected_conditions": [{"name": c.get("name"), "confidence": c.get("confidence")}
                                     for c in (draft.get("suspected_conditions") or [])[:5]
                                     if isinstance(c, dict)],
            "medication_checks_count": len(draft.get("medication_checks") or []),
            "citations_count": len(draft.get("citations") or []),
            "next_step_doctor_review": True}


def _agent_kb_search(query: str, top_k: int = 3) -> list:
    try:
        from rag import search as kb_search
        return kb_search(query, top_k=max(1, min(int(top_k), 8))).get("citations", [])
    except Exception:  # noqa: BLE001 — 知识库故障不阻断 Agent 对话（如实无引用）
        return []


def _agent_finalize(sess: dict, text: str, content: str, tool_trace: list, model: str) -> dict:
    """v71：Agent 回合收口——与零 LLM 路径共用 v70 盖章管线（按钮只来自本轮真实产物）。"""
    from derm_core import DISCLAIMER
    now = datetime.now(timezone.utc).isoformat()
    sess.setdefault("messages", []).append({"role": "user", "content": text[:500], "at": now})
    sess["reply"] = {"content": content, "generated_by": "llm", "model": model,
                     "task_type": sess.get("task_type") or "case_write", "plan": False,
                     "missing": sess.get("missing") or [],
                     "disclaimer": DISCLAIMER if isinstance(sess.get("draft"), dict) else "",
                     "at": now}
    sess["llm_meta"] = {"ok": True, "source": "llm", "model": model, "generated_by": "agent-loop",
                        "task_type": sess.get("task_type") or "case_write", "error": "", "at": now}
    sess.pop("llm_error", None)
    sess["tool_history"] = ((sess.get("tool_history") or []) + tool_trace[-8:])[-20:]
    sess["message"] = ""
    _maybe_summarize(sess)
    _bump_result_version(sess)
    _stamp_message(sess)
    snap = _rebuild_snapshot(sess)
    _persist_msg_artifacts(sess, snap)
    sess["messages"].append({"role": "assistant", "content": str(content)[:300],
                             "message_id": sess.get("message_id") or "", "at": now})
    snap["tool_history"] = sess["tool_history"]
    return snap


def _agent_ctx(text: str) -> dict:
    """Agent 循环桥接上下文：工具实现全部指向 app 内既有纪律函数（单一权威实现）。"""
    return {"llm": _llm_ready(),
            "advance_confirm": _is_confirm(text) or any(k in text for k in _AGENT_ADVANCE_KEYS),
            "diagnose_confirm": any(k in text for k in _AGENT_DIAGNOSE_KEYS),
            "record_labels": _RECORD_LABELS,
            "save_fields": _agent_save_fields,
            "complete_intake": _agent_complete_intake,
            "run_screen": _agent_run_screen,
            "kb_search": _agent_kb_search,
            "finalize": _agent_finalize,
            "audit": audit}


@app.post("/api/cases/{case_id}/chat")
def chat(case_id: str, body: ChatBody):
    """v59 Agent 对话端点：同一病例下延续上下文的自然语言指令。
    - 病例补充类输入（case_write/general）并入病例字段；任务指令只进对话历史与 task_text。
    - 每轮都重跑任务路由；产物（draft/doc）随快照返回，前端按 task_type/doc 渲染。
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "消息不能为空")
    with _lock:
        sess = _load_sess(case_id)
        _merge_clinic_payload(sess, body.clinic)   # v73-fix：病历单内容随消息并入病例，Agent 才能真实检测
        current_phase = sess.get("phase") or sess.get("workflow_state")
        # v71：Agent 工具循环——LLM 自主决定是否调用工具推进流程（开始诊断→病历填写→
        # 情况补充→确认提交→辅助诊断由 Agent 编排；规则守底线见 agent_loop 工具实现）。
        # 显式导航指令零 LLM 即时响应不进循环；LLM 未配置/端点不支持 tools/调用失败
        # → run_agent_turn 返回 None，自然落入下方既有零 LLM 关键词流程（断网降级不变）。
        _base_a, _key_a, _model_a, _eff_a = _llm_ready()
        requested_task = _classify_task(text)
        # 明确的单功能指令走受控规则路由；否则 Agent 可能自行追加完整分析工具。
        # 计划确认态也必须由确定性门禁处理，不能让 LLM 抢先消费“确认”。
        explicit_scoped = requested_task not in ("case_write", "complete_analysis", "smalltalk")
        in_plan = current_phase == "planning"
        if (_base_a and _model_a) and not _classify_agent_command(text) and not explicit_scoped and not in_plan:
            if isinstance(body.regions, list):
                for r in (str(x).strip() for x in body.regions):
                    if r and r not in sess.setdefault("selected_body_regions", []):
                        sess["selected_body_regions"].append(r)
            if isinstance(body.attachments, list) and body.attachments:
                sess["attachments"] = (sess.get("attachments") or [])[-9:] + list(body.attachments)[-10:]
            try:
                import agent_loop
                snap = agent_loop.run_agent_turn(sess, text, _agent_ctx(text))
            except Exception as exc:  # noqa: BLE001 — Agent 循环任何意外都降级，不阻塞问诊
                audit("agent_turn_failed", str(exc)[:160])
                snap = None
            if snap is not None:
                _store_sess(sess)
                audit("chat_agent", {"case_id": case_id, "text": text[:120],
                                     "tools": [t.get("tool") for t in (sess.get("tool_history") or [])[-6:]]})
                return snap
        # 工作流按钮可能经过前端本地化/重试后带少量前后缀，不能只做完全相等判断。
        phase_confirm = (_is_confirm(text)
                         or text in ("确认执行", "下一步")
                         or "确认执行" in text
                         or text.startswith("下一步"))
        if current_phase == "case_intake" and (phase_confirm or any(k in text for k in ("病历填写完成", "填写完成", "病历完成"))):
            sess.update({"phase": "case_supplement", "workflow_state": "case_supplement",
                         "question": "病历基础信息已完成。请补充病情：症状变化、持续时间、诱因、疼痛/瘙痒、发热、接触史、图片或检查资料。补充完成后回复‘情况补充完成’。"})
            sess.setdefault("messages", []).append({"role": "user", "content": text[:500], "at": datetime.now(timezone.utc).isoformat()})
            snap = _snapshot(sess)
            snap["reply"] = {"content": "病历基础信息已完成。现在请补充病情变化、诱因、伴随症状、图片或检查资料。完成后回复‘情况补充完成’，再进入 RAG 和辅助诊断。"}
            _store_sess(sess)
            return snap
        if current_phase == "case_intake":
            # 病历填写期的聊天只是流程交流，不能自动当作患者症状。
            sess.setdefault("messages", []).append({"role": "user", "content": text[:500], "at": datetime.now(timezone.utc).isoformat()})
            snap = _snapshot(sess)
            snap["reply"] = {"content": "当前处于病历填写阶段。这条消息不会写入症状。请在右侧病例单填写资料；完成后点击‘医生确认’或发送‘病历填写完成’。"}
            _store_sess(sess)
            return snap
        if current_phase == "case_supplement" and (phase_confirm or any(k in text for k in ("情况补充完成", "资料补充完成", "补充完成"))):
            sess.update({"phase": "screening", "workflow_state": "screening", "question": "",
                         "task_type": "case_write", "planned_task": "case_write",
                         "plan_confirmed": True, "clarify_min_gate": True})
            sess.setdefault("messages", []).append({"role": "user", "content": text[:500], "at": datetime.now(timezone.utc).isoformat()})
            snap = _screen(sess)
            _stamp_message(sess)
            snap = _rebuild_snapshot(sess, snap)
            _persist_msg_artifacts(sess, snap)
            _store_sess(sess)
            return snap
        if current_phase == "case_supplement":
            # 只有补充问诊阶段的内容才合并为病例资料，并保持在该阶段等待医生确认完成。
            sess["case_prev"] = json.loads(json.dumps(sess.get("case") or {}))
            sess["case"] = merge_reply(sess.get("case") or {}, text)
            sess.setdefault("messages", []).append({"role": "user", "content": text[:500], "at": datetime.now(timezone.utc).isoformat()})
            _sync_record(sess)
            sess.update({"phase": "case_supplement", "workflow_state": "case_supplement"})
            snap = _snapshot(sess)
            snap["reply"] = {"content": "已记录本次情况补充。可以继续补充图片、检查资料或病情变化；资料完整后点击‘确认执行’进入 RAG 检索和辅助诊断。"}
            _store_sess(sess)
            return snap
        if isinstance(body.clinic, dict) and body.clinic:
            merged = _filter_clinic_payload(body.clinic)
            if merged:
                sess.setdefault("case", {}).setdefault("clinic_payload", {})
                sess["case"]["clinic_payload"].update(merged)   # 白名单过滤后增量并入
        task = _classify_task(text)
        # 补充/修正计划中的病例资料时，保持原任务但把新事实合并到病例，随后重建计划。
        prior_planned = str(sess.get("planned_task") or "")
        planning_supplement = (sess.get("workflow_state") == "planning" and not _is_confirm(text)
                               and task in ("case_write", "case_edit")
                               and task == str(sess.get("planned_task") or ""))
        # v74：计划确认态保持原任务；普通确认不得把 scoped task 变成完整分析。
        confirming = sess.get("workflow_state") == "planning" and _is_confirm(text)
        if confirming:
            sess["plan_confirmed"] = True
            task = sess.get("planned_task") or task
        elif task == "complete_analysis":
            # 保留独立任务名直到 _screen 完成计划门禁；门禁之后才进入完整 case_write 管线。
            sess["full_analysis_requested"] = True
        if planning_supplement and prior_planned:
            task = prior_planned
        sess["task_type"] = task
        # v68：确认轮的 text 是「确认」，把上一轮真实任务文本留档，供零 LLM 工具（如病例对比）
        # 在确认执行时仍能取到医生原始指令（含病例 ID）。
        sess["prev_task_text"] = str(sess.get("task_text") or "")
        sess["task_text"] = text
        if isinstance(body.regions, list):
            regions = [str(r) for r in body.regions if str(r).strip()]
            if regions:   # 3D 选点是可选上下文：增量合并，切换模型不丢失
                merged_regions = sess.setdefault("selected_body_regions", [])
                for r in regions:
                    if r not in merged_regions:
                        merged_regions.append(r)
        new_attachments = list(body.attachments) if isinstance(body.attachments, list) else []
        if new_attachments:
            sess["attachments"] = (sess.get("attachments") or [])[-9:] + new_attachments[-10:]
        # 对话历史（服务端持有，供文书/编辑任务的上下文提示词；截断防快照膨胀）
        msgs = sess.setdefault("messages", [])
        msgs.append({"role": "user", "content": text[:500], "at": datetime.now(timezone.utc).isoformat()})
        # 病例补充类输入才并入病例字段；独立功能的补充文本只更新任务上下文，不污染症状字段。
        is_feature_sub = text.startswith("皮损特征")   # 图片特征提交：追加到症状字段
        is_scoped_task = task not in ("case_write", "complete_analysis", "case_edit", "smalltalk")
        # 计划中的普通补充仍要合并病例，并保留原始事实用于计划摘要。
        if planning_supplement and prior_planned and not _is_directive_only(text):
            is_scoped_task = False
            task = prior_planned
        if (is_feature_sub or (task in ("case_write", "case_edit") and sess.get("draft") is None)) \
                and not is_scoped_task and not confirming and not _is_confirm(text) \
                and not _is_directive_only(text) and not _classify_agent_command(text):
            # v67：撤销快照——undo_last 指令回退到本次补充前状态（医生手动字段不受影响）
            sess["case_prev"] = json.loads(json.dumps(sess.get("case") or {}))
            if is_feature_sub:
                # v68 图片特征提交：追加到已有症状，不覆盖医生已录入的描述（merge_reply 是 set 语义）
                cur_sym = str((sess.get("case") or {}).get("symptoms") or "").strip()
                feats = text.split("：", 1)[-1].strip()
                sess["case"]["symptoms"] = (cur_sym + "；" + feats).strip("；； ") if cur_sym else feats
            else:
                sess["case"] = merge_reply(sess.get("case") or {}, text)
        sess["phase"] = "screening"
        summarized_now = _maybe_summarize(sess)   # v68 §十一.4：达阈值自动结构化摘要
        snap = _screen(sess)
        # v70：盖章必须在 _screen 之后——指纹要能看见本轮真实产出（分析/文书/引用），
        # 否则执行轮会被误判为「无产物」。随后重建快照带上本轮 message_id 与 artifacts。
        _stamp_message(sess)
        snap = _rebuild_snapshot(sess, snap)
        _persist_msg_artifacts(sess, snap)
        if summarized_now:
            snap["context_compressed"] = True
        if _wants_case_window(text):   # v65：对话里要求打开/新建病例窗口 → 响应带开窗标志
            snap["open_case"] = True
            _apply_nav(snap, sess)     # v69：开窗标志置位后重算导航（否则仍停在对话工作区）
        if new_attachments:
            # v68（计划 §五.2）：上传照片时询问是否分析，不默认执行、不阻塞主流程
            note = ("\n\n📎 检测到 " + str(len(new_attachments)) +
                    " 张图片附件。回复「分析图片」可进行图片质量提示与特征采集（可选，不执行任何自动诊断）。")
            rep_now = snap.get("reply")
            if isinstance(rep_now, dict) and rep_now.get("content"):
                rep_now["content"] = str(rep_now["content"]) + note
            elif not snap.get("message"):
                snap["message"] = note.strip()
        product = "doc" if snap.get("doc") else "draft"
        # v64：Agent 自然语言回复（smalltalk/clarify）也进对话历史，后续消息能理解上文
        content = ((snap.get("reply") or {}).get("content")
                   or (snap.get("doc") or {}).get("content")
                   or (snap.get("draft") or {}).get("case_summary") or "")
        msgs.append({"role": "assistant", "content": str(content)[:300],
                     "message_id": sess.get("message_id") or "",
                     "at": datetime.now(timezone.utc).isoformat()})
        _store_sess(sess)
    audit("chat", {"case_id": case_id, "task": task, "text": text[:120]})
    return snap


# —— v65：门诊病历单端点（病例窗口 查看/编辑/确认/重新生成/导出） ——

class RecordSaveBody(BaseModel):
    fields: dict = {}


class RecordConfirmBody(BaseModel):
    fields: list = []
    all: bool = False


class RecordRegenBody(BaseModel):
    field: str


@app.get("/api/cases/{case_id}/record")
def get_record(case_id: str):
    with _lock:
        sess = _load_sess(case_id)
        snap = _record_snapshot(sess)
    audit("record_get", {"case_id": case_id})
    return {"ok": True, "record": snap,
            "workflow_state": sess.get("workflow_state") or sess.get("phase") or ""}


@app.put("/api/cases/{case_id}/record")
def save_record(case_id: str, body: RecordSaveBody):
    """医生手工编辑保存：来源标记 doctor_modified；未知字段拒绝；空值=清空该字段。"""
    incoming = body.fields if isinstance(body.fields, dict) else {}
    unknown = [k for k in incoming if k not in _RECORD_LABELS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知病例字段：{unknown[:5]}")
    with _lock:
        sess = _load_sess(case_id)
        rec = sess.get("record") if isinstance(sess.get("record"), dict) else _empty_record()
        sess["record"] = rec
        now = datetime.now(timezone.utc).isoformat()
        history = rec.setdefault("history", [])
        changed = False
        for key, value in incoming.items():
            value = str(value or "").strip()
            before = str(rec["fields"].get(key) or "")
            if before != value:   # v66：医生修改留版本痕迹（前后值+时间），不覆盖历史
                history.append({"field": key, "before": before, "after": value, "at": now, "by": "doctor"})
                if len(history) > 50:
                    del history[:-50]
                changed = True
            rec["fields"][key] = value
            rec["meta"][key] = {"source": "doctor_modified", "at": now}
        rec["rejected"] = False   # 医生重新编辑视为撤销拒绝
        if changed:
            # v66：已确认病例被医生再次修改 → 审核状态回到草稿，未审核修改不得以「已确认」身份导出
            rec["review_status"] = "draft"
            rec["confirmed_at"] = None
        rec["updated_at"] = now
        # 病例面板是诊断采集阶段的权威输入源，保存后同步回分析所用 case。
        case = sess.setdefault("case", {})
        mapping = {
            "chief_complaint": "chief_complaint", "past_history": "past_history",
            "allergy_history": "allergies", "medication_history": "current_medications",
            "skin_lesions": "symptoms", "body_regions": "body_region",
        }
        for record_key, case_key in mapping.items():
            if record_key in incoming:
                case[case_key] = str(rec["fields"].get(record_key) or "")
        if "present_illness" in incoming:
            case["present_illness"] = str(rec["fields"].get("present_illness") or "")
        _store_sess(sess)
    audit("record_save", {"case_id": case_id, "fields": sorted(incoming.keys())})
    return {"ok": True, "record": _record_snapshot(sess), "nav": _record_nav(sess)}


@app.post("/api/cases/{case_id}/record/confirm")
def confirm_record(case_id: str, body: RecordConfirmBody):
    """医生确认：字段级 doctor_confirmed；全部字段确认 → review_status=confirmed（最终审核版本）。
    v66：body_regions 例外——它是 3D 选点的联动字段（来源 model_3d），确认后医生仍可
    继续选点更新，不能被确认锁封死（否则新选点永远写不进病例单）。"""
    targets = [k for k in _RECORD_LABELS] if body.all else [k for k in (body.fields or []) if k in _RECORD_LABELS]
    if not targets:
        raise HTTPException(status_code=422, detail="未指定要确认的字段")
    targets = [k for k in targets if k != "body_regions"]
    if not targets:
        raise HTTPException(status_code=422, detail="未指定要确认的字段")
    with _lock:
        sess = _load_sess(case_id)
        rec = sess.get("record") if isinstance(sess.get("record"), dict) else _empty_record()
        sess["record"] = rec
        now = datetime.now(timezone.utc).isoformat()
        if sess.get("workflow_state") == "case_intake" or sess.get("phase") == "case_intake":
            # 首次确认表示“病历填写完成”，不是最终医疗审核。
            sess.update({"phase": "case_supplement", "workflow_state": "case_supplement",
                         "question": "请补充症状变化、病程、诱因、伴随症状、接触史、图片或检查资料；完成后点击确认执行。"})
            rec["review_status"] = "draft"
            rec["updated_at"] = now
            _store_sess(sess)
            audit("record_intake_complete", {"case_id": case_id})
            return {"ok": True, "intake_completed": True, "workflow_state": "case_supplement",
                    "message": "病历填写已完成，现在进入情况补充。",
                    "record": _record_snapshot(sess), "nav": _record_nav(sess)}
        for key in targets:
            rec["meta"][key] = {"source": "doctor_confirmed", "at": now}
        all_confirmed = all(rec["meta"].get(k, {}).get("source") == "doctor_confirmed"
                            for k, _ in RECORD_FIELDS if k != "body_regions")
        rec["review_status"] = "confirmed" if all_confirmed else "draft"
        rec["confirmed_at"] = now if all_confirmed else None
        _store_sess(sess)
    audit("record_confirm", {"case_id": case_id, "fields": targets[:20],
                             "final": rec["review_status"] == "confirmed"})
    return {"ok": True, "review_status": rec["review_status"], "record": _record_snapshot(sess),
            "nav": _record_nav(sess)}


def _regen_field_llm(sess: dict, field: str) -> tuple:
    """单字段 LLM 重新生成：措辞纪律 + 引用白名单 + 会话上下文。返回 (文本|None, 错误)。"""
    base_url, api_key, model, effort = _llm_ready()
    if not (base_url and model):
        return None, "未配置 LLM 连接（设置 → LLM 与模型）"
    case = {k: v for k, v in (sess.get("case") or {}).items() if v}
    ctx = "\n".join(f"{'医生' if m.get('role') == 'user' else 'Agent'}：{str(m.get('content') or '')[:160]}"
                    for m in (sess.get("messages") or [])[-6:])
    cites = (sess.get("record") or {}).get("citations") or []
    prompt = "\n\n".join([
        "你是皮肤科门诊病历辅助引擎，为医生审核草稿生成单个病历字段内容。只输出该字段正文，"
        "不要标题、JSON、解释或分节符。术语纪律：疾病相关措辞用「疑似/考虑/需要排除/建议医生核对」；"
        "缺失信息写「未提供」，禁止自动写「无/否认」等阴性描述；药物内容只列药物类别、适应证、禁忌、"
        "过敏与相互作用核对项，禁止处方/剂量/疗程；不得确诊、不得编造检查结果或病史；"
        "重要医学结论在句末附 [citation_id]，只能使用下方白名单，白名单为空则不写引用。",
        f"目标字段：{_RECORD_LABELS.get(field, field)}",
        f"病例数据：{json.dumps(case, ensure_ascii=False)}",
        f"近期对话：\n{ctx or '（无）'}",
        f"citation_id 白名单：{[c.get('citation_id') for c in cites if isinstance(c, dict)] or '（空——不得写引用）'}",
    ])
    try:
        text = _chat(base_url, api_key, model, prompt, reasoning_effort=effort)
        return text.strip(), ""
    except (ValueError, ConnectionError) as exc:
        return None, f"LLM 请求失败：{str(exc)[:160]}"


@app.post("/api/cases/{case_id}/record/regen")
def regen_record(case_id: str, body: RecordRegenBody):
    """重新生成当前字段（AI 草稿）。LLM 未连接/失败 → ok:false + 明确提示，
    不伪造内容；已有字段值保持不动，医生仍可手动编辑保存。"""
    field = body.field if body.field in _RECORD_LABELS else ""
    if not field:
        raise HTTPException(status_code=422, detail="未知病例字段")
    text, err = _regen_field_llm(_load_sess(case_id), field)
    if text is None:
        audit("record_regen_failed", {"case_id": case_id, "field": field, "err": err[:120]})
        return {"ok": False, "llm_connected": False,
                "message": "当前未连接 LLM，AI 内容无法生成。您仍可手动填写并保存。"}
    with _lock:
        sess = _load_sess(case_id)
        rec = sess.get("record") if isinstance(sess.get("record"), dict) else _empty_record()
        sess["record"] = rec
        now = datetime.now(timezone.utc).isoformat()
        before = str(rec["fields"].get(field) or "")
        if before != text:   # v66：AI 重新生成同样留版本痕迹（可回溯被覆盖内容）
            history = rec.setdefault("history", [])
            history.append({"field": field, "before": before, "after": text, "at": now, "by": "ai"})
            if len(history) > 50:
                del history[:-50]
        rec["fields"][field] = text
        rec["meta"][field] = {"source": "ai_draft", "at": now}
        rec["updated_at"] = now
        _store_sess(sess)
    audit("record_regen", {"case_id": case_id, "field": field})
    return {"ok": True, "record": _record_snapshot(sess)}


@app.post("/api/cases/{case_id}/record/reject")
def reject_record(case_id: str):
    """v66：医生标记整份病例单为已拒绝（更多菜单）；重新编辑保存即自动撤销。"""
    with _lock:
        sess = _load_sess(case_id)
        rec = sess.get("record") if isinstance(sess.get("record"), dict) else _empty_record()
        sess["record"] = rec
        rec["rejected"] = True
        rec["review_status"] = "draft"
        rec["confirmed_at"] = None
        rec["updated_at"] = datetime.now(timezone.utc).isoformat()
        _store_sess(sess)
    audit("record_reject", {"case_id": case_id})
    return {"ok": True, "record": _record_snapshot(sess), "nav": _record_nav(sess)}


class RegionsBody(BaseModel):
    regions: list = []
    source: str = "model_3d"


@app.post("/api/cases/{case_id}/regions")
def set_regions(case_id: str, body: RegionsBody):
    """v69：3D 选点即时写入当前病例草稿（选中即落库，不依赖下一条消息）。

    - 仅做数据合并 + record 同步，不调 LLM、不进对话历史 → 3D 是可选工具，不阻塞普通对话；
    - 返回导航决策：选完回对话工作区并聚焦输入框（workspace=chat）；
    - 病例不存在时 404，绝不隐式新建（避免误开空病例）。"""
    regions = [str(r).strip() for r in (body.regions or []) if str(r).strip()]
    with _lock:
        sess = _load_sess(case_id)
        merged = sess.setdefault("selected_body_regions", [])
        for r in regions:                     # 增量合并：切换模型/视角不丢已选部位
            if r not in merged:
                merged.append(r)
        _sync_record(sess)                    # 3D 选点 → record.body_regions（来源 model_3d）
        # _sync_record 只在字段发生变化时更新值；选点本身也必须刷新更新时间与
        # 当前病例快照，避免前端面板仍持有旧的空字段。
        rec = sess.get("record") if isinstance(sess.get("record"), dict) else {}
        rec["updated_at"] = datetime.now(timezone.utc).isoformat()
        sess["updated_at"] = rec["updated_at"]
        _store_sess(sess)
        # 选完即回对话并聚焦输入框（显式导航指令：focus_input）
        nav = nav_mod.resolve(sess, {"case_id": case_id, "nav_intent": "focus_input",
                                     "selected_body_regions": merged})
        return {"ok": True, "selected_body_regions": list(merged),
                "record": _record_snapshot(sess), "nav": nav}


@app.get("/api/cases/{case_id}/record/export")
def export_record(case_id: str):
    """v66 门禁：只有医生确认（review_status=confirmed）后才可导出正式病例单，
    防止 AI 草稿/未审核内容以正式报告流出。"""
    with _lock:
        sess = _load_sess(case_id)
        rec = sess.get("record") if isinstance(sess.get("record"), dict) else {}
        if rec.get("review_status") != "confirmed":
            raise HTTPException(status_code=403,
                                detail="导出正式病例前需医生确认（病例窗口 → 医生确认）")
        md = _record_markdown(sess)
    audit("record_export", {"case_id": case_id, "final": True})
    return Response(content=md, media_type="text/markdown; charset=utf-8")


@app.post("/api/cases/{case_id}/review")
def review(case_id: str, body: ReviewBody):
    with _lock:
        sess = _load_sess(case_id)
        # 阶段6：AI 建议（sess["draft"]）与医生判断（sess["doctor_final"]/review_history）分开保存；
        # 完整审核历史逐条留存，医生未确认前不写任何正式记录（draft 只能带 review_status/修订摘要变化）。
        history = sess.setdefault("review_history", [])
        if body.action == "regenerate":
            sess["ask_count"] = 0                      # 允许重新走完整性/红旗分流（资料不足会再次追问）
            sess["task_type"] = "case_write"           # v59：重新生成=回到完整病例分析任务
            sess["phase"] = "screening"
            sess["workflow_state"] = "screening"
            sess["review_status"] = "pending"
            sess["report"] = ""
            sess["message"] = ""
            history.append({"at": datetime.now(timezone.utc).isoformat(), "decision": "regenerate",
                            "action": "regenerate", "feedback": body.feedback[:500],
                            "result_version": sess.get("result_version", "r0")})
            snap = _screen(sess)
            _stamp_message(sess)   # v70：重新生成 = 新一轮产物（_screen 后结算）
            snap = _rebuild_snapshot(sess, snap)
            _persist_msg_artifacts(sess, snap)
            _bump_result_version(sess)
            _store_sess(sess)
            audit("review", {"case_id": case_id, "decision": "regenerate", "feedback": body.feedback[:120]})
            return snap
        decision = classify_review(body.feedback)
        history.append({"at": datetime.now(timezone.utc).isoformat(), "decision": decision,
                        "action": body.action or decision, "feedback": body.feedback[:500],
                        "result_version": sess.get("result_version", "r0")})
        sess["review_status"] = {"confirmed": "approved", "modified": "edited", "rejected": "rejected"}[decision]
        if decision == "confirmed":
            draft = dict(sess["draft"] or {})
            draft["workflow_state"] = "approved"
            draft["review_status"] = "approved"
            sess["draft"] = draft
            # 医生最终判断单独存档（与 AI 建议分开）；报告为待签署草稿，不写正式病历
            sess["doctor_final"] = {"decision": "confirmed", "feedback": body.feedback[:500],
                                    "at": datetime.now(timezone.utc).isoformat(),
                                    "signature_note": SIGNATURE_NOTE}
            sess["final_assist"] = {"suspected_conditions": draft.get("suspected_conditions") or [],
                                     "treatment_reference": draft.get("treatment_reference") or [],
                                     "medication_recommendations": draft.get("medication_recommendations") or [],
                                     "confirmed_at": sess["doctor_final"]["at"],
                                     "status": "doctor_confirmed_reference"}
            sess["report"] = render_report(draft)
            sess["workflow_state"] = "approved"
            sess["phase"] = "done"
            _bump_result_version(sess)
            snap = _snapshot(sess)
        elif decision == "rejected":
            sess["doctor_final"] = {"decision": "rejected", "feedback": body.feedback[:500],
                                    "at": datetime.now(timezone.utc).isoformat(),
                                    "signature_note": SIGNATURE_NOTE}
            sess["workflow_state"] = "rejected"
            sess["phase"] = "closed"
            sess["message"] = f"❌ 医生已拒绝本病例的待审核草稿，按流程不生成报告。拒绝意见：{body.feedback}"
            _bump_result_version(sess)
            snap = _snapshot(sess)
        else:
            draft = dict(sess["draft"])
            draft["uncertainties"] = list(draft.get("uncertainties", []))
            draft["uncertainties"].append(f"已按医生意见修订（服务端规则降级）：{body.feedback[:200]}")
            draft["review_status"] = "edited"
            sess["draft"] = draft
            sess["review_status"] = "edited"
            sess["question"] = build_review_question(draft, sess["red_flags"])
            _bump_result_version(sess)
            snap = _snapshot(sess)
        _store_sess(sess)
    audit("review", {"case_id": case_id, "decision": decision, "feedback": body.feedback[:120]})
    return snap


# —— 阶段§16：本地医学书籍 RAG 知识库（全部本地运行，不依赖云端） ——

class KBImportBody(BaseModel):
    filename: str
    content_base64: str        # MVP：JSON base64 传输（文本类书籍；大文件流式上传在 §16b 扩展）
    meta_note: str = ""


class KBRestoreBody(BaseModel):
    data: dict


@app.get("/api/kb/books")
def kb_books():
    """书籍列表与状态（不含 chunks 全文；引用完整性/OCR 质量状态按 §16.1 展示）。"""
    from rag import load_books
    db = load_books()
    out = []
    for b in sorted(db["books"].values(), key=lambda x: x.get("created_at", ""), reverse=True):
        out.append({"book_id": b["book_id"], "filename": b["filename"], "status": b["status"],
                    "chunks": len(b.get("chunks", [])), "error": b.get("error"),
                    "title": (b.get("meta") or {}).get("title"), "size": b.get("size"),
                    "sha256": (b.get("sha256") or "")[:12], "deleted_at": b.get("deleted_at"),
                    "meta_incomplete": bool((b.get("meta") or {}).get("format_incomplete")),
                    "updated_at": b.get("updated_at")})
    return {"books": out, "embedding_state": __import__("rag").embedding_status()["state"],
            "kb_snapshot": __import__("rag").kb_snapshot(db)}


@app.post("/api/kb/books")
def kb_import(body: KBImportBody):
    import base64
    from rag import import_book
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except Exception as exc:  # noqa: BLE001
        audit("kb_import_rejected", "invalid_base64")
        raise HTTPException(status_code=400, detail=f"invalid base64 content: {exc}") from exc
    result = import_book(body.filename, data, body.meta_note)
    audit("kb_import", {"book_id": result.get("book_id"), "status": result.get("status"),
                        "reason": result.get("reason"), "filename": body.filename[:80]})
    return result


@app.delete("/api/kb/books/{book_id}")
def kb_delete(book_id: str):
    from rag import soft_delete_book
    result = soft_delete_book(book_id)
    audit("kb_soft_delete", {"book_id": book_id, "result": result.get("ok"), "reason": result.get("reason")})
    return result


@app.delete("/api/kb/books/{book_id}/hard")
def kb_delete_hard(book_id: str):
    """v71：物理删除（不可恢复）——web 端「🗑 彻底删除」按钮 + 桌面桥共用。"""
    from rag import delete_book
    result = delete_book(book_id)
    audit("kb_hard_delete", {"book_id": book_id, "result": result.get("ok"), "reason": result.get("reason")})
    return result


@app.post("/api/kb/books/{book_id}/restore")
def kb_restore(book_id: str):
    from rag import restore_book
    result = restore_book(book_id)
    audit("kb_restore", {"book_id": book_id, "result": result.get("ok"), "reason": result.get("reason")})
    return result


@app.get("/api/kb/search")
def kb_search(q: str, top_k: int = None):
    """全库检索（共享知识库默认跨所有已完成索引的书籍；引用门槛：只回 ready）。
    v73：top_k 未显式指定时按「检索强度」偏好取值（低=2 中=3 高=6）。"""
    from rag import search, get_prefs, _STRENGTH_TOPK
    if top_k is None:
        top_k = _STRENGTH_TOPK.get(get_prefs().get("strength"), 3)
    result = search(q, top_k=max(1, min(int(top_k), 20)))
    audit("kb_search", {"q": q[:120], "hits": len(result.get("citations", []))})
    return result


@app.get("/api/kb/backup")
def kb_backup():
    from rag import backup_kb
    result = backup_kb()
    audit("kb_backup", {"books": len(result.get("data", {}).get("books", {}))})
    return result


@app.post("/api/kb/restore")
def kb_restore_backup(body: KBRestoreBody):
    from rag import restore_kb
    result = restore_kb(body.data)
    audit("kb_restore_backup", {"ok": result.get("ok"), "reason": result.get("reason")})
    return result


# —— v49：本地向量模型自选安装（§16.4：首次安装允许联网下载，检索与原文全程本地） ——

@app.get("/api/kb/embedding")
def kb_embedding_info():
    try:
        import rag_embed
        return {"ok": True, "catalog": rag_embed.catalog(),
                "status": rag_embed.status(), "active": rag_embed.active_model(),
                "last_error": rag_embed.last_error()}
    except ImportError:
        return {"ok": False, "reason": "rag_embed_unavailable"}


@app.post("/api/kb/embedding/install")
def kb_embedding_install(body: dict):
    short = str((body or {}).get("model") or "")
    try:
        import rag_embed
    except ImportError:
        return {"ok": False, "reason": "rag_embed_unavailable"}
    result = rag_embed.install(short)
    audit("kb_embedding_install" if result.get("ok") else "kb_embedding_install_rejected",
          {"model": short, "reason": result.get("reason")})
    return result


class RagPrefsBody(BaseModel):
    """v73：RAG 偏好（检索强度 / 分块方式）；分块方式只影响之后导入的书。"""
    strength: str = None
    chunk_mode: str = None


@app.get("/api/rag/prefs")
def rag_prefs_get():
    from rag import get_prefs
    return get_prefs()


@app.post("/api/rag/prefs")
def rag_prefs_set(body: RagPrefsBody):
    from rag import set_prefs
    prefs = set_prefs(strength=body.strength, chunk_mode=body.chunk_mode)
    audit("rag_prefs_set", prefs)
    return {"ok": True, **prefs}


@app.post("/api/kb/rebuild")
def kb_rebuild():
    """v73：按当前切分方式重切全部书籍（无需重新导入）。"""
    from rag import rebuild_index
    result = rebuild_index()
    audit("kb_rebuild", {k: result[k] for k in ("mode", "rebuilt", "skipped")})
    return result


@app.post("/api/kb/import-folder")
def kb_import_folder():
    """v73：导入项目根/book 文件夹里的全部书籍（SHA-256 去重，可安全反复执行）。"""
    from rag import import_folder
    result = import_folder()
    audit("kb_import_folder", {k: result.get(k) for k in ("imported", "skipped", "dir")})
    return result


@app.post("/api/kb/embedding/activate")
def kb_embedding_activate(body: dict):
    short = str((body or {}).get("model") or "")
    try:
        import rag_embed
    except ImportError:
        return {"ok": False, "reason": "rag_embed_unavailable"}
    result = rag_embed.set_active(short)
    if result.get("ok"):
        audit("kb_embedding_activate", short)
    return result


@app.get("/api/nav/map")
def nav_map():
    """v69：统一导航映射表只读导出。
    前端兜底表、跨端实现与回归测试共用这一份权威来源，避免「前端自己猜状态」。"""
    return nav_mod.map_payload()


@app.get("/api/artifacts/map")
def artifacts_map():
    """v70：产物/动作映射表只读导出。
    前端按钮文案与测试共用这一份权威来源——前端只按 available_actions 渲染按钮，
    严禁根据回复文字猜测该打开哪个窗口（验收要求 9）。"""
    return artifacts_mod.map_payload()


@app.get("/api/cases/{case_id}/nav")
def case_nav(case_id: str):
    """v69：按当前病例状态直接问「下一步该去哪个工作区」——用于刷新/重启后的工作区恢复。
    内部走完整快照，保证与对话轮返回的导航决策同源（不会因缺字段而误判）。"""
    with _lock:
        snap = _snapshot(_load_sess(case_id))
    return {"case_id": snap["case_id"], "nav": snap["nav"], "next_action": snap["next_action"],
            "workflow_state": snap.get("workflow_state"), "task_type": snap.get("task_type"),
            # v70：刷新/重启恢复时同样按产物渲染按钮，不靠前端猜
            "message_id": snap.get("message_id"), "artifacts": snap.get("artifacts") or {},
            "available_actions": snap.get("available_actions") or [],
            "authenticity": snap.get("authenticity")}


# —— 静态托管：Web/PWA 主评审入口 + 3D 面板 ——

@app.get("/api/health")
def health():
    cfg = load_llm_config()
    llm = bool((cfg["base_url"] and cfg["model"])
               or (os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL")))
    return {"status": "ok", "schema_version": "1.1", "llm": "server" if llm else "rule-fallback",
            "knowledge_snapshot": "synthetic-kb@2026-09-04"}


def _http_context() -> ssl.SSLContext:
    """macOS 框架 Python 常缺系统 CA：certifi 可用时用其证书束（与 rag_embed 下载器同因同解）。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


@app.get("/api/llm/connections")
def llm_connections():
    return llm_config_mod.list_public()


@app.post("/api/llm/connections")
def llm_connection_create(body: dict):
    try:
        result = llm_config_mod.save_connection(body or {}, clear_key=bool((body or {}).get("clear_key")))
        audit("llm_connection_created", {"id": result["connection"]["id"], "provider": result["connection"]["provider"]})
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/llm/connections/{connection_id}")
def llm_connection_update(connection_id: str, body: dict):
    try:
        result = llm_config_mod.save_connection(body or {}, connection_id, clear_key=bool((body or {}).get("clear_key")))
        audit("llm_connection_updated", {"id": connection_id, "provider": result["connection"]["provider"]})
        return result
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/llm/connections/{connection_id}/activate")
def llm_connection_activate(connection_id: str):
    try:
        result = llm_config_mod.activate(connection_id)
        audit("llm_connection_activated", {"id": connection_id})
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/llm/connections/{connection_id}")
def llm_connection_delete(connection_id: str):
    try:
        result = llm_config_mod.delete(connection_id)
        audit("llm_connection_deleted", {"id": connection_id})
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/llm/connections/{connection_id}/test")
def llm_connection_test(connection_id: str):
    c = llm_config_mod.get_connection(connection_id)
    if not c:
        raise HTTPException(status_code=404, detail="connection_not_found")
    out = {"ok": True, "connection_id": connection_id,
           "configured": bool(c.get("base_url") and c.get("model")),
           "api_key_set": bool(c.get("api_key")), "reachable": False,
           "error_code": "", "message": "", "checked_at": datetime.now(timezone.utc).isoformat()}
    if not c.get("base_url"):
        out.update({"error_code": "not_configured", "message": "未配置 Base URL"})
        return out
    try:
        headers = {"User-Agent": "Mozilla/5.0 DermAgent/1.1"}
        if c.get("api_key"):
            headers["Authorization"] = f"Bearer {c['api_key']}"
        req = urllib.request.Request(f"{c['base_url']}/models", headers=headers)
        with urllib.request.urlopen(req, timeout=6, context=_http_context()):
            out.update({"reachable": True, "message": "连接可达"})
    except urllib.error.HTTPError as exc:
        out["reachable"] = True
        out["error_code"] = "authentication_failed" if exc.code in (401, 403) else "http_error"
        out["message"] = f"服务已响应（HTTP {exc.code}）"
    except Exception as exc:  # noqa: BLE001
        out["error_code"] = "network_error"
        out["message"] = str(exc)[:120]
    audit("llm_connection_tested", {"id": connection_id, "reachable": out["reachable"],
                                    "error_code": out["error_code"]})
    return out


@app.get("/api/llm/status")
def llm_status(check: int = 0):
    """v58：LLM 连接状态探测（启动时/分析前端点调用）。

    configured=配置齐全（服务地址+模型）；reachable 仅在 check=1 时对网关做一次轻量
    GET /models 探测（任何 HTTP 状态码都算可达——路径差异不代表网关不可用）。
    api_key 只回布尔，绝不回值；审计不含 Key。"""
    cfg = load_llm_config()
    base = (cfg["base_url"] or os.environ.get("LLM_BASE_URL", "")).strip().rstrip("/")
    model = cfg["model"] or os.environ.get("LLM_MODEL", "").strip()
    key = cfg["api_key"] or os.environ.get("LLM_API_KEY", "").strip()
    out = {"ok": True, "configured": bool(base and model), "base_url": base, "model": model,
           "api_key_set": bool(key), "reachable": None, "error": "",
           "checked_at": datetime.now(timezone.utc).isoformat()}
    if check and base:
        try:
            req = urllib.request.Request(
                f"{base}/models", headers={"Authorization": f"Bearer {key}"} if key else {})
            with urllib.request.urlopen(req, timeout=4, context=_http_context()):
                out["reachable"] = True
        except urllib.error.HTTPError:
            out["reachable"] = True        # 网关有响应（401/404 等），只是 /models 路径差异
        except Exception as exc:  # noqa: BLE001 — 网络/超时/证书等一律如实标记不可达
            out["reachable"] = False
            out["error"] = str(exc)[:120]
    audit("llm_status_probed", {"configured": out["configured"], "reachable": out["reachable"]})
    return out


WEBUI = os.path.join(ROOT, "webui")   # v75 起不再对外托管（旧版简洁页仅存档于磁盘）
app.mount("/web", StaticFiles(directory=os.path.join(ROOT, "client", "web"), html=True), name="web3d")
# v75：web 端 = 桌面端同一套界面——browser-bridge.js 在无 Qt 壳时经同一批 REST API 编排
app.mount("/", StaticFiles(directory=os.path.join(ROOT, "client", "web"), html=True), name="webui")


@app.exception_handler(404)
async def not_found(request, exc):  # noqa: ANN001
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": getattr(exc, "detail", "Not Found")}, status_code=404)
    return FileResponse(os.path.join(ROOT, "client", "web", "index.html"))


if __name__ == "__main__":
    import uvicorn

    # v73：启动后后台自动扫描 book 文件夹（去重，反复启动安全）——医生放进文件夹的书自动入库
    def _auto_import_book_folder() -> None:
        time.sleep(2.0)
        try:
            from rag import import_folder
            r = import_folder()
            if r.get("imported"):
                audit("kb_import_folder_auto", {"imported": r["imported"], "skipped": r["skipped"]})
        except Exception as exc:  # noqa: BLE001 — 自动扫描失败不影响服务启动
            audit("kb_import_folder_auto_failed", str(exc)[:120])

    threading.Thread(target=_auto_import_book_folder, daemon=True, name="book-folder-scan").start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8765")))
