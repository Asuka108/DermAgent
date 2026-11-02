# -*- coding: utf-8 -*-
"""v77 工具单一真源：一份 ToolSpec 同时服务 LLM function calling、MCP tools/list、
计划白名单、UI 目录与审计元数据。

设计边界：
- 纯数据 + 校验，不 import app / agent_loop（避免循环依赖）；
- 规范名沿用 v71 Agent 现有 5 个工具名（tool_history / plan_object 不变）；
- 15 条 v68 注册表能力保留为别名或独立声明项，`legacy_specs()` 精确复刻旧视图；
- 只有 executor 已绑定的工具可被调用；只有只读且无确认门禁的工具可暴露给 MCP。
"""
import re
import sys

SCHEMA_VERSION = "1.0"

# 任务路由顺序与旧 plan.TASK_TOOL_MAP 完全一致（键集合即旧表 18 项）。
TASK_ORDER = (
    "case_write", "complete_analysis", "case_organizer", "history", "lesion_desc",
    "summary", "referral", "followup", "differential", "treatment_ref",
    "medication_review", "auxiliary", "patient_edu", "medical_kb_search",
    "red_flag_screen", "privacy_deidentify", "body_region_3d", "report_export",
)

_READ_WRITE_TASKS = (
    "case_write", "complete_analysis", "case_organizer", "history", "lesion_desc",
    "summary", "referral", "followup", "differential", "treatment_ref",
    "medication_review", "auxiliary", "patient_edu",
)
_FULL_TASKS = ("case_write", "complete_analysis")
_KB_TASKS = _READ_WRITE_TASKS + ("medical_kb_search",)
_STATUS_ONLY_TASKS = ("red_flag_screen", "privacy_deidentify", "body_region_3d", "report_export")


class ToolSpec:
    """一个工具的完整契约。字段一旦发布只增不改。"""

    __slots__ = (
        "name", "title", "description", "input_schema", "output_schema", "risk",
        "requires_confirmation", "source_required", "executor", "tasks", "aliases",
        "expose_llm", "expose_mcp", "route", "legacy_input", "legacy_output",
        "legacy_risk", "legacy_title", "version", "step_label",
    )

    def __init__(self, name, title, description, input_schema, output_schema, risk,
                 requires_confirmation=False, source_required=False, executor="",
                 tasks=(), aliases=(), expose_llm=False, expose_mcp=False, route="",
                 legacy_input=None, legacy_output=None, legacy_risk="",
                 legacy_title="", version=SCHEMA_VERSION, step_label=""):
        self.name = name
        self.title = title
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.risk = risk
        self.requires_confirmation = requires_confirmation
        self.source_required = source_required
        self.executor = executor
        self.tasks = tuple(tasks)
        self.aliases = tuple(aliases)
        self.expose_llm = expose_llm
        self.expose_mcp = expose_mcp
        self.route = route
        self.legacy_input = legacy_input or {}
        self.legacy_output = legacy_output or {}
        self.legacy_risk = legacy_risk
        self.legacy_title = legacy_title
        self.version = version
        self.step_label = step_label

    @property
    def available(self) -> bool:
        return bool(self.executor)

    def as_dict(self) -> dict:
        return {"name": self.name, "title": self.title, "description": self.description,
                "input_schema": self.input_schema, "output_schema": self.output_schema,
                "risk": self.risk, "requires_confirmation": self.requires_confirmation,
                "source_required": self.source_required, "executor": self.executor,
                "available": self.available, "tasks": list(self.tasks),
                "aliases": list(self.aliases), "expose_llm": self.expose_llm,
                "expose_mcp": self.expose_mcp, "route": self.route,
                "version": self.version}


# —— 5 个可调用工具：schema 与 v71/v76 逐字一致 ——

