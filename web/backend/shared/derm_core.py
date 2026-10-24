"""肤智（DermAgent）——v1.1 固定输出契约与规则管线（单一权威实现）。

被 server/（可信服务层）与 client/（Windows 离线演示资产）共同引用；
TS 智能体侧（src/app/logic.ts）保持同构。契约对齐参赛计划 §6.3（schema_version 1.1）：
workflow_state / blocking_reason / follow_up_questions / red_flag_escalation.is_blocking /
safety_policy_version / review_status；全部 snake_case。
"""

import re
from datetime import datetime, timezone

SCHEMA_VERSION = "1.1"
SAFETY_POLICY_VERSION = "sp-1.0"
KB_SNAPSHOT = "synthetic-kb@2026-09-04"
KB_LABEL = "合成演示知识库（团队自编并经医学审核的合成资料，非真实临床指南）"
DISCLAIMER = (
    "AI 生成内容仅供医生决策支持，不构成诊断或处方；结果可能存在遗漏或错误，"
    "鉴别诊断仅供医生核对；药物名称与参考使用时长仅供医生核对，禁止自动处方、剂量、频次或起停医嘱，"
    "必须由专业人员结合实际情况独立判断。"
)

# 阶段6：确认后生成的是「待签署草稿」——最终签署/执行必须由医生在授权目标系统完成；
# 系统不自动写入正式病历、不自动提交医嘱或处方、不自动执行治疗（计划 §7/阶段6）。
SIGNATURE_NOTE = (
    "本报告为待签署草稿（AI 辅助生成，医生已整份审核确认）。最终签署与执行须由医生在授权目标医疗系统中"
    "完成；本系统不自动写入正式病历，不自动提交医嘱或电子处方，不自动执行任何治疗。"
)

KB = [
    {
        "docId": "DOC-AD-001",
        "docName": "湿疹样皮炎诊疗要点（合成演示版）",
        "version": "v1.0",
        "sourceType": "authorized_case",
        "suggestedDx": ["特应性皮炎（待排）", "接触性皮炎（待排）", "慢性单纯性苔藓（待排）"],
        "medReviewNotes": [
            "保湿剂：基础护理，适用性请医生核对",
            "外用糖皮质激素：强度与疗程须由医生评估，禁止自动处方",
            "口服抗组胺药：可用于止痒，是否使用由医生决定",
        ],
        "sections": [
            {"title": "临床表现",
             "content": "典型表现为瘙痒性红斑、丘疹，可伴干燥脱屑与渗出，常对称分布于屈侧（肘窝、腘窝）及手部；慢性期可见苔藓样变。",
             "keywords": ["瘙痒", "红斑", "丘疹", "脱屑", "干燥", "渗出", "对称", "屈侧", "肘窝", "手部", "湿疹", "苔藓"]},
            {"title": "鉴别诊断",
             "content": "需与接触性皮炎（边界清楚、与接触物相关）、脂溢性皮炎（皮脂溢出部位油腻鳞屑）等鉴别；特应性皮炎多伴特应性体质与个人/家族过敏史。",
             "keywords": ["鉴别", "特应性", "接触性", "脂溢性", "体质", "过敏史"]},
            {"title": "用药核对要点",
             "content": "外用糖皮质激素的强度选择与疗程须由医生评估；保湿剂为基础护理；口服抗组胺药可用于止痒。本知识库不提供自动处方、剂量与疗程建议。",
             "keywords": ["用药", "激素", "保湿", "抗组胺", "处方", "药物"]},
            {"title": "红旗与转诊",
             "content": "出现脓疱、发热、皮疹疼痛加剧或红肿迅速扩散时，提示继发感染（如蜂窝织炎）可能，须升级人工审核。",
             "keywords": ["感染", "脓疱", "发热", "蜂窝织炎", "扩散", "疼痛加剧"]},
        ],
    },
    {
        "docId": "DOC-HZ-001",
        "docName": "带状疱疹识别与红旗清单（合成演示版）",
        "version": "v1.0",
        "sourceType": "authorized_case",
        "suggestedDx": ["带状疱疹（可能）", "眼部带状疱疹（红旗待排）", "接触性皮炎（待排）"],
        "medReviewNotes": [
            "系统抗病毒药物（如阿昔洛韦类）为处方药：启动与否及时机须由医生评估，禁止自动处方、剂量与疗程建议",
            "镇痛用药：须医生核对（自行使用对乙酰氨基酚等止痛药时应记录并评估）",
        ],
        "sections": [
            {"title": "临床表现",
             "content": "单侧、沿单一皮节（如肋间神经）分布的簇集性水疱，伴灼痛、刺痛或皮肤敏感，常「先痛后疹」；老年与免疫低下者风险更高。",
             "keywords": ["水疱", "水泡", "簇集", "单侧", "一侧", "神经", "灼痛", "刺痛", "皮节", "肋间", "带状", "触痛"]},
            {"title": "红旗清单",
             "content": "眼周/眼睑/鼻尖受累（Hutchinson 征）提示眼部带状疱疹风险；免疫抑制、播散性皮损、剧烈神经痛或年龄≥50 岁须升级人工审核。",
             "keywords": ["眼", "眼周", "眼睑", "鼻尖", "免疫", "播散", "发热", "剧烈", "疼痛", "红旗"]},
            {"title": "鉴别诊断",
             "content": "需与接触性皮炎、虫咬皮炎、丹毒等鉴别；单侧皮节分布与神经痛是带状疱疹的重要线索，双侧对称分布更支持湿疹样病变。",
             "keywords": ["鉴别", "接触性", "虫咬", "丹毒", "对称"]},
            {"title": "用药核对要点",
             "content": "系统抗病毒治疗宜在发疹 72 小时内启动，是否启动及方案由医生评估；本知识库禁止给出自动处方、剂量与疗程。",
             "keywords": ["抗病毒", "阿昔洛韦", "处方", "镇痛", "止痛"]},
        ],
    },
    {
        "docId": "DOC-DX-001",
        "docName": "皮肤科结构化问诊与照片规范（合成演示版）",
        "version": "v1.0",
        "sourceType": "authorized_case",
        "suggestedDx": [],
        "medReviewNotes": [],
        "sections": [
            {"title": "九要素问诊清单",
             "content": "结构化问诊应覆盖：身体区域、主诉、症状、病程、变化趋势、诱因、既往史、过敏史、当前用药；资料不足时须明确列出缺失项并要求补充，不得臆测。",
             "keywords": ["问诊", "病史", "要素", "九要素", "资料", "补充", "缺失"]},
            {"title": "照片脱敏要求",
             "content": "皮肤照片须去标识化（去除面部、纹身、饰品等可识别特征），或使用合成照片；记录拍摄部位与日期。支持合成照片描述信息随病例结构化提交。",
             "keywords": ["照片", "脱敏", "合成", "面部", "标识"]},
            {"title": "严重程度分级原则",
             "content": "严重程度应结合皮损范围、症状强度、功能影响与红旗综合评估；本原型给出的分级仅供医生参考，最终由医生判定。",
             "keywords": ["严重", "程度", "分级", "面积", "评估"]},
        ],
    },
]

