# -*- coding: utf-8 -*-
"""动态工具运行时：只读门禁、启用过滤、HTTP/Python 分发、审计脱敏。"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import tool_runtime as rt  # noqa: E402
from server import tool_store as ts  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/redirect"):
            self._send(302, b"", {"Location": "/echo"})
            return
        if parsed.path.startswith("/error"):
            self._send(500, {"error": "boom"})
            return
        if parsed.path.startswith("/big"):
            self._send(200, {"text": "x" * 20000})
            return
        self._send(200, {"query": parse_qs(parsed.query),
                         "auth": self.headers.get("X-Api-Key", "")})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw.decode("utf-8", "replace")}
        self._send(200, {"received": payload,
                         "auth": self.headers.get("X-Api-Key", "")})

    def log_message(self, *_args):  # noqa: D401 - 静默测试服务器
        return


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("DERM_DATA_DIR", str(tmp_path))
    rt.reset()
    return tmp_path


@pytest.fixture
def http_base():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def audit():
    events = []
    rt.configure(audit=lambda event, detail: events.append((event, detail)))
    yield events
    rt.configure(audit=lambda *_a, **_k: None)


def _http_tool(base, path="/echo", method="GET", **extra):
    payload = {"name": "echo", "description": "回显", "url": base + path,
               "method": method, "input_schema": {"type": "object",
                                                   "properties": {"q": {"type": "string"}}}}
    payload.update(extra)
    return ts.upsert_http_tool(payload)


def _py_tool(source: str):
    spec = {"name": "calc", "description": "计算",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}}}
    return ts.install_py_tool("calc.py", source.encode("utf-8"), spec)


def test_http_get_post_and_headers(data, http_base, audit):
    tool = _http_tool(http_base, headers={"X-Api-Key": "secret-key"})
    ts.set_dynamic_enabled(tool["public_name"], True)
    rt.refresh()
    name = rt.get("http_echo")["public_name"]
    out = rt.execute(name, {"q": "abc"})
    assert out["ok"] is True and out["source"] == "http"
    assert out["data"]["query"]["q"] == ["abc"]
    assert out["data"]["auth"] == "secret-key"

    ts.upsert_http_tool({"name": "echo", "description": "回显", "url": http_base + "/echo",
                         "method": "POST",
                         "input_schema": {"type": "object",
                                          "properties": {"q": {"type": "string"}}}},
                        tool_id=tool["id"])
    rt.refresh()
    out = rt.execute("http_echo", {"q": "post"})
    assert out["ok"] is True
    assert out["data"]["received"] == {"q": "post"}

    blob = json.dumps(audit, ensure_ascii=False)
    assert "secret-key" not in blob
    assert "abc" not in blob
    assert any(event == "tool_call" for event, _ in audit)


def test_http_redirect_and_error_and_truncation(data, http_base):
    tool = _http_tool(http_base, path="/redirect")
    ts.set_dynamic_enabled(tool["public_name"], True)
    rt.refresh()
    out = rt.execute("http_echo", {})
    assert out["ok"] is False and "重定向" in out["error"]

    ts.upsert_http_tool({"name": "echo", "description": "回显", "url": http_base + "/error",
                         "method": "GET", "input_schema": {"type": "object"}},
                        tool_id=tool["id"])
    rt.refresh()
    out = rt.execute("http_echo", {})
    assert out["ok"] is False and "HTTP 500" in out["error"]

    ts.upsert_http_tool({"name": "echo", "description": "回显", "url": http_base + "/big",
                         "method": "GET", "input_schema": {"type": "object"}},
                        tool_id=tool["id"])
    rt.refresh()
    out = rt.execute("http_echo", {})
    assert out["ok"] is True
    assert len(json.dumps(out, ensure_ascii=False)) <= rt.MAX_RESULT_CHARS + 200


def test_disabled_tool_not_in_schemas_and_cannot_execute(data, http_base):
    _http_tool(http_base)
    rt.refresh()
    assert rt.schemas() == []
    out = rt.execute("http_echo", {})
    assert out["ok"] is False and "未启用" in out["error"]

    ts.set_dynamic_enabled("http_echo", True)
    rt.refresh()
    assert [s["function"]["name"] for s in rt.schemas()] == ["http_echo"]
    assert rt.names() == {"http_echo"}


def test_python_tool_roundtrip_and_errors(data, monkeypatch):
    tool = _py_tool(
        "TOOL_SPEC = {'name': 'calc', 'description': '计算', "
        "'input_schema': {'type': 'object', 'properties': {'x': {'type': 'integer'}}}}\n"
        "def run(args):\n"
        "    print('noise on stdout')\n"
        "    return {'value': int(args.get('x') or 0) * 2}\n")
    assert tool["enabled"] is False
    ts.set_dynamic_enabled(tool["public_name"], True)
    rt.refresh()
    out = rt.execute(tool["public_name"], {"x": 21})
    assert out["ok"] is True and out["data"]["value"] == 42 and out["source"] == "py"

    bad = ts.install_py_tool(
        "bad.py",
        b"TOOL_SPEC = {'name': 'bad', 'description': 'bad', "
        b"'input_schema': {'type': 'object'}}\ndef run(args):\n    raise RuntimeError('nope')\n",
        {"name": "bad", "description": "bad", "input_schema": {"type": "object"}})
    ts.set_dynamic_enabled(bad["public_name"], True)
    rt.refresh()
    out = rt.execute(bad["public_name"], {})
    assert out["ok"] is False and "nope" in out["error"]

    slow = ts.install_py_tool(
        "slow.py",
        b"import time\nTOOL_SPEC = {'name': 'slow', 'description': 'slow', "
        b"'input_schema': {'type': 'object'}}\ndef run(args):\n    time.sleep(5)\n    return {'ok': True}\n",
        {"name": "slow", "description": "slow", "input_schema": {"type": "object"}})
    ts.set_dynamic_enabled(slow["public_name"], True)
    monkeypatch.setattr(rt, "PY_TIMEOUT", 1)
    rt.refresh()
    out = rt.execute(slow["public_name"], {})
    assert out["ok"] is False and "超时" in out["error"]


def test_guard_host_blocks_metadata():
    with pytest.raises(ValueError):
        rt._guard_host("https://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError):
        rt._guard_host("https://10.0.0.8/api")
    rt._guard_host("https://example.org/api")
    rt._guard_host("http://127.0.0.1:9/api")   # 本机调试走 http，由导入校验放行


def test_unique_names_and_blocked_tool_report(data):
    used = set()
    a = rt._unique("mcp_demo_tool", used)
    b = rt._unique("mcp_demo_tool", used)
    assert a != b and b == "mcp_demo_tool_2"
    blocked = rt._dynamic("mcp_x", "mcp", "srv", "d", {}, False,
                          enabled=True, blocked_reason="未声明只读", status="blocked")
    assert blocked["enabled"] is False

    servers = rt.servers_view()
    assert servers == []
    assert rt.http_tools_view() == [] and rt.py_tools_view() == []


def test_agent_turn_schemas_follow_enabled_state(data, http_base):
    from server import agent_loop

    names = {s["function"]["name"] for s in agent_loop._llm_tool_schemas()}
    assert "get_case_status" in names and "update_record_fields" in names

    ts.set_builtin_enabled("get_case_status", False)
    tool = _http_tool(http_base)
    ts.set_dynamic_enabled(tool["public_name"], True)
    rt.refresh()
    names = {s["function"]["name"] for s in agent_loop._llm_tool_schemas()}
    assert "get_case_status" not in names
    assert "http_echo" in names
    allowed = agent_loop._allowed_with_dynamic(["search_knowledge_base"],
                                               agent_loop._llm_tool_schemas())
    assert "http_echo" in allowed and "get_case_status" not in allowed
