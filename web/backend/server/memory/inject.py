# -*- coding: utf-8 -*-
"""提示词片段组装：患者档案 / 情景记忆的渲染与硬预算裁剪（计划 §6.1/§6.2）。"""

CATEGORY_LABELS = {
    "allergy": "过敏史",
    "past_history": "既往史",
    "personal_history": "个人史",
    "medication": "用药",
    "family_history": "家族史",
    "diagnosis_history": "诊断史",
    "lab": "检验",
}

FIELD_LABELS = {
    "allergen": "过敏原", "reaction": "反应", "severity": "严重度",
    "disease": "疾病", "year": "年份", "status": "状态",
    "smoking": "吸烟", "alcohol": "饮酒", "occupation": "职业", "travel": "旅行",
    "name": "名称", "dose": "剂量", "frequency": "频次", "since": "起始", "indication": "指征",
    "relation": "关系", "diagnosis": "诊断", "certainty": "确定性", "date": "日期",
    "test": "检验项目", "value": "结果", "unit": "单位",
}

SOURCE_LABELS = {
    "doctor_modified": "医生修改",
    "doctor_input": "医生输入",
    "case_import": "病例导入",
    "llm_extract": "自动抽取，需医生核对",
}


_PROFILE_HEADER = "【患者档案（跨就诊慢病信息，仅供参考，当前病例以本轮资料为准）】\n"
_PROFILE_MARKER = "- ……（患者档案超出预算已截断）"
_EPISODIC_HEADER = "【相关历史话题（模糊召回，仅供参考，不改变当前病例事实）】\n"
_EPISODIC_MARKER = "- ……（情景记忆超出预算已截断）"


def clip(text: str, limit: int) -> str:
    return str(text or "")[:max(0, int(limit))]


def _render_block(header: str, marker: str, lines: list, limit: int) -> str:
    """总长（含标题与截断标记）不超过 limit：调用方不得再二次截断。

    先为标记预留空间，保证真被裁时「已截断」一定出现，不出现静默截断。
    """
    limit = max(0, int(limit))
    body = max(0, limit - len(header))
    room = max(0, body - len(marker) - 1)
    out, used = [], 0
    for line in lines:
        cost = len(line) + (1 if out else 0)
        if used + cost > room:
            if not out:
                # 单条就超预算：保留可容纳的前缀并明确标注，避免整条丢失
                keep = max(0, room - len(marker) - 1)
                if keep:
                    out.append(clip(line, keep))
            out.append(marker)
            return header + "\n".join(out)
        out.append(line)
        used += cost
    return header + "\n".join(out)


def render_profile(facts: list, limit: int = 800) -> str:
    """只渲染临床事实（身份字段在查询层就被排除）；超限从末尾裁并如实标注。"""
    if not facts:
        return ""
    order = {cat: i for i, cat in enumerate(CATEGORY_LABELS)}
    rows = sorted(facts, key=lambda f: (order.get(f.get("category"), 99), f.get("fact_id") or 0))
    lines = []
    for fact in rows:
        cat = CATEGORY_LABELS.get(str(fact.get("category") or ""), str(fact.get("category") or ""))
        field = FIELD_LABELS.get(str(fact.get("field") or ""), str(fact.get("field") or ""))
        src = SOURCE_LABELS.get(str(fact.get("source") or ""), "")
        line = f"- {cat}·{field}：{str(fact.get('value') or '').strip()}"
        if src:
            line += f"（{src}）"
        lines.append(line)
    return _render_block(_PROFILE_HEADER, _PROFILE_MARKER, lines, limit)


def render_episodic(hits: list, limit: int = 1200) -> str:
    if not hits:
        return ""
    lines = []
    for hit in hits:
        scope = "病例级" if str(hit.get("scope") or "") == "case" else "医生级"
        date = str(hit.get("date") or hit.get("created_at") or "")[:10]
        title = str(hit.get("title") or "").strip()[:60]
        text = str(hit.get("text") or "").strip()
        head = f"- [{scope}]" + (f" {date}" if date else "") + (f" {title}" if title else "")
        lines.append(head + "\n  " + text)
    return _render_block(_EPISODIC_HEADER, _EPISODIC_MARKER, lines, limit)
