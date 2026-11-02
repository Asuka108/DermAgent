# -*- coding: utf-8 -*-
"""意图识别规则层（零依赖，跨端单一真源）。

第一层：高精度关键词/正则规则。只处理「短、明确、无否定、单目标」的固定表达；
长句、否定、引号内文本、多目标任务一律返回 None，交给服务层第二/三层。

本模块同时提供：
- 状态先验（apply_state_prior）：第二层向量打分的上下文修正；
- 词法候选（lexical_candidates）：桌面 mock / 无向量模型时的离线兜底；
- 确认卡数据与选择解析（build_pending / resolve_pending_choice）：三端同口径。

服务层实现见 server/intent.py；TS 同构实现见 agent-langgraph/src/app/intent/。
"""
from __future__ import annotations

import json
import math
import os
import re
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timezone

_SPEC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intent_spec.json")
_SPEC = None


def spec() -> dict:
    global _SPEC
    if _SPEC is None:
        with open(_SPEC_PATH, "r", encoding="utf-8") as fh:
            _SPEC = json.load(fh)
    return _SPEC


def thresholds() -> dict:
    return dict(spec().get("thresholds") or {})


def intent_meta(intent: str) -> dict:
    return (spec().get("intents") or {}).get(intent) or {}


def intent_label(intent: str) -> str:
    return str(intent_meta(intent).get("label") or intent)


def intent_class(intent: str) -> str:
    return str(intent_meta(intent).get("class") or "business")


INTENT_LABELS = {k: str(v.get("label") or k) for k, v in (spec().get("intents") or {}).items()}
ALLOWED_INTENTS = tuple(INTENT_LABELS.keys())
MEDICAL_DOC_TASKS = {k for k, v in (spec().get("intents") or {}).items() if v.get("class") == "medical"}