DEMO_CASES = {
    "1": {"label": "资料充分的常见病例", "triggers": ["案例1", "case1", "湿疹样皮疹", "湿疹样"],
          "case": {"body_region": "双手背及肘窝（双侧对称）", "chief_complaint": "反复红斑瘙痒3周，加重3天",
                   "symptoms": "对称分布红斑、丘疹，干燥脱屑，夜间瘙痒明显，无渗出", "duration": "3周（近3天加重）",
                   "trend": "近3天皮损范围扩大、瘙痒加重", "triggers": "接触洗涤剂后加重；冬季干燥",
                   "past_history": "特应性体质，季节性过敏性鼻炎", "allergies": "否认药物、食物过敏史",
                   "current_medications": "外用保湿霜（每日2次）",
                   "photo_note": "合成皮肤照片（脱敏，非真实患者）：双手背对称红斑脱屑"}},
    "2": {"label": "红旗病例", "triggers": ["案例2", "case2", "带状疱疹", "带状疱疹红旗"],
          "case": {"body_region": "左侧胸背部（单侧）", "chief_complaint": "左侧胸背灼痛伴成簇水疱2天",
                   "symptoms": "单侧簇集性水疱，沿肋间神经走向呈带状分布，灼痛、刺痛，局部皮肤触痛敏感",
                   "duration": "2天（疼痛先于皮疹1天出现）", "trend": "水疱数量渐增，疼痛持续",
                   "triggers": "近1周劳累、睡眠差", "past_history": "高血压病史10年，规律服药控制",
                   "allergies": "青霉素过敏（皮试阳性史）", "current_medications": "口服对乙酰氨基酚（自行镇痛）",
                   "photo_note": "合成皮肤照片（脱敏，非真实患者）：左侧胸背单侧簇集水疱呈带状分布"}},
    "3": {"label": "信息不足病例", "triggers": ["案例3", "case3", "资料不足"],
          "case": {"body_region": "面部", "chief_complaint": "面部红斑脱屑",
                   "symptoms": "面部片状红斑伴细小脱屑，轻度瘙痒", "duration": "", "trend": "", "triggers": "",
                   "past_history": "", "allergies": "", "current_medications": "",
                   "photo_note": "合成皮肤照片（脱敏，非真实患者）：面部红斑脱屑"}},
    "4": {"label": "无可靠证据病例", "triggers": ["案例4", "case4", "无答案"],
          "case": {"body_region": "右手拇指（甲床）", "chief_complaint": "甲床纵行黑线伴甲板纵裂6个月",
                   "symptoms": "右手拇指甲床纵行黑线，宽约3毫米，伴甲板纵裂，无疼痛，无甲周色素沉着",
                   "duration": "6个月（缓慢增宽）", "trend": "黑线逐渐增宽，甲板纵裂同前",
                   "triggers": "无明确诱因，无外伤史", "past_history": "体健",
                   "allergies": "否认药物食物过敏史", "current_medications": "未使用药物",
                   "photo_note": "合成皮肤照片（脱敏，非真实患者）：右手拇指甲床纵行黑线"}},
}

