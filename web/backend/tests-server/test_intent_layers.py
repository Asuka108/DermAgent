# -*- coding: utf-8 -*-
"""第二层（向量+状态先验）与第三层（LLM 结构化路由）编排：全部用假实现，不联网。"""

import json
import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BACKEND, "shared"))
sys.path.insert(0, os.path.join(_BACKEND, "server"))
sys.path.insert(0, _BACKEND)

os.environ.setdefault("LLM_BASE_URL", "")
os.environ.setdefault("LLM_MODEL", "")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import intent  # noqa: E402
import rag_embed  # noqa: E402

LLM = ("http://127.0.0.1:9", "test-key", "fake-model", "")


def _matrix():
    return ([("differential", "鉴别诊断"), ("treatment_ref", "治疗方向")],
            np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32))


def _vecs(vector):
    return lambda texts, **kw: [list(vector) for _ in texts]


def _tool_call(args):
    return {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "route_intent", "arguments": json.dumps(args, ensure_ascii=False)}}]}}]}


@pytest.fixture(autouse=True)
def _clear_l3_cache():
    intent._L3_CACHE.clear()
    yield
    intent._L3_CACHE.clear()


def test_layer2_executes_on_high_confidence(monkeypatch):
    monkeypatch.setattr(intent, "_proto_matrix", _matrix)
    monkeypatch.setattr(rag_embed, "embed_texts", _vecs([0.95, 0.31]))
    out = intent.route("这个病还要考虑哪些别的诊断", {"phase": "doc_ready"})
    assert out["layer"] == 2 and out["intent"] == "differential"
    assert out["source"] == "vector" and out["needs_confirm"] is False


def test_layer2_ambiguous_goes_to_confirm_when_no_llm(monkeypatch):
    monkeypatch.setattr(intent, "_proto_matrix", _matrix)
    monkeypatch.setattr(rag_embed, "embed_texts", _vecs([0.71, 0.69]))
    out = intent.route("这个病还要考虑哪些别的诊断", {"phase": "doc_ready"})
    assert out["needs_confirm"] is True and out["intent"] is None
    assert out["candidates"][0][0] in ("differential", "treatment_ref")


def test_layer2_skipped_when_model_not_ready(monkeypatch):
    monkeypatch.setattr(intent, "_proto_matrix", lambda: (None, None))
    out = intent.route("患者男，35岁，右前臂红斑伴瘙痒3天", {"phase": "case_intake"})
    assert out["layer"] == 0 and out["intent"] == "case_write" and out["degraded"] is True


def test_layer3_executes_structured_route(monkeypatch):
    monkeypatch.setattr(intent, "_proto_matrix", lambda: (None, None))
    monkeypatch.setattr(intent, "_post", lambda *a, **kw: _tool_call(
        {"intent": "treatment_ref", "confidence": 0.91, "ambiguity": False,
         "reason": "医生在问怎么治"}))
    out = intent.route("这个病一般怎么治", {}, llm=LLM)
    assert out["layer"] == 3 and out["intent"] == "treatment_ref" and out["source"] == "llm"


def test_layer3_ambiguity_yields_confirm_card(monkeypatch):
    monkeypatch.setattr(intent, "_proto_matrix", lambda: (None, None))
    monkeypatch.setattr(intent, "_post", lambda *a, **kw: _tool_call(
        {"intent": "summary", "confidence": 0.6, "ambiguity": True,
         "alternatives": [{"intent": "history", "reason": "也可能是现病史"}],
         "reason": "两种文书都可能"}))
    out = intent.route("帮我整理一下刚才那段", {}, llm=LLM)
    assert out["needs_confirm"] is True and out["intent"] is None
    opts = [c[0] for c in out["candidates"]]
    assert "summary" in opts and "history" in opts


def test_layer3_low_confidence_not_executed(monkeypatch):
    monkeypatch.setattr(intent, "_proto_matrix", lambda: (None, None))
    monkeypatch.setattr(intent, "_post", lambda *a, **kw: _tool_call(
        {"intent": "differential", "confidence": 0.4, "ambiguity": False, "reason": "拿不准"}))
    out = intent.route("看看这个", {}, llm=LLM)
    assert out["needs_confirm"] is True and out["intent"] is None


def test_layer3_rejects_unknown_intent_and_bad_slots(monkeypatch):
    monkeypatch.setattr(intent, "_proto_matrix", lambda: (None, None))
    monkeypatch.setattr(intent, "_post", lambda *a, **kw: _tool_call(
        {"intent": "delete_everything", "confidence": 0.99, "ambiguity": False, "reason": "x"}))
    out = intent.route("随便", {}, llm=LLM)
    assert out["intent"] == "case_write" and out["source"] == "default"

    monkeypatch.setattr(intent, "_post", lambda *a, **kw: _tool_call(
        {"intent": "settings_control", "confidence": 0.9, "ambiguity": False,
         "slots": {"language": "en", "case_id": "C1", "evil": "x"}, "reason": "改语言"}))
    out2 = intent.route("把语言切成英文", {}, llm=LLM)
    assert out2["intent"] == "settings_control"
    assert out2["slots"] == {"language": "en"}


def test_layer3_unreachable_falls_back_without_raising(monkeypatch):
    monkeypatch.setattr(intent, "_proto_matrix", lambda: (None, None))

    def boom(*a, **kw):
        raise ConnectionError("down")
    monkeypatch.setattr(intent, "_post", boom)
    out = intent.route("随便说点什么", {}, llm=LLM)
    assert out["intent"] == "case_write" and out["degraded"] is True


def test_layer3_cache_avoids_repeat_call(monkeypatch):
    monkeypatch.setattr(intent, "_proto_matrix", lambda: (None, None))
    calls = {"n": 0}

    def post(*a, **kw):
        calls["n"] += 1
        return _tool_call({"intent": "summary", "confidence": 0.9, "ambiguity": False, "reason": "摘要"})
    monkeypatch.setattr(intent, "_post", post)
    assert intent.route("帮我出个三句话的版本", {}, llm=LLM)["intent"] == "summary"
    assert intent.route("帮我出个三句话的版本", {}, llm=LLM)["intent"] == "summary"
    assert calls["n"] == 1


def test_route_candidates_are_labelled_for_card():
    meta = {"candidates": [["differential", 0.7], ["summary", 0.2], ["nope", 0.1]]}
    card = intent.candidates_for_card(meta)
    assert [c["intent"] for c in card] == ["differential", "summary"]
    assert all(c["label"] for c in card)
