# -*- coding: utf-8 -*-
"""v76 统一上下文：Agent 与文书/分析提示词共用一套预算、裁剪与摘要。

设计要点：
- 不可裁剪层：病例字段、过敏史、危险信号、医生确认/修改字段；
- 裁剪顺序：工具结果 → RAG 引用 → 较早对话 → 摘要保底 → 硬上限（安全层永不被裁）；
- 摘要 v2（schema_version=2.0）带覆盖区间 covered_from_seq/covered_upto_seq，
  规则抽取为主，`DERM_SUMMARY_LLM=1` 时才额外请求一次 LLM 文字摘要（失败保留规则版）；
- 消息上限（默认 300）超出部分先并入摘要再从会话中删除，`_store_sess` 只写压缩后的消息。
"""
import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:
    from agent_runtime import estimate_tokens
except ImportError:  # pragma: no cover - 包导入路径
    from server.agent_runtime import estimate_tokens

_REQUIRED_FIELDS = None


def _required_fields() -> tuple:
    """延迟导入 derm_core：app 在导入本模块之后才把 shared/ 加入 sys.path。"""
    global _REQUIRED_FIELDS
    if _REQUIRED_FIELDS is None:
        try:
            from derm_core import REQUIRED_FIELDS as fields
            _REQUIRED_FIELDS = tuple(fields)
        except ImportError:  # pragma: no cover - 单测/非标准部署
            _REQUIRED_FIELDS = ()
    return _REQUIRED_FIELDS

SCHEMA_VERSION = "2.0"
SUMMARY_KEEP_RECENT = 20          # 摘要不覆盖最近 N 条消息（保留原文细节）
_SUMMARY_MSG_THRESHOLD = 30
_SUMMARY_CHARS_THRESHOLD = 9000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


BUDGET_TOKENS = _env_int("DERM_CONTEXT_BUDGET_TOKENS", 12000)
SUMMARY_TRIGGER_TOKENS = _env_int("DERM_SUMMARY_TRIGGER_TOKENS", 6000)
MAX_MESSAGES = _env_int("DERM_MAX_MESSAGES", 300)
_PER_MSG_CHARS = {"agent": 1200, "doc": 220}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# —— 不可裁剪层 ——

def _field_items(sess: dict) -> list:
    case = sess.get("case") or {}
    fields = _required_fields()
    if fields:
        return [(key, label, str(case.get(key) or "")) for key, label, _ in fields]
    return [(key, key, str(value or "")) for key, value in case.items()]


def safety_parts(sess: dict) -> list:
    """过敏史 / 危险信号 / 医生修改确认字段——任何裁剪都不得删除。"""
    case = sess.get("case") or {}
    rec = sess.get("record") or {}
    meta = rec.get("meta") or {}
    edits = sorted(f for f, m in meta.items()
                   if isinstance(m, dict) and m.get("source") in ("doctor_modified", "doctor_confirmed"))
    lines = []
    allergy = str(case.get("allergies") or "").strip()
    if allergy:
        lines.append("过敏史：" + allergy)
    flags = [str(f) for f in (sess.get("red_flags") or [])]
    if flags:
        lines.append("危险信号：" + "；".join(flags[:6]))
    if edits:
        lines.append("医生修改/确认过的字段（不得覆盖）：" + "、".join(edits))
    return lines


def safety_block(sess: dict) -> str:
    parts = safety_parts(sess)
    return "【安全上下文（永不裁剪）】" + ("；".join(parts) if parts else "（暂无）")


def case_block(sess: dict) -> str:
    return "【病例字段】" + json.dumps(sess.get("case") or {}, ensure_ascii=False)


def task_block(sess: dict) -> str:
    return ("【当前任务】" + str(sess.get("task_type") or "") + "："
            + str(sess.get("task_text") or "")[:120]
            + "；缺失项：" + ("、".join(str(x) for x in (sess.get("missing") or [])) or "无"))