_FULL_SCHEMAS = [
    ToolSpec(
        name="get_case_status",
        title="读取病例状态",
        description="查看当前病例状态：流程阶段、病历单各字段内容与来源、红旗、是否已有诊断草稿。开始处理医生消息前先调用它了解现状。",
        input_schema={"type": "object", "properties": {}, "required": []},
        output_schema={"type": "object", "properties": {
            "phase": {"type": "string"}, "workflow_state": {"type": "string"},
            "record_fields": {"type": "object"}, "red_flags": {"type": "array"},
            "missing": {"type": "array"}, "has_draft": {"type": "boolean"},
            "record_review_status": {"type": "string"}}},
        risk="read", executor="case.get_status", tasks=TASK_ORDER,
        expose_llm=True, expose_mcp=True, route="case_status",
        step_label="读取病例现状",
    ),
    ToolSpec(
        name="update_record_fields",
        title="写入病历字段",
        description="把医生在对话中口述的病例信息写入病历单字段（主诉/现病史/既往史/个人史/过敏史/用药史/体格检查/皮损描述/辅助检查/身体部位等，字段名以 get_case_status 返回为准）。医生已手工确认或修改过的字段不会被覆盖。每一条口述信息都应落进对应字段。",
        input_schema={"type": "object",
                      "properties": {"fields": {"type": "object",
                                                "description": "字段名→内容，如 {\"chief_complaint\": \"右前臂红斑伴瘙痒3天\"}"}},
                      "required": ["fields"]},
        output_schema={"type": "object", "properties": {
            "ok": {"type": "boolean"}, "updated": {"type": "array"},
            "rejected": {"type": "array"}, "error": {"type": "string"}}},
        risk="write", executor="case.update_fields", tasks=_READ_WRITE_TASKS,
        expose_llm=True, route="record_write", step_label="写入/更新病历字段",
    ),
    ToolSpec(
        name="complete_record_intake",
        title="完成病历填写",
        description="完成「病历填写」阶段，进入「情况补充」阶段。仅当医生明确表示填完了/确认/继续时调用；服务器会校验医生确认，未确认会失败。",
        input_schema={"type": "object", "properties": {}, "required": []},
        output_schema={"type": "object", "properties": {
            "ok": {"type": "boolean"}, "error": {"type": "string"},
            "workflow_state": {"type": "string"}}},
        risk="write", executor="case.complete_intake", tasks=_FULL_TASKS,
        expose_llm=True, route="complete_intake",
        step_label="完成病历填写，进入情况补充",
    ),
    ToolSpec(
        name="finish_supplement_and_analyze",
        title="辅助分析",
        description="完成「情况补充」并生成 AI 辅助诊断草稿（内部执行 RAG 检索与规则管线）。仅当医生明确确认提交或要求直接出诊断时调用；服务器会校验。返回缺失项/红旗/草稿要点，之后你在最终回复里用自然语言向医生汇报病状分析、疑似疾病排名、治疗方向与用药推荐（药名/用法教程/参考使用时间，均供医生核对）。",
        input_schema={"type": "object", "properties": {}, "required": []},
        output_schema={"type": "object", "properties": {
            "ok": {"type": "boolean"}, "error": {"type": "string"},
            "missing": {"type": "array"}, "red_flags": {"type": "array"},
            "draft_generated": {"type": "boolean"}}},
        risk="write", executor="case.analyze", tasks=_FULL_TASKS,
        expose_llm=True, route="doc_task:complete_analysis",
        step_label="检索知识库并生成辅助分析草稿",
    ),
    ToolSpec(
        name="search_knowledge_base",
        title="本地知识库检索",
        description="在本地医学书籍知识库中检索（全本地运行，原文不外发）。用于核对医学事实、为解释提供引用依据。",
        input_schema={"type": "object",
                      "properties": {"query": {"type": "string"},
                                     "top_k": {"type": "integer", "default": 3}},
                      "required": ["query"]},
        output_schema={"type": "object", "properties": {
            "ok": {"type": "boolean"}, "citations": {"type": "array"},
            "error": {"type": "string"}}},
        risk="read", source_required=True, executor="kb.search",
        tasks=_KB_TASKS, aliases=("medical_kb_search",),
        expose_llm=True, expose_mcp=True, route="kb_search",
        step_label="检索本地知识库",
        legacy_title="RAG 医学检索",
        legacy_input={"query": "string(症状+主题)", "top_k": "integer(默认3)"},
        legacy_output={"citations": "array(citation_id/标题/章节/片段/检索时间)"},
    ),
]