REQUIRED_FIELDS = [
    ("body_region", "身体区域", ["身体区域", "部位", "皮损部位"]),
    ("chief_complaint", "主诉", ["主诉"]),
    ("symptoms", "症状", ["症状", "皮损表现", "表现"]),
    ("duration", "病程", ["病程", "持续时间", "病了多久"]),
    ("triggers", "诱因", ["诱因", "诱发因素"]),
    ("past_history", "既往史", ["既往史", "病史"]),
    ("allergies", "过敏史", ["过敏史", "过敏"]),
    ("current_medications", "当前用药", ["当前用药", "用药", "药物"]),
]
CASE_FIELDS = [k for k, _, _ in REQUIRED_FIELDS] + ["trend", "photo_note"]
_FIELD_ALIASES = {}
for _k, _l, _as in REQUIRED_FIELDS:
    _FIELD_ALIASES[_l] = _k
    for _a in _as:
        _FIELD_ALIASES[_a] = _k
_FIELD_ALIASES.update({"变化趋势": "trend", "变化": "trend", "趋势": "trend",
                       "照片说明": "photo_note", "皮损特征": "symptoms"})   # v68 图片特征勾选提交
_NEG_PREFIX = re.compile(r"^补充\s*[:：]?\s*")
_NEGATION_RE = re.compile(r"无|未|不|没|否认|未见")

ESCALATION_ACTIONS = {
    "RF-ANG-001": "立即评估气道与生命体征，紧急处置",
    "RF-SC-001": "立即评估表皮剥脱范围与黏膜受累，考虑紧急转诊",
    "RF-INF-001": "评估感染范围与生命体征，考虑系统抗感染治疗（由医生决定）",
}
STATUS_LABEL = {"present": "有", "absent": "无（医生确认）",
                "unknown": "未知（不得当作「没有」）", "not_asked": "未询问（不得当作「没有」）"}


def is_filled(value) -> bool:
    return bool(value and str(value).strip())


def match_demo_case(text: str):
    t = (text or "").strip().lower()
    if not t:
        return None
    for cid, demo in DEMO_CASES.items():
        for trig in demo["triggers"]:
            if t == trig.lower() or trig.lower() in t:
                return cid
    return None


def extract_fields(text: str):
    """「字段：值」抽取（兼容「补充：病程5天」嵌套与「变化趋势逐渐扩大」无冒号形态）。"""
    pairs, leftovers = [], []
    for seg in re.split(r"[;；\n。！？]", text or ""):
        seg = seg.strip()
        if not seg:
            continue
        stripped = _NEG_PREFIX.sub("", seg).strip()
        if not stripped:
            continue
        pair = None
        m = re.match(r"^([^:：]{1,8})[:：](.+)$", stripped)
        if m and m.group(1).strip() in _FIELD_ALIASES:
            pair = (_FIELD_ALIASES[m.group(1).strip()], m.group(2).strip())
        if pair is None:
            head = m.group(2).strip() if m else stripped
            for alias, key in sorted(_FIELD_ALIASES.items(), key=lambda kv: -len(kv[0])):
                if head.startswith(alias):
                    value = head[len(alias):].lstrip(" :：").strip()
                    if value:
                        pair = (key, value)
                        break
        if pair:
            pairs.append(pair)
        else:
            leftovers.append(seg)
    return pairs, leftovers


def build_case_from_text(text: str) -> dict:
    case = {k: "" for k in CASE_FIELDS}
    pairs, leftovers = extract_fields(text)
    for key, value in pairs:
        case[key] = value
    rest = "；".join(leftovers).strip()
    if not case["symptoms"] and rest:
        case["symptoms"] = rest[:300]
    return case


def merge_reply(case: dict, reply: str) -> dict:
    merged = dict(case)
    pairs, leftovers = extract_fields(reply)
    for key, value in pairs:
        merged[key] = value
    rest = _NEG_PREFIX.sub("", "；".join(leftovers)).strip()
    if rest:
        merged["symptoms"] = (f"{merged['symptoms']}；补充：{rest[:200]}"
                              if merged.get("symptoms") else rest[:200])
    return merged


def compute_missing(case) -> list:
    if case is None:
        return [label for _, label, _ in REQUIRED_FIELDS]
    return [label for key, label, _ in REQUIRED_FIELDS if not is_filled(case.get(key))]


