"""Agent 接入层（新计划 §4.2：客户端不得自行调用模型供应商 API）。

- ServerAgentClient：接入可信服务层（server/app.py，REST），模型 Key 只在服务端；支持跨端续办（case_id）。
- MockAgentClient：离线演示资产（断网/无服务时的确定性管线，与 server 共用 shared/derm_core 单一实现）。
- （llm 直连模式已按新计划移除：模型调用统一收口到女娲编排/服务端。）
"""

import http.client
import json
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import sys as _sys
import os as _os

_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "shared"))
_sys.path.insert(0, _os.path.dirname(_os.path.realpath(__file__)))   # memory_local 同目录（真实路径，兼容软链）

from memory_local import LocalMemory  # noqa: E402  # 桌面离线记忆（SQLite + JSONL）

from derm_core import (  # noqa: E402
    DEMO_CASES, build_case_from_text, build_draft, build_review_question, classify_review,
    compute_missing, detect_red_flags, merge_reply, render_report, retrieve_citations,
)
from intent_core import (  # noqa: E402  # 三层意图识别：规则层跨端单一真源
    build_pending, classify_control, classify_rules, guarded_intents, intent_label,
    is_multi_target, lexical_candidates, resolve_pending_choice, thresholds,
)


def _http_failure(exc) -> ConnectionError:
    """v77：HTTP 错误体优先取新契约 error.message，回退旧 detail（str/dict），最后原文。"""
    raw = ""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    body = {}
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    msg, code = "", ""
    err = body.get("error") if isinstance(body, dict) else None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("message"):
        msg, code = str(err["message"]), str(err.get("code") or "")
    elif isinstance(detail, str):
        msg = detail
    elif isinstance(detail, dict):
        msg, code = str(detail.get("message") or ""), str(detail.get("code") or "")
    if not msg:
        msg = (raw or str(exc))[:200]
    out = ConnectionError(f"HTTP {exc.code}：{msg}")
    out.status = exc.code
    out.code = code
    return out


def _iter_sse(resp):
    """逐行解析 text/event-stream：yield {type, payload, seq}；注释行（心跳）忽略。"""
    ev_type, ev_id, data_lines = "message", 0, []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data_lines:
                try:
                    payload = json.loads("\n".join(data_lines))
                except ValueError:
                    payload = None
                if payload is not None:
                    yield {"type": ev_type, "payload": payload, "seq": ev_id}
            ev_type, ev_id, data_lines = "message", 0, []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            ev_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line.startswith("id:"):
            try:
                ev_id = int(line[3:].strip() or 0)
            except ValueError:
                ev_id = 0


def match_demo_case(text: str):
    t = (text or "").strip().lower()
    if not t:
        return None
    for cid, demo in DEMO_CASES.items():
        for trig in demo["triggers"]:
            if t == trig.lower() or trig.lower() in t:
                return cid
    return None