# —— v68 注册表能力：保留声明、路由与旧视图，其中 10 条仅目录展示 ——

_DECLARED = [
    ToolSpec(
        name="case_extractor", title="病例信息提取",
        description="从自然语言对话中提取主诉、现病史、发病时间、症状、身体部位、既往史、过敏史、用药史和辅助检查；不确定信息标记待确认，不当事实写入病例。",
        input_schema={"type": "object", "properties": {
            "dialog_text": {"type": "string"}, "case_current": {"type": "object"}},
            "required": ["dialog_text"]},
        output_schema={"type": "object", "properties": {
            "case_fields": {"type": "object"}, "unconfirmed": {"type": "array"}}},
        risk="write", route="merge_reply", legacy_risk="low",
        legacy_input={"dialog_text": "string(医生叙述)", "case_current": "object(现有病例字段)"},
        legacy_output={"case_fields": "object(字段增量)", "unconfirmed": "array(待确认项)"},
    ),
    ToolSpec(
        name="dynamic_clarifier", title="动态追问",
        description="按临床重要性对缺失关键字段生成下一轮少量必要问题；追问前说明目的；支持跳过/修改/重新询问。",
        input_schema={"type": "object", "properties": {
            "missing_fields": {"type": "array"}, "case_current": {"type": "object"}}},
        output_schema={"type": "object", "properties": {
            "questions": {"type": "array"}, "min_gate": {"type": "object"}}},
        risk="read", route="route_clarify", legacy_risk="low",
        legacy_input={"missing_fields": "array", "case_current": "object"},
        legacy_output={"questions": "array(≤3，带目的说明)", "min_gate": "object(最低信息要求)"},
    ),
    ToolSpec(
        name="body_region_3d", title="3D 身体部位选择",
        description="可选辅助工具：3D 人体模型圈选受累部位；不选不阻塞任何流程，来源标注 model_3d。",
        input_schema={"type": "object", "properties": {"selected_regions": {"type": "array"}}},
        output_schema={"type": "object", "properties": {
            "body_regions": {"type": "array"}, "source": {"type": "string"}}},
        risk="read", route="client_3d", legacy_risk="low",
        legacy_input={"selected_regions": "array(部位名)"},
        legacy_output={"body_regions": "array", "source": "model_3d"},
    ),
    ToolSpec(
        name="image_analyzer", title="图片分析",
        description="医生上传皮肤照片后的可选分析：质量提示（光线/范围/清晰度）+ 可见特征协作采集（红斑/丘疹/水疱/脓疱/鳞屑/结痂/边界/分布）；仅供辅助不等于诊断；上传后先询问是否分析，不默认执行。",
        input_schema={"type": "object", "properties": {
            "attachment": {"type": "object"}, "doctor_confirm": {"type": "boolean"}},
            "required": ["attachment"]},
        output_schema={"type": "object", "properties": {
            "quality_hints": {"type": "array"}, "features": {"type": "array"},
            "disclaimer": {"type": "string"}}},
        risk="screen", requires_confirmation=True, route="image_analysis",
        legacy_risk="medium",
        legacy_input={"attachment": "object(文件名/大小/类型/拍摄时间)", "doctor_confirm": "boolean"},
        legacy_output={"quality_hints": "array", "features": "array(医生勾选特征)", "disclaimer": "string"},
    ),
    ToolSpec(
        name="differential_assistant", title="鉴别诊断辅助",
        description="先 RAG 检索再生成可能性排序；每个候选疾病给支持/反对依据、需补充信息与建议检查；禁止无依据的确定性诊断。",
        input_schema={"type": "object", "properties": {
            "case": {"type": "object"}, "citations": {"type": "array"}},
            "required": ["case"]},
        output_schema={"type": "object", "properties": {
            "suspected_conditions": {"type": "array"}, "recommended_exams": {"type": "array"},
            "citations": {"type": "array"}}},
        risk="screen", requires_confirmation=True, source_required=True,
        route="doc_task:differential", legacy_risk="high",
        legacy_input={"case": "object", "citations": "array(RAG 检索结果)"},
        legacy_output={"suspected_conditions": "array(排序+依据)",
                       "recommended_exams": "array", "citations": "array"},
    ),
    ToolSpec(
        name="medication_safety_checker", title="用药安全核对",
        description="核对适应证/禁忌证/过敏史/重复用药/相互作用与特殊人群（儿童/孕妇/哺乳/老年/肝肾）风险；输出需医生确认的用药参考，不自动开处方。",
        input_schema={"type": "object", "properties": {
            "case": {"type": "object"}, "medications": {"type": "array"}},
            "required": ["case"]},
        output_schema={"type": "object", "properties": {
            "medication_checks": {"type": "array"}, "citations": {"type": "array"}}},
        risk="screen", requires_confirmation=True, source_required=True,
        route="doc_task:medication_review", legacy_risk="high",
        legacy_input={"case": "object(含用药史/过敏史)", "medications": "array"},
        legacy_output={"medication_checks": "array(items_to_verify/requires_doctor_confirmation)",
                       "citations": "array"},
    ),
    ToolSpec(
        name="red_flag_screen", title="危险信号筛查",
        description="检查高热/快速扩散/明显疼痛/眼周口腔受累/呼吸困难/严重过敏/免疫抑制等危险信号；输出立即急诊/尽快就医/普通门诊观察三级建议；优先级高于普通整理。",
        input_schema={"type": "object", "properties": {"case": {"type": "object"}},
                      "required": ["case"]},
        output_schema={"type": "object", "properties": {
            "red_flags": {"type": "array"}, "escalation": {"type": "object"}}},
        risk="screen", route="detect_red_flags", legacy_risk="medium",
        legacy_input={"case": "object"},
        legacy_output={"red_flags": "array", "escalation": "object(level/reasons)"},
    ),
    ToolSpec(
        name="case_organizer", title="病例整理",
        description="把对话/图片/检索结果整理为标准皮肤科门诊病历（支持初诊/复诊/会诊/转诊模板）；只更新新增或确认字段，不覆盖医生手动修改内容。",
        input_schema={"type": "object", "properties": {
            "case": {"type": "object"},
            "mode": {"type": "string", "enum": ["initial", "followup", "consult", "referral"]}},
            "required": ["case"]},
        output_schema={"type": "object", "properties": {"record": {"type": "object"}}},
        risk="write", requires_confirmation=True, route="case_writer",
        legacy_risk="medium",
        legacy_input={"case": "object", "mode": "enum(initial/followup/consult/referral)"},
        legacy_output={"record": "object(18 字段病历单)"},
    ),
    ToolSpec(
        name="case_comparator", title="病例对比",
        description="对比当前病例与选定历史病例的症状/部位/检查/用药/恢复情况；只做客观字段级对比（变化/新增/缺失），语义判断留给医生。",
        input_schema={"type": "object", "properties": {
            "current_case_id": {"type": "string"}, "history_case_id": {"type": "string"}},
            "required": ["current_case_id"]},
        output_schema={"type": "object", "properties": {
            "changed": {"type": "array"}, "added": {"type": "array"},
            "missing": {"type": "array"}, "disclaimer": {"type": "string"}}},
        risk="read", route="case_compare", legacy_risk="low",
        legacy_input={"current_case_id": "string", "history_case_id": "string"},
        legacy_output={"changed": "array", "added": "array", "missing": "array",
                       "disclaimer": "string"},
    ),
    ToolSpec(
        name="exam_advisor", title="检查建议",
        description="根据症状与鉴别方向给出可能需要的检查，说明每项的目的/适用条件/局限性。",
        input_schema={"type": "object", "properties": {
            "case": {"type": "object"}, "conditions": {"type": "array"}},
            "required": ["case"]},
        output_schema={"type": "object", "properties": {
            "recommended_exams": {"type": "array"}, "citations": {"type": "array"}}},
        risk="screen", requires_confirmation=True, source_required=True,
        route="doc_task:auxiliary", legacy_risk="medium",
        legacy_input={"case": "object", "conditions": "array(鉴别方向)"},
        legacy_output={"recommended_exams": "array(exam/reason)", "citations": "array"},
    ),
    ToolSpec(
        name="patient_communicator", title="患者沟通",
        description="把专业结果转换为患者易懂的说明：用药说明/居家护理/复诊提醒/注意事项；不改变医生确认后的医学结论。",
        input_schema={"type": "object", "properties": {
            "case": {"type": "object"}, "confirmed_conclusions": {"type": "object"}},
            "required": ["case"]},
        output_schema={"type": "object", "properties": {
            "doc": {"type": "object"}, "citations": {"type": "array"}}},
        risk="write", route="doc_task:patient_edu", legacy_risk="low",
        legacy_input={"case": "object", "confirmed_conclusions": "object"},
        legacy_output={"doc": "object(患者教育文书)", "citations": "array"},
    ),
    ToolSpec(
        name="followup_planner", title="随访计划",
        description="按疾病风险/治疗方式/恢复情况生成复诊时间与观察指标；提醒条件仅辅助，不替代医生决定。",
        input_schema={"type": "object", "properties": {
            "case": {"type": "object"}, "conditions": {"type": "array"}},
            "required": ["case"]},
        output_schema={"type": "object", "properties": {
            "follow_up": {"type": "object"}, "recovery_period": {"type": "object"}}},
        risk="write", route="doc_task:followup", legacy_risk="low",
        legacy_input={"case": "object", "conditions": "array"},
        legacy_output={"follow_up": "object(复诊时间/观察指标)", "recovery_period": "object"},
    ),
    ToolSpec(
        name="privacy_deidentifier", title="隐私脱敏",
        description="识别并遮盖姓名/身份证号/手机号/住址/病案号；上传或导出前提供脱敏预览与确认。",
        input_schema={"type": "object", "properties": {
            "payload": {"type": "object"}, "preview": {"type": "boolean"}},
            "required": ["payload"]},
        output_schema={"type": "object", "properties": {
            "deidentified": {"type": "object"}, "masked_fields": {"type": "array"}}},
        risk="read", route="client_deidentify", legacy_risk="low",
        legacy_input={"payload": "object", "preview": "boolean"},
        legacy_output={"deidentified": "object", "masked_fields": "array"},
    ),
    ToolSpec(
        name="case_exporter", title="导出与审计",
        description="导出正式病例单（门禁：医生确认后放行）并记录审计日志；每次工具调用的输入/输出/来源/确认均可追踪。",
        input_schema={"type": "object", "properties": {"case_id": {"type": "string"}},
                      "required": ["case_id"]},
        output_schema={"type": "object", "properties": {
            "markdown": {"type": "string"}, "review_status": {"type": "string"}}},
        risk="export", requires_confirmation=True, route="record_export",
        legacy_risk="medium",
        legacy_input={"case_id": "string"},
        legacy_output={"markdown": "string", "review_status": "confirmed"},
    ),
]