def derive_data_completeness(case, missing) -> str:
    n = len(missing)
    if n == 0:
        return "complete"
    if case is None or n >= len(REQUIRED_FIELDS):
        return "insufficient"
    return "insufficient" if n > len(REQUIRED_FIELDS) / 2 else "partial"


def detect_red_flags(case) -> list:
    if not case:
        return []
    text = "；".join(str(case.get(k, "") or "") for k in ("body_region", "chief_complaint", "symptoms", "trend"))
    flags = []

    def has(p):
        return re.search(p, text) is not None

    if has("水疱|水泡") and has("单侧|一侧|神经|皮节|簇集|带状|肋间") and has("灼痛|刺痛|神经痛|触痛|疼痛|敏感"):
        flags.append("RF-HZ-001：单侧神经分布簇集水疱伴神经痛——带状疱疹可能，升级人工审核（知识库 DOC-HZ-001 红旗清单）")
    if has("眼周|眼睑|眼部|鼻尖") and has("水疱|水泡|红肿|疼痛|受累|皮疹"):
        flags.append("RF-HZ-002：眼周/鼻尖受累——眼部带状疱疹风险待排，升级人工审核")
    if has("发热|发烧") and has("脓疱|糜烂|渗出|红肿|扩散|疼痛加剧|蜂窝织"):
        flags.append("RF-INF-001：发热伴感染征象——继发感染待排，升级人工审核")
    if has("紫癜|瘀点|瘀斑"):
        flags.append("RF-VAS-001：紫癜/瘀点瘀斑——血管炎或血液系统疾病待排，升级人工审核")
    if has("呼吸困难|吞咽困难|喉头水肿|声音嘶哑"):
        flags.append("RF-ANG-001：呼吸道/吞咽受累征象——血管性水肿风险，升级人工审核")
    if has("大疱|表皮剥脱|尼氏征|剥脱性皮炎|全身红斑|弥漫红肿"):
        flags.append("RF-SC-001：重症药疹/大疱性疾病征象待排（SJS/TEN 等），升级人工审核")
    return list(dict.fromkeys(flags))


def derive_severity_level(red_flags, missing) -> str:
    if not red_flags:
        return "unknown" if missing else "routine"
    codes = [f.split("：")[0] for f in red_flags]
    if "RF-ANG-001" in codes:
        return "emergency"
    if "RF-INF-001" in codes or "RF-SC-001" in codes:
        return "urgent"
    return "needs_review"


def _keyword_hit(text: str, keyword: str) -> bool:
    start = 0
    while True:
        idx = text.find(keyword, start)
        if idx == -1:
            return False
        if not _NEGATION_RE.search(text[max(0, idx - 4): idx]):
            return True
        start = idx + len(keyword)


def retrieve_citations(case, max_docs=3, max_sections=2) -> list:
    """检索引用快照（否定词感知），v1.1 snake_case 字段。"""
    if not case:
        return []
    text = "；".join(str(case.get(k, "") or "") for k in
                    ("body_region", "chief_complaint", "symptoms", "trend", "triggers", "past_history", "current_medications"))
    scored = []
    for doc in KB:
        hits = []
        for idx, section in enumerate(doc["sections"]):
            score = sum(1 for kw in section["keywords"] if _keyword_hit(text, kw))
            if score > 0:
                hits.append((score, idx, section))
        hits.sort(key=lambda h: -h[0])
        hits = hits[:max_sections]
        if sum(h[0] for h in hits) > 0:
            scored.append((sum(h[0] for h in hits), doc, hits))
    scored.sort(key=lambda s: -s[0])
    out = []
    for _total, doc, hits in scored[:max_docs]:
        for _score, idx, section in hits:
            out.append({
                "citation_id": f"CIT-{doc['docId'].replace('DOC-', '')}-S{idx + 1}",
                "document_title": doc["docName"],
                "document_version": doc["version"],
                "source_type": doc["sourceType"],
                "page_or_section": section["title"],
                "quoted_text": section["content"],
            })
    return out


def build_symptom_assessment(case, citations) -> list:
    if not case:
        return []
    cite_ids = [c["citation_id"] for c in citations[:1]]
    items = []

    def add(symptom, filled, basis):
        items.append({"symptom": symptom,
                      "status": "present" if filled else "not_asked",
                      "basis": basis if filled else "inference",
                      "citation_ids": cite_ids if filled else []})

    for key, label, _ in REQUIRED_FIELDS:
        add(f"{label}：{case.get(key) or '未询问'}", is_filled(case.get(key)), "patient_report")
    if is_filled(case.get("trend")):
        add(f"变化趋势：{case['trend']}", True, "patient_report")
    if is_filled(case.get("photo_note")):
        items.append({"symptom": f"照片（脱敏/合成）：{case['photo_note']}", "status": "present",
                      "basis": "image_description", "citation_ids": []})
    return items