class MockAgentClient:
    """离线确定性演示管线（shared/derm_core 单一实现，v1.1 契约）。子类仅用于离线演示资产。"""

    def __init__(self, memory_dir=None):
        self.state = {"case_id": None, "case": None, "missing": [], "red_flags": [], "ask_count": 0,
                      "phase": "idle", "workflow_state": "screening", "draft": None,
                      "review_status": "pending", "question": "", "report": "", "message": ""}
        self.case_id = None
        self.last_error = ""
        # 离线记忆：显式目录 → 落盘；未给目录且无 DERM_DATA_DIR（测试）→ 纯内存
        dir_arg = memory_dir if memory_dir is not None else (None if _os.environ.get("DERM_DATA_DIR") else ":memory:")
        self.memory = LocalMemory(dir_arg)
        self._mem_last = {"profile_facts": 0, "episodic_hits": 0, "last_recall_ms": 0}

    def _mem_meta(self) -> dict:
        out = dict(self._mem_last)
        h = self.memory.health()
        out.update({"last_extract_at": None, "degraded": h["degraded"],
                    "truncated": {"profile": False, "episodic": False},
                    "source": "local-sqlite"})
        return out

    def _mem_log(self, role: str, content: str, message_id: str = "", task_type: str = "") -> int:
        cid = self.state.get("case_id")
        if not cid:
            return 0
        return self.memory.append(cid, role, content[:500], message_id=message_id, task_type=task_type)

    def _mem_after_turn(self, text: str, result: dict):
        """收口后：助手消息入窗口 + 每轮问答成对切片（医生级仅 server 提供）。"""
        cid = self.state.get("case_id")
        if not cid:
            return
        content = ""
        reply = (result or {}).get("reply") or {}
        if isinstance(reply, dict) and reply.get("content"):
            content = str(reply["content"])
        elif (result or {}).get("doc"):
            content = str((result.get("doc") or {}).get("content") or "")
        elif (result or {}).get("message"):
            content = str(result.get("message"))
        mid = "mk_" + uuid.uuid4().hex[:10]
        self._mem_log("assistant", content[:1200], message_id=mid,
                      task_type=str((result or {}).get("task_type") or ""))
        if text:
            self.memory.episodic_write({
                "chunk_id": "", "scope": "case", "case_id": cid,
                "patient_id": (self.memory.patient_for_case(cid) or {}).get("patient_id") or "",
                "msg_id": mid, "task_type": str((result or {}).get("task_type") or ""),
                "title": str(text)[:40],
                "text": f"医生：{text[:600]}\n助手：{content[:600]}",
                "created_at": int(datetime.now(timezone.utc).timestamp())})

    def start(self, text: str, clinic: dict | None = None, regions=None, attachments=None) -> dict:
        demo = match_demo_case(text)
        case = dict(DEMO_CASES[demo]["case"]) if demo else build_case_from_text(text)
        if clinic and not demo:  # 离线演示仅本地分析，不落服务端结构
            case["clinic_payload"] = clinic
        cid = "mk" + uuid.uuid4().hex[:10]
        self.case_id = cid
        self.state.update({"case_id": cid, "case": case, "ask_count": 0, "phase": "screening",
                           "review_status": "pending", "report": "", "message": ""})
        label = str((clinic or {}).get("patient") or "") if isinstance(clinic, dict) else ""
        self.memory.ensure_patient(cid, label=label)
        self._mem_log("user", text, task_type="start")
        result = self._screen()
        self._mem_after_turn(text, result)
        self.memory.import_case_facts(cid, self._record_snapshot().get("fields") or {})
        return result

    def _screen(self) -> dict:
        case = self.state["case"]
        red_flags = detect_red_flags(case)
        missing = compute_missing(case)
        self.state.update({"missing": missing, "red_flags": red_flags})
        # v59：删除强制追问（与 server 任务路由一致）——缺失字段由草稿 unknown 字段如实列出
        citations = retrieve_citations(case)
        draft = build_draft(case, citations, red_flags, missing)
        self.state.update({"phase": "awaiting_review", "workflow_state": draft["workflow_state"], "draft": draft})
        return {"action": "review", "workflow_state": draft["workflow_state"], "draft": draft,
                "missing": list(missing),   # v59：缺失字段结构化透传（不追问，但如实列出）
                "llm": self._llm_meta(),   # v58：mock=离线演示，统一来源契约（source="demo"）
                "question": build_review_question(draft, red_flags)}

    # v59 离线任务回退：与 server/_route_doc_task 的降级语义一致——
    # 文书整理类 → 本地模板（只重组已提供字段，缺失一律「未提供」）；
    # 医学判断类 → llm_unavailable（离线绝不编造医学内容）。
    MOCK_TEMPLATE_TASKS = {"history", "lesion_desc", "summary", "referral", "followup", "case_edit"}
    MOCK_MEDICAL_TASKS = {"differential", "treatment_ref", "medication_review", "patient_edu", "auxiliary"}
    _MOCK_FIELD_LABELS = {"body_region": "身体区域", "chief_complaint": "主诉", "symptoms": "症状",
                          "duration": "病程", "trigger": "诱因", "trend": "演变趋势",
                          "past_history": "既往史", "allergy": "过敏史", "medication": "当前用药"}
    _MOCK_SECTIONS = {
        "history": ("现病史（整理）", ["chief_complaint", "symptoms", "duration", "trigger", "trend"]),
        "lesion_desc": ("皮损描述（整理）", ["body_region", "symptoms", "duration", "trend"]),
        "summary": ("病例摘要（整理）", ["chief_complaint", "symptoms", "duration", "past_history",
                                         "allergy", "medication"]),
        "referral": ("转诊摘要（整理）", ["chief_complaint", "symptoms", "duration", "past_history", "medication"]),
        "followup": ("随访记录（整理）", ["chief_complaint", "symptoms", "duration", "medication"]),
        "case_edit": ("病例整理（本地模板）", ["chief_complaint", "symptoms", "duration", "past_history",
                                               "allergy", "medication"]),
    }

    @classmethod
    def _classify_task(cls, text: str) -> str:
        """第一层规则 → 任务类型（未命中返回 case_write，保留旧签名）。"""
        hit = classify_rules(text)
        return str(hit["intent"]) if hit else "case_write"

    def _router_state(self) -> dict:
        return {"phase": self.state.get("phase"), "workflow_state": self.state.get("workflow_state"),
                "has_draft": bool(self.state.get("draft")), "task_type": self.state.get("task_type"),
                "case": self.state.get("case"), "last_assistant": self.state.get("last_assistant")}

    def _route_offline(self, text: str) -> dict:
        """离线三层路由的降级形态：L1 规则 → 词法候选（无向量模型、无 LLM）。

        与 server/intent.py 同口径：高置信直接执行；不确定出候选卡；
        无候选回落 case_write 并明确标注 degraded，离线首次描述病例不卡死。
        """
        hit = classify_rules(text, self._router_state())
        if hit:
            return {"intent": hit["intent"], "layer": 1, "confidence": hit["confidence"],
                    "reason": hit["reason"], "slots": hit.get("slots") or {},
                    "source": "rule", "degraded": False, "pending": None}
        th = thresholds()
        cands = lexical_candidates(text, int(th.get("max_candidates", 4)))
        top_intent, top_score = cands[0] if cands else ("", 0.0)
        second = cands[1][1] if len(cands) > 1 else 0.0
        # 词法相似度只做兜底：case_write 是安全默认值，阈值可放宽；其余意图必须
        # 「高分 + 明显领先」，否则一律出候选卡让医生定——宁可多问，不可猜着执行。
        # 与 server/intent.py 同口径：被否定/引号包裹、显式多目标的文本不得直接执行。
        guarded = top_intent in guarded_intents(text) or is_multi_target(text)
        if top_intent == "case_write":
            execute = (not guarded) and top_score >= float(th.get("lexical_execute_case_write", 0.25))
        else:
            execute = ((not guarded) and top_score >= float(th.get("lexical_execute_strong", 0.6))
                       and top_score - second >= float(th.get("lexical_margin", 0.12)))
        if top_intent and execute:
            return {"intent": top_intent, "layer": 2, "confidence": round(top_score, 4),
                    "reason": "lexical", "slots": {}, "source": "lexical",
                    "degraded": True, "pending": None}
        opts = [{"intent": i, "confidence": s} for i, s in cands
                if s >= float(th.get("lexical_candidate", 0.18))]
        if opts:
            pending = build_pending(text, opts, 2, "lexical")
            self.state["pending_intent"] = pending
            return {"intent": None, "layer": 2, "confidence": round(top_score, 4),
                    "reason": "lexical-ambiguous", "slots": {}, "source": "lexical",
                    "degraded": True, "pending": pending}
        return {"intent": "case_write", "layer": 0, "confidence": 0.0, "reason": "default",
                "slots": {}, "source": "default", "degraded": True, "pending": None}

    def _intent_confirm_reply(self, pending: dict, message: str) -> dict:
        return {"action": "intent_confirm", "workflow_state": self.state.get("workflow_state") or "screening",
                "task_type": "", "doc": None, "draft": self.state.get("draft"), "reply": None,
                "tool_history": [], "pending_intent": pending,
                "intent": {"layer": pending.get("layer", 2), "confidence": 0.0,
                           "reason": "lexical-ambiguous", "source": pending.get("source", "lexical"),
                           "degraded": True, "needs_confirm": True},
                "llm": {"ok": True, "source": "none", "model": "",
                        "generated_by": "intent-router-offline", "error": "", "task_type": ""},
                "message": message, "missing": list(self.state.get("missing") or []),
                "case_id": self.state.get("case_id")}

    def _mock_doc(self, task: str) -> dict:
        case = self.state.get("case") or {}
        title, keys = self._MOCK_SECTIONS.get(task, ("病例整理", ["symptoms"]))
        lines = [f"## {title}", "",
                 "- 整理方式：本地模板整理（非 AI 分析；离线演示模式）"]
        for k in keys:
            v = str(case.get(k) or "").strip()
            lines.append(f"- {self._MOCK_FIELD_LABELS.get(k, k)}：{v if v else '未提供'}")
        if self.state.get("missing"):
            lines += ["", "- 待补充信息（未知字段如实列出，不推断为「无」）："
                      + "、".join(self.state["missing"])]
        lines += ["", "> 本内容为本地模板整理，不构成医学建议；医疗判断须由医生完成。"]
        return {"task_type": task, "content": "\n".join(lines), "generated_by": "local-template",
                "citations": [], "disclaimer": "本地模板整理，不是真实 AI 分析，不可用于医疗判断",
                "at": datetime.now(timezone.utc).isoformat()}

    def chat(self, text: str, clinic: dict | None = None, regions=None, attachments=None) -> dict:
        """离线对话入口：执行本地管线后把本回合写入本地记忆（窗口 + 情景切片）。"""
        result = self._chat_impl(text, clinic=clinic, regions=regions, attachments=attachments)
        self._mem_after_turn(text, result)
        return result

    def _chat_impl(self, text: str, clinic: dict | None = None, regions=None, attachments=None) -> dict:
        """v59 离线对话：三层路由的离线降级（规则 → 词法候选 → 确认卡）。

        无真 LLM：医学判断类零内容；拿不准时出候选卡等医生确认，绝不猜着执行。
        """
        forced = ""
        cancelled = False
        pending = self.state.get("pending_intent")
        if isinstance(pending, dict):
            action, choice = resolve_pending_choice(text, pending)
            if action == "choose":
                forced = choice
                text = str(pending.get("source_text") or text)
                self.state.pop("pending_intent", None)
            elif action == "requote":
                text = (str(pending.get("source_text") or "") + " " + str(text)).strip()
                self.state.pop("pending_intent", None)
            elif action == "reask":
                return self._intent_confirm_reply(pending, "请从上方候选里选一个，或直接说明您想做什么。")
            elif action == "cancel":
                cancelled = True
                self.state.pop("pending_intent", None)
            else:
                self.state.pop("pending_intent", None)

        if cancelled:
            route = {"intent": "deny", "layer": 3, "confidence": 0.99, "reason": "doctor_cancel",
                     "slots": {}, "source": "doctor", "degraded": False, "pending": None}
        elif forced:
            route = {"intent": forced, "layer": 3, "confidence": 0.99, "reason": "doctor_choice",
                     "slots": {}, "source": "doctor", "degraded": False, "pending": None}
        else:
            route = self._route_offline(text)
        if route.get("pending"):
            self.state["task_text"] = text
            self.state.setdefault("messages", []).append({"role": "user", "content": text[:500]})
            self._mem_log("user", text, task_type="intent_pending")
            self.state["intent_meta"] = {k: route[k] for k in ("layer", "confidence", "reason", "source", "degraded")}
            self.state["intent_meta"]["needs_confirm"] = True
            return self._intent_confirm_reply(
                route["pending"],
                "离线演示模式无法确定这句话要做什么，请选择一项（选完只会执行所选意图）：")

        task = route["intent"] or "case_write"
        self.state["intent_meta"] = {k: route[k] for k in ("layer", "confidence", "reason", "source", "degraded")}
        self.state["intent_meta"]["needs_confirm"] = False
        self.state["task_type"] = task
        self.state["task_text"] = text
        self.state.setdefault("messages", []).append({"role": "user", "content": text[:500]})
        self._mem_log("user", text, task_type=task)
        if task == "deny":
            # 医生的「取消/算了/拒绝」是明确控制指令：不生成任何产物，也不当成新病例描述。
            now = datetime.now(timezone.utc).isoformat()
            content = ("已取消本次选择，未执行任何操作。" if route.get("reason") == "doctor_cancel"
                       else "已取消本次计划，未执行任何操作。您可以重新说明需要做什么。")
            self.state["message"] = ""
            return {"action": "reply", "workflow_state": self.state.get("workflow_state") or "screening",
                    "task_type": task, "doc": None, "draft": self.state.get("draft"),
                    "reply": {"content": content, "generated_by": "agent", "model": "",
                              "task_type": task, "plan": False,
                              "missing": list(self.state.get("missing") or []),
                              "disclaimer": "", "at": now},
                    "tool_history": [],
                    "llm": {"ok": True, "source": "agent", "model": "", "generated_by": "agent-cmd",
                            "error": "", "task_type": task, "at": now},
                    "intent": self.state.get("intent_meta"), "pending_intent": None,
                    "missing": list(self.state.get("missing") or []),
                    "case_id": self.state.get("case_id")}
        if task == "smalltalk":
            # v64：离线闲聊零伪造——明确「无法对话回复」，不用本地模板文本冒充 AI
            return {"action": "llm_unavailable", "workflow_state": "llm_unavailable", "task_type": task,
                    "doc": None, "draft": None, "reply": None, "tool_history": [],
                    "llm": {"ok": False, "source": "none", "model": "",
                            "error": "offline", "task_type": task},
                    "message": "离线演示模式未连接 LLM，无法对话回复",
                    "intent": self.state.get("intent_meta"), "pending_intent": None,
                    "missing": list(self.state.get("missing") or []),
                    "case_id": self.state.get("case_id")}
        if task in self.MOCK_MEDICAL_TASKS:
            self.state.update({"phase": "llm_unavailable", "workflow_state": "llm_unavailable",
                               "doc": None, "draft": None, "message": "离线演示模式未连接 LLM，无法生成真实 AI 医疗建议"})
            return {"action": "llm_unavailable", "workflow_state": "llm_unavailable", "task_type": task,
                    "doc": None, "draft": None, "llm": {"ok": False, "source": "none", "model": "",
                                                        "error": "offline", "task_type": task},
                    "message": self.state["message"], "missing": list(self.state.get("missing") or []),
                    "intent": self.state.get("intent_meta"), "pending_intent": None,
                    "case_id": self.state.get("case_id")}
        if task == "report_export":
            draft = self.state.get("draft")
            if not draft:
                return {"action": "doc", "workflow_state": "doc_ready", "task_type": task, "doc": None,
                        "message": "暂无可导出的病例内容，请先完成病例写作或整理",
                        "intent": self.state.get("intent_meta"), "pending_intent": None,
                        "llm": {"ok": False, "source": "none"}, "case_id": self.state.get("case_id")}
            return {"action": "doc", "workflow_state": "doc_ready", "task_type": task,
                    "doc": {"task_type": task, "content": str(draft.get("case_summary") or ""),
                            "generated_by": "local-export", "citations": [],
                            "disclaimer": "本地导出内容，正式病历须经医生签署"},
                    "llm": {"ok": True, "source": "template", "generated_by": "local-export"},
                    "intent": self.state.get("intent_meta"), "pending_intent": None,
                    "case_id": self.state.get("case_id")}
        doc = self._mock_doc(task)
        self.state.update({"phase": "doc_ready", "workflow_state": "doc_ready", "doc": doc, "message": ""})
        return {"action": "doc", "workflow_state": "doc_ready", "task_type": task, "doc": doc,
                "llm": {"ok": True, "source": "template", "model": "", "generated_by": "local-template",
                        "task_type": task, "error": "",
                        "at": datetime.now(timezone.utc).isoformat()},
                "intent": self.state.get("intent_meta"), "pending_intent": None,
                "missing": list(self.state.get("missing") or []),
                "case_id": self.state.get("case_id")}

    @staticmethod
    def _llm_meta() -> dict:
        """离线演示管线的真实性元信息：明确 source=demo，前端据此加离线标识。"""
        return {"ok": True, "source": "demo", "model": "offline_demo", "generated_by": "offline_demo",
                "error": "", "at": datetime.now(timezone.utc).isoformat()}

    def choose_intent(self, pending_id: str, intent: str) -> dict:
        """v75 确认卡点选（离线）：校验 pending 与候选后只执行所选意图，且只执行一次。"""
        pending = self.state.get("pending_intent")
        if not isinstance(pending, dict):
            raise ValueError("确认卡已失效，请重新输入您的需求")
        if pending_id and str(pending.get("id")) != str(pending_id):
            raise ValueError("确认卡已失效，请重新输入您的需求")
        chosen = next((c for c in (pending.get("candidates") or [])
                       if str(c.get("intent")) == str(intent)), None)
        if chosen is None:
            raise ValueError("所选意图不在候选中，请重新选择")
        # 只保留所选候选 → chat() 内部按「选择」解析（执行后 pending 即清空，不会重复执行）
        self.state["pending_intent"] = dict(pending, candidates=[chosen])
        return self.chat(str(chosen.get("label") or intent_label(intent)))

    def supplement(self, reply: str) -> dict:
        self.state["case"] = merge_reply(self.state["case"], reply)
        self.state["ask_count"] += 1
        self.state["phase"] = "screening"
        return self._screen()

    def review(self, feedback: str, action: str = "") -> dict:
        if action == "regenerate":   # 阶段6：医生要求重新生成——重置后重走分析分流
            self.state.update({"ask_count": 0, "phase": "screening", "workflow_state": "screening",
                               "review_status": "pending", "report": "", "message": ""})
            return self._screen()
        decision = classify_review(feedback)
        self.state["feedback"] = feedback
        if decision == "confirmed":
            draft = dict(self.state["draft"] or {})
            draft["workflow_state"] = "approved"
            draft["review_status"] = "approved"
            self.state["draft"] = draft
            self.state["review_status"] = "approved"
            self.state["report"] = render_report(draft)
            self.state["phase"] = "done"
            self.state["workflow_state"] = "approved"
            return {"action": "report", "workflow_state": "approved", "review_status": "approved",
                    "report": self.state["report"], "draft": draft, "llm": self._llm_meta()}
        if decision == "rejected":
            self.state["workflow_state"] = "rejected"
            self.state["review_status"] = "rejected"
            self.state["phase"] = "closed"
            return {"action": "closed", "workflow_state": "rejected", "review_status": "rejected",
                    "message": f"❌ 医生已拒绝本病例的待审核草稿，按流程不生成报告。拒绝意见：{feedback}"}
        draft = dict(self.state["draft"] or {})
        draft["uncertainties"] = list(draft.get("uncertainties", []))
        draft["uncertainties"].append(f"已按医生意见修订（离线演示降级）：{feedback[:200]}")
        draft["review_status"] = "edited"
        self.state.update({"draft": draft, "review_status": "edited", "workflow_state": "awaiting_review"})
        return {"action": "review", "workflow_state": "awaiting_review", "review_status": "edited",
                "draft": draft, "llm": self._llm_meta(),
                "question": build_review_question(draft, self.state["red_flags"])}

    # —— v65：门诊病历单（mock 模式：内存 record，支持医生手动填写/保存/确认/导出；
    #  AI 重新生成在离线演示下明确拒绝，不伪造 AI 内容） ——

    _RECORD_FIELDS = ["patient", "chief_complaint", "present_illness", "past_history",
                      "personal_history", "allergy_history", "medication_history",
                      "physical_exam", "skin_lesions", "auxiliary_exam", "body_regions",
                      "impression", "treatment_ref", "medication_check", "follow_up",
                      "danger_signals", "doctor_review", "notes"]

    def _record_snapshot(self) -> dict:
        rec = self.state.setdefault("record", {"fields": {k: "" for k in self._RECORD_FIELDS},
                                               "meta": {}, "review_status": "draft",
                                               "confirmed_at": None, "citations": [],
                                               "created_at": self.state.get("started_at", ""),
                                               "updated_at": ""})
        out = dict(rec)
        out["case_id"] = self.state["case_id"]
        out["label"] = self.state.get("label", "")
        out["patient"] = self.memory.patient_brief(self.state.get("case_id") or "")
        out["memory"] = self._mem_meta()
        return out

    def set_regions(self, regions) -> dict:
        """v69（本地演示）：3D 选点写入内存病例单「身体部位」，来源标注 3D 模型选择。
        与服务端同契约返回导航决策（选完回对话并聚焦输入框）。"""
        rec = self._record_snapshot()
        self.state["record"] = rec
        merged = [x for x in str(rec["fields"].get("body_regions") or "").split("；") if x]
        for r in (regions or []):
            r = str(r).strip()
            if r and r not in merged:
                merged.append(r)
        if merged:
            rec["fields"]["body_regions"] = "；".join(merged)
            rec.setdefault("meta", {})["body_regions"] = {"source": "model_3d", "at": ""}
        return {"ok": True, "selected_body_regions": merged, "record": self._record_snapshot(),
                "nav": {"nav_state": "awaiting_user_input", "next_action": "focus_input",
                        "workspace": "chat", "focus_input": True, "scroll_to_latest": False,
                        "highlight_fields": [], "case_id": self.state.get("case_id", ""),
                        "reason": "3D 选点已写入病例草稿：回对话窗口并聚焦输入框"}}

    def get_record(self) -> dict:
        return {"ok": True, "record": self._record_snapshot()}

    def save_record(self, fields: dict) -> dict:
        rec = self._record_snapshot()
        self.state["record"] = rec
        for k, v in (fields or {}).items():
            if k in self._RECORD_FIELDS:
                rec["fields"][k] = str(v or "").strip()
                rec["meta"][k] = {"source": "doctor_modified", "at": ""}
        if self.state.get("case_id"):
            self.memory.import_case_facts(self.state["case_id"], rec["fields"])
        return {"ok": True, "record": self._record_snapshot()}

    def confirm_record(self, fields=None, confirm_all: bool = False) -> dict:
        rec = self._record_snapshot()
        self.state["record"] = rec
        targets = self._RECORD_FIELDS if confirm_all else [f for f in (fields or []) if f in self._RECORD_FIELDS]
        for k in targets:
            rec["meta"][k] = {"source": "doctor_confirmed", "at": ""}
        rec["review_status"] = ("confirmed"
                                if all(rec["meta"].get(k, {}).get("source") == "doctor_confirmed"
                                       for k in self._RECORD_FIELDS) else "draft")
        return {"ok": True, "review_status": rec["review_status"], "record": self._record_snapshot()}

    def regen_field(self, field: str):
        return {"ok": False, "llm_connected": False,
                "message": "当前未连接 LLM，AI 内容无法生成。您仍可手动填写并保存。"}

    def reject_record(self) -> dict:
        rec = self._record_snapshot()
        self.state["record"] = rec
        rec["rejected"] = True
        rec["review_status"] = "draft"
        rec["confirmed_at"] = None
        return {"ok": True, "record": self._record_snapshot()}

    def export_record_md(self) -> str:
        rec = self._record_snapshot()
        labels = {"patient": "患者基本信息", "chief_complaint": "主诉", "present_illness": "现病史",
                  "past_history": "既往史", "personal_history": "个人史", "allergy_history": "过敏史",
                  "medication_history": "用药史", "physical_exam": "体格检查", "skin_lesions": "皮损描述",
                  "auxiliary_exam": "辅助检查", "body_regions": "身体部位",
                  "impression": "初步判断/鉴别方向", "treatment_ref": "治疗参考",
                  "medication_check": "用药核对", "follow_up": "随访与恢复情况",
                  "doctor_review": "医生审核意见", "notes": "备注"}
        final = rec.get("review_status") == "confirmed"
        lines = ["# 皮肤科门诊病历", "", f"- 病例编号：{rec['case_id']}",
                 f"- 审核状态：{'最终审核版本（医生已确认）' if final else '医生审核中（AI 草稿/待确认，不可作为正式报告）'}", ""]
        for k, label in labels.items():
            val = str(rec["fields"].get(k) or "").strip()
            if k == "body_regions" and not val:
                val = "未选择 3D 部位"
            lines += [f"## {label}", "", val if val else "未提供", ""]
        lines.append("> 免责声明：本病历中标注「AI 草稿」的内容仅供医生审核参考，不能替代医生诊断与签名。")
        return "\n".join(lines)

    # —— 三层记忆（mock：本地 SQLite + JSONL，无 LLM 抽取，如实标 degraded） ——

    def patient_list(self, query: str = "") -> list:
        return self.memory.list_patients(query)

    def patient_create(self, fields: dict) -> dict:
        patient = self.memory.create_patient(fields or {})
        return {"ok": True, "patient": patient}

    def patient_update(self, patient_id: str, fields: dict) -> dict:
        try:
            patient = self.memory.update_patient(patient_id, fields or {})
        except KeyError:
            return {"ok": False, "error": "患者不存在"}
        return {"ok": True, "patient": patient}

    def patient_facts(self, patient_id: str) -> dict:
        try:
            patient = self.memory.get_patient(patient_id)
        except KeyError:
            return {"ok": False, "error": "患者不存在", "facts": []}
        return {"ok": True, "patient": patient, "facts": self.memory.facts(patient_id, active_only=False)}

    def patient_add_fact(self, patient_id: str, fact: dict) -> dict:
        try:
            return self.memory.add_fact(patient_id, fact or {}, source="doctor_modified")
        except KeyError:
            return {"ok": False, "error": "患者不存在"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)[:160]}

    def patient_delete_fact(self, patient_id: str, fact_id) -> dict:
        try:
            return self.memory.delete_fact(patient_id, fact_id)
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)[:160]}

    def patient_delete(self, patient_id: str) -> dict:
        try:
            return self.memory.delete_patient(patient_id)
        except KeyError:
            return {"ok": False, "error": "患者不存在"}

    def patient_merge(self, source_id: str, target_id: str) -> dict:
        return {"ok": False, "error": "离线演示不支持跨病例合并患者（请在服务端模式下操作）"}

    def patient_link(self, case_id: str, patient_id: str) -> dict:
        try:
            if not patient_id:
                return self.memory.unlink_case(case_id)
            return self.memory.link_case(case_id, patient_id)
        except KeyError:
            return {"ok": False, "error": "患者不存在"}

    def case_memory(self, case_id: str) -> dict:
        patient = self.memory.patient_brief(case_id)
        window = self.memory.window(case_id)
        return {"ok": True, "case_id": case_id, "patient": patient,
                "memory": self._mem_meta(), "window_count": len(window),
                "short_term_degraded": False, "last_job": None,
                "episodic_chunks": self.memory.episodic_count(case_id=case_id)}

    def memory_health(self) -> dict:
        return self.memory.health()


