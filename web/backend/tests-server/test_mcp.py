# -*- coding: utf-8 -*-
"""v77 MCP 只读桥：开关、令牌、工具白名单、病例绑定、审计。"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_MODEL"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from server import mcp_bridge  # noqa: E402
from server.app import AUDIT_FILE, app  # noqa: E402

client = TestClient(app)


def _rpc(method, params=None, req_id=1, headers=None):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers or {})


def _case() -> str:
    r = client.post("/api/cases", json={"text": "患者男，35岁，右前臂红斑伴瘙痒7天"})
    assert r.status_code == 200
    return r.json()["case_id"]


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DERM_MCP", raising=False)
    assert _rpc("tools/list").status_code == 404
    assert client.get("/api/mcp/manifest").status_code == 404


def test_initialize_and_list_only_readonly(monkeypatch):
    monkeypatch.setenv("DERM_MCP", "1")
    r = _rpc("initialize")
    assert r.status_code == 200
    init = r.json()["result"]
    assert init["protocolVersion"] == mcp_bridge.PROTOCOL_VERSION
    assert init["capabilities"]["tools"]["listChanged"] is False
    assert init["serverInfo"]["name"] == "dermagent"

    tools = _rpc("tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_case_status", "search_knowledge_base"}
    for t in tools:
        assert t["inputSchema"]["required"][0] == "case_id"
        assert "patient_name" not in json.dumps(t, ensure_ascii=False)


def test_tools_call_readonly_and_write_denied(monkeypatch):
    monkeypatch.setenv("DERM_MCP", "1")
    cid = _case()
    r = _rpc("tools/call", {"name": "get_case_status", "arguments": {"case_id": cid}})
    body = r.json()["result"]
    assert body["isError"] is False
    payload = json.loads(body["content"][0]["text"])
    assert payload["ok"] is True and payload["phase"]

    r = _rpc("tools/call", {"name": "update_record_fields",
                            "arguments": {"case_id": cid, "fields": {"chief_complaint": "x"}}})
    err = r.json()["error"]
    assert err["code"] == mcp_bridge.POLICY_DENIED

    r = _rpc("tools/call", {"name": "get_case_status", "arguments": {}})
    assert r.json()["error"]["code"] == mcp_bridge.INVALID_PARAMS
    r = _rpc("tools/call", {"name": "get_case_status", "arguments": {"case_id": "no_such"}})
    assert r.json()["error"]["code"] == mcp_bridge.CASE_NOT_FOUND


def test_unknown_method_and_notification(monkeypatch):
    monkeypatch.setenv("DERM_MCP", "1")
    assert _rpc("no/such").json()["error"]["code"] == mcp_bridge.METHOD_NOT_FOUND
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and not r.content


def test_token_and_method_gate(monkeypatch):
    monkeypatch.setenv("DERM_MCP", "1")
    monkeypatch.setenv("DERM_MCP_TOKEN", "s3cret")
    assert _rpc("tools/list").status_code == 401
    r = _rpc("tools/list", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    g = client.get("/mcp")
    assert g.status_code == 405 and g.headers.get("allow") == "POST"


def test_call_is_audited_without_case_content(monkeypatch):
    monkeypatch.setenv("DERM_MCP", "1")
    cid = _case()
    _rpc("tools/call", {"name": "get_case_status", "arguments": {"case_id": cid}})
    lines = []
    if os.path.exists(AUDIT_FILE):
        with open(AUDIT_FILE, encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
    calls = [x for x in lines if x.get("event") == "mcp_tool_call"]
    assert calls, "MCP 调用必须写审计"
    last = calls[-1]
    detail = json.loads(last["detail"])
    assert detail["case_id"] == cid
    assert "chief_complaint" not in json.dumps(last, ensure_ascii=False)


def test_manifest_matches_tools_list(monkeypatch):
    monkeypatch.setenv("DERM_MCP", "1")
    manifest = client.get("/api/mcp/manifest").json()
    assert manifest["protocolVersion"] == mcp_bridge.PROTOCOL_VERSION
    assert {t["name"] for t in manifest["tools"]} == {"get_case_status", "search_knowledge_base"}