# —— 第一层：业务任务关键词（顺序即优先级，与原 app.py TASK_PATTERNS 完全一致） ——
TASK_PATTERNS = [
    ("settings_control", ("修改设置", "更改设置", "改一下设置", "设置修改", "切换连接", "激活连接",
                          "换连接", "换线路", "切换模型", "换模型", "模型改成", "模型设为", "模型换成",
                          "思考改成", "思考调", "思考等级",
                          "语言改成", "语言设为", "语言换成", "语言切换", "切换语言", "切换到英文",
                          "切换到中文", "改成英文", "改成中文", "设为英文", "设为中文", "界面语言")),
    ("context_compress", ("压缩上下文", "压缩记忆", "压缩对话", "整理上下文", "上下文压缩")),
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

# —— 第一层：对话控制指令（顺序敏感） ——
AGENT_COMMANDS = {
    "skip_3d": ("跳过3D", "跳过3d", "不用3D", "不用3d", "不使用3D", "不使用3d",
                "不用人体模型", "不用模型选点", "先不选部位"),
    "add_image": ("补充图片", "添加图片", "补充一张图片", "添加一张图片",
                  "补充照片", "添加照片", "上传图片", "上传照片"),
    "reask": ("重新询问患者", "重新追问", "重新梳理缺失", "问我还缺什么", "还缺什么信息"),
    "undo_last": ("撤销刚才的信息", "撤销刚才补充", "撤销上一条信息", "撤销刚才那条"),
    "save_case": ("保存当前病例", "保存病例草稿", "保存当前草稿", "把病例保存", "先保存病例"),
    "back_to_chat": ("返回对话", "回到对话", "返回聊天", "回到聊天", "回到主对话", "回到对话框"),
    "show_sources": ("查看刚才的引用", "查看引用", "刚才的引用", "引用详情", "看引用", "查看文献引用"),
    "open_model": ("打开人体模型", "打开3D", "打开3d", "打开三维模型", "打开模型选点",
                   "人体模型选点", "去选部位", "打开人体图"),
    "open_analysis": ("打开分析结果", "查看分析结果", "查看完整分析", "打开结果窗口"),
}

GREET_EXACT = {"hi", "hello", "hey", "你好", "您好", "在吗", "在么", "嗨", "hello？", "hi？"}
SMALLTALK_KEYS = ("你是谁", "你能做什么", "你能干什么", "你叫什么", "介绍你能", "介绍下你",
                  "你会做什么", "你会什么", "你有什么功能", "有什么功能", "你能帮我做什么",
                  "怎么使用", "这是什么软件", "你负责什么")
CORE_CONFIRMS = ("确认", "确认执行", "同意", "没问题", "可以了", "开始吧", "执行吧",
                 "就这样", "好的", "可以开始", "继续执行", "ok", "go")
DENY_KEYS = ("不对", "不是", "重来", "重新来", "拒绝", "不要这个", "这个不对",
             "不是这个意思", "取消", "算了", "先不要", "停", "停止", "退出",
             "不用了", "不做了", "重新生成", "否")

# 否定判定必须覆盖「不需要分析图片」「不必做鉴别诊断」这类「否定词 + 情态/动词」结构：
# 只匹配紧贴关键词的否定词后缀会漏判。可选连词/情态组只吸收到关键词之间的虚词，
# 不跨标点；无法判定时宁可下放（第二/三层），绝不执行被否定的动作。
_NEGATION_RE = re.compile(
    r"(?:不|别|无需|无须|不必|不用|不要|不想|不打算|没必要|没有必要|没|没有|未|先不|暂不|暂时不)"
    r"(?:需要|需|要|用|必|想|打算|再|做|写|生成|进行|帮我|给我|去|来){0,2}\s*$")
_QUOTE_PAIRS = (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"))
_CONJUNCTION_RE = re.compile(r"(?:然后|再|顺便|同时|并且|以及|，并|。并)")
# 多目标判定：并列连词 +（≥2 个不同任务关键词 或 ≥2 个动作动词）。
# 后者覆盖「分析一下这张图，然后整理病历，再导出报告」这类只命中一个关键词、
# 但实际包含多个动作的句子——第一层不得只挑一个执行。
_ACTION_VERB_RE = re.compile(
    r"(?:分析|整理|导出|生成|写|查|检索|做|看看|看一下|评估|总结|描述|推荐|建议|对比|比较|"
    r"打开|保存|撤销|切换|记录|列出|给出|压缩|脱敏)")
_SEQ_VERB_RE = re.compile(
    r"^(?:请|帮我|麻烦|给我|我要|我想|需要)?\s*"
    r"(?:把|写|整理|改成|改写|重写|导出|生成|查|检索|分析|对比|做|打开|保存|撤销|压缩|切换|记录|列出|描述|总结|给出|推荐|建议)"
)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    return t.rstrip("。，！？!?~～…、；;：: ")


def is_confirm(text: str) -> bool:
    return normalize(text) in CORE_CONFIRMS


def is_smalltalk(text: str) -> bool:
    t = normalize(text)
    if not t:
        return False
    if t in GREET_EXACT:
        return True
    return any(k in t for k in SMALLTALK_KEYS)


def _quoted_spans(text: str) -> list:
    spans = []
    for left, right in _QUOTE_PAIRS:
        start = 0
        while True:
            i = text.find(left, start)
            if i < 0:
                break
            j = text.find(right, i + len(left))
            if j < 0:
                break
            spans.append((i, j + len(right)))
            start = j + len(right)
    return spans


def _negated(text: str, idx: int) -> bool:
    return bool(_NEGATION_RE.search(text[max(0, idx - 6):idx]))


def _quoted(text: str, idx: int, spans: list) -> bool:
    return any(a <= idx < b for a, b in spans)


def _keyword_ok(text: str, key: str, spans: list) -> bool:
    start = 0
    k = key.lower()
    while True:
        idx = text.find(k, start)
        if idx < 0:
            return False
        if not _negated(text, idx) and not _quoted(text, idx, spans):
            return True
        start = idx + len(k)


def _collect_matches(text: str):
    spans = _quoted_spans(text)
    out = []
    for intent, keys in TASK_PATTERNS:
        hit = next((k for k in keys if _keyword_ok(text, k, spans)), None)
        if hit:
            out.append((intent, hit))
    return out, spans


def guarded_intents(text: str) -> set:
    """关键词出现了、但全部是否定/引号包裹的意图。

    第一层会因此下放；第二层不得再用向量相似度把它们「捞回来」直接执行
    （「不需要分析图片」与「分析图片」的句向量几乎相同）。
    """
    t = normalize(text)
    if not t:
        return set()
    spans = _quoted_spans(t)
    out = set()
    groups = list(TASK_PATTERNS) + list(AGENT_COMMANDS.items())
    for intent, keys in groups:
        if any(k.lower() in t for k in keys) and not any(_keyword_ok(t, k, spans) for k in keys):
            out.add(intent)
    return out


def is_multi_target(text: str) -> bool:
    """显式并列（然后/再/同时/顺便…）且包含多个目标或动作。"""
    t = normalize(text)
    if not t or not _CONJUNCTION_RE.search(t):
        return False
    matches, _ = _collect_matches(t)
    distinct = {i for i, _ in matches if i != "case_write"}
    if len(distinct) >= 2:
        return True
    return len(_ACTION_VERB_RE.findall(t)) >= 2


def classify_control(text: str) -> str:
    """控制指令只看「未被否定、未被引号包裹」的明确短语。

    「不要跳过3D」「别打开人体模型」「无需撤销刚才的信息」都是对控制动作的否定，
    绝不能按包含匹配执行成相反动作——这类输入下放第二/三层或反问医生。
    """
    t = normalize(text)
    if not t or len(t) > 60:
        return ""
    spans = _quoted_spans(t)
    for cmd, keys in AGENT_COMMANDS.items():
        if any(_keyword_ok(t, k, spans) for k in keys):
            return cmd
    return ""


def classify_rules(text: str, state: dict | None = None):
    """第一层：返回 {intent, confidence, layer, reason, slots, candidates} 或 None。

    高精度优先：任何不确定（长句、多目标、否定只剩空、引号内）都返回 None。
    """
    t = normalize(text)
    if not t:
        return None
    state = state or {}

    cmd = classify_control(t)
    if cmd:
        return {"intent": cmd, "confidence": 0.99, "layer": 1,
                "reason": f"control:{cmd}", "slots": {}, "candidates": []}

    wf = str(state.get("workflow_state") or "")
    has_draft = bool(state.get("has_draft") or state.get("draft"))
    if is_confirm(t) and (wf in ("planning", "awaiting_review", "awaiting_follow_up") or has_draft
                          or state.get("phase") == "planning"):
        return {"intent": "confirm", "confidence": 0.99, "layer": 1,
                "reason": "confirm", "slots": {}, "candidates": []}
    if t in DENY_KEYS and (wf in ("planning", "awaiting_review") or has_draft):
        return {"intent": "deny", "confidence": 0.99, "layer": 1,
                "reason": "deny", "slots": {}, "candidates": []}

    if is_smalltalk(t):
        return {"intent": "smalltalk", "confidence": 0.99, "layer": 1,
                "reason": "smalltalk", "slots": {}, "candidates": []}

    m = re.match(r"^对比\s*([0-9a-f]{6,12})$", t)
    if m:
        return {"intent": "case_compare", "confidence": 0.99, "layer": 1,
                "reason": "case_id", "slots": {"case_id": m.group(1)}, "candidates": []}

    matches, spans = _collect_matches(t)
    if not matches:
        return None

    # 长度守卫：长句只有在「句首动词短语」时才允许 L1 终局
    if len(t) > 40 and not _SEQ_VERB_RE.search(t):
        return None
    # 多目标守卫：显式并列且包含多个任务/动作 → 交第二层
    if is_multi_target(t):
        return None

    intent, key = matches[0]
    return {"intent": intent, "confidence": 0.99, "layer": 1,
            "reason": f"rule:{key}", "slots": {}, "candidates": []}


# —— 状态先验（第二层打分修正；mock 也可复用） ——

_REFER_RE = re.compile(r"(?:它|这个|那个|刚才|刚刚|再来|接着|继续|换成|改一下|再一份)")


def _case_empty(state: dict) -> bool:
    if "case_empty" in state:
        return bool(state.get("case_empty"))
    case = state.get("case")
    if not isinstance(case, dict):
        return False
    keys = ("chief_complaint", "symptoms", "duration", "trigger", "trend",
            "past_history", "allergy", "medication", "body_region")
    return not any(str(case.get(k) or "").strip() for k in keys)


def apply_state_prior(text: str, state: dict | None, scores: dict) -> dict:
    """对 [0,1] 的原型相似度做规则加减分，返回新字典。"""
    if not state or not scores:
        return dict(scores)
    out = dict(scores)
    phase = str(state.get("phase") or state.get("workflow_state") or "")
    prev = str(state.get("task_type") or "")
    has_draft = bool(state.get("has_draft") or state.get("draft"))
    empty = _case_empty(state)

    def add(intent, delta):
        if intent in out:
            out[intent] = out[intent] + delta

    if phase in ("case_intake", "case_supplement"):
        add("case_write", 0.06)
        add("case_edit", 0.03)
        add("settings_control", -0.05)
        for t in MEDICAL_DOC_TASKS:
            add(t, -0.05)
    if has_draft:
        add("complete_analysis", 0.05)
        add("report_export", 0.03)
        add("case_write", -0.03)
    if phase == "doc_ready":
        add("report_export", 0.05)
        add("case_edit", 0.03)
    if empty:
        add("case_write", 0.05)
        add("case_organizer", 0.05)
        for t in MEDICAL_DOC_TASKS:
            add(t, -0.05)
    last = str(state.get("last_assistant") or "")
    if last and len(normalize(text)) <= 12:
        add("case_write", 0.04)
        add("complete_analysis", 0.04)
    if prev and _REFER_RE.search(normalize(text)):
        add(prev, 0.05)
    return {k: min(1.0, max(0.0, float(v))) for k, v in out.items()}


# —— 词法兜底（mock / 无向量模型） ——

def _tokens(text: str) -> Counter:
    t = normalize(text)
    toks = []
    for run in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", t):
        if re.fullmatch(r"[a-z0-9]+", run):
            toks.append(run)
        elif len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    return Counter(toks)


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def lexical_candidates(text: str, top_k: int = 3, include_control: bool = False) -> list:
    """对原型句做字符 bigram 余弦，返回 [(intent, score)] 降序。

    控制指令默认不参与：控制类必须是医生明确说出的固定短语（第一层精确匹配），
    词法相似度不足以证明「他要跳过 3D」这类改变行为的指令。
    """
    q = _tokens(text)
    if not q:
        return []
    best = {}
    for intent, meta in (spec().get("intents") or {}).items():
        if not include_control and meta.get("class") == "control":
            continue
        score = 0.0
        for proto in meta.get("prototypes") or []:
            score = max(score, _cosine(q, _tokens(proto)))
        if score > 0:
            best[intent] = round(score, 4)
    return sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]


# —— 确认卡 ——

def build_pending(text: str, candidates: list, layer: int, source: str = "router") -> dict:
    """candidates: [{"intent","label","confidence","reason"}]，最多 4 个。"""
    opts = []
    for c in (candidates or [])[:4]:
        intent = str(c.get("intent") or "")
        if intent not in INTENT_LABELS:
            continue
        opts.append({"intent": intent,
                     "label": str(c.get("label") or intent_label(intent)),
                     "confidence": round(float(c.get("confidence") or 0.0), 4),
                     "reason": str(c.get("reason") or "")[:120]})
    return {"id": "pi_" + uuid.uuid4().hex[:8], "source_text": str(text or ""),
            "candidates": opts, "layer": int(layer or 3), "source": source,
            "created_at": datetime.now(timezone.utc).isoformat()}


def resolve_pending_choice(text: str, pending: dict | None):
    """返回 (action, intent)：action in {"choose","requote","reask","cancel",""}。

    requote = 医生否定这张卡并补了新说法 → 用「原话 + 新说法」重跑三层；
    cancel = 医生明确取消 → 清卡、不执行任何 handler；
    其他文本 = 与卡无关 → 清卡后按新消息正常路由。
    """
    if not isinstance(pending, dict):
        return "", None
    t = normalize(text)
    opts = [o for o in (pending.get("candidates") or []) if isinstance(o, dict)]
    if not t or not opts:
        return "", None
    if any(k in t for k in ("都不是", "重新说", "我来说", "重新讲", "换个说法")):
        return "requote", None
    if any(k in t for k in ("取消", "算了", "不选了", "停止", "退出")):
        return "cancel", None
    m = re.fullmatch(r"([1-4])", t)
    if m:
        idx = int(m.group(1)) - 1
        if idx < len(opts):
            return "choose", str(opts[idx].get("intent") or "")
    for o in opts:
        if t == normalize(o.get("label") or "") or t == normalize(o.get("intent") or ""):
            return "choose", str(o.get("intent") or "")
    if is_confirm(t):
        if len(opts) == 1 and float(opts[0].get("confidence") or 0) >= 0.75:
            return "choose", str(opts[0].get("intent") or "")
        return "reask", None
    return "", None