_SPECS = _FULL_SCHEMAS + _DECLARED
_REGISTRY = {s.name: s for s in _SPECS}
_ALIAS_INDEX = {}
for _s in _SPECS:
    for _alias in _s.aliases:
        _ALIAS_INDEX[_alias] = _s
# 旧 v68 任务 → 注册表工具名（保留 tool_for_task 的行为）
LEGACY_TASK_TOOLS = {
    "case_edit": "case_organizer", "report_export": "case_exporter",
    "differential": "differential_assistant", "treatment_ref": "exam_advisor",
    "medication_review": "medication_safety_checker", "patient_edu": "patient_communicator",
    "followup": "followup_planner", "history": "case_organizer",
    "lesion_desc": "case_organizer", "case_write": "case_write",
    "complete_analysis": "case_write", "case_compare": "case_comparator",
    "image_analyze": "image_analyzer", "privacy_deidentify": "privacy_deidentifier",
    "body_region_3d": "body_region_3d", "medical_kb_search": "medical_kb_search",
    "red_flag_screen": "red_flag_screen", "auxiliary": "exam_advisor",
}
_ROUTE_INDEX = {}
for _s in _SPECS:
    if _s.route:
        _ROUTE_INDEX[_s.route] = _s
        if _s.route.startswith("doc_task:"):
            _ROUTE_INDEX[_s.route.split(":", 1)[1]] = _s
