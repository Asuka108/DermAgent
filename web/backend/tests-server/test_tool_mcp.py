# -*- coding: utf-8 -*-
"""外部 MCP 客户端：真实 stdio 握手、只读过滤、调用、热重载与失败状态。"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import tool_mcp as tm  # noqa: E402
from server import tool_runtime as rt  # noqa: E402
from server import tool_store as ts  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("DERM_DATA_DIR", str(tmp_path))
    tm.stop_all()
    rt.reset()
    tm.configure(audit=lambda *_a, **_k: None)
    yield tmp_path
    tm.stop_all()
    rt.reset()


def _wait_status(server_id, want="connected", timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = tm.status(server_id)
        if state.get("status") == want:
            return state
        time.sleep(0.1)
    return tm.status(server_id)


def test_connect_list_filter_and_call(data):
    server = ts.upsert_server({"name": "fake", "transport": "stdio",
                               "command": sys.executable, "args": [FAKE],
                               "timeout_s": 20})
    result = tm.reload_all()
    assert result["ok"] is True
    state = _wait_status(server["id"])
    assert state["status"] == "connected", state

    tools = {t["name"]: t for t in state["tools"]}
    assert tools["echo_read"]["read_only"] is True
    assert tools["write_thing"]["read_only"] is False
    assert tools["write_thing"]["blocked_reason"]

    ts.set_mcp_tool_state(server["id"], "echo_read", True)
    rt.refresh()
    names = rt.names()
    assert any(n.startswith("mcp_fake_echo_read") for n in names)
    public = next(n for n in names if n.startswith("mcp_fake_echo_read"))
    out = rt.execute(public, {"text": "hi"})
    assert out["ok"] is True
    assert "echo:hi" in out["data"]["text"]

    blocked = next(t for t in rt.catalog() if t.get("original_name") == "write_thing")
    assert blocked["read_only"] is False and blocked["enabled"] is False
    assert rt.execute(blocked["name"], {"text": "x"})["ok"] is False


def test_hot_reload_and_stop(data):
    server = ts.upsert_server({"name": "fake", "transport": "stdio",
                               "command": sys.executable, "args": [FAKE],
                               "timeout_s": 20})
    tm.reload_all()
    assert _wait_status(server["id"])["status"] == "connected"

    ts.set_server_enabled(server["id"], False)
    tm.reload_all()
    assert tm.status(server["id"])["status"] == "disabled"
    assert server["id"] not in tm._SERVERS

    ts.set_server_enabled(server["id"], True)
    tm.reload_all()
    assert _wait_status(server["id"])["status"] == "connected"

    ts.delete_server(server["id"])
    tm.reload_all()
    assert tm.status(server["id"]) == {}
    assert not tm._SERVERS


def test_bad_command_reports_error(data):
    server = ts.upsert_server({"name": "broken", "transport": "stdio",
                               "command": "/nonexistent/mcp-server", "args": [],
                               "timeout_s": 5})
    tm.reload_all()
    state = _wait_status(server["id"], "error", timeout=20)
    assert state["status"] == "error"
    assert state.get("error")
    assert "Traceback" not in state["error"] or len(state["error"]) <= 300