def summary_block(summary: dict) -> str:
    if not isinstance(summary, dict) or not summary:
        return ""
    body = json.dumps(summary, ensure_ascii=False)
    head = "【病例摘要（较早对话已压缩，以此为准）】"
    if summary.get("narrative"):
        return head + "\n" + str(summary["narrative"]) + "\n结构化事实：" + body
    return head + body


def memory_text(sess: dict) -> str:
    mem = sess.get("_memory_ctx") if isinstance(sess.get("_memory_ctx"), dict) else {}
    parts = []
    for key in ("profile_text", "episodic_text"):
        text = str(mem.get(key) or "").strip()
        if text:
            parts.append(text)   # 渲染层已按预算裁剪并自带截断标记，不得再次切片
    return "\n".join(parts)


# —— 可裁剪层 ——

@dataclass
class ContextBundle:
    header: str = ""
    history: list = field(default_factory=list)
    tools_text: str = ""
    rag_text: str = ""
    tokens: int = 0
    estimated: bool = True
    dropped: int = 0
    summary_included: bool = False

    def dialog_text(self) -> str:
        if not self.history:
            return ""
        lines = [f"{'医生' if m.get('role') == 'user' else '助手'}：{m.get('content')}"
                 for m in self.history]
        return "【最近对话】\n" + "\n".join(lines)

    def render(self) -> str:
        parts = [self.header] if self.header else []
        tail = self.dialog_text()
        for block in (self.tools_text, self.rag_text):
            if block:
                tail = (tail + "\n" + block) if tail else block
        if tail:
            parts.append(tail)
        return "\n\n".join(parts)


def _tool_lines(sess: dict) -> list:
    tools = [t for t in (sess.get("tool_history") or []) if isinstance(t, dict)]
    return [f"- {t.get('tool')}（{t.get('risk_level', 'low')}）：{str(t.get('goal') or '')[:40]}"
            f" → {str(t.get('output_digest') or '')[:60]}" for t in reversed(tools[-5:])]


def _rag_line(sess: dict) -> str:
    rags = [c.get("citation_id") for c in (sess.get("book_citations") or [])
            if isinstance(c, dict) and c.get("citation_id")]
    return ("【RAG 引用来源】" + "、".join(str(r) for r in rags[:8])) if rags else ""