for _task, _tool in LEGACY_TASK_TOOLS.items():
    if _tool in _REGISTRY:
        _ROUTE_INDEX.setdefault(_task, _REGISTRY[_tool])

_RISK_LEVEL = {"read": "low", "write": "medium", "screen": "medium", "export": "medium"}


def all() -> list:
    return list(_SPECS)


def get(name_or_alias: str):
    """按规范名、旧别名、旧 route / route 后缀、旧任务名解析工具；未知返回 None。"""
    key = str(name_or_alias or "").strip()
    if not key:
        return None
    return _REGISTRY.get(key) or _ALIAS_INDEX.get(key) or _ROUTE_INDEX.get(key)


def spec(name: str) -> dict:
    s = get(name)
    return s.as_dict() if s else {}


def llm_tools() -> list:
    """OpenAI function calling 视图；顺序与 v71 TOOLS 一致。"""
    return [{"type": "function", "function": {
        "name": s.name, "description": s.description, "parameters": s.input_schema}}
        for s in _SPECS if s.expose_llm]


def mcp_tools() -> list:
    """MCP tools/list 视图：只读、无医生确认门禁的工具才暴露；自动追加 case_id。"""
    out = []
    for s in _SPECS:
        if not (s.expose_mcp and s.risk == "read" and not s.requires_confirmation):
            continue
        schema = dict(s.input_schema)
        props = dict(schema.get("properties") or {})
        props["case_id"] = {"type": "string", "description": "病例 ID（当前会话）"}
        schema["properties"] = props
        required = [k for k in (schema.get("required") or []) if k != "case_id"]
        schema["required"] = ["case_id"] + required
        out.append({"name": s.name, "description": s.description, "inputSchema": schema,
                    "annotations": {"readOnlyHint": True, "destructiveHint": False}})
    return out


