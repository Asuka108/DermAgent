# -*- coding: utf-8 -*-
"""v76 Agent 运行时：单轮预算、工具参数修复、失败回注、诚实收尾、事件注册表。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import agent_runtime as rt  # noqa: E402

SCHEMAS = [
    {"type": "function", "function": {
        "name": "update_record_fields",
        "parameters": {"type": "object",
                       "properties": {"fields": {"type": "object"}},
                       "required": ["fields"]}}},
    {"type": "function", "function": {
        "name": "get_case_status",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]


def _call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _run(chat, execute=None, budget=None, allowed=None, schemas=SCHEMAS, emit=None):
    calls = []

    def _execute(fn, args, sess, ctx):
        calls.append((fn, args))
        return {"ok": True, "updated": list(args.keys())}

    res = rt.run_loop(
        [{"role": "user", "content": "右前臂红斑 3 天"}],
        {"case_id": "c1"}, {"llm": ("http://x", "k", "m", "")},
        budget=budget or rt.TurnBudget(max_steps=4, max_tokens=100000),
        emit=emit or (lambda *_: None),
        chat=chat, execute=execute or _execute,
        summarize=lambda fn, args, result: fn, schemas=schemas,
        allowed_tools=allowed)
    return res, calls


# —— 参数解析 ——

def test_parse_tool_args_repairs_code_fence_and_trailing_comma():
    raw = '```json\n{"fields": {"chief_complaint": "红斑",}}\n```'
    args, err = rt.parse_tool_args(raw, SCHEMAS[0]["function"]["parameters"])
    assert err == "" and args == {"fields": {"chief_complaint": "红斑"}}


def test_parse_tool_args_rejects_missing_required_and_bad_type():
    schema = SCHEMAS[0]["function"]["parameters"]
    args, err = rt.parse_tool_args("{}", schema)
    assert args == {} and "缺少必填参数" in err
    args, err = rt.parse_tool_args('{"fields": "不是对象"}', schema)
    assert args == {} and "应为对象" in err


def test_parse_tool_args_empty_is_still_validated():
    # 空参数串不能绕过必填校验（写入型工具必须在执行前被拦住）
    args, err = rt.parse_tool_args("", SCHEMAS[0]["function"]["parameters"])
    assert args == {} and "缺少必填参数" in err
    args, err = rt.parse_tool_args(None, SCHEMAS[1]["function"]["parameters"])
    assert args == {} and err == ""          # 无必填参数的工具允许空参数


def test_parse_tool_args_integer_string_is_coerced():
    top_k_schema = {"type": "object",
                    "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
                    "required": ["query"]}
    args, err = rt.parse_tool_args('{"query": "湿疹", "top_k": "5"}', top_k_schema)
    assert err == "" and args["top_k"] == 5


# —— 预算 ——

def test_token_budget_stops_before_tool_execution():
    def chat(*_a):
        return {"content": "", "tool_calls": [_call("update_record_fields",
                                                    '{"fields": {"chief_complaint": "红斑"}}')],
                "_usage": {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120}}

    res, calls = _run(chat, budget=rt.TurnBudget(max_steps=4, max_tokens=50))
    assert calls == []                                  # 预算耗尽后不执行写工具
    assert "max_tokens" in res.meta["partial_reasons"]
    assert res.meta["status"] == "partial"
    assert "未完成" in res.final and "已完成" not in res.final


def test_missing_usage_falls_back_to_estimate():
    def chat(*_a):
        return {"content": "好的", "tool_calls": []}

    res, _ = _run(chat)
    budget = res.meta["budget"]
    assert budget["tokens_estimated"] is True
    assert budget["tokens_in"] > 0 and budget["tokens_out"] > 0


def test_wall_budget_exhausts_turn():
    b = rt.TurnBudget(max_steps=4, max_tokens=100000, wall_ms=0)
    res, calls = _run(lambda *_: {"content": "不该调用", "tool_calls": []}, budget=b)
    assert calls == [] and "budget_wall" in res.meta["partial_reasons"]


# —— 步数 / 工具失败 ——

def test_max_steps_is_partial_and_not_disguised_as_success():
    def chat(*_a):
        return {"content": "", "tool_calls": [_call("get_case_status", "{}")]}

    res, calls = _run(chat, budget=rt.TurnBudget(max_steps=2, max_tokens=100000))
    assert len(calls) == 2
    assert "max_steps" in res.meta["partial_reasons"]
    assert "未完成" in res.final and "已完成" not in res.final


def test_invalid_args_repaired_once_without_executing_write_tool():
    replies = [
        {"content": "", "tool_calls": [_call("update_record_fields", "{bad json")]},
        {"content": "已记录", "tool_calls": []},
    ]

    def chat(*_a):
        return replies.pop(0)

    res, calls = _run(chat)
    assert calls == []                                  # 写工具绝不在非法参数下执行
    assert res.tool_trace[0]["status"] == "args_invalid"
    assert res.final == "已记录"
    assert res.meta["status"] == "completed"


def test_repeated_invalid_args_stop_the_turn():
    def chat(*_a):
        return {"content": "", "tool_calls": [_call("update_record_fields", "{bad")]}

    res, calls = _run(chat, budget=rt.TurnBudget(max_steps=6, max_tokens=100000))
    assert calls == []
    assert "tool_args_invalid" in res.meta["partial_reasons"]
    assert "未完成" in res.final


def test_tool_exception_is_traced_and_fed_back():
    replies = [
        {"content": "", "tool_calls": [_call("get_case_status", "{}")]},
        {"content": "工具失败了，我改用对话继续", "tool_calls": []},
    ]

    def chat(*_a):
        return replies.pop(0)

    def boom(*_a):
        raise RuntimeError("kb down")

    res, _ = _run(chat, execute=boom)
    assert res.tool_trace[0]["status"] == "error" and "kb down" in res.tool_trace[0]["error"]
    assert res.final.startswith("工具失败")


def test_tool_not_in_plan_is_denied():
    replies = [
        {"content": "", "tool_calls": [_call("update_record_fields", '{"fields": {}}')]},
        {"content": "好的", "tool_calls": []},
    ]

    def chat(*_a):
        return replies.pop(0)

    res, calls = _run(chat, allowed=["get_case_status"])
    assert calls == []
    assert res.tool_trace[0]["status"] == "denied"
    assert res.final == "好的"


# —— 事件注册表 ——

def test_registry_is_idempotent_and_tracks_running_case():
    reg = rt.TurnRegistry(ttl_s=60)
    st, created = reg.begin("case-a", "client-1")
    assert created and reg.is_running("case-a")
    same, created2 = reg.begin("case-a", "client-1")
    assert created2 is False and same.turn_id == st.turn_id
    reg.emit(st.turn_id, "progress", {"message": "第一步"})
    data = reg.read(st.turn_id, 0)
    assert data["events"][0]["type"] == "progress"
    reg.finish(st.turn_id, snapshot={"ok": True})
    assert not reg.is_running("case-a")
    assert reg.read(st.turn_id, 0)["state"] == "done"
    assert reg.read(st.turn_id, 0)["snapshot"] == {"ok": True}


def test_registry_second_turn_on_same_case_is_rejected():
    reg = rt.TurnRegistry(ttl_s=60)
    first, _ = reg.begin("case-b")
    second, created = reg.begin("case-b", "client-2")
    assert created is False and second.turn_id == first.turn_id


def test_loop_emits_tool_event_payload():
    events = []
    state = {"n": 0}

    def sequence(*_a):
        state["n"] += 1
        if state["n"] == 1:
            return {"content": "", "tool_calls": [_call("get_case_status", "{}")]}
        return {"content": "完成", "tool_calls": []}

    res, _ = _run(sequence, emit=lambda t, p: events.append((t, p)))
    tools = [p for t, p in events if t == "tool"]
    assert tools and tools[0]["tool"] == "get_case_status"
    assert tools[0]["args_digest"] and tools[0]["risk"] == "read"
    assert res.final == "完成"
    assert json.dumps(res.meta, ensure_ascii=False)  # meta 必须可序列化