def _history(sess, summary, purpose, remaining_tokens, messages=None):
    """从最新往旧装配对话；跳过已被摘要覆盖的消息；预算不足时丢弃更长或更旧的。"""
    per_msg = _PER_MSG_CHARS.get(purpose, 220)
    upto = int((summary or {}).get("covered_upto_seq") or 0)
    source = messages if isinstance(messages, list) and messages else (sess.get("messages") or [])
    msgs = [m for m in source
            if isinstance(m, dict) and str(m.get("content") or "").strip()
            and m.get("role") in ("user", "assistant")]
    hist, dropped = [], 0
    for m in reversed(msgs):
        try:
            seq = int(m.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if upto and seq <= upto:
            dropped += 1
            continue
        content = str(m.get("content"))[:per_msg]
        cost = estimate_tokens(content)
        if cost > remaining_tokens and hist:
            dropped += 1
            break
        if cost > remaining_tokens and not hist:
            content = content[:max(remaining_tokens, 1) * 2]   # 硬上限：截断最新一条
            cost = estimate_tokens(content)
        remaining_tokens -= cost
        hist.insert(0, {"role": m.get("role"), "content": content})
    return hist, max(remaining_tokens, 0), dropped


def build(sess: dict, purpose: str = "agent", budget_tokens: int = None,
          include_memory: bool = True, messages: list = None) -> ContextBundle:
    """按预算装配上下文。purpose=agent 只给安全层+摘要+对话（病例状态在系统提示词里）；
    purpose=doc 额外给病例字段、当前任务、工具记录和 RAG 来源。"""
    purpose = purpose if purpose in ("agent", "doc") else "agent"
    budget = int(budget_tokens or BUDGET_TOKENS)
    summary = sess.get("context_summary") if isinstance(sess.get("context_summary"), dict) else {}
    safety = safety_block(sess)
    head_parts = [case_block(sess), safety, task_block(sess)] if purpose == "doc" else [safety]
    summ = summary_block(summary)
    if summ:
        head_parts.append(summ)
    mem = memory_text(sess) if include_memory else ""
    if mem:
        head_parts.append(mem)
    header = "\n\n".join(head_parts)

    bundle = ContextBundle(header=header, summary_included=bool(summ))
    header_tokens = estimate_tokens(header)
    if header_tokens > budget:
        # 硬上限：先压摘要，再压病例字段；安全层保持原文
        reserve = estimate_tokens(safety)
        room = max(budget - reserve, 200)
        trimmed = []
        for part in head_parts:
            if part == safety or room <= 0:
                trimmed.append(part)
                continue
            cost = estimate_tokens(part)
            if cost > room:
                part = part[:max(room * 2, 1)]
                cost = estimate_tokens(part)
            room -= cost
            trimmed.append(part)
        header = "\n\n".join(trimmed)
        bundle.header = header
        header_tokens = estimate_tokens(header)

    remaining = max(budget - header_tokens, 100)
    tools_text = rag_text = ""
    if purpose == "doc":
        tool_lines = _tool_lines(sess)
        rag_line = _rag_line(sess)
        if tool_lines:
            candidate = "【工具执行记录】\n" + "\n".join(tool_lines)
            cost = estimate_tokens(candidate)
            if cost <= max(remaining // 4, 0):      # 裁剪顺序第一：工具结果
                tools_text = candidate
                remaining -= cost
        if rag_line:
            cost = estimate_tokens(rag_line)
            if cost <= max(remaining // 4, 0):      # 裁剪顺序第二：RAG 引用
                rag_text = rag_line
                remaining -= cost

    hist, left, dropped = _history(sess, summary, purpose, remaining, messages)
    bundle.history = hist
    bundle.tools_text = tools_text
    bundle.rag_text = rag_text
    bundle.dropped = dropped
    bundle.tokens = budget - left
    return bundle


# —— 摘要 v2 ——

def _summary_threshold_hit(msgs: list, force: bool) -> bool:
    if force:
        return True
    total_chars = sum(len(str(m.get("content") or "")) for m in msgs)
    return (len(msgs) >= _SUMMARY_MSG_THRESHOLD
            or total_chars >= _SUMMARY_CHARS_THRESHOLD
            or estimate_tokens("".join(str(m.get("content") or "") for m in msgs))
            >= SUMMARY_TRIGGER_TOKENS)


def _rule_summary(sess: dict, msgs: list, prev: dict) -> dict:
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
    covered = msgs[:max(len(msgs) - SUMMARY_KEEP_RECENT, 0)]
    seqs = []
    for m in covered:
        try:
            seqs.append(int(m.get("seq") or 0))
        except (TypeError, ValueError):
            pass
    decisions = [{"seq": m.get("seq"), "text": str(m.get("content") or "")[:120]}
                 for m in msgs if m.get("role") == "user"][-5:]
    covered_from = min(seqs) if seqs else 0
    prev_upto = int((prev or {}).get("covered_upto_seq") or 0)
    if prev_upto and covered_from <= prev_upto:
        covered_from = prev_upto + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "version": int((prev or {}).get("version") or 0) + 1,
        "covered_from_seq": covered_from,
        "covered_upto_seq": max(seqs) if seqs else prev_upto,
        "confirmed_facts": {key: value.strip()[:200]
                            for key, _label, value in _field_items(sess) if value.strip()},
        "unconfirmed_items": [str(x) for x in (sess.get("missing") or [])],
        "allergy_and_risk_flags": {"allergies": str(case.get("allergies") or "").strip(),
                                   "red_flags": [str(x) for x in (sess.get("red_flags") or [])]},
        "doctor_edits": doctor_edits,
        "tool_results": tool_results,
        "rag_sources": rag_ids,
        "decisions": decisions,
        "current_task": str(sess.get("task_type") or ""),
        "next_action": "按医生下一条指令继续；缺失项见 unconfirmed_items（不推断为「无」）。",
        "generated_by": "rule",
        "at": _now(),
    }


def _llm_narrative(llm: tuple, summary: dict, messages: list):
    """可选：让 LLM 用一段话概述较早对话（DERM_SUMMARY_LLM=1 才调用）。
    只做文字概述，事实仍以结构化字段为准；失败返回 (None, 错误)。"""
    base_url, api_key, model = (llm or ("", "", ""))[:3]
    if not (base_url and model):
        return None, "llm_not_configured"
    dialog = "\n".join(f"{'医生' if m.get('role') == 'user' else '助手'}：{str(m.get('content'))[:200]}"
                       for m in messages[-30:])
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "你在医生监督下压缩问诊记录。只重组已有信息，不推断诊断，"
                                          "不补充任何未出现的事实；简体中文，150 字以内。"},
            {"role": "user", "content": "结构化事实（权威，不得改写）："
                                        + json.dumps({k: summary.get(k) for k in
                                                      ("confirmed_facts", "allergy_and_risk_flags",
                                                       "unconfirmed_items")}, ensure_ascii=False)
                                        + "\n较早对话：\n" + dialog},
        ],
    }
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_env_int("DERM_SUMMARY_LLM_TIMEOUT_S", 30)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data["choices"][0]["message"]["content"]).strip()[:600], ""
    except Exception as exc:  # noqa: BLE001 — 摘要失败绝不阻断对话
        return None, str(exc)[:120]


