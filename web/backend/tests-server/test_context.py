# -*- coding: utf-8 -*-
"""v76 统一上下文：安全层永不裁剪、裁剪顺序、摘要 v2 滚动、消息上限、旧摘要兼容。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_MODEL"] = ""

from server import context as C  # noqa: E402


def _sess(n=0, content="观察记录", **extra):
    sess = {"case_id": "", "case": {"chief_complaint": "右前臂红斑", "allergies": "青霉素"},
            "red_flags": ["伴发热"], "record": {"meta": {"present_illness": {"source": "doctor_modified"}}},
            "messages": [{"seq": i + 1, "role": "user" if i % 2 == 0 else "assistant",
                          "content": f"{content}{i}"} for i in range(n)],
            "msg_seq": n}
    sess.update(extra)
    return sess


def test_estimate_tokens_cjk_and_ascii():
    assert C.estimate_tokens("") == 0
    assert C.estimate_tokens("右前臂红斑") == 6
    assert C.estimate_tokens("abcdefg") >= 1


def test_doc_context_keeps_safety_layer_under_tiny_budget():
    sess = _sess(30, content="很长的对话内容" * 40)
    sess["tool_history"] = [{"tool": "search_knowledge_base", "goal": "检索" * 20,
                             "output_digest": "摘要" * 60, "risk_level": "low"}]
    sess["book_citations"] = [{"citation_id": "C1"}]
    bundle = C.build(sess, purpose="doc", budget_tokens=200)
    text = bundle.render()
    assert "【安全上下文（永不裁剪）】" in text
    assert "青霉素" in text and "伴发热" in text and "present_illness" in text
    assert "【病例字段】" in text
    assert bundle.tokens <= 260          # 硬上限附近（留出安全层原文的估算余量）


def test_trim_order_tools_then_rag_then_dialog():
    sess = _sess(20, content="对话" * 300)
    sess["tool_history"] = [{"tool": "t", "goal": "g" * 100, "output_digest": "d" * 200,
                             "risk_level": "low"} for _ in range(5)]
    sess["book_citations"] = [{"citation_id": "CIT-" + "0" * 20} for _ in range(8)]
    tight = C.build(sess, purpose="doc", budget_tokens=220)
    assert not tight.tools_text and not tight.rag_text
    assert "【最近对话】" in tight.render()          # 对话优先于工具/RAG 保留
    loose = C.build(sess, purpose="doc", budget_tokens=12000)
    assert loose.tools_text and loose.rag_text


def test_agent_context_has_safety_but_not_case_duplicate():
    bundle = C.build(_sess(4), purpose="agent")
    assert "【安全上下文（永不裁剪）】" in bundle.header
    assert "【病例字段】" not in bundle.header
    assert len(bundle.history) == 4
    assert bundle.history[-1]["content"] == "观察记录3"


def test_legacy_summary_without_coverage_keeps_history():
    sess = _sess(6, context_summary={"current_task": "case_write", "generated_by": "local-summary"})
    bundle = C.build(sess, purpose="doc")
    assert len(bundle.history) == 6
    assert "【病例摘要" in bundle.header


def test_summary_v2_rolling_versions_and_coverage():
    sess = _sess(40)
    assert C.update_summary(sess) is True
    first = sess["context_summary"]
    assert first["schema_version"] == "2.0" and first["version"] == 1
    assert first["covered_upto_seq"] == 20          # 保留最近 20 条原文
    assert first["generated_by"] == "rule"
    assert first["allergy_and_risk_flags"]["allergies"] == "青霉素"
    assert first["confirmed_facts"]["chief_complaint"] == "右前臂红斑"
    assert first["doctor_edits"] == ["present_illness"]
    assert first["decisions"]

    sess["messages"].extend({"seq": i + 41, "role": "user", "content": f"新增{i}"} for i in range(10))
    assert C.update_summary(sess, force=True) is True
    second = sess["context_summary"]
    assert second["version"] == 2
    assert second["covered_from_seq"] > first["covered_upto_seq"]
    assert second["covered_upto_seq"] > first["covered_upto_seq"]


def test_summary_below_threshold_is_not_generated():
    sess = _sess(5)
    assert C.update_summary(sess) is False
    assert "context_summary" not in sess


def test_llm_summary_failure_keeps_rule_version(monkeypatch):
    monkeypatch.setenv("DERM_SUMMARY_LLM", "1")
    monkeypatch.setattr(C, "_llm_narrative", lambda *a, **k: (None, "timeout"))
    sess = _sess(40)
    C.update_summary(sess, llm=("http://x", "k", "m"))
    s = sess["context_summary"]
    assert s["generated_by"] == "rule" and s["llm_error"] == "timeout"


def test_llm_summary_success_marks_generated_by_llm(monkeypatch):
    monkeypatch.setenv("DERM_SUMMARY_LLM", "1")
    monkeypatch.setattr(C, "_llm_narrative", lambda *a, **k: ("较早对话概述（规则事实不变）", ""))
    sess = _sess(40)
    C.update_summary(sess, llm=("http://x", "k", "m"))
    s = sess["context_summary"]
    assert s["generated_by"] == "llm" and s["narrative"]
    assert "较早对话概述" in C.summary_block(s)


def test_message_cap_summarizes_then_trims():
    from server import app as app_mod

    sess = _sess(0)
    for i in range(app_mod.ctx_mod.MAX_MESSAGES + 5):
        app_mod._log_msg(sess, "user" if i % 2 == 0 else "assistant", f"第{i}条消息")
    assert len(sess["messages"]) <= app_mod.ctx_mod.SUMMARY_KEEP_RECENT + 5
    assert sess["context_summary"]["covered_upto_seq"] > 0
    assert sess["msg_seq"] == app_mod.ctx_mod.MAX_MESSAGES + 5      # seq 不回退
    assert sess["msg_trimmed"] > 0
    # 删除后仍能继续追加，且安全层与摘要可继续使用
    app_mod._log_msg(sess, "user", "追加一条")
    assert sess["messages"][-1]["seq"] == sess["msg_seq"]
    text = C.build(sess, purpose="doc").render()
    assert "青霉素" in text and "【病例摘要" in text


def test_build_skips_messages_covered_by_summary():
    sess = _sess(40)
    C.update_summary(sess, force=True)
    covered = sess["context_summary"]["covered_upto_seq"]
    bundle = C.build(sess, purpose="doc")
    assert bundle.history
    assert int(bundle.history[-1]["content"].replace("观察记录", "")) >= covered
    assert "观察记录0" not in [m["content"] for m in bundle.history]
