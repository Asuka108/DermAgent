# -*- coding: utf-8 -*-
"""v79 流式 LLM 客户端：增量/工具分片合并、能力回退矩阵、截断不重试。"""

import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import agent_loop as al  # noqa: E402

BASE = "http://fake.local/v1"
MODEL = "fake-model"
MESSAGES = [{"role": "user", "content": "整理病史"}]


class _Resp:
    """最小 HTTP 响应替身：按行迭代字节，headers 提供 Content-Type。"""

    def __init__(self, lines=None, body=b"", ctype="text/event-stream"):
        self._lines = list(lines or [])
        self._body = body
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body

    def __iter__(self):
        for ln in self._lines:
            yield ln if isinstance(ln, bytes) else (ln + "\n").encode("utf-8")


def _sse(*frames):
    lines = []
    for frame in frames:
        lines.append("data: " + json.dumps(frame, ensure_ascii=False))
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return lines


def _http400():
    return urllib.error.HTTPError(f"{BASE}/chat/completions", 400, "Bad Request", {},
                                  io.BytesIO(b'{"error":{"message":"bad"}}'))


def _reset():
    al._TOOLS_UNSUPPORTED = False
    al._STREAM_UNSUPPORTED.clear()
    al._STREAM_USAGE_UNSUPPORTED.clear()


def _install(monkeypatch, handler):
    calls = []

    def fake_urlopen(req, timeout=None, context=None):
        calls.append(json.loads(req.data.decode("utf-8")))
        out = handler(req, len(calls))
        if isinstance(out, BaseException):        # HTTPError 必须是「抛出」，不是当作响应体
            raise out
        return out

    monkeypatch.setattr(al.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_stream_accumulates_content_usage_and_tool_fragments(monkeypatch):
    _reset()
    deltas = []
    frames = [
        {"choices": [{"delta": {"content": "先"}}]},
        {"choices": [{"delta": {"content": "查体。"}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "search_knowledge_base", "arguments": "{\"q\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"红斑\"}"}}]}}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 34}},
    ]
    _install(monkeypatch, lambda req, n: _Resp(_sse(*frames)))

    out = al._chat_tools(BASE, "k", MODEL, MESSAGES, "", on_delta=deltas.append)

    assert out["content"] == "先查体。"
    assert deltas == ["先", "查体。"]
    assert out["_streamed"] is True
    assert out["_usage"] == {"prompt_tokens": 12, "completion_tokens": 34}
    call = out["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "search_knowledge_base"
    assert json.loads(call["function"]["arguments"]) == {"q": "红斑"}


def test_stream_options_rejected_retries_without_them(monkeypatch):
    _reset()
    calls = _install(monkeypatch, lambda req, n: _http400() if n == 1
                     else _Resp(_sse({"choices": [{"delta": {"content": "好"}}]})))
    out = al._chat_tools(BASE, "k", MODEL, MESSAGES, "", on_delta=lambda _: None)
    assert out["content"] == "好"
    assert "stream_options" in calls[0] and calls[0]["stream"] is True
    assert "stream_options" not in calls[1] and calls[1]["stream"] is True
    assert (BASE, MODEL) in al._STREAM_USAGE_UNSUPPORTED
    assert (BASE, MODEL) not in al._STREAM_UNSUPPORTED


def test_stream_4xx_falls_back_to_non_stream_and_never_marks_tools_unsupported(monkeypatch):
    _reset()
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "非流式结果"}}]}).encode()
    calls = _install(monkeypatch, lambda req, n: _http400()
                     if n <= 2 else _Resp(body=body, ctype="application/json"))
    out = al._chat_tools(BASE, "k", MODEL, MESSAGES, "", on_delta=lambda _: None)
    assert out["content"] == "非流式结果"
    assert len(calls) == 3
    assert calls[2]["stream"] is False
    assert (BASE, MODEL) in al._STREAM_UNSUPPORTED
    assert al._TOOLS_UNSUPPORTED is False          # 流式 4xx 不是「端点不支持 tools」


def test_no_delta_callback_keeps_blocking_request(monkeypatch):
    _reset()
    body = json.dumps({"choices": [{"message": {"content": "整包"}}]}).encode()
    calls = _install(monkeypatch, lambda req, n: _Resp(body=body, ctype="application/json"))
    out = al._chat_tools(BASE, "k", MODEL, MESSAGES, "")
    assert out["content"] == "整包"
    assert calls[0]["stream"] is False
    assert out["_streamed"] is False


def test_gateway_ignoring_stream_returns_json_is_parsed(monkeypatch):
    _reset()
    body = json.dumps({"choices": [{"message": {"content": "网关忽略流式"}}]}).encode()
    _install(monkeypatch, lambda req, n: _Resp(body=body, ctype="application/json"))
    out = al._chat_tools(BASE, "k", MODEL, MESSAGES, "", on_delta=lambda _: None)
    assert out["content"] == "网关忽略流式"
    assert out["_streamed"] is False


def test_truncation_after_output_raises_without_retry(monkeypatch):
    _reset()

    def broken(resp):
        for line in _sse({"choices": [{"delta": {"content": "半截"}}]})[:-2]:
            yield line.encode("utf-8")
        raise OSError("connection reset")

    class _BrokenResp(_Resp):
        def __iter__(self):
            return broken(self)

    calls = _install(monkeypatch, lambda req, n: _BrokenResp())
    try:
        al._chat_tools(BASE, "k", MODEL, MESSAGES, "", on_delta=lambda _: None)
        assert False, "should raise"
    except ConnectionError as exc:
        assert "流式中断" in str(exc)
    assert len(calls) == 1          # 已有增量：绝不重试，避免重复副作用


def test_critic_request_never_streams(monkeypatch):
    """核验请求保持整包：评审 JSON 不能走增量，也不得带 stream_options。"""
    _reset()
    body = json.dumps({"choices": [{"message": {"content": '{"verdict":"pass","issues":[]}'}}]}).encode()
    calls = _install(monkeypatch, lambda req, n: _Resp(body=body, ctype="application/json"))
    msg = al._chat_reflect(BASE, "k", MODEL, MESSAGES)
    assert calls[0]["stream"] is False and "stream_options" not in calls[0]
    assert msg["_model"] == MODEL
