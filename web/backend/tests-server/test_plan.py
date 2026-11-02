# -*- coding: utf-8 -*-
"""v76 显式计划：生命周期、版本递增、执行期工具白名单、旧会话兼容。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_MODEL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import plan as P  # noqa: E402
from server.app import app  # noqa: E402

client = TestClient(app)


# —— 纯函数：生命周期 ——

def test_lifecycle_proposed_approved_executing_completed():
    sess = {}
    plan = P.create(sess, "differential", missing=["病程"], action="整理" * 2, turn_id="trn_abc")
    assert plan["state"] == "proposed" and plan["schema_version"] == "1.0"
    assert plan["turn_id"] == "trn_abc"
    assert [s["tool"] for s in plan["steps"]] == P.tools_for("differential")
    assert all(s["status"] == "pending" for s in plan["steps"])
    # 未确认前不允许执行（白名单不生效也不放行写工具判断交给 _plan_gate）
    assert P.allowed_tools(sess) is None

    P.approve(sess)
    assert plan["state"] == "approved" and plan["approved_at"]
    assert P.allowed_tools(sess) == P.tools_for("differential")
    assert P.check_tool(sess, "get_case_status") is True
    assert P.check_tool(sess, "finish_supplement_and_analyze") is False

    P.record_trace(sess, [{"tool": "get_case_status", "status": "ok", "at": "t1"}])
    assert plan["state"] == "executing"
    done = [s for s in plan["steps"] if s["status"] == "done"]
    assert len(done) == 1 and done[0]["tool"] == "get_case_status"

    P.complete(sess)
    assert plan["state"] == "completed"
    assert all(s["status"] != "pending" for s in plan["steps"])


def test_failed_turn_marks_plan_failed_and_skips_pending():
    sess = {}
    P.create(sess, "history")
    P.approve(sess)
    P.record_trace(sess, [{"tool": "update_record_fields", "status": "error",
                           "error": "boom", "at": "t1"}])
    assert any(s["status"] == "failed" and s["error"] == "boom" for s in sess["plan_object"]["steps"])
    P.complete(sess, ok=False)
    assert sess["plan_object"]["state"] == "failed"
    assert all(s["status"] != "pending" for s in sess["plan_object"]["steps"])


def test_same_task_replans_bumps_version_and_resets_state():
    sess = {}
    P.create(sess, "history")
    P.approve(sess)
    again = P.create(sess, "history")
    assert again["version"] == 2 and again["state"] == "proposed"
    assert P.allowed_tools(sess) is None


def test_task_switch_supersedes_old_plan():
    sess = {}
    P.create(sess, "history")
    P.approve(sess)
    old = sess["plan_object"]
    P.supersede(sess)
    assert old["state"] == "superseded"
    new = P.create(sess, "differential")
    assert new["version"] == 1 and new["state"] == "proposed"


def test_cancel_and_supersede_are_terminal():
    sess = {}
    P.create(sess, "history")
    P.cancel(sess)
    assert sess["plan_object"]["state"] == "cancelled"
    P.approve(sess)
    assert sess["plan_object"]["state"] == "cancelled"   # 取消后不可复活
    assert P.allowed_tools(sess) is None


def test_legacy_session_without_plan_is_unrestricted():
    assert P.allowed_tools({}) is None
    assert P.check_tool({}, "finish_supplement_and_analyze") is True
    assert P.current({}) == {}
    assert P.record_trace({}, [{"tool": "x"}]) == {}
    assert P.complete({}) == {}


def test_write_tools_have_risk_labels():
    plan = P.create({}, "case_write")
    risks = {s["tool"]: s["risk"] for s in plan["steps"]}
    assert risks["update_record_fields"] == "write"
    assert risks["get_case_status"] == "read"


# —— API：计划卡与确认/取消闭环 ——

def _case(text="患者男，35岁，右前臂红斑伴瘙痒7天"):
    r = client.post("/api/cases", json={"text": text, "demo": ""})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _chat(case_id, text):
    r = client.post(f"/api/cases/{case_id}/chat", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def test_planning_snapshot_exposes_plan_object():
    case_id = _case()
    snap = _chat(case_id, "请整理现病史")
    assert snap["workflow_state"] == "planning"
    plan = snap["plan"]
    assert plan and plan["state"] == "proposed" and plan["task_type"] == "history"
    assert plan["steps"] and all(s["label"] for s in plan["steps"])
    assert snap["reply"]["plan"] is True           # 旧字段继续写入


def test_confirm_approves_and_executes_without_replanning():
    case_id = _case()
    first = _chat(case_id, "请整理现病史")
    pid = first["plan"]["plan_id"]
    out = _chat(case_id, "确认执行")
    assert out["workflow_state"] != "planning"
    assert out["plan"]["plan_id"] == pid
    assert out["plan"]["state"] in ("approved", "executing", "completed")


def test_cancel_plan_marks_cancelled_and_does_not_execute():
    case_id = _case()
    first = _chat(case_id, "做一下鉴别诊断")
    assert first["workflow_state"] == "planning"
    out = _chat(case_id, "取消")
    assert out["workflow_state"] != "planning"
    assert out["plan"]["state"] == "cancelled"
    assert out.get("doc") is None


def test_supplement_while_planning_replans_same_task():
    case_id = _case()
    first = _chat(case_id, "请整理现病史")
    assert first["plan"]["version"] == 1
    again = _chat(case_id, "补充现病史：无药物过敏史")
    assert again["workflow_state"] == "planning"
    assert again["plan"]["task_type"] == "history"
    assert again["plan"]["version"] == 2


def test_task_change_supersedes_and_restarts_version():
    case_id = _case()
    first = _chat(case_id, "请整理现病史")
    second = _chat(case_id, "做一下鉴别诊断")
    assert second["workflow_state"] == "planning"
    assert second["plan"]["task_type"] == "differential"
    assert second["plan"]["plan_id"] != first["plan"]["plan_id"]
    assert second["plan"]["version"] == 1


def test_plan_survives_reload():
    case_id = _case()
    first = _chat(case_id, "请整理现病史")
    reloaded = client.get(f"/api/cases/{case_id}").json()
    assert reloaded["plan"]["plan_id"] == first["plan"]["plan_id"]
