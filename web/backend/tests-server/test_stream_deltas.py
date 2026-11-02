# -*- coding: utf-8 -*-
"""v79 增量管道：delta 先于工具/核验事件、前导与修正 reset、事件上限、默认不流式。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import agent_runtime as rt  # noqa: E402

SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_case_status",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]


def _call(name, arguments="{}", call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _run(chat, stream=True, budget=None):
    events = []

    def execute(fn, args, sess, ctx):
        return {"ok": True}

    messages = [{"role": "user", "content": "右前臂红斑 3 天"}]
    res = rt.run_loop(
        messages, {"case_id": "c1"},
        {"llm": ("http://x", "k", "m", ""), "stream_deltas": stream},
        budget=budget or rt.TurnBudget(max_steps=4, max_tokens=100000),
        emit=lambda t, p: events.append((t, p)),
        chat=chat, execute=execute,
        summarize=lambda fn, args, result: fn, schemas=SCHEMAS)
    return res, events, messages


def _tool_then_reply(record=None):
    """第一次调用流出前导说明并请求工具；第二次流出最终回复。"""

    def chat(base_url, api_key, model, messages, effort, on_delta=None):
        record.append(("call", len(messages)))
        if len(record) == 1:
            for chunk in ["先", "补充病史。"]:
                on_delta(chunk)
            return {"role": "assistant", "content": "先补充病史。",
                    "tool_calls": [_call("get_case_status")]}
        for chunk in ["右前臂红斑", "，建议保湿。"]:
            on_delta(chunk)
        return {"role": "assistant", "content": "右前臂红斑，建议保湿。"}

    return chat


def test_delta_precedes_tool_and_preamble_is_reset():
    record = []
    res, events, _ = _run(_tool_then_reply(record))
    deltas = [p for t, p in events if t == "message_delta"]
    # 前导复述在工具调用前被 reset，且过程卡有如实说明
    first_reset = next(i for i, (t, p) in enumerate(events)
                       if t == "message_delta" and p.get("reset"))
    first_tool = next(i for i, (t, _p) in enumerate(events) if t == "tool")
    assert first_reset < first_tool
    pre = "".join(p["text"] for _t, p in events[:first_reset] if _t == "message_delta")
    assert pre == "先补充病史。"
    assert any(t == "progress" and p.get("phase") == "tools"
               and "将执行的步骤" in str(p.get("message")) for t, p in events)
    assert res.final == "右前臂红斑，建议保湿。"
    tail = "".join(p["text"] for _t, p in events[first_reset + 1:]
                   if _t == "message_delta" and not p.get("reset"))
    assert tail == res.final
    assert all(d["source"] == "model" for d in deltas)


def test_reflection_event_flushed_after_delta():
    record = []
    _res, events, _ = _run(_tool_then_reply(record))
    delta_idx = max(i for i, (t, _p) in enumerate(events) if t == "message_delta")
    refl_idx = next(i for i, (t, _p) in enumerate(events) if t == "reflection")
    assert delta_idx < refl_idx


def test_without_stream_flag_no_delta_events():
    record = []
    _res, events, _ = _run(_tool_then_reply(record), stream=False)
    assert not [1 for t, _p in events if t == "message_delta"]


def test_legacy_five_param_chat_still_works_when_streaming_enabled():
    calls = []

    def chat(base_url, api_key, model, messages, effort):   # 不接受 on_delta
        calls.append(1)
        for chunk in ["最终", "回复"]:
            pass
        return {"role": "assistant", "content": "最终回复"}

    res, events, _ = _run(chat)
    assert res.final == "最终回复"
    assert not [1 for t, _p in events if t == "message_delta"]   # 能力探测失败 → 不假造增量
    assert len(calls) == 1


def test_long_stream_bounded_events_and_text_complete():
    total = 50000

    def chat(base_url, api_key, model, messages, effort, on_delta=None):
        for i in range(total):
            on_delta("字")
        return {"role": "assistant", "content": "字" * total}

    res, events, _ = _run(chat)
    deltas = [p for t, p in events if t == "message_delta"]
    assert 1 <= len(deltas) <= 301          # 硬上限：远低于 TurnRegistry 的 500
    assert "".join(d["text"] for d in deltas) == "字" * total
    assert res.final == "字" * total


def test_degrade_resets_draft_preview(monkeypatch):
    """核验阻断且修正次数用尽 → 降级文本前 reset，未通过核验的草稿不再作为正文。"""

    draft = "建议使用激素，疗程 2 周。"

    def chat(base_url, api_key, model, messages, effort, on_delta=None):
        on_delta(draft)                     # 触发 regimen 规则（无检索依据的疗程）
        return {"role": "assistant", "content": draft}

    res, events, _ = _run(chat, budget=rt.TurnBudget(max_steps=1, max_tokens=100000,
                                                     max_reflection_calls=0))
    deltas = [p for t, p in events if t == "message_delta"]
    assert any(d.get("reset") for d in deltas)
    assert res.final != draft and "核验" in res.final
