# -*- coding: utf-8 -*-
"""首轮流式端到端：假 OpenAI 兼容网关 → /api/cases/stream 的增量与收口语义。

覆盖：
- 正常流式：≥3 条非 final message_delta 先于 reflection/done，final 文本 == 快照正文；
- 严格网关（拒绝 stream）：回退非流式，仍有唯一 final 终稿；
- 设置/压缩类指令不建病例：只发一条 done，不产生 message_delta。
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_MODEL"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from server import agent_loop  # noqa: E402
from server.app import app  # noqa: E402

client = TestClient(app)

REPLY = "请补充皮损的边界、颜色变化和近期用药情况，我再结合现有信息继续整理。"
CHUNKS = [REPLY[i:i + 4] for i in range(0, len(REPLY), 4)]
CRITIC = '{"verdict":"pass","issues":[]}'


class _Gateway:
    """进程内假网关：stream=true 发 SSE；否则按有无 tools 区分回复/评审。"""

    def __init__(self):
        self.mode = "stream"           # stream | reject_stream | reject_stream_options
        self.requests = []
        self.chunk_delay = 0.03
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):  # 静音测试输出
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (ValueError, TypeError):
                    payload = {}
                outer.requests.append(payload)
                if self.path.rstrip("/") != "/chat/completions":
                    self._json(404, {"error": "not found"})
                    return
                if payload.get("stream"):
                    reject = (outer.mode == "reject_stream"
                              or (outer.mode == "reject_stream_options"
                                  and "stream_options" in payload))
                    if reject:
                        self._json(400, {"error": {"message": "stream unsupported"}})
                        return
                    self._sse()
                    return
                if payload.get("tools"):
                    self._json(200, outer.chat_payload())
                else:
                    self._json(200, outer.critic_payload())

            def _json(self, code, data):
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for chunk in CHUNKS:
                    frame = {"choices": [{"index": 0, "delta": {"content": chunk}}]}
                    self.wfile.write(
                        ("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    time.sleep(outer.chunk_delay)
                usage = {"choices": [], "usage": {"prompt_tokens": 10,
                                                  "completion_tokens": 12, "total_tokens": 22}}
                self.wfile.write(
                    ("data: " + json.dumps(usage) + "\n\n").encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def chat_payload(self):
        return {"choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": REPLY}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22}}

    def critic_payload(self):
        return {"choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": CRITIC}}]}

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
def gateway(monkeypatch):
    with _Gateway() as gw:
        monkeypatch.setenv("LLM_BASE_URL", gw.base_url)
        monkeypatch.setenv("LLM_API_KEY", "test-key")
        monkeypatch.setenv("LLM_MODEL", "fake-model")
        # 其他用例可能置脏过全局能力缓存：这里是独立端口，恢复干净状态再跑。
        monkeypatch.setattr(agent_loop, "_TOOLS_UNSUPPORTED", False)
        monkeypatch.setattr(agent_loop, "_STREAM_UNSUPPORTED", set())
        monkeypatch.setattr(agent_loop, "_STREAM_USAGE_UNSUPPORTED", set())
        yield gw


def _stream(case_id, payload):
    """返回 (events[(type, data, seq)], headers)。"""
    events = []
    with client.stream("POST", f"/api/cases/{case_id}/chat/stream", json=payload) as resp:
        assert resp.status_code == 200, resp.read()
        seq = None
        event = ""
        for line in resp.iter_lines():
            if line.startswith("id:"):
                seq = int(line.split(":", 1)[1].strip())
            elif line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                events.append((event, data, seq))
                seq = None
    return events


def _start_stream(payload):
    events = []
    with client.stream("POST", "/api/cases/stream", json=payload) as resp:
        assert resp.status_code == 200, resp.read()
        case_id = resp.headers.get("x-case-id", "")
        seq = None
        event = ""
        for line in resp.iter_lines():
            if line.startswith("id:"):
                seq = int(line.split(":", 1)[1].strip())
            elif line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
                events.append((event, data, seq))
                seq = None
    return events, case_id


def _types(events):
    return [e for e, _d, _s in events if e != "heartbeat"]


def test_first_turn_streams_tokens_before_done(gateway):
    events, case_id = _start_stream({"text": "记录一下这次门诊情况"})
    assert case_id, "建案流必须下发 X-Case-Id"
    types = _types(events)
    assert types[0] == "turn_started"
    assert types.count("done") == 1 and types[-1] == "done"

    deltas = [d for e, d, _s in events if e == "message_delta"]
    model_deltas = [d for d in deltas if not d.get("final") and not d.get("reset")]
    finals = [d for d in deltas if d.get("final")]
    assert len(model_deltas) >= 3, f"实际增量过少：{len(model_deltas)}"
    assert len(finals) == 1 and finals[0].get("source") == "final"
    assert "".join(d["text"] for d in model_deltas) == REPLY
    assert types.index("reflection") > next(
        i for i, (e, _d, _s) in enumerate(events) if e == "message_delta")
    assert types.index("reflection") < types.index("done")

    done = [d for e, d, _s in events if e == "done"][0]["snapshot"]
    assert done["reply"]["content"] == REPLY
    assert finals[0]["text"] == done["reply"]["content"]


def test_first_turn_logs_user_and_assistant_once(gateway):
    """首轮 Agent finalize 与建案收尾共用一次写入，历史不得出现重复消息。"""
    events, case_id = _start_stream({"text": "记录一下这次门诊情况"})
    assert case_id
    snap = client.get(f"/api/cases/{case_id}").json()
    roles = [m.get("role") for m in (snap.get("messages") or [])]
    assert roles == ["user", "assistant"], roles
    assert snap["messages"][1].get("message_id")


def test_strict_gateway_falls_back_and_still_has_final(gateway):
    gateway.mode = "reject_stream"
    events, case_id = _start_stream({"text": "记录一下这次门诊情况"})
    types = _types(events)
    assert case_id and types.count("done") == 1 and types[-1] == "done"
    deltas = [d for e, d, _s in events if e == "message_delta"]
    assert not [d for d in deltas if not d.get("final")]      # 无伪增量
    finals = [d for d in deltas if d.get("final")]
    assert len(finals) == 1 and finals[0].get("source") == "final"
    done = [d for e, d, _s in events if e == "done"][0]["snapshot"]
    assert finals[0]["text"] == done["reply"]["content"] == REPLY
    # 流式 4xx 不得关闭工具能力（否则后续回合全部退化为关键词流程）
    assert agent_loop._TOOLS_UNSUPPORTED is False


def test_settings_command_without_case_emits_single_done(gateway):
    events, case_id = _start_stream({"text": "压缩上下文"})
    types = _types(events)
    assert case_id == ""
    assert types == ["turn_started", "done"]


def test_chat_stream_reuses_deltas_for_followup_turn(gateway):
    events, case_id = _start_stream({"text": "记录一下这次门诊情况"})
    follow = _stream(case_id, {"text": "继续整理"})
    types = _types(follow)
    assert types.count("done") == 1 and types[-1] == "done"
    model_deltas = [d for e, d, _s in follow
                    if e == "message_delta" and not d.get("final") and not d.get("reset")]
    assert len(model_deltas) >= 3
    done = [d for e, d, _s in follow if e == "done"][0]["snapshot"]
    finals = [d for e, d, _s in follow if e == "message_delta" and d.get("final")]
    assert len(finals) == 1 and finals[0]["text"] == done["reply"]["content"]
