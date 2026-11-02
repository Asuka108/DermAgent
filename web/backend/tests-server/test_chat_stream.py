# -*- coding: utf-8 -*-
"""v76 流式对话：事件顺序、done 唯一、与阻塞接口快照一致、断线重连、同病例并发 409。"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_MODEL"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from server import agent_runtime as rt  # noqa: E402
from server.app import app  # noqa: E402

client = TestClient(app)

VOLATILE = {"turn_id", "plan_id", "created_at", "at", "updated_at", "message_id", "case_id",
            "seq", "version", "result_version", "started_at", "ended_at", "approved_at",
            "patient", "memory", "nav", "nav_intent", "ui_action", "open_case", "messages",
            "turn_products", "msg_artifacts", "artifacts", "available_actions",
            "result_summary", "authenticity", "next_action", "token", "elapsed_ms"}


def _case(text="患者男，35岁，右前臂红斑伴瘙痒7天"):
    r = client.post("/api/cases", json={"text": text, "demo": ""})
    assert r.status_code == 200, r.text
    return r.json()["case_id"]


def _stream(case_id, payload):
    events, turn_id = [], ""
    with client.stream("POST", f"/api/cases/{case_id}/chat/stream", json=payload) as resp:
        assert resp.status_code == 200, resp.read()
        turn_id = resp.headers.get("x-turn-id", "")
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                events.append((event, data))
    return events, turn_id


def _types(events):
    return [e for e, _ in events if e != "heartbeat"]


def _start_stream(payload):
    events = []
    with client.stream("POST", "/api/cases/stream", json=payload) as resp:
        assert resp.status_code == 200, resp.read()
        case_id = resp.headers.get("x-case-id", "")
        event = ""
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                events.append((event, json.loads(line.split(":", 1)[1].strip())))
    return events, case_id


def _normalize(value):
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def test_stream_event_order_and_single_done():
    case_id = _case()
    events, turn_id = _stream(case_id, {"text": "请整理现病史"})
    types = _types(events)
    assert turn_id.startswith("trn_")
    assert types[0] == "turn_started"
    assert types[-1] == "done" and types.count("done") == 1
    assert types.index("progress") < types.index("plan_ready") < types.index("done")
    prog = [d for e, d in events if e == "progress"]
    assert any(p.get("phase") == "fallback" for p in prog)     # 未配置 LLM 时如实提示降级
    plan = [d for e, d in events if e == "plan_ready"][0]["plan"]
    assert plan["state"] == "proposed" and plan["task_type"] == "history"
    assert plan["turn_id"] == turn_id                          # 计划可追溯到本轮 turn
    done = [d for e, d in events if e == "done"][0]["snapshot"]
    assert done["plan"]["plan_id"] == plan["plan_id"]


def test_stream_matches_blocking_snapshot():
    stream_case = _case()
    events, _ = _stream(stream_case, {"text": "请整理现病史"})
    streamed = [d for e, d in events if e == "done"][0]["snapshot"]

    block_case = _case()
    r = client.post(f"/api/cases/{block_case}/chat", json={"text": "请整理现病史"})
    assert r.status_code == 200
    blocked = r.json()

    assert _normalize(streamed) == _normalize(blocked)


def test_turns_endpoint_replays_after_disconnect():
    case_id = _case()
    events, turn_id = _stream(case_id, {"text": "请整理现病史"})
    total = len([e for e in _types(events) if e != "turn_started"])   # turn_started 由连接本地生成
    full = client.get(f"/api/cases/{case_id}/turns/{turn_id}").json()
    assert full["done"] is True and full["state"] == "done"
    assert full["snapshot"]["plan"]["task_type"] == "history"
    assert len([e for e in full["events"] if e["type"] != "heartbeat"]) == total
    mid = full["events"][2]["seq"]
    tail = client.get(f"/api/cases/{case_id}/turns/{turn_id}?after_seq={mid}").json()
    assert all(e["seq"] > mid for e in tail["events"])


def test_turns_endpoint_rejects_unknown_or_foreign_turn():
    case_id = _case()
    assert client.get(f"/api/cases/{case_id}/turns/trn_missing").status_code == 404
    _events, turn_id = _stream(case_id, {"text": "请整理现病史"})
    other = _case()
    assert client.get(f"/api/cases/{other}/turns/{turn_id}").status_code == 404


def test_same_case_concurrent_turn_returns_409():
    case_id = _case()
    reg = rt.registry()
    running, created = reg.begin(case_id, "other-client")
    assert created
    try:
        r = client.post(f"/api/cases/{case_id}/chat/stream", json={"text": "请整理现病史"})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "turn_in_progress"
    finally:
        reg.finish(running.turn_id)


def test_client_turn_id_is_idempotent():
    case_id = _case()
    first, tid1 = _stream(case_id, {"text": "请整理现病史", "client_turn_id": "cid-1"})
    second, tid2 = _stream(case_id, {"text": "请整理现病史", "client_turn_id": "cid-1"})
    assert tid1 == tid2
    assert [d for e, d in second if e == "done"][0]["snapshot"]["plan"]["plan_id"] == \
        [d for e, d in first if e == "done"][0]["snapshot"]["plan"]["plan_id"]
    state = rt.registry().get(tid1)
    assert state is not None and state.done_event.is_set()


def test_stream_can_be_disabled(monkeypatch):
    monkeypatch.setenv("DERM_AGENT_STREAM", "0")
    case_id = _case()
    r = client.post(f"/api/cases/{case_id}/chat/stream", json={"text": "请整理现病史"})
    assert r.status_code == 404


def test_different_cases_are_not_serialized():
    import threading

    ids = [_case(), _case()]
    results = {}

    def run(cid):
        results[cid] = _stream(cid, {"text": "请整理现病史"})[1]

    threads = [threading.Thread(target=run, args=(cid,)) for cid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(results.get(cid, "").startswith("trn_") for cid in ids)
    assert len(set(results.values())) == 2


def test_stream_exposes_turn_header_for_browser_reconnect():
    """file:// 页面跨源读取 X-Turn-Id 需 CORS expose，否则断线重连拿不到 turn。"""
    case_id = _case()
    with client.stream("POST", f"/api/cases/{case_id}/chat/stream",
                       json={"text": "请整理现病史"},
                       headers={"Origin": "null"}) as resp:
        assert resp.status_code == 200
        exposed = resp.headers.get("access-control-expose-headers", "").lower()
        assert "x-turn-id" in exposed


