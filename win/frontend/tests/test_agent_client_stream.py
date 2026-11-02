# -*- coding: utf-8 -*-
"""桌面端 SSE 消费：事件顺序、X-Case-Id 提前落定、中途断流带 turn_id、404 可回退。"""

import json
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_client import ServerAgentClient  # noqa: E402


def _frame(event, payload, seq=None):
    head = f"id: {seq}\n" if seq is not None else ""
    return (head + f"event: {event}\n"
            + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


class _StreamServer:
    """假服务端：按路径返回流式事件；abort 模式用超长 Content-Length 制造断流。"""

    def __init__(self):
        self.mode = "ok"          # ok | abort | missing
        self.case_id_header = "case_from_header"
        self.event_case_id = "case_from_event"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                if outer.mode == "missing":
                    body = json.dumps({"error": {"message": "stream_disabled",
                                                 "code": "stream_disabled"}}).encode("utf-8")
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/cases/stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("X-Turn-Id", "trn_start")
                    if outer.case_id_header:
                        self.send_header("X-Case-Id", outer.case_id_header)
                    self.end_headers()
                    self.wfile.write(_frame("turn_started",
                                            {"turn_id": "trn_start",
                                             "case_id": outer.event_case_id}, 1))
                    self.wfile.write(_frame("done", {"snapshot": {}}, 2))
                    self.wfile.flush()
                    return
                if outer.mode == "abort":
                    # 先推两帧，再用 RST 强制断开 → 客户端读取抛出网络异常
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("X-Turn-Id", "trn_abc")
                    self.end_headers()
                    self.wfile.write(_frame("turn_started",
                                            {"turn_id": "trn_abc", "case_id": "c1"}, 1))
                    self.wfile.write(_frame("message_delta",
                                            {"text": "半截", "final": False, "reset": False}, 2))
                    self.wfile.flush()
                    time.sleep(0.1)
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                               struct.pack("ii", 1, 0))
                    self.connection.close()
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("X-Turn-Id", "trn_ok")
                self.end_headers()
                for idx, (event, payload) in enumerate((
                        ("turn_started", {"turn_id": "trn_ok", "case_id": "c1"}),
                        ("progress", {"phase": "intent", "message": "理解指令"}),
                        ("message_delta", {"text": "建议", "final": False}),
                        ("message_delta", {"text": "保湿。", "final": False}),
                        ("reflection", {"stage": "final", "verdict": "pass"}),
                        ("done", {"snapshot": {"case_id": "c1",
                                              "reply": {"content": "建议保湿。"}}})), 1):
                    self.wfile.write(_frame(event, payload, seq=idx))
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:" + str(self.server.server_address[1])

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def server():
    with _StreamServer() as srv:
        yield srv


def test_stream_turn_yields_events_in_order(server):
    cli = ServerAgentClient(server.base_url)
    cli.case_id = "c1"
    events = list(cli.stream_turn("请整理病史"))
    types = [e["type"] for e in events]
    assert types[0] == "turn_started"
    assert types.index("progress") < types.index("message_delta") < types.index("done")
    assert types.count("done") == 1
    delta = [e for e in events if e["type"] == "message_delta"]
    assert [d["payload"]["text"] for d in delta] == ["建议", "保湿。"]
    assert events[-1]["payload"]["snapshot"]["reply"]["content"] == "建议保湿。"


def test_stream_start_sets_case_id_from_header(server):
    server.event_case_id = ""
    cli = ServerAgentClient(server.base_url)
    events = list(cli.stream_start("记录门诊情况"))
    assert cli.case_id == "case_from_header"
    assert [e["type"] for e in events] == ["turn_started", "done"]


def test_stream_start_falls_back_to_turn_started_case_id(server):
    server.case_id_header = ""
    cli = ServerAgentClient(server.base_url)
    events = list(cli.stream_start("记录门诊情况"))
    assert cli.case_id == "case_from_event"
    assert events[-1]["type"] == "done"


def test_midstream_abort_raises_with_turn_id(server):
    server.mode = "abort"
    cli = ServerAgentClient(server.base_url)
    cli.case_id = "c1"
    with pytest.raises(ConnectionError) as err:
        for _ev in cli.stream_turn("继续"):
            pass
    assert getattr(err.value, "turn_id", "") == "trn_abc"


def test_stream_404_maps_to_not_implemented(server):
    server.mode = "missing"
    cli = ServerAgentClient(server.base_url)
    cli.case_id = "c1"
    with pytest.raises(NotImplementedError):
        list(cli.stream_turn("继续"))
