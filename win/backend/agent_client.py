"""Agent 接入层（新计划 §4.2：客户端不得自行调用模型供应商 API）。

- ServerAgentClient：接入可信服务层（server/app.py，REST），模型 Key 只在服务端；支持跨端续办（case_id）。
- MockAgentClient：离线演示资产（断网/无服务时的确定性管线，与 server 共用 shared/derm_core 单一实现）。
- （llm 直连模式已按新计划移除：模型调用统一收口到女娲编排/服务端。）
"""

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

import sys as _sys
import os as _os

_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "shared"))

from derm_core import (  # noqa: E402
    DEMO_CASES, build_case_from_text, build_draft, build_review_question, classify_review,
    compute_missing, detect_red_flags, merge_reply, render_report, retrieve_citations,
)


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

    def __init__(self):
        self.state = {"case_id": None, "case": None, "missing": [], "red_flags": [], "ask_count": 0,
                      "phase": "idle", "workflow_state": "screening", "draft": None,
                      "review_status": "pending", "question": "", "report": "", "message": ""}
        self.last_error = ""

    def start(self, text: str, clinic: dict | None = None, regions=None, attachments=None) -> dict:
        demo = match_demo_case(text)
        case = dict(DEMO_CASES[demo]["case"]) if demo else build_case_from_text(text)
        if clinic and not demo:  # 离线演示仅本地分析，不落服务端结构
            case["clinic_payload"] = clinic
        self.state.update({"case": case, "ask_count": 0, "phase": "screening",
                           "review_status": "pending", "report": "", "message": ""})
        return self._screen()

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
    MOCK_MEDICAL_TASKS = {"differential", "treatment_ref", "medication_review", "patient_edu"}
    MOCK_TASK_PATTERNS = [
        ("case_edit", ("改成", "改写", "重写", "修改病例", "编辑病例")),
        ("report_export", ("导出报告", "生成报告")),
        ("history", ("现病史",)), ("lesion_desc", ("皮损描述", "描述皮损", "皮损情况")),
        ("differential", ("鉴别",)), ("medication_review", ("用药核对", "用药清单")),
        ("treatment_ref", ("治疗参考", "治疗建议参考")), ("patient_edu", ("健康教育", "患者能看懂")),
        ("referral", ("转诊",)), ("followup", ("随访",)), ("summary", ("病例摘要", "病历摘要", "摘要")),
    ]
    # v64：离线闲聊/能力询问判定（与服务端 _is_smalltalk 口径一致，离线态绝不伪造对话回复）
    MOCK_GREET_EXACT = {"hi", "hello", "hey", "你好", "您好", "在吗", "在么", "嗨"}
    MOCK_SMALLTALK_KEYS = ("你是谁", "你能做什么", "你能干什么", "你叫什么", "介绍你能", "介绍下你",
                           "你会做什么", "你会什么", "你有什么功能", "有什么功能", "你能帮我做什么",
                           "怎么使用", "这是什么软件", "你负责什么")
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
        t = (text or "").strip()
        if t.lower() in cls.MOCK_GREET_EXACT or any(k in t for k in cls.MOCK_SMALLTALK_KEYS):
            return "smalltalk"
        for task, keys in cls.MOCK_TASK_PATTERNS:
            if any(k in t for k in keys):
                return task
        return "case_write"

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
        """v59 离线对话：任务分类后走对应回退管线（无真 LLM；医学判断类零内容）。"""
        task = self._classify_task(text)
        self.state["task_type"] = task
        self.state["task_text"] = text
        self.state.setdefault("messages", []).append({"role": "user", "content": text[:500]})
        if task == "smalltalk":
            # v64：离线闲聊零伪造——明确「无法对话回复」，不用本地模板文本冒充 AI
            return {"action": "llm_unavailable", "workflow_state": "llm_unavailable", "task_type": task,
                    "doc": None, "draft": None, "reply": None, "tool_history": [],
                    "llm": {"ok": False, "source": "none", "model": "",
                            "error": "offline", "task_type": task},
                    "message": "离线演示模式未连接 LLM，无法对话回复",
                    "missing": list(self.state.get("missing") or []),
                    "case_id": self.state.get("case_id")}
        if task in self.MOCK_MEDICAL_TASKS:
            self.state.update({"phase": "llm_unavailable", "workflow_state": "llm_unavailable",
                               "doc": None, "draft": None, "message": "离线演示模式未连接 LLM，无法生成真实 AI 医疗建议"})
            return {"action": "llm_unavailable", "workflow_state": "llm_unavailable", "task_type": task,
                    "doc": None, "draft": None, "llm": {"ok": False, "source": "none", "model": "",
                                                        "error": "offline", "task_type": task},
                    "message": self.state["message"], "missing": list(self.state.get("missing") or []),
                    "case_id": self.state.get("case_id")}
        if task == "report_export":
            draft = self.state.get("draft")
            if not draft:
                return {"action": "doc", "workflow_state": "doc_ready", "task_type": task, "doc": None,
                        "message": "暂无可导出的病例内容，请先完成病例写作或整理",
                        "llm": {"ok": False, "source": "none"}, "case_id": self.state.get("case_id")}
            return {"action": "doc", "workflow_state": "doc_ready", "task_type": task,
                    "doc": {"task_type": task, "content": str(draft.get("case_summary") or ""),
                            "generated_by": "local-export", "citations": [],
                            "disclaimer": "本地导出内容，正式病历须经医生签署"},
                    "llm": {"ok": True, "source": "template", "generated_by": "local-export"},
                    "case_id": self.state.get("case_id")}
        doc = self._mock_doc(task)
        self.state.update({"phase": "doc_ready", "workflow_state": "doc_ready", "doc": doc, "message": ""})
        return {"action": "doc", "workflow_state": "doc_ready", "task_type": task, "doc": doc,
                "llm": {"ok": True, "source": "template", "model": "", "generated_by": "local-template",
                        "task_type": task, "error": "",
                        "at": datetime.now(timezone.utc).isoformat()},
                "missing": list(self.state.get("missing") or []),
                "case_id": self.state.get("case_id")}

    @staticmethod
    def _llm_meta() -> dict:
        """离线演示管线的真实性元信息：明确 source=demo，前端据此加离线标识。"""
        return {"ok": True, "source": "demo", "model": "offline_demo", "generated_by": "offline_demo",
                "error": "", "at": datetime.now(timezone.utc).isoformat()}

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
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise ConnectionError(f"HTTP {exc.code}：{detail}") from exc
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
                "task_type": snap.get("task_type") or "case_write",
                "review_history": snap.get("review_history", []),
                "doctor_final": snap.get("doctor_final"),
                "llm": snap.get("llm") or {},   # v58 真实性元信息（来源/模型/失败原因）
                "missing": snap.get("missing") or [],
                # v64 Agent 对话：自然语言回复与按需工具调用记录（前端可展开过程消息）
                "reply": snap.get("reply"),
                "tool_history": snap.get("tool_history") or [],
                "selected_body_regions": snap.get("selected_body_regions") or [],
                "messages": snap.get("messages") or [],   # v64-P1：对话历史（重启恢复用）
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
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise ConnectionError(f"HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"服务不可达：{exc}") from exc

    def delete_all_cases(self) -> dict:
        """v62：清空全部历史病例（服务端批量硬删，返回删除数）。"""
        req = urllib.request.Request(f"{self.base_url}/api/cases", method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise ConnectionError(f"HTTP {exc.code}：{detail}") from exc
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

    def review(self, feedback: str, action: str = "") -> dict:
        snap = self._req(f"/api/cases/{self.case_id}/review", {"feedback": feedback, "action": action})
        return self._render(snap)

    # —— v65：门诊病历单（病例窗口）——

    def get_record(self) -> dict:
        return self._req(f"/api/cases/{self.case_id}/record")

    def save_record(self, fields: dict) -> dict:
        return self._req(f"/api/cases/{self.case_id}/record", {"fields": fields or {}}, method="PUT")

    def set_regions(self, regions) -> dict:
        """v69：3D 选点即时写入当前病例草稿（服务端合并 + record 同步，返回导航决策）。"""
        return self._req(f"/api/cases/{self.case_id}/regions", {"regions": list(regions or [])})

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
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise ConnectionError(f"HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"服务不可达：{exc}") from exc
