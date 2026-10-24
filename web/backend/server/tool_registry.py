#!/usr/bin/env python3
"""v68 工具注册表（计划 §三/§五.1）：DermAgent 医疗辅助工具统一协议。

每个工具统一定义六要素（计划 §五.1）：
  name                 工具唯一名
  label                中文标签（前端显示）
  description          适用场景
  input/output_schema  输入/输出字段摘要（结构化校验依据，简明 dict 描述）
  requires_confirmation  是否需要医生确认后才执行（高风险操作门禁）
  source_required      是否必须提供 RAG 来源（无引用不得输出医学结论）
  risk_level           low / medium / high（前端意图卡风险标识）
  route                对应的服务端执行路径（已有能力映射 / 新工具入口）

设计边界（计划 §十）：所有工具都是医生辅助；医学判断类（鉴别/用药/检查）
必须 source_required=True 且 requires_confirmation=True；调度规则见 app.py _screen。
"""

RISK_LOW, RISK_MED, RISK_HIGH = "low", "medium", "high"

TOOLS = [
    {
        "name": "case_extractor",
        "label": "病例信息提取",
        "description": "从自然语言对话中提取主诉、现病史、发病时间、症状、身体部位、既往史、过敏史、用药史和辅助检查；不确定信息标记待确认，不当事实写入病例。",
        "input_schema": {"dialog_text": "string(医生叙述)", "case_current": "object(现有病例字段)"},
        "output_schema": {"case_fields": "object(字段增量)", "unconfirmed": "array(待确认项)"},
        "requires_confirmation": False,
        "source_required": False,
        "risk_level": RISK_LOW,
        "route": "merge_reply",
    },
    {
        "name": "dynamic_clarifier",
        "label": "动态追问",
        "description": "按临床重要性对缺失关键字段生成下一轮少量必要问题；追问前说明目的；支持跳过/修改/重新询问。",
        "input_schema": {"missing_fields": "array", "case_current": "object"},
        "output_schema": {"questions": "array(≤3，带目的说明)", "min_gate": "object(最低信息要求)"},
        "requires_confirmation": False,
        "source_required": False,
        "risk_level": RISK_LOW,
        "route": "route_clarify",
    },
    {
        "name": "body_region_3d",
        "label": "3D 身体部位选择",
        "description": "可选辅助工具：3D 人体模型圈选受累部位；不选不阻塞任何流程，来源标注 model_3d。",
        "input_schema": {"selected_regions": "array(部位名)"},
        "output_schema": {"body_regions": "array", "source": "model_3d"},
        "requires_confirmation": False,
        "source_required": False,
        "risk_level": RISK_LOW,
        "route": "client_3d",
    },
    {
        "name": "image_analyzer",
        "label": "图片分析",
        "description": "医生上传皮肤照片后的可选分析：质量提示（光线/范围/清晰度）+ 可见特征协作采集（红斑/丘疹/水疱/脓疱/鳞屑/结痂/边界/分布）；仅供辅助不等于诊断；上传后先询问是否分析，不默认执行。",
        "input_schema": {"attachment": "object(文件名/大小/类型/拍摄时间)", "doctor_confirm": "boolean"},
        "output_schema": {"quality_hints": "array", "features": "array(医生勾选特征)", "disclaimer": "string"},
        "requires_confirmation": True,
        "source_required": False,
        "risk_level": RISK_MED,
        "route": "image_analysis",
    },
    {
        "name": "medical_kb_search",
        "label": "RAG 医学检索",
        "description": "在已审核本地知识库检索医学资料；每条引用带来源/章节/片段/检索时间；无可靠来源时明确说明不编造。",
        "input_schema": {"query": "string(症状+主题)", "top_k": "integer(默认3)"},
        "output_schema": {"citations": "array(citation_id/标题/章节/片段/检索时间)"},
        "requires_confirmation": False,
        "source_required": True,
        "risk_level": RISK_LOW,
        "route": "kb_search",
    },
    {
        "name": "differential_assistant",
        "label": "鉴别诊断辅助",
        "description": "先 RAG 检索再生成可能性排序；每个候选疾病给支持/反对依据、需补充信息与建议检查；禁止无依据的确定性诊断。",
        "input_schema": {"case": "object", "citations": "array(RAG 检索结果)"},
        "output_schema": {"suspected_conditions": "array(排序+依据)", "recommended_exams": "array", "citations": "array"},
        "requires_confirmation": True,
        "source_required": True,
        "risk_level": RISK_HIGH,
        "route": "doc_task:differential",
    },
    {
        "name": "medication_safety_checker",
        "label": "用药安全核对",
        "description": "核对适应证/禁忌证/过敏史/重复用药/相互作用与特殊人群（儿童/孕妇/哺乳/老年/肝肾）风险；输出需医生确认的用药参考，不自动开处方。",
        "input_schema": {"case": "object(含用药史/过敏史)", "medications": "array"},
        "output_schema": {"medication_checks": "array(items_to_verify/requires_doctor_confirmation)", "citations": "array"},
        "requires_confirmation": True,
        "source_required": True,
        "risk_level": RISK_HIGH,
        "route": "doc_task:medication_review",
    },
    {
        "name": "red_flag_screen",
        "label": "危险信号筛查",
        "description": "检查高热/快速扩散/明显疼痛/眼周口腔受累/呼吸困难/严重过敏/免疫抑制等危险信号；输出立即急诊/尽快就医/普通门诊观察三级建议；优先级高于普通整理。",
        "input_schema": {"case": "object"},
        "output_schema": {"red_flags": "array", "escalation": "object(level/reasons)"},
        "requires_confirmation": False,
        "source_required": False,
        "risk_level": RISK_MED,
        "route": "detect_red_flags",
    },
    {
        "name": "case_organizer",
        "label": "病例整理",
        "description": "把对话/图片/检索结果整理为标准皮肤科门诊病历（支持初诊/复诊/会诊/转诊模板）；只更新新增或确认字段，不覆盖医生手动修改内容。",
        "input_schema": {"case": "object", "mode": "enum(initial/followup/consult/referral)"},
        "output_schema": {"record": "object(18 字段病历单)"},
        "requires_confirmation": True,
        "source_required": False,
        "risk_level": RISK_MED,
        "route": "case_writer",
    },
    {
        "name": "case_comparator",
        "label": "病例对比",
        "description": "对比当前病例与选定历史病例的症状/部位/检查/用药/恢复情况；只做客观字段级对比（变化/新增/缺失），语义判断留给医生。",
        "input_schema": {"current_case_id": "string", "history_case_id": "string"},
        "output_schema": {"changed": "array", "added": "array", "missing": "array", "disclaimer": "string"},
        "requires_confirmation": False,
        "source_required": False,
        "risk_level": RISK_LOW,
        "route": "case_compare",
    },
    {
        "name": "exam_advisor",
        "label": "检查建议",
        "description": "根据症状与鉴别方向给出可能需要的检查，说明每项的目的/适用条件/局限性。",
        "input_schema": {"case": "object", "conditions": "array(鉴别方向)"},
        "output_schema": {"recommended_exams": "array(exam/reason)", "citations": "array"},
        "requires_confirmation": True,
        "source_required": True,
        "risk_level": RISK_MED,
        "route": "doc_task:auxiliary",
    },
    {
        "name": "patient_communicator",
        "label": "患者沟通",
        "description": "把专业结果转换为患者易懂的说明：用药说明/居家护理/复诊提醒/注意事项；不改变医生确认后的医学结论。",
        "input_schema": {"case": "object", "confirmed_conclusions": "object"},
        "output_schema": {"doc": "object(患者教育文书)", "citations": "array"},
        "requires_confirmation": False,
        "source_required": False,
        "risk_level": RISK_LOW,
        "route": "doc_task:patient_edu",
    },
    {
        "name": "followup_planner",
        "label": "随访计划",
        "description": "按疾病风险/治疗方式/恢复情况生成复诊时间与观察指标；提醒条件仅辅助，不替代医生决定。",
        "input_schema": {"case": "object", "conditions": "array"},
        "output_schema": {"follow_up": "object(复诊时间/观察指标)", "recovery_period": "object"},
        "requires_confirmation": False,
        "source_required": False,
        "risk_level": RISK_LOW,
        "route": "doc_task:followup",
    },
    {
        "name": "privacy_deidentifier",
        "label": "隐私脱敏",
        "description": "识别并遮盖姓名/身份证号/手机号/住址/病案号；上传或导出前提供脱敏预览与确认。",
        "input_schema": {"payload": "object", "preview": "boolean"},
        "output_schema": {"deidentified": "object", "masked_fields": "array"},
        "requires_confirmation": False,
        "source_required": False,
        "risk_level": RISK_LOW,
        "route": "client_deidentify",
    },
    {
        "name": "case_exporter",
        "label": "导出与审计",
        "description": "导出正式病例单（门禁：医生确认后放行）并记录审计日志；每次工具调用的输入/输出/来源/确认均可追踪。",
        "input_schema": {"case_id": "string"},
        "output_schema": {"markdown": "string", "review_status": "confirmed"},
        "requires_confirmation": True,
        "source_required": False,
        "risk_level": RISK_MED,
        "route": "record_export",
    },
]