def test_stream_reflection_event_precedes_done_and_survives_replay():
    case_id = _case()
    events, turn_id = _stream(case_id, {"text": "请整理现病史"})
    types = _types(events)
    assert "reflection" in types
    assert types.index("reflection") < types.index("done")
    full = client.get(f"/api/cases/{case_id}/turns/{turn_id}").json()
    replayed = [e["type"] for e in full["events"] if e["type"] != "heartbeat"]
    assert replayed.index("reflection") < replayed.index("done")


def test_plan_turn_emits_no_message_delta():
    """计划卡是结构化产物：不产生正文增量，避免前端出现空流式气泡。"""
    case_id = _case()
    events, _ = _stream(case_id, {"text": "请整理现病史"})
    done = [d for e, d in events if e == "done"][0]["snapshot"]
    assert done["plan"]["state"] == "proposed"
    assert not [d for e, d in events if e == "message_delta"]


def test_first_turn_stream_without_llm_never_fakes_tokens():
    """未配置 LLM 时只有一条权威终稿（或没有正文），绝不伪造逐字增量。"""
    events, case_id = _start_stream({"text": "患者男，35岁，右前臂红斑伴瘙痒7天"})
    assert case_id
    types = [e for e, _d in events if e != "heartbeat"]
    assert types.count("done") == 1
    deltas = [d for e, d in events if e == "message_delta"]
    assert not [d for d in deltas if not d.get("final") and not d.get("reset")]
    finals = [d for d in deltas if d.get("final")]
    assert len(finals) <= 1
    if finals:
        assert finals[0]["source"] == "final"
        done = [d for e, d in events if e == "done"][0]["snapshot"]
        assert finals[0]["text"] == done["reply"]["content"]


def test_agent_finalize_persists_verification_and_next_turn_clears_it():
    import server.app as sa

    case_id = _case()
    sess = sa._load_sess(case_id)
    sess["_turn_reflection"] = {
        "verdict": "pass", "status": "ok", "summary": "已核验通过", "codes": [],
        "corrections": 0, "checked_at": "2026-11-01T00:00:00+00:00", "model": "m",
        "allowed_citations": []}
    sess["_turn_citations"] = []
    snap = sa._agent_finalize(sess, "继续", "建议保湿。见 CIT-unknown-1。", [], "m")
    assert snap["verification"]["verdict"] == "corrected"       # 白名单外引用被删除
    assert "CIT-unknown-1" not in snap["reply"]["content"]
    mid = snap["message_id"]
    sa._store_sess(sess)

    later = client.get(f"/api/cases/{case_id}").json()
    stored = (later.get("msg_artifacts") or {})[mid]["verification"]
    assert stored["verdict"] == "corrected"
    assert stored == later["verification"]                      # 同一轮快照与该消息存档一致

    r = client.post(f"/api/cases/{case_id}/chat", json={"text": "你好"}).json()
    assert r.get("verification") is None                        # 新一轮不继承上一轮结论
    later = client.get(f"/api/cases/{case_id}").json()
    assert (later.get("msg_artifacts") or {}).get(mid, {}).get("verification") == stored