def update_summary(sess: dict, force: bool = False, llm: tuple = None,
                   on_save=None, audit=None) -> bool:
    """达到阈值（或 force）时更新摘要 v2；返回是否更新。"""
    msgs = [m for m in (sess.get("messages") or []) if isinstance(m, dict)]
    if not _summary_threshold_hit(msgs, force):
        return False
    prev = sess.get("context_summary") if isinstance(sess.get("context_summary"), dict) else {}
    summary = _rule_summary(sess, msgs, prev)
    if os.environ.get("DERM_SUMMARY_LLM") == "1":
        narrative, err = _llm_narrative(llm or ("", "", ""), summary, msgs)
        if narrative:
            summary["narrative"] = narrative
            summary["generated_by"] = "llm"
        else:
            summary["llm_error"] = err
    summary["tokens"] = estimate_tokens(summary_block(summary))
    sess["context_summary"] = summary
    if callable(on_save) and sess.get("case_id"):
        try:
            on_save(str(sess["case_id"]), summary)
        except Exception:  # noqa: BLE001
            pass
    if callable(audit):
        try:
            audit("context_summarized", {"case_id": sess.get("case_id"), "messages": len(msgs),
                                         "version": summary["version"],
                                         "generated_by": summary["generated_by"],
                                         "covered_upto_seq": summary["covered_upto_seq"]})
        except Exception:  # noqa: BLE001
            pass
    return True


def trim_messages(sess: dict, keep_recent: int = SUMMARY_KEEP_RECENT) -> int:
    """把已被摘要覆盖的旧消息从会话中删除（seq 单调不回退），返回删除条数。"""
    msgs = sess.get("messages") or []
    if len(msgs) <= MAX_MESSAGES:
        return 0
    upto = int((sess.get("context_summary") or {}).get("covered_upto_seq") or 0)
    if not upto:
        return 0
    keep_from = max(0, len(msgs) - keep_recent)
    dropped = [m for m in msgs[:keep_from] if int(m.get("seq") or 0) <= upto]
    if not dropped:
        return 0
    sess["messages"] = [m for m in msgs if int(m.get("seq") or 0) > upto]
    sess["msg_trimmed"] = int(sess.get("msg_trimmed") or 0) + len(dropped)
    return len(dropped)