TOOL_REGISTRY = {t["name"]: t for t in TOOLS}

# 任务路由 → 工具名映射（_screen 分流使用；与 app.py 既有 _route_* 对齐）
TASK_TOOLS = {
    "case_edit": "case_organizer",
    "report_export": "case_exporter",
    "differential": "differential_assistant",
    "treatment_ref": "exam_advisor",          # 治疗参考走检查建议同源管线（doc_task）
    "medication_review": "medication_safety_checker",
    "patient_edu": "patient_communicator",
    "followup": "followup_planner",
    "history": "case_organizer",
    "lesion_desc": "case_organizer",
    "case_write": "case_write",
    "complete_analysis": "case_write",
    "case_compare": "case_comparator",
    "image_analyze": "image_analyzer",
    "privacy_deidentify": "privacy_deidentifier",
    "body_region_3d": "body_region_3d",
    "medical_kb_search": "medical_kb_search",
    "red_flag_screen": "red_flag_screen",
    "auxiliary": "exam_advisor",
}


def tool_for_task(task: str) -> str:
    """任务类型 → 注册表工具名（未匹配回退病例提取）。"""
    return TASK_TOOLS.get(task, "case_extractor")


def spec(name: str) -> dict:
    """取工具协议（未知工具给中性缺省，防御畸形调用）。"""
    return TOOL_REGISTRY.get(name) or {
        "name": name, "label": name, "description": "",
        "input_schema": {}, "output_schema": {},
        "requires_confirmation": False, "source_required": False,
        "risk_level": RISK_LOW, "route": name,
    }
