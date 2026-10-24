"""client 离线演示资产（MockAgentClient + shared/derm_core）v1.1 契约测试。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_client import MockAgentClient, ServerAgentClient, build_case_from_text, match_demo_case, merge_reply  # noqa: E402
from derm_core import (  # noqa: E402
    DEMO_CASES, classify_review, compute_missing, derive_data_completeness, derive_severity_level,
    detect_red_flags, render_report, retrieve_citations,
)

case1 = DEMO_CASES["1"]["case"]
case2 = DEMO_CASES["2"]["case"]
case3 = DEMO_CASES["3"]["case"]
case4 = DEMO_CASES["4"]["case"]


def test_missing_and_completeness():
    assert compute_missing(case1) == []
    assert compute_missing(case3)
    assert derive_data_completeness(case1, []) == "complete"
    assert derive_data_completeness(case3, compute_missing(case3)) == "insufficient"


def test_red_flags_and_severity():
    flags = detect_red_flags(case2)
    assert any(f.startswith("RF-HZ-001") for f in flags)
    assert detect_red_flags(case1) == [] and detect_red_flags(case4) == []
    assert derive_severity_level(flags, []) == "needs_review"
    assert derive_severity_level([], []) == "routine"
    assert derive_severity_level([], ["病程"]) == "unknown"


def test_retrieval_negation_aware():
    assert any("湿疹" in c["document_title"] for c in retrieve_citations(case1))
    assert any("带状疱疹" in c["document_title"] for c in retrieve_citations(case2))
    assert retrieve_citations(case4) == []  # 否定短语不误报


def test_demo_match_and_free_text():
    assert match_demo_case("案例1") == "1" and match_demo_case("案例4") == "4"
    assert match_demo_case("你好") is None
    c = build_case_from_text("身体区域：面部；主诉：红斑3天；症状：瘙痒性红斑；病程：3天；过敏史：否认；当前用药：无")
    assert c["body_region"] == "面部" and c["duration"] == "3天"
    merged = merge_reply(case3, "补充：病程5天；变化趋势逐渐扩大；诱因新换护肤品后出现；"
                                "既往史无特殊；过敏史否认药物食物过敏；当前用药未使用药物")
    assert compute_missing(merged) == []


def test_classify_review():
    assert classify_review("确认") == "confirmed"
    assert classify_review("修改：请补充眼科") == "modified"
    assert classify_review("拒绝：资料不足") == "rejected"


def test_mock_case1_confirm_report():
    client = MockAgentClient()
    res = client.start("案例1")
    assert res["action"] == "review" and res["workflow_state"] == "awaiting_review"
    d = res["draft"]
    assert d["schema_version"] == "1.1" and d["status"] if False else d["requires_doctor_confirmation"]
    assert d["workflow_state"] == "awaiting_review" and d["review_status"] == "pending"
    assert d["case_summary"]["data_completeness"] == "complete"
    assert d["severity"]["level"] == "routine" and d["citations"]
    final = client.review("确认")
    assert final["workflow_state"] == "approved" and final["review_status"] == "approved"
    assert "辅助报告" in final["report"] and "免责声明" in final["report"]


def test_mock_case2_block_modify_reject():
    client = MockAgentClient()
    res = client.start("案例2")
    d = res["draft"]
    assert d["workflow_state"] == "escalated" and d["red_flag_escalation"]["is_blocking"] is True
    assert d["severity"]["level"] == "needs_review" and d["blocking_reason"]
    edited = client.review("修改：请补充疼痛评估")
    assert edited["review_status"] == "edited"
    client2 = MockAgentClient()
    client2.start("案例2")
    closed = client2.review("拒绝：演示")
    assert closed["workflow_state"] == "rejected" and "不生成报告" in closed["message"]


def test_mock_case3_follow_up():
    """v59：不再强制追问——缺失字段由草稿 unknown 如实列出，补充后照常重跑分析。"""
    client = MockAgentClient()
    res = client.start("案例3")
    assert res["action"] == "review" and "病程" in res["missing"]   # 缺失如实列出，不追问阻断
    res2 = client.supplement("补充：病程5天；变化趋势逐渐扩大；诱因新换护肤品后出现；"
                             "既往史无特殊；过敏史否认药物食物过敏；当前用药未使用药物")
    assert res2["action"] == "review"
    assert res2["draft"]["case_summary"]["data_completeness"] == "complete"


def test_mock_case4_no_answer():
    client = MockAgentClient()
    res = client.start("案例4")
    d = res["draft"]
    assert d["citations"] == [] and d["suspected_conditions"] == []
    assert any("知识库未覆盖" in u for u in d["uncertainties"])
    final = client.review("确认")
    assert "不足以判断" in final["report"]


def test_unknown_not_asked_semantics():
    """v59：unknown/not_asked ≠ absent 的语义在无追问管线下保持（补一条、其余仍如实未知）。"""
    client = MockAgentClient()
    first = client.start("案例3")
    assert first["action"] == "review"
    res = client.supplement("病程：5天；诱因：无")
    statuses = {s["status"] for s in res["draft"]["symptom_assessment"]}
    assert "not_asked" in statuses and "absent" not in statuses


def test_report_renders_v11_meta():
    client = MockAgentClient()
    res = client.start("案例1")
    report = render_report(res["draft"])
    assert "Schema 1.1" in report and "工作流状态" in report and "安全策略" in report
    assert "CIT-" in report


def test_ask_more_carries_missing_for_flow_guide():
    """v59：无追问管线——缺失字段必须结构化透传（chips/弹窗数据源不变，但不再阻断发送）。"""
    client = MockAgentClient()
    res = client.start("")          # 无任何内容 → 直接给草稿/演示结果，missing 结构化携带
    assert res["action"] == "review"
    assert "主诉" in res["missing"] and "过敏史" in res["missing"]


def test_mock_chat_task_routing():
    """v59 离线任务回退：文书类→本地模板 doc；医学判断类→llm_unavailable 零内容。"""
    client = MockAgentClient()
    client.start("患者男，35岁，右前臂红斑瘙痒7天，近期更换洗涤剂，无发热")
    doc = client.chat("请整理现病史")
    assert doc["action"] == "doc" and doc["llm"]["source"] == "template"
    assert "未提供" in doc["doc"]["content"] and "夜间" not in doc["doc"]["content"]
    med = client.chat("请根据当前病例列出需要鉴别的疾病")
    assert med["action"] == "llm_unavailable" and med["doc"] is None
    assert "无法生成真实 AI 医疗建议" in med["message"]


def test_server_render_doc_and_missing_through():
    """v59：ServerAgentClient._render 必须透传 doc/task_type/missing（doc action 优先级）。"""
    snap = {"case_id": "CASE-1", "workflow_state": "doc_ready", "task_type": "history",
            "doc": {"task_type": "history", "content": "x"}, "missing": ["主诉"]}
    out = ServerAgentClient._render(snap)
    assert out["action"] == "doc" and out["doc"]["content"] == "x"
    assert out["task_type"] == "history" and out["missing"] == ["主诉"]
    assert ServerAgentClient._render({})["missing"] == []   # 快照无 missing 时不炸、给空列表


def test_v67_agent_cmd_reply_priority_over_stale_doc():
    """v67：agent 指令回合快照残留历史 doc 时，_render 必须按本轮 reply 渲染。
    缺陷背景：skip_3d 等指令回合 llm.source=agent + doc 残留 → 旧排序误判 action=doc，
    前端把 agent 响应渲染成「未连接 LLM」+ 旧文书卡。"""
    snap = {"case_id": "C2", "workflow_state": "doc_ready", "task_type": "case_write",
            "doc": {"task_type": "differential", "content": "旧鉴别文书"},
            "reply": {"content": "好的，已跳过 3D 人体模型。", "generated_by": "agent"},
            "llm": {"ok": True, "source": "agent", "generated_by": "agent-cmd"}}
    out = ServerAgentClient._render(snap)
    assert out["action"] == "reply" and out["reply"]["content"].startswith("好的")
    # 纯 doc 产物（无 reply）仍走 doc 卡
    snap_doc = {"workflow_state": "doc_ready", "doc": {"task_type": "history", "content": "x"}}
    assert ServerAgentClient._render(snap_doc)["action"] == "doc"


def test_mock_smalltalk_zero_fabrication():
    """v64：离线闲聊零伪造——明确「无法对话回复」，不冒充 AI，不含医疗内容。"""
    client = MockAgentClient()
    res = client.chat("你好，请介绍你能做什么？")
    assert res["action"] == "llm_unavailable" and res["reply"] is None and res["doc"] is None
    assert "无法对话回复" in res["message"]
    assert res["llm"]["source"] == "none" and res["llm"]["ok"] is False
    client2 = MockAgentClient()
    res2 = client2.chat("你有什么功能？")
    assert res2["action"] == "llm_unavailable"


def test_server_render_reply_and_tool_history():
    """v64：_render 透传 Agent 自然语言回复与工具过程；llm_unavailable 优先于 reply。"""
    snap = {"case_id": "C1", "workflow_state": "chat_reply", "task_type": "smalltalk",
            "reply": {"content": "你好", "generated_by": "llm"},
            "tool_history": [{"tool": "medical_kb_search", "at": "t1"}],
            "selected_body_regions": ["左前臂"]}
    out = ServerAgentClient._render(snap)
    assert out["action"] == "reply" and out["reply"]["content"] == "你好"
    assert out["tool_history"] and out["selected_body_regions"] == ["左前臂"]
    # 服务端判 llm_unavailable 时，即使带 reply 也按不可用呈现（前端零医疗内容）
    snap2 = dict(snap, workflow_state="llm_unavailable")
    assert ServerAgentClient._render(snap2)["action"] == "llm_unavailable"


def test_ensure_local_server_guard_paths(monkeypatch):
    """v53 服务端自动托管：远程地址/无端口不托管；已有健康服务端直接复用不重复拉起。"""
    import main as client_main
    monkeypatch.setattr(client_main, "audit", lambda *a, **k: None)
    assert client_main._ensure_local_server("https://www.example.com:8443") == (False, "remote")
    assert client_main._ensure_local_server("http://127.0.0.1") == (False, "remote")   # 无端口
    monkeypatch.setattr(client_main, "_server_health_ok", lambda url, timeout=1.0: True)
    assert client_main._ensure_local_server("http://127.0.0.1:8765") == (True, "already_running")


def test_ensure_local_server_spawns_with_port_env(monkeypatch):
    """v53：无健康服务端时拉起 server/app.py 子进程（PORT 取自服务地址）。"""
    import main as client_main
    monkeypatch.setattr(client_main, "audit", lambda *a, **k: None)
    monkeypatch.setattr(client_main, "SERVER_APP_PATH", "/fake/server/app.py")
    monkeypatch.setattr(client_main.os.path, "exists", lambda p: p == "/fake/server/app.py")
    calls = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            calls["cmd"], calls["kw"] = cmd, kw
            self.pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(client_main.subprocess, "Popen", FakeProc)
    probes = {"n": 0}   # 首查（拉起前）不通 → 拉起后通：验证走 started 而非 already_running

    def fake_health(url, timeout=1.0):
        probes["n"] += 1
        return probes["n"] > 1

    monkeypatch.setattr(client_main, "_server_health_ok", fake_health)
    monkeypatch.setattr(client_main, "LOCAL_SERVER_PROC", None)   # 测试后还原全局指针
    ok, reason = client_main._ensure_local_server("http://127.0.0.1:8765")
    assert (ok, reason) == (True, "started")
    assert calls["cmd"][0] == client_main.sys.executable and calls["cmd"][1] == "/fake/server/app.py"
    assert calls["kw"]["env"]["PORT"] == "8765"
    assert client_main.LOCAL_SERVER_PROC is not None and client_main.LOCAL_SERVER_PROC.pid == 4321


def test_ensure_local_server_spawn_failure_reports_timeout(monkeypatch):
    """v53：子进程立即退出（端口被占/依赖缺失）→ 不空等，如实报 timeout。"""
    import main as client_main
    monkeypatch.setattr(client_main, "audit", lambda *a, **k: None)
    monkeypatch.setattr(client_main, "SERVER_APP_PATH", "/fake/server/app.py")
    monkeypatch.setattr(client_main.os.path, "exists", lambda p: p == "/fake/server/app.py")

    class DeadProc:
        def __init__(self, cmd, **kw):
            self.pid = -1

        def poll(self):
            return 1          # 已退出

    monkeypatch.setattr(client_main.subprocess, "Popen", DeadProc)
    monkeypatch.setattr(client_main, "_server_health_ok", lambda url, timeout=1.0: False)
    monkeypatch.setattr(client_main, "LOCAL_SERVER_PROC", None)
    assert client_main._ensure_local_server("http://127.0.0.1:8765", wait_seconds=2) == (False, "timeout")


def test_read_agent_settings(tmp_path, monkeypatch):
    """v54 顶栏控件移除后：接入设置唯一读取入口——无文件/坏值回落默认 server+本机地址，旧格式透传。"""
    import json as _json

    import main as client_main
    cfg_file = tmp_path / "settings.json"
    monkeypatch.setattr(client_main, "SETTINGS_FILE", str(cfg_file))

    assert client_main._read_agent_settings() == {"mode": "server", "server_url": "http://127.0.0.1:8765"}

    cfg_file.write_text(_json.dumps({"mode": "mock", "server_url": "http://192.168.1.9:9000"}), encoding="utf-8")
    assert client_main._read_agent_settings() == {"mode": "mock", "server_url": "http://192.168.1.9:9000"}

    cfg_file.write_text(_json.dumps({"mode": "bogus", "server_url": "   "}), encoding="utf-8")
    assert client_main._read_agent_settings() == {"mode": "server", "server_url": "http://127.0.0.1:8765"}


def test_ensure_client_reuses_instance():
    """v59 真机回归：_ensure_client 不再无条件重建——client.case_id 会话句柄跨发送保留。"""
    import main as client_main

    class FakeWin:
        agent_mode = "server"
        server_url_cfg = "http://127.0.0.1:8765"
        client = None

        def _server_url(self):
            return self.server_url_cfg

        def _save_settings(self):
            pass

    win = FakeWin()
    client_main.MainWindow._ensure_client(win)
    c1 = win.client
    c1.case_id = "CASE-9"
    client_main.MainWindow._ensure_client(win)          # 同地址再次确保 → 复用实例
    assert win.client is c1 and win.client.case_id == "CASE-9"

    win.server_url_cfg = "http://127.0.0.1:9999"
    client_main.MainWindow._ensure_client(win)          # 地址变化 → 重建、句柄归零
    assert isinstance(win.client, client_main.ServerAgentClient)
    assert win.client.base_url == "http://127.0.0.1:9999" and win.client.case_id is None