def build_medication_checks(case, citations) -> list:
    if not case or not is_filled(case.get("current_medications")):
        return []
    cite_ids = [c["citation_id"] for c in citations[:2]]
    names = [n.strip() for n in re.split(r"[、，,;；/]", case["current_medications"]) if n.strip()]
    names = [n for n in names if not re.fullmatch(r"无|未使用(药物)?|未用药|否认", n)][:4]
    titles = {c["document_title"] for c in citations}
    notes = [n for d in KB if d["docName"] in titles for n in d["medReviewNotes"]]
    return [{"generic_name": name,
             "purpose": notes[0] if notes else "医生核对药物与病情的相关性（禁止自动处方、剂量与疗程）",
             "items_to_verify": ["indication", "allergy", "contraindication", "interaction"],
             "requires_doctor_confirmation": True,
             "citation_ids": cite_ids} for name in names]


def follow_up_questions(missing: list) -> list:
    return [{"question": f"请补充：{m}", "display_priority": "blocking", "action_type": "ask_user"}
            for m in missing]


def build_draft(case, citations, red_flags, missing, generated_by="rule-fallback",
                model_version="rule-engine@demo", review_note=None, llm_out=None) -> dict:
    """构造 v1.1 草稿。workflow_state 状态机：escalated > blocked > awaiting_review。"""
    if llm_out:
        return normalize_llm_draft(llm_out, citations, red_flags, missing, model_version, generated_by)
    allowed = {c["citation_id"] for c in citations}
    doc_titles = {c["document_title"] for c in citations}
    dx_seen, suspected = set(), []
    for doc in KB:
        if doc["docName"] not in doc_titles:
            continue
        for dx in doc["suggestedDx"]:
            if dx not in dx_seen:
                dx_seen.add(dx)
                cite = next((c for c in citations if c["document_title"] == doc["docName"]), None)
                suspected.append({"name": dx, "confidence": "low",
                                  "supporting_evidence": [{"statement": f"知识库匹配：{cite['page_or_section']}" if cite else "知识库匹配",
                                                            "citation_ids": [cite["citation_id"]] if cite else []}],
                                  "missing_information": list(missing), "red_flags": list(red_flags)})
    uncertainties = ["本草稿由规则引擎生成（合成演示模式，未经模型生成）"]
    if not citations:
        uncertainties.append("知识库未覆盖本病例主题，结论缺乏已审核证据支撑，不足以判断，请医生自行评估")
    uncertainties.append(f"证据来源：{KB_LABEL}")
    action = "未触发红旗，按常规医生审核流程处理"
    if red_flags:
        code = red_flags[0].split("：")[0]
        action = ESCALATION_ACTIONS.get(code, "升级人工审核：由主诊医生复核后再继续")
    severity_level = derive_severity_level(red_flags, missing)
    next_action = ("红旗升级人工审核：由主诊医生复核评估后再决定处理" if red_flags else
                   "知识库未覆盖，建议皮肤科门诊面诊评估（本草稿不足以判断）" if not citations else
                   "结合引文核对鉴别诊断与用药，由医生决定下一步")
    urgency = "emergency" if any(f.startswith("RF-ANG") for f in red_flags) else ("soon" if red_flags else "routine")
    draft = {
        "schema_version": SCHEMA_VERSION,
        "workflow_state": "escalated" if red_flags else "awaiting_review",
        "blocking_reason": (f"命中红旗 {red_flags[0].split('：')[0]}，普通分析流程被阻断，须医生人工审核" if red_flags else None),
        "disclaimer": DISCLAIMER,
        "case_summary": {
            "selected_body_region": (case or {}).get("body_region", ""),
            "symptom_summary": (f"{case.get('chief_complaint') or '主诉未提供'}；症状：{case.get('symptoms') or '未提供'}；"
                                f"病程：{case.get('duration') or '未提供'}" if case else "病例信息缺失"),
            "data_completeness": derive_data_completeness(case, missing),
        },
        "follow_up_questions": follow_up_questions(missing),
        "suspected_conditions": suspected,
        "symptom_assessment": build_symptom_assessment(case, citations),
        "medication_checks": build_medication_checks(case, citations),
        "medication_recommendations": [],
        "treatment_reference": [],
        "severity": {"level": severity_level,
                     "basis": ["命中红旗规则，需人工审核" if red_flags else "基于症状与病程的初步评估（仅供医生参考）"]
                              + ([f"资料不全（缺失 {len(missing)} 项）"] if missing else []),
                     "citation_ids": [c["citation_id"] for c in citations[:1]]},
        "red_flag_escalation": {"triggered": bool(red_flags), "reasons": list(red_flags),
                                "recommended_action": action, "is_blocking": bool(red_flags)},
        "recommended_next_steps": [{"action": next_action, "urgency": urgency,
                                    "citation_ids": [c["citation_id"] for c in citations[:1]]}],
        "uncertainties": uncertainties,
        "citations": [c for c in citations if c["citation_id"] in allowed],
        "model_version": model_version,
        "knowledge_snapshot": KB_SNAPSHOT,
        "safety_policy_version": SAFETY_POLICY_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "review_status": "pending",
        "requires_doctor_confirmation": True,
        "generated_by": generated_by,
    }
    if review_note:
        draft["review_note"] = review_note
    return draft