def for_task(task: str) -> list:
    """任务 → 允许的 Agent 工具（与旧 plan.TASK_TOOL_MAP 逐项等价）。"""
    key = str(task or "")
    names = [s.name for s in _SPECS if s.expose_llm and key in s.tasks]
    if not names:
        names = ["get_case_status", "search_knowledge_base"]
    return names


def task_tool_map() -> dict:
    return {task: tuple(for_task(task)) for task in TASK_ORDER}


def risk_of(name: str) -> str:
    s = get(name)
    return s.risk if s else "read"


def catalog() -> list:
    return [s.as_dict() for s in _SPECS]


def legacy_specs() -> list:
    """v68 的 15 条 × 9 键视图（旧测试与 UI 目录共用）。"""
    out = []
    for s in _DECLARED:
        out.append({
            "name": s.name, "label": s.title, "description": s.description,
            "input_schema": dict(s.legacy_input), "output_schema": dict(s.legacy_output),
            "requires_confirmation": s.requires_confirmation,
            "source_required": s.source_required,
            "risk_level": s.legacy_risk or _RISK_LEVEL.get(s.risk, "low"),
            "route": s.route,
        })
    kb = _REGISTRY["search_knowledge_base"]
    out.append({
        "name": "medical_kb_search", "label": kb.legacy_title or kb.title,
        "description": "在已审核本地知识库检索医学资料；每条引用带来源/章节/片段/检索时间；"
                       "无可靠来源时明确说明不编造。",
        "input_schema": dict(kb.legacy_input), "output_schema": dict(kb.legacy_output),
        "requires_confirmation": False, "source_required": True,
        "risk_level": "low", "route": "kb_search",
    })
    return out


# —— JSON Schema 校验（无第三方依赖） ——

