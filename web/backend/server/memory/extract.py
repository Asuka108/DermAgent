# -*- coding: utf-8 -*-
"""异步记忆抽取：LLM 结构化 schema、证据校验、身份脱敏与情景切片构造。

纪律（计划 §5.2/§5.3）：
- 只接受医生（user）轮陈述；助手推断、诊断结论、处方/剂量建议一律丢弃；
- evidence 必须能在医生原话中找到（归一化子串），否则该条事实作废；
- 进入 LLM / Milvus 的文本先过 redact，身份字段绝不外发；
- 输出失败返回 None，由 worker 记 failed 并留审计，绝不伪造事实。
"""
import hashlib
import json
import re
import ssl
import time
import urllib.error
import urllib.request

from . import config, redact
from .profile import normalize_value, validate_facts

_MAX_TOPIC = 300
_MAX_TITLE = 64


def _http_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _norm_text(text: str) -> str:
    return re.sub(r"[\s，。；、,.;:：!！?？()（）\[\]【】\"'“”‘’\-—…]+", "", str(text or ""))


def _categories_help() -> str:
    cats = (config.profile_rule().get("categories") or {})
    return "；".join(f"{cat}: {', '.join(fields)}" for cat, fields in cats.items())


def build_messages(user_text: str, case_brief: dict, active_facts: list, patient: dict | None) -> list:
    names = redact.identity_values(patient)
    redacted = redact.redact_identity(user_text, names)
    brief = {k: redact.redact_identity(str(v or ""), names)[:120]
             for k, v in (case_brief or {}).items() if str(v or "").strip()}
    known = [{"category": f.get("category"), "field": f.get("field"),
              "value": redact.redact_identity(str(f.get("value") or ""), names)}
             for f in (active_facts or [])[:20]]
    system = "\n".join([
        "你是皮肤科病历记忆抽取器。只从医生（user）本轮原话中抽取可长期复用的精确患者事实，"
        "并输出 JSON；不得推断、补全或改写医生没说的内容。",
        "允许的类别与字段：" + _categories_help(),
        "硬约束：",
        "1. 只抽取医生明确陈述的事实；助手推断、诊断结论、处方建议、疗程建议一律不得写入。",
        "2. 不抽取身份信息（姓名/电话/身份证/门诊号/住院号/住址）。",
        "3. evidence 必须是医生原话的连续片段（可截断），不得改写；找不到原文依据的条目不要输出。",
        "4. confidence 取 0-1；不确定就不输出。药物只记录医生已说的通用名/剂量/频次/起始/指征。",
        "5. 多值事实（过敏、用药、家族史、诊断史、检验）逐条列出，不要合并成一句话。",
        "6. topic 概括本轮问答的临床话题（≤300 字，不含身份信息）；"
        "generalizable=true 仅当该话题可复用于其他患者（如处置原则、鉴别思路），患者个人细节必须 false。",
        "输出仅一个 JSON 对象，不要 markdown：",
        '{"facts":[{"category":"allergy","field":"allergen","value":"青霉素",'
        '"evidence":"医生原话片段","confidence":0.92}],'
        '"topic":{"title":"湿疹急性期渗出处理","summary":"……","generalizable":true}}',
    ])
    user = "\n".join([
        "当前病例摘要：" + json.dumps(brief, ensure_ascii=False),
        "患者已知事实（去重参考，不要重复输出）：" + json.dumps(known, ensure_ascii=False),
        "医生本轮原话：\n" + redacted,
    ])
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _strip_fences(text: str) -> str:
    out = str(text or "").strip()
    out = re.sub(r"^```(?:json)?\s*", "", out)
    out = re.sub(r"\s*```$", "", out)
    return out.strip()