def normalize_llm_draft(parsed, citations, red_flags, missing, model_version, generated_by) -> dict:
    """LLM 输出归一化（§6.4）：枚举收拢、引用白名单、权威字段本地覆盖。"""
    allowed = {c["citation_id"] for c in citations}
    dropped = 0

    def s(v, fb=""):
        return v.strip() if isinstance(v, str) and v.strip() else fb

    def ids(v):
        nonlocal dropped
        raw = [str(i) for i in (v or []) if str(i).strip()]
        out = [i for i in raw if i in allowed]
        dropped += len(raw) - len(out)
        return out

    def arr(v):
        return [str(x) for x in (v or []) if str(x).strip()]

    conditions = []
    for c in (parsed.get("suspected_conditions") or parsed.get("suspectedConditions") or [])[:6]:
        if not isinstance(c, dict):
            continue
        evs = []
        for e in (c.get("supporting_evidence") or c.get("supportingEvidence") or [])[:8]:
            if isinstance(e, dict):
                evs.append({"statement": s(e.get("statement")), "citation_ids": ids(e.get("citation_ids") or e.get("citationIds"))})
        conditions.append({
            "rank": len(conditions) + 1,
            "name": s(c.get("name"), "待排（未命名）"),
            "confidence": c.get("confidence") if c.get("confidence") in ("low", "medium", "high") else "unknown",
            "supporting_evidence": evs,
            "missing_information": arr(c.get("missing_information") or c.get("missingInformation")),
            "red_flags": arr(c.get("red_flags") or c.get("redFlags"))})
    assessment = []
    for it in (parsed.get("symptom_assessment") or parsed.get("symptomAssessment") or [])[:12]:
        if not isinstance(it, dict):
            continue
        assessment.append({"symptom": s(it.get("symptom")),
                           "status": it.get("status") if it.get("status") in ("present", "absent", "not_asked") else "unknown",
                           "basis": it.get("basis") if it.get("basis") in ("patient_report", "doctor_observation",
                                                                            "image_description", "test_result") else "inference",
                           "citation_ids": ids(it.get("citation_ids") or it.get("citationIds"))})
    meds = []
    for m in (parsed.get("medication_checks") or parsed.get("medicationChecks") or [])[:6]:
        if not isinstance(m, dict):
            continue
        meds.append({"generic_name": s(m.get("generic_name") or m.get("genericName")),
                     "purpose": s(m.get("purpose"), "医生核对药物与病情的相关性"),
                     "items_to_verify": arr(m.get("items_to_verify") or m.get("itemsToVerify")) or
                                        ["indication", "allergy", "contraindication", "interaction"],
                     "requires_doctor_confirmation": True,
                     "citation_ids": ids(m.get("citation_ids") or m.get("citationIds"))})
    next_steps = []
    for n in (parsed.get("recommended_next_steps") or parsed.get("recommendedNextSteps") or [])[:6]:
        if not isinstance(n, dict):
            continue
        next_steps.append({"action": s(n.get("action")),
                           "urgency": n.get("urgency") if n.get("urgency") in ("routine", "soon", "urgent", "emergency") else "routine",
                           "citation_ids": ids(n.get("citation_ids") or n.get("citationIds"))})
    exams = []
    for e in (parsed.get("recommended_exams") or parsed.get("recommendedExams") or [])[:6]:
        if not isinstance(e, dict):
            continue
        exams.append({"exam": s(e.get("exam")), "reason": s(e.get("reason")),
                      "citation_ids": ids(e.get("citation_ids") or e.get("citationIds"))})
    tref = []
    for t in (parsed.get("treatment_reference") or parsed.get("treatmentReference") or [])[:6]:
        if not isinstance(t, dict):
            continue
        tref.append({"approach": s(t.get("approach")),
                     "indication_or_goal": s(t.get("indication_or_goal") or t.get("indicationOrGoal")),
                     "basis": s(t.get("basis")),
                     "when_to_refer": s(t.get("when_to_refer") or t.get("whenToRefer")),
                     "citation_ids": ids(t.get("citation_ids") or t.get("citationIds"))})
    med_recs = []
    for m in (parsed.get("medication_recommendations") or parsed.get("medicationRecommendations") or [])[:6]:
        if not isinstance(m, dict):
            continue
        # 只保留参考时长；明确丢弃剂量/频次/起停指令，且强制医生确认。
        med_recs.append({"generic_name": s(m.get("generic_name") or m.get("genericName")),
                         "purpose": s(m.get("purpose")),
                         "reference_duration": s(m.get("reference_duration") or m.get("referenceDuration")),
                         "usage_notes": s(m.get("usage_notes") or m.get("usageNotes")),
                         "contraindications_or_checks": arr(m.get("contraindications_or_checks") or m.get("contraindicationsOrChecks")),
                         "requires_doctor_confirmation": True,
                         "citation_ids": ids(m.get("citation_ids") or m.get("citationIds"))})
    rp_raw = parsed.get("recovery_period") or parsed.get("recoveryPeriod")
    recovery = {}
    if isinstance(rp_raw, dict):
        recovery = {"typical_course": s(rp_raw.get("typical_course") or rp_raw.get("typicalCourse")),
                    "influence_factors": arr(rp_raw.get("influence_factors") or rp_raw.get("influenceFactors")),
                    "citation_ids": ids(rp_raw.get("citation_ids") or rp_raw.get("citationIds"))}
    raw_citations = [c for c in (parsed.get("citations") or []) if isinstance(c, dict)]
    dropped += len([c for c in raw_citations if c.get("citation_id") not in allowed and c.get("citationId") not in allowed])
    kept_citations = list(citations)  # 引用发布恒为本次检索快照

    uncertainties = arr(parsed.get("uncertainties"))
    if dropped > 0:
        uncertainties.append(f"检测到 {dropped} 条非知识库引用，已按「禁止编造引用」约束剔除")
    if not citations:
        uncertainties.append("知识库未覆盖本病例主题，结论缺乏已审核证据支撑，不足以判断，请医生自行评估")
    uncertainties.append(f"证据来源：{KB_LABEL}")
    severity_basis = arr((parsed.get("severity") or {}).get("basis")) or \
        (["命中红旗规则，需人工审核"] if red_flags else ["基于症状与病程的初步评估（仅供医生参考）"])
    wf = "escalated" if red_flags else "awaiting_review"

    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_state": wf,
        "blocking_reason": (f"命中红旗 {red_flags[0].split('：')[0]}，普通分析流程被阻断，须医生人工审核" if red_flags else None),
        "disclaimer": s(parsed.get("disclaimer"), DISCLAIMER) or DISCLAIMER,
        "case_summary": {"selected_body_region": s((parsed.get("case_summary") or parsed.get("caseSummary") or {}).get("selected_body_region") or (parsed.get("caseSummary") or {}).get("selectedBodyRegion")),
                          "symptom_summary": s((parsed.get("case_summary") or parsed.get("caseSummary") or {}).get("symptom_summary") or (parsed.get("caseSummary") or {}).get("symptomSummary")),
                          "data_completeness": derive_data_completeness(None, missing) if missing else "complete"},
        "follow_up_questions": follow_up_questions(missing),
        "suspected_conditions": conditions,
        "symptom_assessment": assessment,
        "medication_checks": meds,
        "medication_recommendations": med_recs,
        "severity": {"level": derive_severity_level(red_flags, missing),
                     "basis": severity_basis,
                     "citation_ids": ids((parsed.get("severity") or {}).get("citation_ids") or (parsed.get("severity") or {}).get("citationIds"))},
        "red_flag_escalation": {"triggered": bool(red_flags), "reasons": list(red_flags),
                                "recommended_action": ESCALATION_ACTIONS.get(red_flags[0].split("：")[0] if red_flags else "",
                                                                              "升级人工审核：由主诊医生复核后再继续") if red_flags
                                else "未触发红旗，按常规医生审核流程处理",
                                "is_blocking": bool(red_flags)},
        "recommended_next_steps": next_steps,
        "recommended_exams": exams,
        "treatment_reference": tref,
        "recovery_period": recovery,
        "uncertainties": list(dict.fromkeys(uncertainties)),
        "citations": kept_citations,
        "model_version": model_version,
        "knowledge_snapshot": KB_SNAPSHOT,
        "safety_policy_version": SAFETY_POLICY_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "review_status": "pending",
        "requires_doctor_confirmation": True,
        "generated_by": generated_by,
    }