class ServerAgentClient:
    """可信服务层 REST 客户端：同一 case_id 可在 Web/PWA/另一 Windows 继续同一会话。"""

    def __init__(self, base_url: str, timeout: int = 240):   # agent 工具循环回合可达数分钟
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.case_id = None
        self.last_error = ""

    def _req(self, path: str, body=None, method: str = "") -> dict:
        m = method or ("POST" if body is not None else "GET")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json; charset=utf-8"} if body is not None else {},
            method=m)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"服务不可达：{exc}") from exc

    @staticmethod
    def _render(snap: dict) -> dict:
        if snap.get("report"):
            action = "report"
        elif snap.get("workflow_state") == "rejected" or snap.get("phase") == "closed":
            action = "closed"
        elif snap.get("workflow_state") == "llm_unavailable":
            action = "llm_unavailable"      # v58：LLM 不可用（不生成医疗内容），前端显示连接引导
        elif snap.get("workflow_state") == "llm_error":
            action = "llm_error"            # v64：已配置但请求失败/超时——错误卡 + 重试按钮
        elif snap.get("pending_intent"):
            action = "intent_confirm"       # 三层路由拿不准：等医生点选，未执行任何 handler
        elif snap.get("reply"):
            action = "reply"                # v64：Agent 自然语言回复（smalltalk/clarify），前端对话流渲染
        elif snap.get("doc"):
            # v59：文书/医学判断任务产物（doc_ready），前端按 task_type 渲染。
            # v67：排到 reply 之后——agent 指令回合（skip_3d/undo 等）产物是 reply，
            # 快照中的 doc 只是历史残留，必须按本轮响应渲染而非误报「未连接」+旧 doc。
            action = "doc"
        elif snap.get("draft"):
            action = "review"
        else:
            action = "review"
        return {"action": action, "workflow_state": snap.get("workflow_state"),
                "agent_state": snap.get("agent_state", ""),   # v64-P2：Agent 状态机（9 态）
                "review_status": snap.get("review_status", "pending"), "draft": snap.get("draft"),
                "doc": snap.get("doc"),         # v59 文书产物（content/task_type/generated_by/citations）
                # 本地书籍引用独立于合成引用（draft/doc 各自携带，前端单独渲染）
                "book_citations": (snap.get("book_citations")
                                   or (snap.get("doc") or {}).get("book_citations")
                                   or (snap.get("draft") or {}).get("book_citations") or []),
                "task_type": snap.get("task_type") or "case_write",
                "review_history": snap.get("review_history", []),
                "doctor_final": snap.get("doctor_final"),
                "llm": snap.get("llm") or {},   # v58 真实性元信息（来源/模型/失败原因）
                "missing": snap.get("missing") or [],
                # v64 Agent 对话：自然语言回复与按需工具调用记录（前端可展开过程消息）
                "reply": snap.get("reply"),
                # v78：本轮独立核验结论（紧凑摘要；详细问题只在服务端审计）
                "verification": snap.get("verification"),
                "plan": snap.get("plan") or None,   # v76：显式计划对象（步骤/状态/工具白名单）
                "tool_history": snap.get("tool_history") or [],
                "selected_body_regions": snap.get("selected_body_regions") or [],
                "messages": snap.get("messages") or [],   # v64-P1：对话历史（重启恢复用）
                # 三层记忆：患者摘要 + 记忆层计数/降级状态（不含事实原文）
                "patient": snap.get("patient"), "memory": snap.get("memory") or {},
                "context_compressed": bool(snap.get("context_compressed")),   # v68 §十一.5 压缩提示
                "question": snap.get("question", ""), "report": snap.get("report", ""),
                "message": snap.get("message", ""), "case_id": snap.get("case_id"),
                # v65：Agent 判定「打开/新建病例窗口」意图 → MainWindow 自动开窗
                "open_case": snap.get("open_case"),
                # v69：服务端权威导航决策（nav_state/next_action/workspace/case_id/
                # workflow_state/task_type/tool_calls/highlight_fields）——前端 __nav 直接执行，
                # 不再自行从状态推断工作区。next_action 单列便于日志与断言。
                "nav": snap.get("nav") or {},
                "next_action": snap.get("next_action") or "",
                # v70：对话优先、结果按需打开——按钮只来自服务端派生，
                # 前端不得根据回复文字猜测该打开哪个窗口（available_actions 权威）。
                "message_id": snap.get("message_id") or "",
                "artifacts": snap.get("artifacts") or {},
                "available_actions": snap.get("available_actions") or [],
                "result_summary": snap.get("result_summary") or [],
                "authenticity": snap.get("authenticity") or "",
                # v73：Agent 触发的界面设置动作（语言等），必须透传到 Web 执行
                "ui_action": snap.get("ui_action"),
                # 三层意图路由：本轮决策元信息 + 待医生确认的候选（无则 None）
                "intent": snap.get("intent"),
                "pending_intent": snap.get("pending_intent"),
                # v70：历史各条回复自己的产物/按钮（渲染历史卡按钮区，不串消息）
                "msg_artifacts": snap.get("msg_artifacts") or {}}

    def refine_regions(self, payload: dict) -> dict:
        """Submit geometry-only manual regions to the trusted refinement service."""
        return self._req("/api/anatomy/refine", payload)

    def start(self, text: str, clinic: dict | None = None, regions=None, attachments=None) -> dict:
        self.last_error = ""
        body = {"text": text, "demo": ""}
        if clinic:  # 医生端脱敏资料载荷 → server case["clinic_payload"]
            body["clinic"] = clinic
        if regions:      # v64 会话状态对象：3D 选点/附件随消息入会话
            body["regions"] = list(regions)
        if attachments:
            body["attachments"] = list(attachments)
        snap = self._req("/api/cases", body)
        self.case_id = snap["case_id"]
        return self._render(snap)

    def start_demo(self, demo: str) -> dict:
        self.last_error = ""
        snap = self._req("/api/cases", {"text": "", "demo": demo})
        self.case_id = snap["case_id"]
        return self._render(snap)

    def resume(self, case_id: str) -> dict:
        snap = self._req(f"/api/cases/{case_id}")
        self.case_id = snap["case_id"]
        return self._render(snap)

    def list_cases(self) -> list:
        return self._req("/api/cases")

    def list_records(self) -> list:
        """v73 病历库：全部已产生病历名录。"""
        return self._req("/api/records")

    def delete_case(self, case_id: str) -> dict:
        """v60：删除历史病例（服务端硬删，列表/续办立即失效）。"""
        req = urllib.request.Request(f"{self.base_url}/api/cases/{case_id}", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"服务不可达：{exc}") from exc

    def delete_all_cases(self) -> dict:
        """v62：清空全部历史病例（服务端批量硬删，返回删除数）。"""
        req = urllib.request.Request(f"{self.base_url}/api/cases", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"服务不可达：{exc}") from exc

    def supplement(self, reply: str) -> dict:
        snap = self._req(f"/api/cases/{self.case_id}/supplement", {"reply": reply})
        return self._render(snap)

    def chat(self, text: str, clinic: dict | None = None, regions=None, attachments=None) -> dict:
        """v59 Agent 对话：同一病例下延续上下文的自然语言指令（任务识别/路由在服务端）。"""
        body = {"text": text}
        if clinic:
            body["clinic"] = clinic
        if regions:      # v64：可选 3D 选点上下文增量并入会话
            body["regions"] = list(regions)
        if attachments:
            body["attachments"] = list(attachments)
        snap = self._req(f"/api/cases/{self.case_id}/chat", body)
        return self._render(snap)

    # —— v76：流式 turn（读响应头即断开，服务端继续执行；进度走 turn_events 轮询） ——

    def start_turn(self, text: str, clinic: dict | None = None, regions=None,
                   attachments=None, client_turn_id: str = "") -> dict:
        body = {"text": text, "client_turn_id": client_turn_id or ("ct-" + uuid.uuid4().hex[:12])}
        if clinic:
            body["clinic"] = clinic
        if regions:
            body["regions"] = list(regions)
        if attachments:
            body["attachments"] = list(attachments)
        req = urllib.request.Request(
            f"{self.base_url}/api/cases/{self.case_id}/chat/stream",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return {"turn_id": resp.headers.get("X-Turn-Id") or "",
                        "client_turn_id": body["client_turn_id"]}
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405, 501):
                raise NotImplementedError("服务端未启用流式接口") from exc
            raise _http_failure(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"服务不可达：{exc}") from exc

    def turn_events(self, turn_id: str, after_seq: int = 0) -> dict:
        from urllib.parse import quote
        return self._req(f"/api/cases/{self.case_id}/turns/{quote(str(turn_id))}?after_seq={int(after_seq)}")

    # —— v79：真正消费 SSE（逐字正文 + 过程事件），失败可回退轮询 ——

    def _open_stream(self, path: str, body: dict, timeout_s: float = 45):
        """POST 流式端点并返回已打开的响应；未受理阶段的错误保持可回退语义。"""
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Accept": "text/event-stream"}, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=timeout_s)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405, 501):
                raise NotImplementedError("服务端未启用流式接口") from exc
            raise _http_failure(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"服务不可达：{exc}") from exc

    def stream_turn(self, text: str, clinic: dict | None = None, regions=None,
                    attachments=None, client_turn_id: str = "", timeout_s: float = 45):
        """生成器：yield {type, payload, seq}，含最终 done（带快照）。
        服务端已受理后读流中断 → 抛带 .turn_id 的 ConnectionError（调用方轮询该 turn，不重发）。"""
        body = {"text": text, "client_turn_id": client_turn_id or ("ct-" + uuid.uuid4().hex[:12])}
        if clinic:
            body["clinic"] = clinic
        if regions:
            body["regions"] = list(regions)
        if attachments:
            body["attachments"] = list(attachments)
        resp = self._open_stream(f"/api/cases/{self.case_id}/chat/stream", body, timeout_s)
        turn_id = resp.headers.get("X-Turn-Id") or ""
        with resp:
            try:
                for ev in _iter_sse(resp):
                    yield ev
            except (urllib.error.URLError, TimeoutError, OSError,
                    http.client.HTTPException) as exc:
                err = ConnectionError(f"流式读取中断：{exc}")
                err.turn_id = turn_id
                raise err from exc

    def stream_start(self, text: str, demo: str = "", clinic: dict | None = None,
                     regions=None, attachments=None, patient_id: str = "",
                     client_turn_id: str = "", timeout_s: float = 45):
        """生成器：首条消息建案并流式输出；X-Case-Id/turn_started 提前落定 case_id。"""
        body = {"text": text, "demo": demo,
                "client_turn_id": client_turn_id or ("ct-" + uuid.uuid4().hex[:12])}
        if clinic:
            body["clinic"] = clinic
        if regions:
            body["regions"] = list(regions)
        if attachments:
            body["attachments"] = list(attachments)
        if patient_id:
            body["patient_id"] = patient_id
        resp = self._open_stream("/api/cases/stream", body, timeout_s)
        cid = resp.headers.get("X-Case-Id") or ""
        if cid:
            self.case_id = cid
        turn_id = resp.headers.get("X-Turn-Id") or ""
        with resp:
            try:
                for ev in _iter_sse(resp):
                    payload = ev.get("payload") or {}
                    if ev["type"] == "turn_started" and payload.get("case_id"):
                        self.case_id = payload["case_id"]
                    elif ev["type"] == "done":
                        snap = payload.get("snapshot") or {}
                        if snap.get("case_id"):
                            self.case_id = snap["case_id"]
                    yield ev
            except (urllib.error.URLError, TimeoutError, OSError,
                    http.client.HTTPException) as exc:
                err = ConnectionError(f"流式读取中断：{exc}")
                err.turn_id = turn_id
                raise err from exc

    def render_snapshot(self, snap: dict) -> dict:
        return self._render(snap or {})

    def review(self, feedback: str, action: str = "") -> dict:
        snap = self._req(f"/api/cases/{self.case_id}/review", {"feedback": feedback, "action": action})
        return self._render(snap)

    def choose_intent(self, pending_id: str, intent: str) -> dict:
        """v75 确认卡点选：服务端校验 pending_id 与候选 → 只执行所选意图（且只执行一次）。"""
        snap = self._req(f"/api/cases/{self.case_id}/intent/choose",
                         {"intent": intent, "pending_id": pending_id})
        return self._render(snap)

    # —— v65：门诊病历单（病例窗口）——

    def get_record(self) -> dict:
        return self._req(f"/api/cases/{self.case_id}/record")

    def save_record(self, fields: dict) -> dict:
        return self._req(f"/api/cases/{self.case_id}/record", {"fields": fields or {}}, method="PUT")

    def set_regions(self, regions) -> dict:
        """v69：3D 选点即时写入当前病例草稿（服务端合并 + record 同步，返回导航决策）。"""
        return self._req(f"/api/cases/{self.case_id}/regions", {"regions": list(regions or [])})

    # —— 三层记忆：患者档案（身份字段只在本机 UI/MySQL，不进提示词） ——

    def patient_list(self, query: str = "") -> list:
        from urllib.parse import quote
        return self._req(f"/api/patients?query={quote(str(query or ''))}").get("patients") or []

    def patient_create(self, fields: dict) -> dict:
        return self._req("/api/patients", dict(fields or {})).get("patient") or {}

    def patient_update(self, patient_id: str, fields: dict) -> dict:
        return self._req(f"/api/patients/{patient_id}", dict(fields or {}), method="PATCH").get("patient") or {}

    def patient_facts(self, patient_id: str) -> dict:
        return self._req(f"/api/patients/{patient_id}/facts")

    def patient_add_fact(self, patient_id: str, fact: dict) -> dict:
        return self._req(f"/api/patients/{patient_id}/facts", dict(fact or {}))

    def patient_delete_fact(self, patient_id: str, fact_id) -> dict:
        return self._req(f"/api/patients/{patient_id}/facts/{int(fact_id)}", method="DELETE")

    def patient_delete(self, patient_id: str) -> dict:
        return self._req(f"/api/patients/{patient_id}", method="DELETE")

    def patient_merge(self, source_id: str, target_id: str) -> dict:
        return self._req(f"/api/patients/{source_id}/merge", {"target_id": target_id})

    def patient_link(self, case_id: str, patient_id: str) -> dict:
        return self._req(f"/api/cases/{case_id}/patient", {"patient_id": patient_id or ""})

    def case_memory(self, case_id: str) -> dict:
        return self._req(f"/api/cases/{case_id}/memory")

    def memory_health(self) -> dict:
        return self._req("/api/memory/health")

    def confirm_record(self, fields=None, confirm_all: bool = False) -> dict:
        body = {"all": bool(confirm_all), "fields": list(fields or [])}
        return self._req(f"/api/cases/{self.case_id}/record/confirm", body)

    def regen_field(self, field: str):
        return self._req(f"/api/cases/{self.case_id}/record/regen", {"field": field})

    def reject_record(self) -> dict:
        return self._req(f"/api/cases/{self.case_id}/record/reject", {})

    def export_record_md(self) -> str:
        req = urllib.request.Request(f"{self.base_url}/api/cases/{self.case_id}/record/export")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"服务不可达：{exc}") from exc
