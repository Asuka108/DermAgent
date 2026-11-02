# -*- coding: utf-8 -*-
"""工具管理 API：目录合并、内置启停、MCP/HTTP/Python 导入、试调用与审计脱敏。"""

import base64
import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app as app_mod  # noqa: E402
from server import tool_mcp as tm  # noqa: E402
from server import tool_runtime as rt  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DERM_DATA_DIR", str(tmp_path))
    tm.stop_all()
    rt.reset()
    yield TestClient(app_mod.app)
    tm.stop_all()
    rt.reset()


def _wait_server(client, server_id, want="connected", timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get("/api/tools/servers").json()["servers"]
        item = next((s for s in data if s["id"] == server_id), None)
        if item and item.get("status") == want:
            return item
        time.sleep(0.1)
    return item


def test_catalog_merges_builtin_and_keeps_legacy_fields(client):
    body = client.get("/api/tools").json()
    assert body["ok"] is True
    assert len(body["tools"]) == 19
    assert body["sources"]["builtin"] == 19
    assert "get_case_status" in body["available"]
    assert "mcp_inbound" in body and "config_error" in body


def test_builtin_toggle_and_core_warning(client):
    res = client.patch("/api/tools/builtin/get_case_status", json={"enabled": False})
    assert res.status_code == 200
    body = res.json()
    assert body["tool"]["enabled"] is False
    assert body["warning"]
    assert "get_case_status" not in client.get("/api/tools").json()["llm_tools"]
    res = client.patch("/api/tools/builtin/search_knowledge_base", json={"enabled": True})
    assert "warning" not in res.json()
    assert client.patch("/api/tools/builtin/nope", json={"enabled": False}).status_code == 404


def test_invalid_payloads_are_chinese_400(client):
    res = client.post("/api/tools/servers", json={"name": "x", "transport": "udp"})
    assert res.status_code == 400 and "传输" in res.json()["detail"]
    res = client.post("/api/tools/http", json={"name": "t", "description": "d",
                                               "url": "https://e.org", "method": "DELETE",
                                               "input_schema": {"type": "object"}})
    assert res.status_code == 400 and "只允许 GET 或 POST" in res.json()["detail"]
    res = client.post("/api/tools/import/mcp", json={})
    assert res.status_code == 400


def test_mcp_import_test_toggle_and_secret_masking(client):
    res = client.post("/api/tools/import/mcp", json={"mcpServers": {"fake": {
        "command": sys.executable, "args": [FAKE],
        "env": {"API_TOKEN": "super-secret"}}}})
    body = res.json()
    assert body["ok"] is True and body["created"], body
    server_id = body["created"][0]["id"]

    servers = client.get("/api/tools/servers").json()["servers"]
    blob = json.dumps(servers, ensure_ascii=False)
    assert "super-secret" not in blob and "secrets_enc" not in blob
    assert servers[0]["masked_env"]["API_TOKEN"] == "••••"

    tested = client.post(f"/api/tools/servers/{server_id}/test").json()
    assert tested["server"]["status"] == "connected", tested
    tools = {t["name"]: t for t in tested["server"]["tools"]}
    assert tools["echo_read"]["read_only"] is True

    blocked = client.patch(f"/api/tools/servers/{server_id}/tools/write_thing",
                           json={"enabled": True})
    assert blocked.status_code == 400 and "只读" in blocked.json()["detail"]

    ok = client.patch(f"/api/tools/servers/{server_id}/tools/echo_read",
                      json={"enabled": True})
    assert ok.status_code == 200

    catalog = client.get("/api/tools").json()
    dynamic = next(t for t in catalog["tools"] if t.get("original_name") == "echo_read")
    assert dynamic["source"] == "mcp" and dynamic["enabled"] is True
    assert dynamic["name"] in catalog["llm_tools"]

    call = client.post("/api/tools/test", json={"name": dynamic["name"],
                                                "args": {"text": "hello"}}).json()
    assert call["ok"] is True
    assert "echo:hello" in call["result"]["data"]["text"]

    assert client.delete(f"/api/tools/servers/{server_id}").status_code == 200
    assert client.get("/api/tools/servers").json()["servers"] == []


def test_mcp_import_errors_reported(client):
    res = client.post("/api/tools/import/mcp", json={"servers": {
        "bad": {"command": ""}}})
    body = res.json()
    assert body["ok"] is False and body["errors"]
    assert "命令" in body["errors"][0]["error"]


def test_http_import_toggle_and_test(client):
    res = client.post("/api/tools/http", json={
        "name": "echo", "description": "回显",
        "url": "http://127.0.0.1:9/echo", "method": "GET",
        "input_schema": {"type": "object", "properties": {}},
        "headers": {"X-Api-Key": "hidden-value"}})
    assert res.status_code == 200
    tool = res.json()["tool"]
    assert "hidden-value" not in json.dumps(tool, ensure_ascii=False)
    assert tool["header_keys"] == ["X-Api-Key"]

    listing = client.get("/api/tools/http").json()["tools"]
    assert "hidden-value" not in json.dumps(listing, ensure_ascii=False)

    assert client.patch(f"/api/tools/dynamic/{tool['public_name']}",
                        json={"enabled": True}).json()["ok"] is True
    schema_names = [t["name"] for t in client.get("/api/tools").json()["tools"]]
    assert tool["public_name"] in schema_names

    tested = client.post("/api/tools/test", json={"name": tool["public_name"],
                                                  "args": {}}).json()
    assert tested["ok"] is False and tested["result"]["ok"] is False

    assert client.delete(f"/api/tools/dynamic/{tool['public_name']}").status_code == 200
    assert client.get("/api/tools/http").json()["tools"] == []


def test_python_import_and_test_only_dynamic(client):
    source = (
        "TOOL_SPEC = {'name': 'calc', 'description': '计算', "
        "'input_schema': {'type': 'object', 'properties': {'x': {'type': 'integer'}}}}\n"
        "def run(args):\n    return {'value': int(args.get('x') or 0) + 1}\n")
    res = client.post("/api/tools/import/python",
                      json={"filename": "calc.py", "content_b64": base64.b64encode(source.encode()).decode()})
    assert res.status_code == 200, res.text
    tool = res.json()["tool"]
    assert tool["enabled"] is False and res.json()["warning"]

    # multipart 仅在 python-multipart 已安装时可用；JSON base64 永远是主路径
    source2 = source.replace("'calc'", "'calc2'")
    res = client.post("/api/tools/import/python",
                      files={"file": ("calc2.py", source2.encode(), "text/x-python")})
    assert res.status_code == 200, res.text

    res = client.post("/api/tools/import/python",
                      json={"filename": "bad.py",
                            "content_b64": base64.b64encode(b"print('no spec')\n").decode()})
    assert res.status_code == 400 and "TOOL_SPEC" in res.json()["detail"]

    res = client.post("/api/tools/import/python", json={"filename": "x.py", "content_b64": "!!!"})
    assert res.status_code == 400 and "base64" in res.json()["detail"]

    client.patch(f"/api/tools/dynamic/{tool['public_name']}", json={"enabled": True})
    out = client.post("/api/tools/test", json={"name": tool["public_name"],
                                               "args": {"x": 41}}).json()
    assert out["ok"] is True and out["result"]["data"]["value"] == 42

    builtin = client.post("/api/tools/test", json={"name": "get_case_status", "args": {}})
    assert builtin.status_code == 400 and "内置工具" in builtin.json()["detail"]


def test_audit_endpoint_is_redacted(client):
    client.post("/api/tools/http", json={
        "name": "echo", "description": "回显", "url": "http://127.0.0.1:9/echo",
        "method": "GET", "input_schema": {"type": "object"},
        "headers": {"X-Api-Key": "hidden-value"}})
    client.patch("/api/tools/dynamic/http_echo", json={"enabled": True})
    client.post("/api/tools/test", json={"name": "http_echo", "args": {"q": "private-arg"}})
    body = client.get("/api/tools/audit?limit=20").json()
    blob = json.dumps(body, ensure_ascii=False)
    assert "hidden-value" not in blob and "private-arg" not in blob
    assert any(item["event"] in ("tool_call", "tool_import", "tool_test")
               for item in body["events"])


def test_server_imports_without_python_multipart(tmp_path):
    """回归：python-multipart 缺失时服务端仍能启动（工具上传接口不得用 File(...)）。"""
    backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys\n"
        "sys.modules['python_multipart'] = None\n"
        "sys.modules['multipart'] = None\n"
        "sys.path.insert(0, %r)\n"
        "import server.app\n"
        "print('ok')\n" % backend
    )
    env = {**os.environ, "DERM_DATA_DIR": str(tmp_path), "DERM_MCP": "0",
           "DERM_MEMORY_DISABLED": "1", "DERM_KB_BACKEND": "fake",
           "DERM_KB_AUTOSTART_WORKER": "0"}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert "ok" in proc.stdout