def classify_review(feedback: str) -> str:
    fb = (feedback or "").strip()
    if re.match(r"^(拒绝|否决|驳回|reject)", fb, re.IGNORECASE):
        return "rejected"
    if re.match(r"^(ok|okay|确认|确定|同意|通过|批准|confirmed?|approved?|yes|好的?|lgtm)([\s!！。.~，,].*)?$",
                fb, re.IGNORECASE):
        return "confirmed"
    return "modified"


def build_review_question(draft, red_flags) -> str:
    """审核问题（含 workflow_state 与阻断说明）。"""
    import json
    banners = []
    if red_flags:
        banners.append(f"🚩 **红旗警示（workflow_state={draft.get('workflow_state')}，阻断普通分析）**")
    if draft.get("generated_by") == "llm-invalid":
        banners.append("⚠️ **本草稿未通过系统校验**，仅作降级演示，不得当作正常医疗建议。")
    if not draft.get("citations"):
        banners.append("ℹ️ **知识库未覆盖本病例主题**：不给出鉴别诊断（不足以判断），不会编造引用。")
    lines = banners + ["", "📄 **待审核草稿**", "", "```json", json.dumps(draft, ensure_ascii=False, indent=2), "```", "",
                       "**症状评估（unknown/未询问 ≠ 没有）**："]
    lines += [f"- {s['symptom']}｜状态：{STATUS_LABEL[s['status']]}｜依据：{s['basis']}"
              for s in draft.get("symptom_assessment", [])] or ["- 无"]
    lines += ["", "**医生审核操作**：回复「确认」/「修改：意见」/「拒绝：理由」。"]
    return "\n".join(lines)


