# -*- coding: utf-8 -*-
"""提示词注入：档案/情景只作参考、热窗口替换历史、快照元数据如实降级。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_MODEL"] = ""

import pytest  # noqa: E402

from server import agent_loop  # noqa: E402
from server import app as server_app  # noqa: E402
from server.memory import inject  # noqa: E402


def _profile_text():
    return inject.render_profile([
        {"fact_id": 1, "category": "allergy", "field": "allergen",
         "value": "青霉素", "source": "doctor_modified"}])


def _episodic_text():
    return inject.render_episodic([
        {"scope": "case", "title": "湿疹", "text": "上次渗出处理", "date": "2026-10-01"}])


def _sess():
    return {"case_id": "c1", "phase": "screening", "workflow_state": "screening",
            "case": {"chief_complaint": "面部红斑"}, "record": {"fields": {}},
            "messages": [{"role": "user", "content": "旧消息不该进窗口"},
                         {"role": "assistant", "content": "旧回复"}]}


def test_system_prompt_injects_profile_and_episodic_as_reference():
    ctx = {"profile_text": _profile_text(), "episodic_text": _episodic_text()}
    prompt = agent_loop._system_prompt(_sess(), ctx)
    assert "青霉素" in prompt and "上次渗出处理" in prompt
    assert "不改变当前病例事实" in prompt
    assert "禁止自动处方" in prompt


def test_run_agent_turn_uses_memory_window(monkeypatch):
    captured = {}

    def fake_chat(base, key, model, messages, effort):
        captured["messages"] = messages
        return {"content": "好的", "tool_calls": []}

    monkeypatch.setattr(agent_loop, "_chat_tools", fake_chat)
    window = [{"role": "user", "content": "窗口里的医生话"},
              {"role": "assistant", "content": "窗口里的助手话"}]
    ctx = {"llm": ("http://x", "k", "m", ""), "memory_window": window,
           "profile_text": "", "episodic_text": "",
           "finalize": lambda sess, text, final, trace, model: {"ok": True}}
    assert agent_loop.run_agent_turn(_sess(), "本轮问题", ctx) == {"ok": True}
    contents = [m.get("content") for m in captured["messages"]]
    assert "窗口里的医生话" in contents
    assert "旧消息不该进窗口" not in contents
    assert contents[-1] == "本轮问题"


def test_build_context_adds_memory_layers_but_keeps_safety():
    sess = _sess()
    sess["case"]["allergies"] = "青霉素"
    sess["_memory_ctx"] = {"profile_text": inject.render_profile([
        {"fact_id": 1, "category": "past_history", "field": "disease",
         "value": "湿疹", "source": "doctor_modified"}]),
        "episodic_text": inject.render_episodic([
            {"scope": "doctor", "title": "处置", "text": "渗出处理原则",
             "date": "2026-10-01"}]),
        "window": [{"role": "user", "content": "窗口对话"}],
        "meta": {}}
    ctx = server_app._build_context(sess)
    assert "既往史·疾病：湿疹" in ctx and "渗出处理原则" in ctx
    assert "青霉素" in ctx                        # 安全层仍在
    assert "不改变当前病例事实" in ctx


def test_build_context_does_not_re_truncate_memory_layers():
    """渲染层已按预算裁剪并带「已截断」标记；上下文层再切片会静默吞掉标记。"""
    facts = [{"fact_id": i, "category": "allergy", "field": "allergen",
              "value": "药" * 200, "source": "llm_extract"} for i in range(10)]
    profile = inject.render_profile(facts, 800)
    assert "已截断" in profile and len(profile) <= 800
    sess = _sess()
    sess["_memory_ctx"] = {"profile_text": profile, "episodic_text": "", "meta": {}}
    assert "已截断" in server_app._build_context(sess)


def test_snapshot_memory_meta_reports_degradation(monkeypatch):
    monkeypatch.setenv("DERM_MEMORY_DISABLED", "1")
    from server import memory
    memory.reset()
    sess = {"case_id": "c1", "phase": "screening", "messages": [], "case": {}}
    meta = memory.snapshot_meta(sess, has_patient=False)
    assert meta["degraded"] == {"short_term": True, "profile": True,
                                "episodic": True, "worker": True}
    assert meta["profile_facts"] == 0 and meta["episodic_hits"] == 0
    monkeypatch.setenv("DERM_MEMORY_DISABLED", "0")
    meta = memory.snapshot_meta({**sess, "memory_meta": {
        "profile_facts": 3, "episodic_hits": 2, "latency_ms": 12.5,
        "truncated": {"profile": True}, "degraded": {}}}, has_patient=True)
    assert meta["profile_facts"] == 3 and meta["episodic_hits"] == 2
    assert meta["truncated"]["profile"] is True
    assert meta["degraded"]["worker"] is True    # worker 未启动：如实标注