_TYPE_NAMES = {"string": "字符串", "integer": "整数", "number": "数字",
               "boolean": "布尔值", "object": "对象", "array": "数组"}


def _type_ok(value, typ: str) -> bool:
    if typ == "string":
        return isinstance(value, str)
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ == "boolean":
        return isinstance(value, bool)
    if typ == "object":
        return isinstance(value, dict)
    if typ == "array":
        return isinstance(value, list)
    return True


def validate_schema(args, schema: dict, path: str = "") -> str:
    """递归校验：required / type / enum / items / 嵌套对象 / 字符串长度。返回中文错误。"""
    if not isinstance(schema, dict) or not schema:
        return ""
    if not isinstance(args, dict):
        return "参数必须是 JSON 对象"
    required = schema.get("required") or []
    for key in required:
        if key not in args or args.get(key) in (None, ""):
            return f"缺少必填参数：{path + key if not path else path + '.' + key}"
    props = schema.get("properties") or {}
    extra_ok = schema.get("additionalProperties", True)
    if extra_ok is False:
        for key in args:
            if key not in props:
                return f"未知参数：{path + key if not path else path + '.' + key}"
    for key, value in args.items():
        sub = props.get(key)
        if not isinstance(sub, dict):
            continue
        where = path + key if not path else path + "." + key
        typ = sub.get("type")
        if typ and value not in (None, ""):
            if typ == "integer" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
                args[key] = int(value)
                value = args[key]
            if not _type_ok(value, typ):
                return f"参数 {where} 应为{_TYPE_NAMES.get(typ, typ)}"
        enum = sub.get("enum")
        if enum and value not in enum:
            return f"参数 {where} 取值不在允许范围：{value}"
        if typ == "string" and isinstance(value, str):
            if sub.get("minLength") and len(value) < int(sub["minLength"]):
                return f"参数 {where} 过短"
            if sub.get("maxLength") and len(value) > int(sub["maxLength"]):
                return f"参数 {where} 过长"
        if typ == "integer" and isinstance(value, int) and not isinstance(value, bool):
            if sub.get("minimum") is not None and value < int(sub["minimum"]):
                return f"参数 {where} 小于最小值 {sub['minimum']}"
            if sub.get("maximum") is not None and value > int(sub["maximum"]):
                return f"参数 {where} 大于最大值 {sub['maximum']}"
        if typ == "array" and isinstance(value, list):
            items = sub.get("items")
            if isinstance(items, dict) and items.get("type"):
                for i, item in enumerate(value):
                    if not _type_ok(item, items["type"]):
                        return f"参数 {where}[{i}] 应为{_TYPE_NAMES.get(items['type'], items['type'])}"
                if sub.get("maxItems") and len(value) > int(sub["maxItems"]):
                    return f"参数 {where} 数量超过上限 {sub['maxItems']}"
        if typ == "object" and isinstance(value, dict):
            err = validate_schema(value, sub, where + ".")
            if err:
                return err
    return ""


def validate(name: str, args: dict) -> str:
    s = get(name)
    if s is None:
        return f"未知工具：{name}"
    return validate_schema(args, s.input_schema)


_SAFE_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def assert_valid_registry() -> None:
    """启动/测试自检：名字唯一、别名不冲突、可调用工具必须有 executor。"""
    names = [s.name for s in _SPECS]
    assert len(names) == len(set(names)), "工具名重复"
    seen = set()
    for s in _SPECS:
        assert _SAFE_IDENT.match(s.name), f"非法工具名：{s.name}"
        for alias in s.aliases:
            assert alias not in _REGISTRY and alias not in seen, f"别名冲突：{alias}"
            seen.add(alias)
        if s.expose_llm or s.expose_mcp:
            assert s.available, f"对外暴露的工具必须有 executor：{s.name}"
            assert s.input_schema.get("type") == "object", f"{s.name} schema 必须是 object"


assert_valid_registry()

# 同一进程里两种导入方式必须指向同一模块（动态工具按对象身份查表）。
sys.modules.setdefault("tool_spec", sys.modules[__name__])
sys.modules.setdefault("server.tool_spec", sys.modules[__name__])