def render_report(draft) -> str:
    if not draft:
        return "未找到已确认的草稿，无法生成报告。"
    dx = "\n".join(f"- {i}. {c['name']}（置信度：{c['confidence']}）："
                   f"{'；'.join(e['statement'] for e in c['supporting_evidence']) or '无证据条目'}"
                   for i, c in enumerate(draft.get("suspected_conditions", []), 1)) or "- 知识库未覆盖，不足以判断"
    assess = "\n".join(f"- {s['symptom']}｜状态：{STATUS_LABEL[s['status']]}｜依据：{s['basis']}"
                       for s in draft.get("symptom_assessment", [])) or "- 无"
    tref = "\n".join(f"- {t.get('approach') or '治疗方向待核实'}：{t.get('indication_or_goal') or '目标待核实'}"
                     f"（依据：{t.get('basis') or '待核实'}；转诊：{t.get('when_to_refer') or '按医生判断'}）"
                     for t in draft.get("treatment_reference", [])) or "- 无治疗方向参考"
    med_recs = "\n".join(f"- {m.get('generic_name') or '药物名称待核实'}：{m.get('purpose') or '用途待核实'}；"
                          f"参考使用时长：{m.get('reference_duration') or '待核实'}；"
                          f"核对：{'、'.join(m.get('contraindications_or_checks') or []) or '过敏、禁忌、相互作用'}；需医生确认"
                          for m in draft.get("medication_recommendations", [])) or "- 无用药参考"
    meds = "\n".join(f"- {m['generic_name']}：核对项【{'、'.join(m['items_to_verify'])}】（需医生确认）"
                     for m in draft.get("medication_checks", [])) or "- 无用药信息"
    next_steps = "\n".join(f"- {n['action']}（紧急度：{n['urgency']}）"
                           for n in draft.get("recommended_next_steps", [])) or "- 无"
    cites = "\n".join(f"- {c['citation_id']}｜{c['document_title']}（{c['document_version']}，{c['source_type']}）"
                      f"§{c['page_or_section']}：「{c['quoted_text']}」"
                      for c in draft.get("citations", [])) or "- 知识库未覆盖"
    red = draft.get("red_flag_escalation", {})
    cs = draft.get("case_summary", {})
    missing = draft.get("suspected_conditions", [{}])[0].get("missing_information", []) if draft.get("suspected_conditions") else []
    return "\n".join([
        "# 肤智（DermAgent）辅助报告（医生已确认）",
        "",
        f"> Schema {draft['schema_version']}｜工作流状态 {draft['workflow_state']}｜模型 {draft['model_version']}"
        f"｜知识库快照 {draft['knowledge_snapshot']}｜安全策略 {draft['safety_policy_version']}"
        f"｜生成时间 {draft['generated_at']}",
        "",
        "## 一、病例摘要",
        f"{cs.get('selected_body_region') or '（部位未记录）'}：{cs.get('symptom_summary')}"
        f"（资料完备度：{cs.get('data_completeness')}）",
        "",
        "## 二、疑似疾病排序（仅供医生核对，非确诊）", dx, "",
        "## 三、症状依据", assess, "",
        "## 四、治疗方向（非处方，待医生核定）", tref, "",
        "## 五、用药参考（药名与参考时长均须医生确认，不得直接执行）", med_recs, "",
        "## 六、用药安全核对", meds, "",
        f"## 七、严重程度（{draft['severity']['level']}）",
        "\n".join(f"- {b}" for b in draft["severity"]["basis"]) or "- 无", "",
        "## 六、红旗与升级",
        ("\n".join(f"- {r}" for r in red.get("reasons", [])) + f"\n- 建议动作：{red.get('recommended_action', '')}"
         if red.get("triggered") else "- 未触发红旗"),
        "",
        "## 七、缺失信息", "\n".join(f"- {m}" for m in missing) or "- 无", "",
        "## 八、建议下一步（由医生决定）", next_steps, "",
        "## 九、不确定性", "\n".join(f"- {u}" for u in draft.get("uncertainties", [])), "",
        "## 十、引用", cites, "",
        "---", "## 十一、待签署声明（SIGNATURE_NOTE）", SIGNATURE_NOTE,
        "---", "## 免责声明", draft["disclaimer"],
    ])
