# -*- coding: utf-8 -*-
"""桌面工具管理：请求体、错误透出、密钥掩码与只读门控。"""
import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from kb_client import KBClient  # noqa: E402


def _client(paths):
    cli = KBClient(base_url="http://127.0.0.1:9", mode="server")

    def fake_send(req, timeout=None):
        paths.append((req.get_method(), req.full_url, req.data,
                      req.get_header("Content-type"), timeout))
        return {"ok": True}

    cli._send = fake_send
    return cli


def test_tool_requests_shape():
    seen = []
    cli = _client(seen)
    cli.tools_catalog()
    cli.set_builtin_enabled("case_comparator", False)
    cli.save_server({"name": "本地", "transport": "stdio"}, "")
    cli.save_server({"name": "本地"}, "srv_1")
    cli.test_server("srv_1")
    cli.set_mcp_tool("srv_1", "open page", True)
    cli.delete_mcp_tool("srv_1", "open page")
    cli.import_mcp_json({"mcpServers": {}})
    cli.save_http_tool({"name": "pubmed"})
    cli.delete_http_tool("htt_1")
    cli.set_dynamic_enabled("http_pubmed", True)
    cli.delete_dynamic_tool("http_pubmed")
    cli.test_tool("py_calc", {"a": 1})
    cli.tool_audit(20)

    calls = [(m, u.split("127.0.0.1:9", 1)[1], body, t) for m, u, body, _, t in seen]
    assert calls[0] == ("GET", "/api/tools", None, None)
    assert calls[1][:2] == ("PATCH", "/api/tools/builtin/case_comparator")
    assert json.loads(calls[1][2]) == {"enabled": False}
    assert calls[2][:2] == ("POST", "/api/tools/servers") and calls[2][3] == 25
    assert calls[3][:2] == ("PUT", "/api/tools/servers/srv_1")
    assert calls[4][:2] == ("POST", "/api/tools/servers/srv_1/test") and calls[4][3] == 40
    assert calls[5][:2] == ("PATCH", "/api/tools/servers/srv_1/tools/open%20page")
    assert calls[6][:2] == ("DELETE", "/api/tools/servers/srv_1/tools/open%20page")
    assert calls[7][:2] == ("POST", "/api/tools/import/mcp") and calls[7][3] == 30
    assert calls[8][:2] == ("POST", "/api/tools/http")
    assert calls[9][:2] == ("DELETE", "/api/tools/http/htt_1")
    assert calls[10][:2] == ("PATCH", "/api/tools/dynamic/http_pubmed")
    assert calls[11][:2] == ("DELETE", "/api/tools/dynamic/http_pubmed")
    assert calls[12][:2] == ("POST", "/api/tools/test") and calls[12][3] == 60
    assert json.loads(calls[12][2]) == {"name": "py_calc", "args": {"a": 1}}
    assert calls[13][:2] == ("GET", "/api/tools/audit?limit=20")

    seen.clear()
    cli.list_http_tools()
    cli.list_py_tools()
    cli.list_servers()
    assert [c[1].split("127.0.0.1:9", 1)[1] for c in seen] == [
        "/api/tools/http", "/api/tools/python", "/api/tools/servers"]


def test_python_upload_base64_json_and_mock_guard():
    seen = []
    cli = _client(seen)
    cli.import_py_tool("../evil.py", b"TOOL_SPEC = {}\n")
    method, url, body, ctype, timeout = seen[0]
    assert method == "POST" and url.endswith("/api/tools/import/python")
    assert timeout == 30 and ctype == "application/json"
    payload = json.loads(body)
    assert payload["filename"] == "evil.py"
    assert base64.b64decode(payload["content_b64"]) == b"TOOL_SPEC = {}\n"

    mock = KBClient(mode="mock")
    assert mock.save_server({"name": "x"}) == {"ok": False, "reason": "server_unavailable"}
    assert mock.import_py_tool("t.py", b"") == {"ok": False, "reason": "server_unavailable"}
    assert mock.tool_audit() == {"ok": False, "reason": "server_unavailable"}


@pytest.fixture()
def detail_server():
    state = {}

    class Handler(BaseHTTPRequestHandler):
        def do_PATCH(self):
            state["method"] = self.command
            body = json.dumps({"ok": False, "detail": "核心工具不能删除"}, ensure_ascii=False)
            raw = body.encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = "http://127.0.0.1:%d" % srv.server_address[1]
    yield state
    srv.shutdown()


def test_error_detail_is_preserved(detail_server):
    cli = KBClient(base_url=detail_server["url"], mode="server")
    res = cli.set_builtin_enabled("update_record_fields", False)
    assert detail_server["method"] == "PATCH"
    assert res["ok"] is False and res["detail"] == "核心工具不能删除"


def test_parse_kv_lines_and_mcp_dialog_payload():
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    assert main._parse_kv_lines("A=1\n\n# c\nbad\nB = v=2 ") == {"A": "1", "B": "v=2"}

    dlg = main._McpServerDialog(server={
        "name": "chrome", "transport": "stdio", "command": "npx",
        "args": ["-y", "pkg"], "timeout_s": 45, "env_keys": ["TOKEN"],
        "header_keys": []})
    assert "TOKEN" in dlg.env.placeholderText()
    payload = dlg.payload()
    assert payload["args"] == ["-y", "pkg"] and payload["timeout_s"] == 45
    assert payload["env"] == {} and "headers" in payload

    dlg.transport.setCurrentIndex(dlg.transport.findData("http"))
    dlg.url.setText("https://example.com/mcp")
    dlg.env.setPlainText("TOKEN=secret")
    dlg.clear_secrets.setChecked(True)
    payload = dlg.payload()
    assert payload["url"].startswith("https://") and "command" not in payload
    assert payload["env"] == {"TOKEN": "secret"} and payload["clear_secrets"] is True

    dlg.args.setText("[not json")
    dlg.transport.setCurrentIndex(dlg.transport.findData("stdio"))
    with pytest.raises(ValueError):
        dlg.payload()
    dlg.deleteLater()
    app.processEvents()


def test_settings_tools_tab_is_read_only_in_mock_mode():
    from PySide6.QtWidgets import QApplication
    import main

    app = QApplication.instance() or QApplication([])
    original = main.load_kb_client
    main.load_kb_client = lambda: KBClient(mode="mock")
    try:
        dlg = main.SettingsDialog()
        assert dlg._read_only is True
        texts = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
        assert any("工具" in t for t in texts)
        assert [dlg.tools_subtabs.tabText(i) for i in range(dlg.tools_subtabs.count())] == [
            "内置", "MCP", "导入", "审计"]
        assert dlg._write_widgets and all(not w.isEnabled() for w in dlg._write_widgets)
        assert dlg.tools_banner.isHidden()
        dlg.deleteLater()
        app.processEvents()
    finally:
        main.load_kb_client = original