def parse_extraction(raw: str, user_text: str, patient: dict | None = None) -> dict | None:
    """解析并校验 LLM 输出；无有效内容返回 {'facts': [], 'topic': None}，非 JSON 返回 None。"""
    try:
        data = json.loads(_strip_fences(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    names = redact.identity_values(patient)
    source_norm = _norm_text(user_text)
    facts = []
    for fact in validate_facts(data.get("facts") or [], "llm_extract"):
        evidence = str(fact.get("evidence") or "")
        if not evidence or _norm_text(evidence) not in source_norm:
            continue
        # 事实值也必须能在证据/原话中找到：LLM 不许用「合规证据 + 编造值」组合入档。
        value_norm = _norm_text(fact.get("value"))
        if not value_norm or value_norm not in _norm_text(evidence):
            continue
        if redact.has_identity(evidence, names):
            continue
        fact["evidence"] = redact.redact_identity(evidence, names)
        facts.append(fact)
    topic = data.get("topic") if isinstance(data.get("topic"), dict) else None
    clean_topic = None
    if topic:
        raw_title = str(topic.get("title") or "")[:_MAX_TITLE]
        raw_summary = str(topic.get("summary") or "")[:_MAX_TOPIC]
        # 原文里出现身份信息 = 患者个人细节：即使脱敏后也不得进入医生级通用话题
        personal = redact.has_identity(raw_title + raw_summary, names)
        title = redact.redact_identity(raw_title, names)
        summary = redact.redact_identity(raw_summary, names)
        if title or summary:
            clean_topic = {"title": title, "summary": summary,
                           "generalizable": bool(topic.get("generalizable")) and not personal}
    return {"facts": facts, "topic": clean_topic}


def call_llm(llm: tuple, messages: list, timeout: int = 20) -> str | None:
    """调用活动 LLM 连接（temperature=0，强制 JSON）；失败重试 2 次（2s/10s）。"""
    base_url, api_key, model, effort = (list(llm or []) + ["", "", "", ""])[:4]
    if not (base_url and model):
        return None
    payload = {"model": model, "temperature": 0, "stream": False, "messages": messages}
    if effort:
        payload["reasoning_effort"] = effort
    req = urllib.request.Request(
        f"{str(base_url).rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="POST")
    for attempt, delay in enumerate((0, 2, 10)):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_http_context()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
            if content.strip():
                return content
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
    return None


# —— 情景切片 ——

def chunk_id(scope: str, case_id: str, msg_id: str, text: str) -> str:
    return hashlib.sha1(f"{scope}|{case_id}|{msg_id}|{text}".encode("utf-8")).hexdigest()[:32]


def _seq_of(msg: dict, index: int) -> int:
    try:
        return int(msg.get("seq") or 0) or index + 1
    except (TypeError, ValueError):
        return index + 1


def build_turn_chunks(messages: list, case_id: str, patient_id: str, patient: dict | None,
                      task_type: str = "") -> list:
    """每轮问答成对切片（病例级）；助手回复缺失的轮次不切片。"""
    names = redact.identity_values(patient)
    max_chars = int(config.episodic_rule().get("chunk_max_chars") or 1200)
    out, pending = [], []
    for idx, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            pending.append(content)
            continue
        if role != "assistant" or not pending:
            continue
        user_text = "；".join(pending)
        pending = []
        text = "医生：" + user_text + "\n助手：" + content
        text = redact.redact_identity(text, names)[:max_chars]
        if not text.strip():
            continue
        msg_id = str(msg.get("message_id") or "") or f"seq-{_seq_of(msg, idx)}"
        out.append({"chunk_id": chunk_id("case", case_id, msg_id, text),
                    "scope": "case", "case_id": case_id, "patient_id": patient_id,
                    "doctor_id": "local", "msg_id": msg_id, "task_type": str(task_type or "")[:32],
                    "title": redact.redact_identity(user_text, names)[:32],
                    "text": text, "generalizable": False})
    return out


def build_topic_chunk(topic: dict | None, case_id: str, patient_id: str, msg_id: str,
                      patient: dict | None) -> dict | None:
    if not isinstance(topic, dict) or not topic.get("generalizable"):
        return None
    names = redact.identity_values(patient)
    raw_title = str(topic.get("title") or "")[:_MAX_TITLE]
    raw_summary = str(topic.get("summary") or "")[:_MAX_TOPIC]
    if redact.has_identity(raw_title + raw_summary, names):
        return None                         # 含身份 = 个人细节，不进跨病例医生级
    title = redact.redact_identity(raw_title, names).strip()
    summary = redact.redact_identity(raw_summary, names).strip()
    if not summary:
        return None
    text = (f"{title}：{summary}" if title else summary)[
        :int(config.episodic_rule().get("topic_max_chars") or 300)]
    cid = chunk_id("doctor", case_id, msg_id, text)
    return {"chunk_id": cid, "scope": "doctor", "case_id": case_id, "patient_id": patient_id,
            "doctor_id": "local", "msg_id": msg_id, "task_type": "", "title": title,
            "text": text, "generalizable": True}


def dedupe_chunks(chunks: list) -> list:
    seen, out = set(), []
    for chunk in chunks or []:
        cid = chunk.get("chunk_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(chunk)
    return out


def fact_digest(facts: list) -> list:
    return [{"category": f.get("category"), "field": f.get("field"),
             "normalized_value": normalize_value(f.get("value"))} for f in facts or []]
