# -*- coding: utf-8 -*-
"""三层意图识别：规格、第一层守卫、状态先验、确认卡与离线降级。

不依赖 BGE/LLM：向量层与 LLM 层的真实模型验收见 tools/eval_intent.py。
"""

import json
import os
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_BACKEND))
sys.path.insert(0, os.path.join(_BACKEND, "shared"))
sys.path.insert(0, _BACKEND)


def _load_mock_module():
    """按文件路径加载桌面 client——它内部有同名 server/ 符号链接，不能塞进 sys.path。"""
    import importlib.util
    path = os.path.join(_REPO, "win", "backend", "agent_client.py")
    spec = importlib.util.spec_from_file_location("derm_mock_agent_client", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_mock_client():
    return _load_mock_module().MockAgentClient

os.environ.setdefault("LLM_BASE_URL", "")
os.environ.setdefault("LLM_MODEL", "")

import pytest  # noqa: E402

import intent_core  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_GOLDEN = os.path.join(_HERE, "data", "intent_golden.jsonl")


def _golden_rows():
    rows = []
    with open(_GOLDEN, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _state_of(row: dict) -> dict:
    return {"phase": row.get("phase"), "workflow_state": row.get("workflow_state"),
            "has_draft": not row.get("case_empty", True),
            "case_empty": row.get("case_empty", True)}


# —— 规格 ——

def test_spec_covers_all_intents_with_prototypes():
    """意图目录必须覆盖现有业务全集 + 控制类，且每类有可审阅的原型句。"""
    intents = intent_core.spec()["intents"]
    for name in ("case_write", "complete_analysis", "case_edit", "case_organizer", "case_compare",
                 "report_export", "image_analyze", "privacy_deidentify", "body_region_3d",
                 "medical_kb_search", "red_flag_screen", "differential", "treatment_ref",
                 "medication_review", "auxiliary", "patient_edu", "history", "lesion_desc",
                 "summary", "referral", "followup", "smalltalk",
                 "skip_3d", "add_image", "reask", "undo_last", "save_case", "back_to_chat",
                 "show_sources", "open_model", "open_analysis", "settings_control",
                 "context_compress", "confirm", "deny"):
        assert name in intents, f"缺少意图 {name}"
        assert len(intents[name].get("prototypes") or []) >= 18, f"{name} 原型句不足"


def test_spec_thresholds_are_valid():
    th = intent_core.thresholds()
    assert 0 < th["layer2_execute_default"] <= th["layer2_execute_medical"] <= 1
    assert 0 < th["layer3_execute"] < 1
    assert th["lexical_execute_strong"] > th["lexical_candidate"]


# —— 第一层：标注集 ——

def test_golden_l1_is_high_precision():
    """第一层必须零误判：L1 子集全部答对，非 L1 子集一条都不能抢走。"""
    bad = []
    for row in _golden_rows():
        hit = intent_core.classify_rules(row["text"], _state_of(row))
        if row["expect_layer"] == 1:
            if not hit or hit["intent"] != row["expect"]:
                bad.append((row["text"], row["expect"], hit and hit["intent"]))
        elif hit:
            bad.append((row["text"], f"应下放 L2+（{row['expect']}）", hit["intent"]))
    assert not bad, bad


def test_legacy_classification_unchanged():
    """既有 15 条分类断言（app._classify_task）行为不变。"""
    import server.app as sa
    cases = [("请帮我写一份病例", "case_write"), ("请整理现病史", "history"),
             ("请描述皮损情况", "lesion_desc"), ("请生成用药核对清单", "medication_review"),
             ("请把这份病例整理成治疗参考", "treatment_ref"), ("请生成转诊摘要", "referral"),
             ("请生成随访记录", "followup"), ("请生成病例摘要", "summary"),
             ("请导出报告", "report_export"), ("今天天气不错", "case_write")]
    for text, want in cases:
        assert sa._classify_task(text) == want, text


# —— 第一层：四道守卫 ——

def test_negation_guard():
    # 「不要生成诊断和治疗建议」不得抢走「只写现病史」
    hit = intent_core.classify_rules("请只写现病史，不要生成诊断和治疗建议")
    assert hit and hit["intent"] == "history"
    # 被否定的目标单独出现时，L1 不得命中
    assert intent_core.classify_rules("先不要导出报告") is None
    assert intent_core.classify_rules("暂时不生成报告") is None


def test_negation_covers_modal_phrases():
    """「不需要/不必/无须/未」+ 动作同样是否定，不能被当成请求执行。"""
    for text in ("不需要分析图片", "不必做鉴别诊断", "不需要再导出报告",
                 "不想做鉴别诊断", "别再导出报告", "先不做鉴别诊断",
                 "没有做用药核对", "未做脱敏", "没必要做鉴别诊断"):
        assert intent_core.classify_rules(text) is None, text
    # 正常描述里的「不同/不错」不能被误判为否定
    assert intent_core.classify_rules("不同部位的鉴别诊断")["intent"] == "differential"
    assert intent_core.classify_rules("不错的鉴别诊断")["intent"] == "differential"


def test_quote_guard():
    hit = intent_core.classify_rules("医生说「鉴别诊断」这个词不用管，先写现病史")
    assert hit and hit["intent"] == "history"
    assert intent_core.classify_rules("「导出报告」是什么意思") is None


def test_length_guard():
    long_text = ("患者男35岁，皮疹三天，先帮我看看这个情况，另外我想问一下是否需要做鉴别诊断，"
                 "还想知道有没有必要做真菌镜检，以及后续怎么随访")
    assert len(long_text) > 40
    assert intent_core.classify_rules(long_text) is None


def test_multi_target_guard():
    assert intent_core.classify_rules("帮我写现病史，然后再做鉴别诊断") is None
    # 只命中一个关键词但并列了多个动作，也不能只挑一个执行
    assert intent_core.classify_rules("分析一下这张图，然后整理病历，再导出报告") is None
    # 无并列连词的单一目标仍走 L1（保持旧语义）
    hit = intent_core.classify_rules("请整理现病史")
    assert hit and hit["intent"] == "history"
    hit = intent_core.classify_rules("再帮我写一份病历")
    assert hit and hit["intent"] == "case_write"


def test_guarded_and_multi_helpers():
    assert "image_analyze" in intent_core.guarded_intents("不需要分析图片")
    assert "differential" in intent_core.guarded_intents("医生说「鉴别诊断」不用管")
    assert "skip_3d" in intent_core.guarded_intents("不要跳过3D")
    assert intent_core.guarded_intents("分析图片") == set()
    assert intent_core.is_multi_target("分析一下这张图，然后整理病历，再导出报告") is True
    assert intent_core.is_multi_target("打喷嚏，然后皮疹加重") is False


def test_confirm_deny_are_state_gated():
    assert intent_core.classify_rules("确认") is None      # 无待确认上下文
    assert intent_core.classify_rules("确认", {"workflow_state": "awaiting_review"})["intent"] == "confirm"
    assert intent_core.classify_rules("拒绝") is None
    assert intent_core.classify_rules("拒绝", {"workflow_state": "planning"})["intent"] == "deny"


def test_control_commands_precede_business_rules():
    assert intent_core.classify_rules("跳过3D")["intent"] == "skip_3d"
    assert intent_core.classify_rules("保存当前病例")["intent"] == "save_case"
    assert intent_core.classify_rules("把模型改成 qwen")["intent"] == "settings_control"


def test_negated_control_never_fires():
    """控制指令被否定时绝不能执行成相反动作（「不要跳过3D」≠ 跳过3D）。"""
    for text in ("不要跳过3D", "别打开人体模型", "无需撤销刚才的信息", "不要保存当前病例"):
        assert intent_core.classify_rules(text) is None, text
    # 明确指令仍然生效
    assert intent_core.classify_rules("不用3D")["intent"] == "skip_3d"
    assert intent_core.classify_rules("打开人体模型")["intent"] == "open_model"


def test_case_id_slot():
    hit = intent_core.classify_rules("对比 abcdef12")
    assert hit["intent"] == "case_compare" and hit["slots"] == {"case_id": "abcdef12"}


# —— 第二层：状态先验（不依赖真实向量） ——

def test_state_prior_boosts_case_write_in_intake():
    base = {"case_write": 0.70, "differential": 0.72, "settings_control": 0.75}
    out = intent_core.apply_state_prior("患者皮疹三天", {"phase": "case_intake", "case_empty": True}, base)
    assert out["case_write"] > base["case_write"]
    assert out["settings_control"] < base["settings_control"]


def test_state_prior_follows_previous_task_on_reference():
    out = intent_core.apply_state_prior(
        "再来一份", {"task_type": "summary", "has_draft": True}, {"summary": 0.60, "history": 0.62})
    assert out["summary"] > out["history"]


def test_state_prior_clamps_to_unit_interval():
    out = intent_core.apply_state_prior("ok", {"phase": "case_intake", "case_empty": True},
                                        {"case_write": 0.99})
    assert out["case_write"] <= 1.0


# —— 第二层：向量分数再高也不能越过否定/控制/多目标防线 ——

def _patch_rank(monkeypatch, scores):
    import server.intent as ir

    def fake_rank(text, state=None, timeout_ms=300):
        return sorted(scores.items(), key=lambda kv: -kv[1])

    monkeypatch.setattr(ir, "rank_intents", fake_rank)
    return ir


def test_layer2_never_executes_control_or_guarded(monkeypatch):
    cases = [
        ("打开人体模型的相反操作", {"skip_3d": 0.90, "body_region_3d": 0.40}),
        ("不需要分析图片", {"image_analyze": 0.91, "medical_kb_search": 0.30}),
        ("医生说「鉴别诊断」不用管", {"differential": 0.89, "history": 0.34}),
        ("分析一下这张图，然后整理病历，再导出报告",
         {"image_analyze": 0.90, "report_export": 0.40, "case_organizer": 0.30}),
    ]
    for text, scores in cases:
        ir = _patch_rank(monkeypatch, scores)
        out = ir.layer2(text, {})
        assert out and out["execute"] is False, text
        assert out["candidates"], "仍未定案时必须保留候选供第三层/反问"


def test_layer2_still_executes_clear_medical_request(monkeypatch):
    ir = _patch_rank(monkeypatch, {"differential": 0.90, "history": 0.40})
    out = ir.layer2("这个病还要考虑哪些别的诊断", {})
    assert out and out["execute"] is True


def test_layer3_cannot_override_negation(monkeypatch):
    """即使模型把「不要跳过3D」路由成 skip_3d，也不能直接执行。"""
    import server.intent as ir
    monkeypatch.setattr(ir, "layer2", lambda text, state=None: None)
    monkeypatch.setattr(ir, "layer3", lambda text, state, cands, llm=None: {
        "intent": "skip_3d", "confidence": 0.99, "ambiguity": False, "slots": {},
        "alternatives": [], "reason": "fake", "source": "llm"})
    meta = ir.route("不要跳过3D", {}, ("http://x", "", "fake-model", ""))
    assert meta["intent"] is None and meta["needs_confirm"] is True
    assert [c[0] for c in meta["candidates"]] == ["skip_3d"]


# —— 词法兜底 + 确认卡 ——

def test_lexical_candidates_prefers_matching_prototype():
    top = intent_core.lexical_candidates("这个病还要考虑哪些别的诊断")
    assert top and top[0][0] == "differential"


def test_build_pending_and_choose_by_number():
    pending = intent_core.build_pending("x", [{"intent": "differential", "confidence": 0.6},
                                             {"intent": "treatment_ref", "confidence": 0.5}], 2)
    assert pending["id"].startswith("pi_") and len(pending["candidates"]) == 2
    assert intent_core.resolve_pending_choice("2", pending) == ("choose", "treatment_ref")
    assert intent_core.resolve_pending_choice("鉴别分析", pending) == ("choose", "differential")
    assert intent_core.resolve_pending_choice("都不是", pending) == ("requote", None)
    assert intent_core.resolve_pending_choice("确认", pending) == ("reask", None)
    # 取消是「清卡不执行」，不能被当成补充说法重新路由
    assert intent_core.resolve_pending_choice("取消", pending) == ("cancel", None)


def test_build_pending_drops_unknown_intents():
    pending = intent_core.build_pending("x", [{"intent": "not_an_intent"}, {"intent": "summary"}], 3)
    assert [c["intent"] for c in pending["candidates"]] == ["summary"]


def test_mock_offline_routing_degraded():
    """桌面离线：规则命中直接执行；模糊输入出候选卡；无候选回落 case_write 并标注降级。"""
    client = _load_mock_client()()
    doc = client.chat("请整理现病史")
    assert doc["action"] == "doc" and doc["intent"]["layer"] == 1 and doc["intent"]["degraded"] is False

    ask = client.chat("这个病还要考虑哪些别的诊断")
    assert ask["action"] == "intent_confirm" and ask["pending_intent"]["candidates"]
    assert ask["intent"]["needs_confirm"] is True

    # 点选候选项 → 只执行所选意图，不出第二张卡
    chosen = client.chat("1")
    assert chosen["action"] == "llm_unavailable" and chosen["task_type"] == "differential"
    assert chosen.get("pending_intent") is None


def test_mock_offline_lexical_never_guesses_control():
    """词法兜底不得把普通提问猜成控制指令（宁可出卡）。"""
    client = _load_mock_client()()
    res = client.chat("需不需要忌口")
    assert res["action"] == "intent_confirm"
    assert all(c["intent"] != "skip_3d" for c in res["pending_intent"]["candidates"])


def test_mock_offline_never_executes_negated_input():
    """离线也不得把被否定的动作当请求执行（宁可出卡）。"""
    for text in ("不要跳过3D", "别打开人体模型", "不需要分析图片", "不必做鉴别诊断"):
        client = _load_mock_client()()
        res = client.chat(text)
        assert res["action"] == "intent_confirm", text
        assert all(c["intent"] != "skip_3d" for c in res["pending_intent"]["candidates"])


def test_mock_cancel_card_and_deny_plan():
    """取消确认卡 / 计划态拒绝：只回取消说明，不生成新文档。"""
    client = _load_mock_client()()
    client.chat("这个病还要考虑哪些别的诊断")
    res = client.chat("取消")
    assert res["action"] == "reply" and "已取消" in res["reply"]["content"]
    assert res.get("pending_intent") is None

    client = _load_mock_client()()
    client.chat("请整理现病史")
    client.state.update({"workflow_state": "planning"})
    res = client.chat("取消")
    assert res["action"] == "reply" and "已取消" in res["reply"]["content"]
    assert res["task_type"] == "deny"


def test_mock_choose_intent_executes_only_selected():
    """确认卡点选（离线）：只执行所选意图，执行后 pending 即失效（不会重复执行）。"""
    client = _load_mock_client()()
    ask = client.chat("这个病还要考虑哪些别的诊断")
    pending = ask["pending_intent"]
    chosen = pending["candidates"][0]
    res = client.choose_intent(pending["id"], chosen["intent"])
    assert res["task_type"] == chosen["intent"]
    assert res.get("pending_intent") is None
    with pytest.raises(ValueError):
        client.choose_intent(pending["id"], chosen["intent"])


def test_mock_choose_intent_rejects_stale_or_unknown():
    client = _load_mock_client()()
    with pytest.raises(ValueError):
        client.choose_intent("pi_x", "history")          # 无待确认卡
    ask = client.chat("这个病还要考虑哪些别的诊断")
    pending = ask["pending_intent"]
    with pytest.raises(ValueError):
        client.choose_intent("pi_wrong", pending["candidates"][0]["intent"])
    with pytest.raises(ValueError):
        client.choose_intent(pending["id"], "not_an_intent")
    # 失败不消耗 pending：仍可用正确 id 选择
    ok = client.choose_intent(pending["id"], pending["candidates"][0]["intent"])
    assert ok["task_type"] == pending["candidates"][0]["intent"]


def test_server_client_choose_intent_endpoint():
    """服务端客户端：确认卡点选走 /intent/choose，携带 pending_id 与所选意图。"""
    mod = _load_mock_module()
    calls = {}

    class FakeClient(mod.ServerAgentClient):
        def __init__(self):
            self.base_url = "http://x"
            self.timeout = 5
            self.case_id = "abc123"

        def _req(self, path, body=None, method=""):
            calls["path"], calls["body"] = path, body
            return {"case_id": "abc123", "workflow_state": "doc_ready",
                    "task_type": "history", "doc": {"content": "x"}}

    res = FakeClient().choose_intent("pi_1", "history")
    assert calls == {"path": "/api/cases/abc123/intent/choose",
                     "body": {"intent": "history", "pending_id": "pi_1"}}
    assert res["action"] == "doc" and res["task_type"] == "history"
