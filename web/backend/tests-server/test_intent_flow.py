# -*- coding: utf-8 -*-
"""确认卡闭环：拿不准只出卡不执行；医生点选后只执行所选意图；刷新后卡还在。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_MODEL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server.app import app  # noqa: E402

client = TestClient(app)

AMBIGUOUS = "这个病还要考虑哪些别的诊断"


def _case(text="患者男，35岁，右前臂红斑伴瘙痒7天"):
    r = client.post("/api/cases", json={"text": text, "demo": ""})
    assert r.status_code == 200
    return r.json()["case_id"]


@pytest.fixture
def fake_l2(monkeypatch):
    """把第二层替换成「不确定但有两个候选」，不依赖 BGE。"""
    import server.intent as intent_router

    def layer2(text, state=None):
        return {"intent": "differential", "confidence": 0.7, "margin": 0.01,
                "threshold": 0.82, "execute": False,
                "candidates": [["differential", 0.70], ["treatment_ref", 0.69]]}
    monkeypatch.setattr(intent_router, "layer2", layer2)
    return intent_router


@pytest.fixture
def fake_l2_single(monkeypatch):
    """单候选确认卡：医生回「确认」时应直接选中该候选（置信度 ≥ 0.75）。"""
    import server.intent as intent_router

    def layer2(text, state=None):
        return {"intent": "differential", "confidence": 0.80, "margin": 0.20,
                "threshold": 0.82, "execute": False,
                "candidates": [["differential", 0.80]]}
    monkeypatch.setattr(intent_router, "layer2", layer2)
    return intent_router


def _chat(case_id, text):
    r = client.post(f"/api/cases/{case_id}/chat", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_uncertain_input_only_issues_card(fake_l2):
    case_id = _case()
    before = client.get(f"/api/cases/{case_id}").json()
    snap = _chat(case_id, AMBIGUOUS)
    assert snap["pending_intent"] and snap["agent_state"] == "waiting_user"
    opts = [c["intent"] for c in snap["pending_intent"]["candidates"]]
    assert opts[:2] == ["differential", "treatment_ref"]
    assert snap["intent"]["needs_confirm"] is True
    # 未执行任何 handler：没有新草稿/文书，版本不变
    assert snap["result_version"] == before["result_version"]
    assert snap.get("doc") == before.get("doc")


def test_choose_executes_only_selected_intent(fake_l2):
    case_id = _case()
    snap = _chat(case_id, AMBIGUOUS)
    pid = snap["pending_intent"]["id"]
    r = client.post(f"/api/cases/{case_id}/intent/choose",
                    json={"intent": "differential", "pending_id": pid})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["task_type"] == "differential"
    assert out["pending_intent"] is None
    assert out["intent"]["source"] == "doctor"
    # 医学判断类先给计划确认（不直接产出内容），确认后才会调 LLM/降级
    assert out["workflow_state"] == "planning"


def test_choose_rejects_stale_or_unknown_option(fake_l2):
    case_id = _case()
    snap = _chat(case_id, AMBIGUOUS)
    pid = snap["pending_intent"]["id"]
    assert client.post(f"/api/cases/{case_id}/intent/choose",
                       json={"intent": "differential", "pending_id": "pi_wrong"}).status_code == 409
    assert client.post(f"/api/cases/{case_id}/intent/choose",
                       json={"intent": "report_export", "pending_id": pid}).status_code == 409
    # 失败的请求不得消费调用方的待确认状态
    assert client.get(f"/api/cases/{case_id}").json()["pending_intent"]["id"] == pid
    # 没有待确认时再选 → 409
    client.post(f"/api/cases/{case_id}/intent/choose", json={"intent": "differential", "pending_id": pid})
    assert client.post(f"/api/cases/{case_id}/intent/choose",
                       json={"intent": "differential", "pending_id": pid}).status_code == 409


def test_pending_survives_reload(fake_l2):
    case_id = _case()
    _chat(case_id, AMBIGUOUS)
    again = client.get(f"/api/cases/{case_id}").json()
    assert again["pending_intent"]["source_text"] == AMBIGUOUS
    assert again["pending_intent"]["id"].startswith("pi_")


def test_unrelated_message_clears_pending(fake_l2):
    case_id = _case()
    _chat(case_id, AMBIGUOUS)
    snap = _chat(case_id, "请整理现病史")
    assert snap["pending_intent"] is None
    assert snap["task_type"] == "history"
    assert snap["workflow_state"] == "planning"   # 文书任务先给计划确认


def test_requote_reroutes_with_merged_text(fake_l2):
    case_id = _case()
    first = _chat(case_id, AMBIGUOUS)
    second = _chat(case_id, "都不是，我是想让你总结一下")
    assert second["pending_intent"] is None or second["pending_intent"]["id"] != first["pending_intent"]["id"]


def test_no_candidates_falls_back_to_case_write_without_card():
    """无向量、无 LLM：不得出空卡卡死，回落 case_write 并如实标注降级。"""
    case_id = _case()
    snap = _chat(case_id, "今天天气不错")
    assert snap["pending_intent"] is None
    assert snap["intent"]["degraded"] is True
    assert snap["task_type"] == "case_write"


def test_confirm_selects_single_candidate(fake_l2_single):
    """有确认卡时「确认」必须先解析卡片，不能绕过卡片执行 case_write。"""
    case_id = _case()
    snap = _chat(case_id, AMBIGUOUS)
    assert [c["intent"] for c in snap["pending_intent"]["candidates"]] == ["differential"]
    out = _chat(case_id, "确认")
    assert out["pending_intent"] is None
    assert out["task_type"] == "differential"
    assert out["intent"]["source"] == "doctor"
    assert out["workflow_state"] == "planning"     # 医学判断类先给计划确认


def test_confirm_with_multiple_candidates_reissues_card(fake_l2):
    """多候选时「确认」不能代替选择：重发同一张卡，且不执行任何 handler。"""
    case_id = _case()
    first = _chat(case_id, AMBIGUOUS)
    second = _chat(case_id, "确认")
    assert second["pending_intent"]["id"] == first["pending_intent"]["id"]
    assert second["result_version"] == first["result_version"]
    assert second["task_type"] == first["task_type"]
    assert second.get("doc") == first.get("doc")


def test_cancel_card_executes_nothing(fake_l2):
    case_id = _case()
    before = _chat(case_id, AMBIGUOUS)
    out = _chat(case_id, "取消")
    assert out["pending_intent"] is None
    assert out["task_type"] == before["task_type"]
    assert out.get("doc") == before.get("doc")
    assert out.get("draft") == before.get("draft")
    assert out["intent"]["reason"] == "doctor_cancel"
    assert "已取消" in (out.get("reply") or {}).get("content", "")


def test_deny_during_planning_cancels_instead_of_building_plan(fake_l2):
    case_id = _case()
    planned = _chat(case_id, "做一下鉴别诊断")
    assert planned["workflow_state"] == "planning"
    out = _chat(case_id, "取消")
    assert out["intent"]["intent"] == "deny"
    assert out["workflow_state"] == "screening"
    assert "已取消" in (out.get("reply") or {}).get("content", "")
    assert out.get("doc") is None
    assert out["workflow_state"] != "planning"


def test_negated_control_never_executes_opposite():
    case_id = _case()
    out = _chat(case_id, "不要跳过3D")
    assert out["intent"]["intent"] != "skip_3d"
    assert "已跳过 3D" not in ((out.get("reply") or {}).get("content") or "")


def test_multi_action_sentence_not_collapsed():
    case_id = _case()
    out = _chat(case_id, "分析一下这张图，然后整理病历，再导出报告")
    assert out["intent"]["intent"] != "report_export"
    assert out["task_type"] != "report_export"


def test_every_turn_carries_intent_meta():
    case_id = _case()
    snap = _chat(case_id, "确认")
    assert snap["intent"] and "layer" in snap["intent"] and "latency_ms" in snap["intent"]
    # 流程推进短语不进三层路由（由既有确定性分支处理）
    assert snap["intent"]["source"] == "flow"
