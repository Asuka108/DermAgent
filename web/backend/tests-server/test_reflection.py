# -*- coding: utf-8 -*-
"""v78 独立核验节点：规则码正/反例、评审解析、隐私边界、降级文本、运行时自纠。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import agent_runtime as rt  # noqa: E402
from server import reflection as rf  # noqa: E402


def _bundle(draft="", *, query="右前臂红斑 3 天，怎么处理？", sess=None,
            evidence=None, allowed=None):
    return rf.build_bundle(sess or {"case_id": "c1"}, query, evidence or [],
                           draft=draft, allowed=allowed)


def _codes(bundle):
    return [i["code"] for i in rf.run_rules(bundle)]


def _kb_evidence(citation_id="RB-1", text="湿疹护理：保湿，避免搔抓。", ok=True):
    return rf.evidence_item("search_knowledge_base", {"query": "湿疹护理"},
                            {"ok": ok, "citations": [{"citation_id": citation_id,
                                                      "text": text}]}, ok, 1)


# —— 规则码：正例 + 不误报反例 ——

def test_unknown_citation():
    assert "unknown_citation" in _codes(_bundle("见 CIT-abcd-1 的说明"))
    allowed = [{"citation_id": "CIT-abcd-1"}]
    assert "unknown_citation" not in _codes(_bundle("见 CIT-abcd-1 的说明", allowed=allowed))


def test_citation_without_retrieval():
    allowed = [{"citation_id": "RB-2024-001"}]
    bundle = _bundle("见 RB-2024-001", allowed=allowed)
    assert "citation_without_retrieval" in _codes(bundle)
    ev = [_kb_evidence("RB-2024-001")]
    assert "citation_without_retrieval" not in _codes(
        _bundle("见 RB-2024-001", allowed=allowed, evidence=ev))


def test_unsupported_source_claim():
    assert "unsupported_source_claim" in _codes(_bundle("根据指南，建议保湿"))
    ev = [_kb_evidence()]
    assert "unsupported_source_claim" not in _codes(
        _bundle("根据指南，建议保湿", evidence=ev))


def test_certain_diagnosis_with_negation_guard():
    assert "certain_diagnosis" in _codes(_bundle("确诊为湿疹"))
    assert "certain_diagnosis" not in _codes(_bundle("尚未确诊，建议面诊进一步评估"))
    assert "certain_diagnosis" not in _codes(_bundle("不能诊断为银屑病"))


def test_write_status_mismatch_only_after_failed_write():
    failed = rf.evidence_item("update_record_fields", {"fields": {}},
                              {"ok": False, "error": "boom"}, False, 1)
    assert "write_status_mismatch" in _codes(_bundle("已保存主诉和现病史", evidence=[failed]))
    ok = rf.evidence_item("update_record_fields", {"fields": {}}, {"ok": True}, True, 1)
    assert "write_status_mismatch" not in _codes(_bundle("已保存主诉和现病史", evidence=[ok]))


def test_red_flag_ignored():
    sess = {"red_flags": ["呼吸困难"]}
    assert "red_flag_ignored" in _codes(_bundle("考虑湿疹，可外用药膏", sess=sess))
    assert "red_flag_ignored" not in _codes(_bundle("建议立即急诊处理", sess=sess))


def test_raw_tool_dump():
    assert "raw_tool_dump" in _codes(_bundle('结果：{"ok": true, "x": 1}'))
    assert "raw_tool_dump" not in _codes(_bundle("已记录随访计划"))


def test_identity_leak():
    assert "identity_leak" in _codes(_bundle("联系电话 13800138000"))
    assert "identity_leak" not in _codes(_bundle("每日两次外用药膏"))


def test_dose_not_in_evidence_is_warn_only():
    issues = {i["code"]: i for i in rf.run_rules(_bundle("口服 10 mg 每日一次"))}
    assert issues["dose_not_in_evidence"]["severity"] == "warn"
    ev = [_kb_evidence(text="成人可口服 10 mg，每日一次。")]
    assert "dose_not_in_evidence" not in _codes(_bundle("口服 10 mg 每日一次", evidence=ev))


def test_regimen_without_retrieval():
    assert "regimen_without_retrieval" in _codes(_bundle("每日 2 次，连续 7 天观察疗效"))
    ev = [_kb_evidence()]
    assert "regimen_without_retrieval" not in _codes(
        _bundle("每日 2 次，连续 7 天观察疗效", evidence=ev))


# —— 评审解析与 evaluate ——

def test_parse_critic_accepts_plain_and_fenced_json():
    raw = '{"verdict":"pass","issues":[]}'
    assert rf.parse_critic(raw)["status"] == "ok"
    fenced = "```json\n{\"verdict\":\"correct\",\"issues\":[{\"code\":\"unknown_citation\","\
             "\"severity\":\"high\",\"detail\":\"x\"}]}\n```"
    out = rf.parse_critic(fenced)
    assert out["status"] == "ok" and out["verdict"] == "correct"
    assert out["issues"][0]["code"] == "unknown_citation"


def test_parse_critic_rejects_garbage_and_unknown_codes_are_downgraded():
    assert rf.parse_critic("抱歉，我无法判断")["status"] == "error"
    assert rf.parse_critic('{"verdict":"correct","issues":[]}')["status"] == "error"
    out = rf.parse_critic(json.dumps({"verdict": "correct", "issues": [
        {"code": "made_up_code", "severity": "high", "detail": "x"}]}))
    assert out["status"] == "ok" and out["issues"][0]["code"] == "critic_other"
    assert out["issues"][0]["severity"] == "warn"


def test_evaluate_rules_keep_working_without_llm():
    out = rf.evaluate(_bundle("确诊为湿疹"), "final", reflect=None, allow_llm=True)
    assert out["status"] == "budget" and out["verdict"] == "correct"
    assert "certain_diagnosis" in out["codes"]


def test_evaluate_survives_reflect_exception():
    def boom(*_a):
        raise ConnectionError("critic down")

    out = rf.evaluate(_bundle("确诊为湿疹"), "final", reflect=boom,
                      llm=("http://x", "k", "m"), allow_llm=True)
    assert out["status"] == "llm_unavailable"
    assert "certain_diagnosis" in out["codes"]


def test_evaluate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("DERM_REFLECTION", "0")
    out = rf.evaluate(_bundle("确诊为湿疹"), "final", reflect=lambda *_: {}, allow_llm=True)
    assert out["status"] == "disabled" and out["verdict"] == "correct"


# —— 隐私边界 ——

def test_bundle_redacts_identity_and_never_carries_memory_or_summary():
    sess = {"case_id": "c1", "profile_text": "患者长期高血压", "episodic_text": "上次用了激素",
            "summary": {"confirmed_facts": ["过敏：青霉素"]}, "red_flags": ["呼吸困难"]}
    ev = [rf.evidence_item("get_case_status", {},
                           {"ok": True, "patient": "患者姓名：张小明 电话 13800138000"}, True, 1)]
    bundle = rf.build_bundle(sess, "患者姓名：张小明，电话 13800138000，怎么办",
                             ev, draft="患者姓名：张小明 电话 13800138000", allowed=[])
    blob = json.dumps(bundle, ensure_ascii=False)
    assert "13800138000" not in blob and "张小明" not in blob
    assert "患者长期高血压" not in blob and "上次用了激素" not in blob
    assert "青霉素" not in blob
    assert set(bundle) == {"query", "facts", "allowed_citation_ids",
                           "allowed_citations", "evidence", "draft"}
    assert set(bundle["facts"]) == {"phase", "workflow_state", "red_flags", "missing",
                                    "kb_searched", "analyzed", "last_write_ok",
                                    "write_failed_tools", "draft_has_identity"}
    assert bundle["facts"]["draft_has_identity"] is True


def test_bundle_does_not_trust_draft_citations():
    bundle = _bundle("见 RB-fake-1", evidence=[], allowed=[])
    assert bundle["allowed_citation_ids"] == []
    assert "unknown_citation" in _codes(bundle)


def test_evidence_excerpt_is_truncated():
    heavy = {"ok": True, "text": "长" * 5000}
    item = rf.evidence_item("search_knowledge_base", {}, heavy, True, 1)
    assert len(item["excerpt"]) <= rf.MAX_EXCERPT_CHARS + 1


# —— 降级与摘要 ——

def test_degrade_text_only_appends_note():
    base = "建议保湿，避免搔抓。"
    out = rf.degrade_text(base, [{"code": "certain_diagnosis"}])
    assert out.startswith(base)
    assert "未通过自动核验" in out and "确定性诊断措辞" in out


def test_compact_summary_has_no_detail_or_excerpt():
    out = rf.evaluate(_bundle("确诊为湿疹"), "final", reflect=None, allow_llm=False)
    compact = rf.compact_summary(out)
    blob = json.dumps(compact, ensure_ascii=False)
    assert set(compact) == {"verdict", "status", "summary", "codes",
                            "corrections", "checked_at", "model"}
    assert "detail" not in blob and "确诊/确定是" not in blob
    stored = rf.storage_view(out)
    assert stored["allowed_citations"] == [] and stored["high_count"] == 1


# —— 运行时：一次自纠与预算 ——

def _call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _run(chat, execute=None, budget=None, schemas=None, reflect=None, emit=None):
    def _execute(fn, args, sess, ctx):
        return {"ok": True}

    return rt.run_loop(
        [{"role": "user", "content": "右前臂红斑 3 天，怎么处理？"}],
        {"case_id": "c1"}, {"llm": ("http://x", "k", "m", "")},
        budget=budget or rt.TurnBudget(max_steps=4, max_tokens=100000),
        emit=emit or (lambda *_: None),
        chat=chat, execute=execute or _execute,
        summarize=lambda fn, args, result: fn,
        schemas=schemas if schemas is not None else [], reflect=reflect)


def test_final_violation_is_corrected_once_with_user_role_feedback():
    replies = [
        {"content": "确诊为湿疹", "tool_calls": []},
        {"content": "考虑湿疹，建议面诊进一步确认。", "tool_calls": []},
    ]
    seen = []

    def chat(base, key, model, messages, effort):
        seen.append([dict(m) for m in messages])
        return replies.pop(0)

    events = []
    res = _run(chat, emit=lambda t, p: events.append((t, p)))
    assert len(seen) == 2
    feedback = seen[1][-1]
    assert feedback["role"] == "user" and "[内部核验反馈，非医生消息]" in feedback["content"]
    assert "确诊" not in res.final
    assert res.reflection["verdict"] == "corrected"
    assert res.reflection["codes"] == ["certain_diagnosis"]
    assert res.reflection["corrections"] == 1
    assert any(p.get("verdict") == "correcting" for t, p in events if t == "reflection")


def test_second_violation_degrades_without_second_correction():
    replies = [
        {"content": "确诊为湿疹", "tool_calls": []},
        {"content": "确诊为接触性皮炎", "tool_calls": []},
    ]
    calls = {"n": 0}

    def chat(*_a):
        calls["n"] += 1
        return replies.pop(0)

    res = _run(chat)
    assert calls["n"] == 2                       # 只自纠一次
    assert "未通过自动核验" in res.final
    assert res.reflection["verdict"] == "degraded"
    assert res.reflection["corrections"] == 1


def test_no_budget_means_no_correction_and_honest_degrade():
    replies = [
        {"content": "确诊为湿疹", "tool_calls": []},
        {"content": "不该被调用", "tool_calls": []},
    ]
    calls = {"n": 0}

    def chat(*_a):
        calls["n"] += 1
        return replies.pop(0)

    res = _run(chat, budget=rt.TurnBudget(max_steps=4, max_llm_calls=1, max_tokens=100000))
    assert calls["n"] == 1
    assert "未通过自动核验" in res.final
    assert res.reflection["verdict"] == "degraded"
    assert res.meta["budget"]["reflection_calls"] == 0


def test_reflect_exception_keeps_rules_and_does_not_500():
    replies = [
        {"content": "确诊为湿疹", "tool_calls": []},
        {"content": "考虑湿疹，建议面诊。", "tool_calls": []},
    ]

    def chat(*_a):
        return replies.pop(0)

    def boom(*_a):
        raise ConnectionError("critic down")

    res = _run(chat, reflect=boom)
    assert "确诊" not in res.final
    assert res.reflection["status"] == "llm_unavailable"
    assert res.reflection["verdict"] == "corrected"


def test_retrieval_reflection_runs_only_once_per_turn():
    stages = []

    def reflect(base, key, model, messages):
        stages.append(json.loads(messages[-1]["content"])["stage"])
        return {"content": '{"verdict":"pass","issues":[]}', "_model": model}

    kb_schema = {"type": "function", "function": {
        "name": "search_knowledge_base",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}}
    replies = [
        {"content": "", "tool_calls": [_call("search_knowledge_base", '{"query":"湿疹"}', "c1")]},
        {"content": "", "tool_calls": [_call("search_knowledge_base", '{"query":"护理"}', "c2")]},
        {"content": "检索到保湿与避免搔抓的建议。", "tool_calls": []},
    ]

    def chat(*_a):
        return replies.pop(0)

    def execute(fn, args, sess, ctx):
        return {"ok": True, "citations": [{"citation_id": "RB-1", "text": "保湿"}]}

    res = _run(chat, execute=execute, schemas=[kb_schema], reflect=reflect)
    assert stages == ["retrieval", "final"]
    assert res.reflection["verdict"] == "pass"
    assert res.meta["budget"]["reflection_calls"] == 2


def test_raw_tool_result_never_leaks_into_trace_meta_or_reflection():
    marker = "RAW_PAYLOAD_SECRET_9f3"
    replies = [
        {"content": "", "tool_calls": [_call("get_case_status", "{}")]},
        {"content": "已完成", "tool_calls": []},
    ]

    def chat(*_a):
        return replies.pop(0)

    def execute(fn, args, sess, ctx):
        return {"ok": True, "raw": marker}

    schema = {"type": "function", "function": {
        "name": "get_case_status", "parameters": {"type": "object", "properties": {}}}}
    res = _run(chat, execute=execute, schemas=[schema])
    for blob in (json.dumps(res.tool_trace, ensure_ascii=False),
                 json.dumps(res.meta, ensure_ascii=False),
                 json.dumps(res.reflection, ensure_ascii=False)):
        assert marker not in blob
    assert set(res.reflection) == {"verdict", "status", "summary", "codes", "corrections",
                                   "checked_at", "model", "allowed_citations",
                                   "issue_count", "high_count", "stage"}


def test_budget_partial_is_never_reported_as_verified():
    def chat(*_a):
        return {"content": "", "tool_calls": [_call("get_case_status", "{}")]}

    schema = {"type": "function", "function": {
        "name": "get_case_status", "parameters": {"type": "object", "properties": {}}}}
    res = _run(chat, schemas=[schema], budget=rt.TurnBudget(max_steps=1, max_tokens=100000))
    assert res.meta["status"] == "partial"
    assert res.reflection["verdict"] == "unverified"
    assert res.reflection["status"] == "partial"


# —— 评审 HTTP 路径不得污染全局工具开关 ——

def test_critic_http_400_does_not_disable_tools(monkeypatch):
    """评审请求不带 tools：4xx 只是评审不可用，绝不能锁死全局工具开关。"""
    import urllib.error

    from server import agent_loop

    monkeypatch.setattr(agent_loop, "_TOOLS_UNSUPPORTED", False)

    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)

    monkeypatch.setattr(agent_loop.urllib.request, "urlopen", _raise)

    with pytest.raises(ConnectionError):
        agent_loop._post_chat("http://x", "k", {"model": "m"}, tools_matters=False)
    assert agent_loop._TOOLS_UNSUPPORTED is False

    with pytest.raises(agent_loop._ToolsUnsupported):
        agent_loop._post_chat("http://x", "k", {"model": "m"}, tools_matters=True)
    assert agent_loop._TOOLS_UNSUPPORTED is False   # 只有运行时拿到 ToolsUnsupported 才置位

