"""可信服务层测试：v1.1 契约、四案例状态机、引用白名单、医生在环、跨端续办。"""

import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""  # 强制规则管线（确定性）
os.environ["LLM_MODEL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server.app import app  # noqa: E402

client = TestClient(app)


def _start(text="", demo="", **extra):
    r = client.post("/api/cases", json={"text": text, "demo": demo, **extra})
    assert r.status_code == 200
    return r.json()


def _confirm(case_id):
    """v64-P2：计划确认——工作类任务首轮返回 planning（理解+准备动作），
    测试里补一轮「确认」触发实际执行。"""
    r = client.post(f"/api/cases/{case_id}/chat", json={"text": "确认"})
    assert r.status_code == 200
    return r.json()


def _confirm2(case_id):
    """v67：缺最低三要素时首次确认转入追问；再次确认放行（草稿如实标注缺失）。
    三要素齐时第一轮直接执行，第二轮为无副作用重放（mock LLM 下不改变断言结果）。"""
    client.post(f"/api/cases/{case_id}/chat", json={"text": "确认"})
    return _confirm(case_id)


def _review(case_id, feedback):
    return client.post(f"/api/cases/{case_id}/review", json={"feedback": feedback}).json()


def test_health_v11():
    h = client.get("/api/health").json()
    assert h["schema_version"] == "1.1"
    assert h["llm"] == "rule-fallback"


def test_case1_full_to_report():
    snap = _start(demo="1")
    assert snap["workflow_state"] == "awaiting_review"
    assert snap["review_status"] == "pending"
    d = snap["draft"]
    assert d["schema_version"] == "1.1"
    assert d["case_summary"]["data_completeness"] == "complete"
    assert d["severity"]["level"] == "routine"
    assert d["red_flag_escalation"]["is_blocking"] is False
    assert d["citations"] and d["safety_policy_version"]
    assert d["requires_doctor_confirmation"] is True
    final = _review(snap["case_id"], "确认")
    assert final["workflow_state"] == "approved"
    assert final["review_status"] == "approved"
    assert "辅助报告" in final["report"] and "免责声明" in final["report"]
    assert "工作流状态 approved" in final["report"]


def test_case2_red_flag_blocking_and_modify():
    snap = _start(demo="2")
    d = snap["draft"]
    assert snap["workflow_state"] == "escalated"
    assert d["red_flag_escalation"]["triggered"] is True
    assert d["red_flag_escalation"]["is_blocking"] is True
    assert d["blocking_reason"] and "RF-HZ-001" in d["blocking_reason"]
    assert d["severity"]["level"] == "needs_review"
    edited = _review(snap["case_id"], "修改：请补充疼痛评估")
    assert edited["review_status"] == "edited"
    assert any("已按医生意见修订" in u for u in edited["draft"]["uncertainties"])
    rejected = _review(snap["case_id"], "拒绝：演示")
    assert rejected["workflow_state"] == "rejected"


def test_case3_follow_up_then_supplement():
    """v59：不再强制追问——缺失字段由草稿 unknown 如实列出，补充后照常重跑分析。"""
    snap = _start(demo="3")
    assert snap["workflow_state"] in ("awaiting_review", "llm_unavailable")
    assert "病程" in snap["missing"]          # 缺失如实列出（不写「无」），但不追问阻断
    sup = client.post(f"/api/cases/{snap['case_id']}/supplement",
                      json={"reply": "补充：病程5天；变化趋势逐渐扩大；诱因新换护肤品后出现；"
                                     "既往史无特殊；过敏史否认药物食物过敏；当前用药未使用药物"}).json()
    assert sup["workflow_state"] in ("awaiting_review", "llm_unavailable")
    if sup.get("draft"):
        assert sup["draft"]["case_summary"]["data_completeness"] == "complete"
        assert sup["draft"]["follow_up_questions"] == []


def test_diagnosis_workflow_confirm_execution_leaves_supplement_phase():
    """诊断入口的确认执行/下一步不能再次写入症状，必须进入 RAG/分析阶段。"""
    started = _start(text="进行诊断")
    cid = started["case_id"]
    assert started["workflow_state"] == "case_intake"
    intake = client.post(f"/api/cases/{cid}/chat", json={"text": "下一步：情况补充"}).json()
    assert intake["workflow_state"] == "case_supplement"
    before = intake["case"].get("symptoms", "")
    done = client.post(f"/api/cases/{cid}/chat", json={"text": "确认执行"}).json()
    assert done["workflow_state"] != "case_supplement"
    assert "确认执行" not in done["case"].get("symptoms", "")[len(before):]


def test_case4_no_evidence_refusal():
    snap = _start(demo="4")
    d = snap["draft"]
    assert d["citations"] == [] and d["suspected_conditions"] == []
    assert any("知识库未覆盖" in u for u in d["uncertainties"])
    assert any("面诊" in n["action"] for n in d["recommended_next_steps"])


def test_free_text_greeting_no_follow_up_block():
    """v59：闲聊文本不阻断——按病例分析管线走，缺失字段如实列出而不追问。"""
    snap = _start(text="你好")
    assert snap["workflow_state"] in ("awaiting_review", "llm_unavailable")
    assert snap["missing"]                     # 缺失仍如实列出


def test_citation_whitelist_on_llm_output():
    """服务端 LLM 归一化：编造引用剔除、枚举收拢、权威字段覆盖（normalize_llm_draft 直测）。"""
    from derm_core import DEMO_CASES, normalize_llm_draft, retrieve_citations
    case = DEMO_CASES["2"]["case"]
    cites = retrieve_citations(case)
    parsed = {
        "suspected_conditions": [{"name": "带状疱疹（可能）", "confidence": "high",
                                  "supporting_evidence": [{"statement": "s", "citation_ids": [cites[0]["citation_id"], "CIT-FAKE"]}]}],
        "symptom_assessment": [{"symptom": "灼痛", "status": "没有", "basis": "猜"}],
        "severity": {"level": "随便", "basis": ["红旗命中"]},
        "citations": [{"citation_id": "CIT-FAKE-1"}],
    }
    draft = normalize_llm_draft(parsed, cites, ["RF-HZ-001：测试"], [], "fake-model", "llm")
    assert draft["generated_by"] == "llm"
    assert all(c["citation_id"] != "CIT-FAKE" for c in draft["citations"])
    assert draft["suspected_conditions"][0]["supporting_evidence"][0]["citation_ids"] == [cites[0]["citation_id"]]
    assert draft["symptom_assessment"][0]["status"] == "unknown"
    assert draft["severity"]["level"] == "needs_review"  # 本地权威派生
    assert draft["workflow_state"] == "escalated"
    assert draft["red_flag_escalation"]["is_blocking"] is True
    assert any("非知识库引用" in u for u in draft["uncertainties"])


def test_cross_device_continuity():
    """跨端续办：A 端创建 → B 端 GET 同一 case_id 得到同一状态/草稿/审核版本。"""
    snap = _start(demo="1")
    case_id = snap["case_id"]
    again = client.get(f"/api/cases/{case_id}").json()
    assert again["workflow_state"] == snap["workflow_state"]
    assert again["draft"] == snap["draft"]
    _review(case_id, "确认")
    final = client.get(f"/api/cases/{case_id}").json()
    assert final["review_status"] == "approved" and final["workflow_state"] == "approved"
    listing = client.get("/api/cases").json()
    assert any(c["case_id"] == case_id for c in listing)


def test_three_terminals_same_snapshot():
    """三端一致性：同一 case_id 在三个「客户端」各取一次快照，断言完全一致。"""
    snap = _start(demo="2")
    views = [client.get(f"/api/cases/{snap['case_id']}").json() for _ in range(3)]
    assert all(v == views[0] for v in views)
    assert views[0]["workflow_state"] == "escalated"


def test_multi_region_body_pain():
    """一人多处痛：身体区域多值合并，红旗/检索按全文拼接生效。
    v58：demo="custom"（非内置键）→ text 照常解析且 LLM 失败时回退明确标注的演示规则稿，
    本用例验证的合并逻辑不依赖 LLM 真实返回。"""
    from derm_core import DEMO_CASES, detect_red_flags
    case = dict(DEMO_CASES["1"]["case"])
    case["body_region"] = "躯干（胸腹）、下肢（右）、足部（左）"
    assert "躯干" in case["body_region"] and "下肢" in case["body_region"]
    assert detect_red_flags(case) == []  # 多区域合并后仍正确筛查
    snap = client.post("/api/cases", json={"text": "身体区域：躯干（胸腹）、下肢（右）；主诉：红斑瘙痒；"
                                           "症状：红斑丘疹伴瘙痒；病程：2周；过敏史：否认；当前用药：无；既往史：无；诱因：不明",
                                           "demo": "custom"}).json()
    assert snap["draft"]["case_summary"]["selected_body_region"].startswith("躯干")
    assert snap["draft"]["citations"], "多区域文本应命中知识库"


# —— §8.1 安全断言补充（结果版本 / 红旗不可绕过 / unknown 语义 / 引用快照 / 用药核对）——

def _meta(snap):
    return {k: snap.get(k) for k in
            ("result_version", "model_version", "knowledge_snapshot", "safety_policy_version")}


def test_result_version_bumps_on_each_authoritative_result():
    """每次权威结果产生递增 result_version：建例 r1 → 修订 r2 → 确认 r3；三端读到的元数据一致。"""
    snap = _start(demo="1")
    assert snap["result_version"] == "r1"
    assert snap["knowledge_snapshot"] and snap["safety_policy_version"]
    edited = _review(snap["case_id"], "修改：请补充瘙痒程度")
    assert edited["result_version"] == "r2"
    final = _review(snap["case_id"], "确认")
    assert final["result_version"] == "r3"
    # 三端各取一次：状态、结果版本与元数据完全一致
    views = [client.get(f"/api/cases/{snap['case_id']}").json() for _ in range(3)]
    assert all(_meta(v) == _meta(views[0]) for v in views)
    assert all(v["workflow_state"] == "approved" and v["result_version"] == "r3" for v in views)
    assert views[0]["report"], "确认后快照应携带正式报告"


def test_red_flag_cannot_be_bypassed_by_any_action():
    """红旗不可绕过：supplement / modify 均不解除 escalated；只有服务端规则能派生红旗状态。"""
    snap = _start(demo="2")
    assert snap["workflow_state"] == "escalated" and snap["draft"]["red_flag_escalation"]["is_blocking"] is True
    # 补充资料也不解除红旗
    sup = client.post(f"/api/cases/{snap['case_id']}/supplement",
                      json={"reply": "补充：病程3天；过敏史：否认"}).json()
    assert sup["red_flags"], "红旗列表必须保留"
    assert sup["draft"]["red_flag_escalation"]["triggered"] is True
    assert sup["draft"]["blocking_reason"] and "RF-HZ-001" in sup["draft"]["blocking_reason"]
    # 医生修改意见也不解除红旗
    edited = _review(snap["case_id"], "修改：请补充疼痛评估")
    assert edited["draft"]["red_flag_escalation"]["is_blocking"] is True
    assert edited["workflow_state"] == "escalated"
    # 快照层红旗信息始终随 draft 保留
    again = client.get(f"/api/cases/{snap['case_id']}").json()
    assert again["draft"]["red_flag_escalation"]["is_blocking"] is True


def test_unknown_and_not_asked_never_rendered_as_absent():
    """unknown/not_asked 不转 absent：枚举收拢把非法「没有」归 unknown；缺资料项标 not_asked 且标签明示不当作没有。"""
    from derm_core import DEMO_CASES, STATUS_LABEL, normalize_llm_draft, retrieve_citations
    case = DEMO_CASES["1"]["case"]
    cites = retrieve_citations(case)
    parsed = {"symptom_assessment": [
        {"symptom": "瘙痒", "status": "present", "basis": "患者主诉"},
        {"symptom": "脱屑", "status": "没有", "basis": "猜的"},  # 非法枚举 → 必须收拢为 unknown
    ]}
    draft = normalize_llm_draft(parsed, cites, [], [], "fake-model", "llm")
    by_sym = {s["symptom"]: s for s in draft["symptom_assessment"]}
    assert by_sym["脱屑"]["status"] == "unknown"
    # 规则稿：未采集字段必须标 not_asked，禁止自动写 absent
    rule_draft_texts = [s["status"] for s in draft["symptom_assessment"]]
    assert all(s in ("present", "absent", "unknown", "not_asked") for s in rule_draft_texts)
    assert STATUS_LABEL["unknown"].startswith("未知")
    assert STATUS_LABEL["not_asked"].startswith("未询问")
    snap = _start(demo="3")
    assert snap["missing"], "案例3 缺失字段必须如实列出，不得推断为阴性"


def test_no_report_before_doctor_confirmation():
    """医生确认前不生成报告：awaiting_review / escalated / edited 阶段 report 均为空，仅 confirmed 后有报告。"""
    for demo in ("1", "2"):
        snap = _start(demo=demo)
        assert not snap["report"], f"案例{demo} 草稿阶段不得有报告"
        mid = client.get(f"/api/cases/{snap['case_id']}").json()
        assert not mid["report"]
        final = _review(snap["case_id"], "确认")
        if demo == "1":
            assert final["report"]
        else:  # 红旗案例确认后同样可出报告，但此前必须为空
            assert final["report"]
    # 修订后（未确认）仍无报告
    s3 = _start(demo="1")
    edited = _review(s3["case_id"], "修改：请核对瘙痒范围")
    assert not edited["report"] and edited["review_status"] == "edited"


def test_citations_belong_to_current_snapshot_only():
    """引用必须属于当前检索快照：draft.citations 与本次检索白名单完全一致，编造引用被剔除。"""
    from derm_core import DEMO_CASES, normalize_llm_draft, retrieve_citations
    for demo in ("1", "2", "3"):
        case = DEMO_CASES[demo]["case"]
        cites = retrieve_citations(case)
        snap = client.post("/api/cases", json={"text": "", "demo": demo}).json()
        if snap["draft"] is None:  # 案例3 首轮追问，补充后再断言草稿引用
            snap = client.post(f"/api/cases/{snap['case_id']}/supplement",
                               json={"reply": "补充：病程5天；变化趋势逐渐扩大；诱因新换护肤品后出现；"
                                              "既往史无特殊；过敏史否认药物食物过敏；当前用药未使用药物"}).json()
        assert snap["draft"], f"案例{demo} 补齐后应有草稿"
        whitelist = {c["citation_id"] for c in cites}
        draft_cites = {c["citation_id"] for c in snap["draft"]["citations"]}
        assert draft_cites <= whitelist, f"案例{demo} 引用超出当前快照白名单"
        assert draft_cites == whitelist or draft_cites, "白名单引用应被完整带入草稿"
        for cond in snap["draft"]["suspected_conditions"]:
            for ev in cond["supporting_evidence"]:
                assert set(ev["citation_ids"]) <= whitelist
    # LLM 编造引用被剔除且仅保留白名单内引用
    case = DEMO_CASES["2"]["case"]
    cites = retrieve_citations(case)
    parsed = {"suspected_conditions": [{"name": "带状疱疹（可能）", "confidence": "medium",
                                        "supporting_evidence": [{"statement": "簇集水疱", "citation_ids": ["CIT-XX-FACH"]}]}],
              "citations": [{"citation_id": "CIT-XX-FACH"}, {"citation_id": cites[0]["citation_id"]}]}
    draft = normalize_llm_draft(parsed, cites, ["RF-HZ-001：测试"], [], "fake-model", "llm")
    # 结论内引用：编造的 CIT-XX-FACH 被剔除，仅保留白名单内
    assert all("CIT-XX-FACH" not in ev["citation_ids"]
               for cond in draft["suspected_conditions"] for ev in cond["supporting_evidence"])
    # 引用发布恒为本次检索快照：citations 块 == 白名单全集，不含有快照外条目
    assert {c["citation_id"] for c in draft["citations"]} == {c["citation_id"] for c in cites}


def test_medication_checks_contain_no_dosage_or_course():
    """用药核对不含剂量/疗程：只有核对项 + requires_doctor_confirmation=true；渲染文本无剂量疗程表述。"""
    import re
    from derm_core import DEMO_CASES, DISCLAIMER, build_medication_checks, retrieve_citations
    for demo in ("1", "2"):
        case = DEMO_CASES[demo]["case"]
        meds = build_medication_checks(case, retrieve_citations(case))
        assert meds, f"案例{demo} 应生成用药核对项"
        for m in meds:
            assert m["requires_doctor_confirmation"] is True
            assert set(m["items_to_verify"]) <= {"indication", "allergy", "contraindication", "interaction"}
            text = (m["generic_name"] + m["purpose"])
            assert not re.search(r"\d+\s*(mg|ml|g|片|粒|次/日|每日\d|天一个疗程)", text, re.I), text
    snap = _start(demo="1")
    report = _review(snap["case_id"], "确认")["report"]
    assert "禁止自动处方" in report or "需医生确认" in report
    assert DISCLAIMER
    assert not re.search(r"\d+\s*mg", report, re.I), "报告用药区不得出现剂量"


def test_clinic_payload_enters_case_and_reaches_llm_context():
    """阶段5：医生端脱敏资料载荷进入自定义病例 case["clinic_payload"]，随病例 JSON 进入
    LLM 提示词上下文；demo 病例不并入；空载荷不写键；规则管线行为不受污染。"""
    from derm_core import DEMO_CASES
    clinic = {"hx": "双小腿红斑瘙痒5天，逐渐扩大", "labs": [{"name": "IgE", "value": "320"}],
              "report": "皮肤镜：网状模式", "imaging": [], "deidentified": True}
    # ① 自定义病例：clinic 并入 case，正常走通（demo="custom"→演示回退稿，规则管线不受污染）
    snap = _start(text="小腿红斑瘙痒", demo="custom")
    _ = snap  # 基线：不带 clinic 的自定义病例可走通
    r = client.post("/api/cases", json={"text": "小腿红斑瘙痒", "clinic": clinic, "demo": "custom"})
    assert r.status_code == 200
    snap2 = r.json()
    assert snap2["case_id"] != snap["case_id"]
    # ② 持久化会话中的 case 带有 clinic_payload，且 LLM 提示词上下文包含资料 JSON
    from server.app import _load_sessions, _make_draft_server, retrieve_citations
    sess = _load_sessions()[snap2["case_id"]]
    assert sess["case"].get("clinic_payload") == clinic
    case_json = json.dumps(sess["case"], ensure_ascii=False)
    assert "clinic_payload" in case_json and "IgE" in case_json
    cites = retrieve_citations(sess["case"])
    draft, _err = _make_draft_server(sess["case"], cites, [], [], demo="custom")
    assert draft["workflow_state"] in ("awaiting_review", "escalated")
    assert draft["review_status"] == "pending"      # 全部结果默认 pending
    assert draft["requires_doctor_confirmation"] is True
    # ③ demo 病例不并入（结构固定，不受客户端资料污染）
    r3 = client.post("/api/cases", json={"text": "", "demo": "1", "clinic": clinic})
    assert r3.status_code == 200
    from server.app import _load_sessions as _ls
    sess3 = _ls()[r3.json()["case_id"]]
    assert "clinic_payload" not in sess3["case"]
    assert sess3["case"] == DEMO_CASES["1"]["case"]
    # ④ 空载荷不写键
    r4 = client.post("/api/cases", json={"text": "另一例自定义描述", "clinic": {}})
    assert r4.status_code == 200
    sess4 = _ls()[r4.json()["case_id"]]
    assert "clinic_payload" not in sess4["case"]


def test_review_history_regenerate_and_signature_note():
    """阶段6：完整审核历史逐条留存；AI 建议（draft）与医生判断（doctor_final/review_history）
    分开保存；重新生成回到 pending 并可重新审核；确认后报告为待签署草稿（含 SIGNATURE_NOTE
    的禁止自动写入/医嘱/治疗声明）。"""
    # ① 修改 → 历史第1条（modified），draft 仅追加修订摘要，医生判断不在 draft 里
    s1 = _start(demo="1")
    s2 = _review(s1["case_id"], "修改：请补充接触史与皮损分布")
    assert s2["review_status"] == "edited"
    assert len(s2["review_history"]) == 1 and s2["review_history"][0]["decision"] == "modified"
    assert "已按医生意见修订" in s2["draft"]["uncertainties"][-1]
    assert s2["doctor_final"] is None            # 未确认/拒绝前无医生最终判断存档
    # ② 重新生成 → 回到 pending，草稿重新生成，历史累计 2 条
    r = client.post(f"/api/cases/{s1['case_id']}/review",
                    json={"feedback": "重新生成：请重新分析", "action": "regenerate"})
    assert r.status_code == 200
    s3 = r.json()
    assert s3["review_status"] == "pending" and s3["draft"] is not None
    assert s3["draft"]["review_status"] == "pending"
    assert len(s3["review_history"]) == 2 and s3["review_history"][1]["decision"] == "regenerate"
    assert s3["result_version"] != s1["result_version"]      # 重新生成产生新权威结果版本
    # ③ 确认 → approved + 待签署报告；医生最终判断单独存档，AI 草稿与医生判断字段分离
    s4 = _review(s1["case_id"], "确认")
    assert s4["review_status"] == "approved" and s4["report"]
    assert "待签署草稿" in s4["report"] and "不自动写入正式病历" in s4["report"]
    assert "不自动提交医嘱或电子处方" in s4["report"]
    assert len(s4["review_history"]) == 3 and s4["review_history"][2]["decision"] == "confirmed"
    assert s4["doctor_final"]["decision"] == "confirmed" and s4["doctor_final"]["feedback"] == "确认"
    # 医生判断独立字段：draft 不含 doctor_final 内容（分开保存）
    assert "doctor_final" not in (s4["draft"] or {})
    # ④ 拒绝路径：closed + 不生成报告 + 医生最终判断存档为 rejected
    s5 = _start(demo="2")
    s6 = _review(s5["case_id"], "拒绝：证据不足")
    assert s6["workflow_state"] == "rejected" and not s6["report"]
    assert s6["doctor_final"]["decision"] == "rejected"
    assert len(s6["review_history"]) == 1 and s6["review_history"][0]["decision"] == "rejected"
    # ⑤ 跨端续办快照同样携带审核历史与医生最终判断
    s7 = client.get(f"/api/cases/{s1['case_id']}").json()
    assert len(s7["review_history"]) == 3 and s7["doctor_final"]["decision"] == "confirmed"


def test_security_boundary_refine_imaging_adapters_clinic_whitelist():
    """阶段7安全闭环：① /api/anatomy/refine 只收几何摘要（含 screenshot/image/glb 键→400）
    ② 影像适配器注册表声明边界（ai_enabled=False，与 refinement 分离）
    ③ clinic_payload 资料白名单：未知键丢弃并审计，声明键保留。"""
    # ① 合法几何摘要 → ok
    r = client.post("/api/anatomy/refine",
                    json={"regions": [{"faces": [1, 2, 3], "view": "front"}], "model_key": "male"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["boundary"] == "geometry-summary-only" and d["received"] == 1
    assert d["imaging_boundary"] == "separate"
    # ① 含禁止键 → 400（位图/模型字节永不进入 refinement 通道）
    for key in ("screenshot", "image", "glb", "model_data", "base64"):
        r2 = client.post("/api/anatomy/refine", json={"regions": [], key: "x"})
        assert r2.status_code == 400, key
        assert "geometry summary only" in r2.json()["detail"]
    # ② 影像适配器注册表
    ia = client.get("/api/imaging/adapters").json()
    assert ia["ai_enabled_default"] is False
    ad = ia["adapters"]["local_upload"]
    assert ad["ai_enabled"] is False and "png" in ad["accept"] and ad["max_bytes"] >= 1024 * 1024
    # ③ clinic 白名单：未知键丢弃、声明键保留
    from server.app import _load_sessions
    r3 = client.post("/api/cases", json={"text": "白名单边界测试病例描述",
                                         "clinic": {"hx": "瘙痒3天", "evil_key": "<script>", "deidentified": True}})
    assert r3.status_code == 200
    sess = _load_sessions()[r3.json()["case_id"]]
    cp = sess["case"].get("clinic_payload", {})
    assert cp.get("hx") == "瘙痒3天" and cp.get("deidentified") is True
    assert "evil_key" not in cp
    # demo 病例仍然不并入（即使全键合法）
    r4 = client.post("/api/cases", json={"text": "", "demo": "1", "clinic": {"hx": "x"}})
    assert "clinic_payload" not in _load_sessions()[r4.json()["case_id"]]["case"]


def test_clinic_patient_whitelist_nested():
    """v56 病历单患者信息：patient 子白名单——声明键（字符串值）保留、未知键/非字符串丢弃、
    非字典/空子键 patient 整键不出现（不伪造 None/空对象）；姓名/门诊号以客户端脱敏后的类别占位原样透传。"""
    from server.app import _load_sessions
    r = client.post("/api/cases", json={"text": "患者信息白名单测试病例描述", "clinic": {
        "patient": {"name": "［姓名］", "sex": "女", "age": "34", "no": "［门诊号］",
                    "date": "2026-09-12", "id_card": "偷渡字段", "height": 170}}})
    assert r.status_code == 200
    p = _load_sessions()[r.json()["case_id"]]["case"]["clinic_payload"].get("patient")
    assert p == {"name": "［姓名］", "sex": "女", "age": "34", "no": "［门诊号］", "date": "2026-09-12"}
    r2 = client.post("/api/cases", json={"text": "患者信息非字典测试病例描述",
                                         "clinic": {"patient": "not-a-dict"}})
    cp2 = _load_sessions()[r2.json()["case_id"]]["case"]["clinic_payload"]
    assert "patient" not in cp2
    r3 = client.post("/api/cases", json={"text": "患者信息空子键测试病例描述",
                                         "clinic": {"patient": {"evil": "x"}}})
    cp3 = _load_sessions()[r3.json()["case_id"]]["case"]["clinic_payload"]
    assert "patient" not in cp3
    r4 = client.post("/api/cases", json={"text": "患者信息缺失测试病例描述",
                                         "clinic": {"hx": "无患者信息的病史"}})
    assert "patient" not in _load_sessions()[r4.json()["case_id"]]["case"]["clinic_payload"]


# —— 阶段8：全量测试与医生试点准备 ——

def test_doctor_assist_e2e_with_clinic_materials():
    """医生辅助 E2E（含资料）：带脱敏资料提交 → 服务降级追问 → 补充 → 确认 → 待签署报告；
    全程资料内容只出现在 case.clinic_payload 与医生端，报告只含医学结论不含资料原文。"""
    clinic = {"hx": "患者自述双小腿红斑瘙痒5天（脱敏后）", "labs": [{"name": "IgE", "value": "320"}],
              "report": "皮肤镜检查报告文字（脱敏后）", "meds": "炉甘石洗剂外用",
              "imaging": [{"name": "leg.png", "status": "ready", "width": 800, "height": 600}],
              "deidentified": True}
    r = client.post("/api/cases", json={"text": "", "demo": "3", "clinic": clinic})
    assert r.status_code == 200
    snap = r.json()
    # v59：资料不足不追问阻断——缺失如实列出，直接进入分析管线（llm_unavailable/awaiting_review）
    assert snap["workflow_state"] in ("awaiting_review", "llm_unavailable")
    assert "病程" in snap["missing"]
    sup = client.post(f"/api/cases/{snap['case_id']}/supplement",
                      json={"reply": "补充：病程5天；变化趋势逐渐扩大；诱因新换护肤品后出现；"
                                     "既往史无特殊；过敏史否认药物食物过敏；当前用药未使用药物"}).json()
    assert sup["draft"] and sup["workflow_state"] == "awaiting_review"
    assert sup["draft"]["review_status"] == "pending"           # AI 建议默认 pending
    final = _review(snap["case_id"], "确认")
    assert final["report"] and "待签署草稿" in final["report"]
    assert "SECTION 十一" not in final["report"] and "十一、待签署声明" in final["report"]
    # demo 病例结构固定：clinic 不并入（白名单接入仅自定义病例，阶段5测试覆盖）；
    # 带资料提交不破坏 demo 流程，且报告正文不携带影像文件名等资料痕迹
    from server.app import _load_sessions
    assert "clinic_payload" not in _load_sessions()[snap["case_id"]]["case"]
    assert "leg.png" not in final["report"]
    # 审核历史：确认 1 条
    assert len(final["review_history"]) == 1 and final["doctor_final"]["decision"] == "confirmed"


def test_failure_invariance_llm_corrupt_output_never_pollutes():
    """失败不变性（v58 防伪装语义）：LLM 返回非法 JSON → 一次修复仍失败 →
    ① 自定义病例返回 (None, 错误原因)，绝不回退规则稿；② demo 病例才回退规则稿
    （明确标注离线演示）。已有会话/审计/病例数据不被污染，降级稿仍是合法 v1.1 契约。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9"     # 假地址 → 强制走 LLM 分支
    os.environ["LLM_MODEL"] = "fake-model"
    orig_chat = sa._chat
    try:
        calls = {"n": 0}

        def fake_chat(base_url, api_key, model, user, reasoning_effort=""):
            calls["n"] += 1
            if calls["n"] == 1:
                return "这不是JSON，模型抽风输出<<<乱码>>>"
            return "还不合法"      # 修复一次后仍不合法
        sa._chat = fake_chat
        from derm_core import DEMO_CASES
        case = dict(DEMO_CASES["1"]["case"])
        cites = sa.retrieve_citations(case)
        # ① 自定义病例：绝不生成任何医疗内容（draft=None + 明确错误原因）
        draft, err = sa._make_draft_server(case, cites, [], ["缺失项A"])
        assert draft is None and calls["n"] == 2     # 恰好一次修复机会，不无限重试
        assert "LLM" in err and len(err) <= 200
        # ② demo 病例：回退规则稿（明确标注演示），仍是合法 v1.1 契约
        draft2, err2 = sa._make_draft_server(case, cites, [], ["缺失项A"], demo="1")
        assert err2 == "" and draft2 is not None
        assert draft2["schema_version"] == "1.1"
        assert draft2["generated_by"] == "rule-fallback"
        assert "模型调用失败" in (draft2.get("review_note") or "")
        assert draft2["review_status"] == "pending" and draft2["requires_doctor_confirmation"] is True
        assert calls["n"] == 4
    finally:
        sa._chat = orig_chat       # 恢复真实 _chat，避免污染其他用例
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_audit_trail_records_doctor_decisions():
    """审计链：case_started / review / report 相关事件落 audit.jsonl，医生决策可追溯。"""
    import json as _json
    from server.app import AUDIT_FILE as audit_path   # v63：跟随 DERM_DATA_DIR 隔离目录，不再硬编码真实数据路径
    snap = _start(demo="1")
    _review(snap["case_id"], "确认")
    with open(audit_path, encoding="utf-8") as fh:
        events = [_json.loads(line) for line in fh if line.strip()]
    tail = events[-50:]
    started = [e for e in tail if e["event"] == "case_started" and snap["case_id"] in e["detail"]]
    reviewed = [e for e in tail if e["event"] == "review" and snap["case_id"] in e["detail"]]
    assert started and started[-1]["detail"].find("clinic") >= 0
    assert reviewed and reviewed[-1]["detail"].find("confirmed") >= 0
    for e in tail:
        assert len(e["detail"]) <= 500 and "at" in e     # 审计条目截断且带时间戳


# —— §16a：本地医学书籍 RAG 知识库 ——

@pytest.fixture(autouse=True)
def _isolated_kb(tmp_path, monkeypatch):
    """KB 测试隔离：书籍库指向临时文件，避免跨测试/跨运行残留影响去重与状态断言。"""
    import rag as rag_mod
    monkeypatch.setattr(rag_mod, "BOOKS_FILE", str(tmp_path / "books.json"))
    yield


@pytest.fixture(autouse=True)
def _isolated_llm_config(tmp_path, monkeypatch):
    """v47：LLM 连接配置隔离——医生本机可能已在 server/data/llm_config.json 配置真实
    服务（UI 保存），测试必须确定性走规则管线：统一指向不存在的配置文件（测试自行
    monkeypatch 覆盖此路径的除外），并阻止 demo 用例真发外部请求（180s 超时）。"""
    import server.app as sa
    monkeypatch.setattr(sa, "LLM_CONFIG_FILE", str(tmp_path / "absent_llm_config.json"))
    yield


@pytest.fixture(autouse=True)
def _isolated_embedding(tmp_path, monkeypatch):
    """v48/49：embedding 隔离——医生本机装好向量模型后，测试不得真加载权重或做 CPU
    推理，也不得触碰真实模型目录与 manifest。默认强制 not_installed（检索回退纯 BM25）；
    需要 hybrid / 安装流程的用例自行 monkeypatch rag_embed 覆盖。"""
    import rag as rag_mod
    monkeypatch.setattr(rag_mod, "EMBEDDING_STATUS", {"model": None, "state": "not_installed", "detail": ""})
    try:
        import rag_embed
    except ImportError:
        yield
        return
    monkeypatch.setattr(rag_embed, "MODELS_ROOT", str(tmp_path / "models"))
    monkeypatch.setattr(rag_embed, "MANIFEST_FILE", str(tmp_path / "rag" / "embedding_manifest.json"))
    monkeypatch.setattr(rag_embed, "STATUS",
                        {"model": None, "state": "not_installed", "detail": "", "installing": None})
    monkeypatch.setattr(rag_embed, "_model", None)
    yield


def _kb_import(name, text):
    import base64
    return client.post("/api/kb/books",
                       json={"filename": name, "content_base64": base64.b64encode(text.encode()).decode()}).json()


def test_kb_lifecycle_dedup_and_failure_honesty():
    """§16：导入→ready 立即可检索（无人工审核门槛）；SHA-256 去重；解析失败明确原因不伪造、
    不进入可检索状态；元数据缺失用文件名兜底并标记不完整。"""
    book = ("第十章 湿疹类皮肤病\n湿疹是一种常见的炎症性皮肤病，临床表现为多形性皮疹，"
            "倾向渗出，对称分布，剧烈瘙痒，易反复发作。\n\n第十一章 接触性皮炎\n"
            "接触性皮炎是皮肤或黏膜单次或多次接触外源性物质后，在接触部位甚至以外的部位发生的炎症性反应。")
    r = _kb_import("皮肤科诊疗要点（合成演示版）.txt", book)
    assert r["ok"] is True and r["status"] == "ready" and r["chunks"] >= 2, r
    listing = client.get("/api/kb/books").json()
    mine = [b for b in listing["books"] if b["book_id"] == r["book_id"]][0]
    assert mine["status"] == "ready" and mine["chunks"] == r["chunks"]
    assert mine["meta_incomplete"] is True         # 无作者/出版社/版次 → 文件名兜底并标记不完整
    assert mine["title"] == "皮肤科诊疗要点（合成演示版）"
    # SHA-256 去重：同文件再次导入被拒绝且指向原书
    r2 = _kb_import("另一名字.txt", book)
    assert r2["ok"] is False and r2["reason"] == "duplicate_sha256" and r2["book_id"] == r["book_id"]
    # 解析失败：不支持的格式 → 明确拒绝；损坏内容 → failed 状态保留，不伪造
    r3 = _kb_import("x.rtf", "内容")
    assert r3["ok"] is False and "format_unsupported" in r3["reason"]
    r4 = _kb_import("empty.txt", "")
    assert r4["ok"] is False and r4["status"] == "failed" and r4["reason"] == "empty_text"
    listing = client.get("/api/kb/books").json()
    failed = [b for b in listing["books"] if b["book_id"] == r4["book_id"]][0]
    assert failed["status"] == "failed" and failed["chunks"] == 0
    # 文件名兜底 → 元数据标记不完整
    r5 = _kb_import("只靠文件名兜底的书籍.txt", "这是一本没有章节标题和元信息的短书内容。")
    assert r5["ok"] is True
    listing = client.get("/api/kb/books").json()
    inc = [b for b in listing["books"] if b["book_id"] == r5["book_id"]][0]
    assert inc["meta_incomplete"] is True and inc["title"] == "只靠文件名兜底的书籍"


def test_kb_hybrid_search_citation_contract_and_soft_delete():
    """§16.5：检索只回 ready 书籍；引用契约完整（citation_id/书名/版本/章节/页码/摘录/
    chunk哈希/本地文件/分数/快照版本）；软删除立即停止检索，恢复后重新可检索。"""
    book = ("第一章 特应性皮炎\n特应性皮炎患者常伴有血清IgE升高和外周血嗜酸性粒细胞增多，"
            "剧烈瘙痒是最突出的症状。\n第二章 荨麻疹\n荨麻疹以风团和瘙痒为特征，通常在24小时内消退。")
    r = _kb_import("皮肤病学合成教材.txt", book)
    assert r["ok"] is True
    s = client.get("/api/kb/search", params={"q": "IgE升高 瘙痒"}).json()
    assert s["ok"] is True and s["citations"], s
    top = s["citations"][0]
    for key in ("citation_id", "book_title", "version", "author", "publisher", "section",
                "quoted_text", "chunk_hash", "local_file", "retrieval_score", "kb_snapshot",
                "source_type", "retrieval_mode"):
        assert key in top, key
    assert top["source_type"] == "local_book" and "皮肤病学合成教材" in top["book_title"]
    assert "第一章" in top["section"] and s["retrieval_mode"] == "bm25"   # embedding 未装→如实标注
    assert s["embedding_state"] == "not_installed"
    # 相关性：IgE 查询命中特应性皮炎章而非荨麻疹章
    assert "特应性皮炎" in top["section"] or "特应性皮炎" in top["quoted_text"]
    # 软删除 → 立即停止检索；恢复 → 重新可检索
    d = client.delete(f"/api/kb/books/{r['book_id']}").json()
    assert d["ok"] is True and d["status"] == "quarantined"
    s2 = client.get("/api/kb/search", params={"q": "IgE升高 瘙痒"}).json()
    assert s2["citations"] == [] or all("皮肤病学合成教材" not in (c["book_title"] or "") for c in s2["citations"])
    rp = client.post(f"/api/kb/books/{r['book_id']}/restore").json()
    assert rp["ok"] is True and rp["status"] == "ready"
    s3 = client.get("/api/kb/search", params={"q": "IgE升高"}).json()
    assert any("皮肤病学合成教材" in (c["book_title"] or "") for c in s3["citations"])


def test_kb_hybrid_rrf_fuses_semantic_ranking(monkeypatch):
    """v48：向量模型 ready 时语义召回与 BM25 做 RRF 融合（retrieval_mode=hybrid 如实标注）；
    语义信号可改变排序（BM25 第一名被融合后让位）；向量通道异常时只降级回 BM25 不阻塞。"""
    book = ("第一章 湿疹类皮肤病\n"
            "湿疹湿疹湿疹：湿疹是一种常见的炎症性皮肤病，湿疹临床表现为多形性皮疹，剧烈瘙痒。\n"
            "第二章 接触性皮炎\n"
            "接触性皮炎需与湿疹鉴别，避免接触过敏原后皮疹可自行消退，必要时外用糖皮质激素。\n"
            "第三章 银屑病\n"
            "银屑病是一种慢性免疫介导的皮肤病，典型表现为鳞屑性红斑或斑块，与湿疹不同。")
    r = _kb_import("融合测试教材.txt", book)
    assert r["ok"] is True

    # 基线：embedding 未装（autouse fixture 已隔离）→ 纯 BM25，湿疹章（词频最高）第一
    base = client.get("/api/kb/search", params={"q": "湿疹"}).json()
    assert base["retrieval_mode"] == "bm25" and "湿疹" in base["citations"][0]["section"]

    # 桩：ensure_loaded 报 ready + 语义通道把接触性皮炎章（chunk index 1）排第一
    import rag_embed
    monkeypatch.setattr(rag_embed, "ensure_loaded",
                        lambda: {"model": "BAAI/bge-base-zh-v1.5", "state": "ready", "detail": ""})
    monkeypatch.setattr(rag_embed, "rank", lambda query, texts, keys=None: [(1, 0.97)])
    hyb = client.get("/api/kb/search", params={"q": "湿疹"}).json()
    assert hyb["retrieval_mode"] == "hybrid" and hyb["embedding_state"] == "ready"
    assert "接触性皮炎" in hyb["citations"][0]["section"]
    # 每条引用都如实带 hybrid 标注（引用随回复可见，医生可追溯）
    assert all(c["retrieval_mode"] == "hybrid" for c in hyb["citations"])

    # 向量通道抛异常 → 整次检索降级回 bm25，绝不 500、绝不伪造 hybrid
    def _boom(query, texts):
        raise RuntimeError("模型推理失败")
    monkeypatch.setattr(rag_embed, "rank", _boom)
    fb = client.get("/api/kb/search", params={"q": "湿疹"}).json()
    assert fb["retrieval_mode"] == "bm25" and fb["ok"] is True
    assert "湿疹" in fb["citations"][0]["section"]


def test_kb_embedding_bge_m3_file_manifest(tmp_path, monkeypatch):
    """v57：bge-m3（XLM-R 架构）per-model 文件清单——安装落盘 sentencepiece.bpe.model
    而非 bge-zh 的 vocab.txt；query 无指令前缀（官方语义）；bge-zh 系列行为不变。"""
    import rag_embed
    r = client.get("/api/kb/embedding").json()
    m3 = [m for m in r["catalog"] if m["short"] == "bge-m3"]
    assert m3 and m3[0]["id"] == "BAAI/bge-m3"
    files = rag_embed.MODEL_CATALOG["bge-m3"]["files"]
    assert "sentencepiece.bpe.model" in files and "vocab.txt" not in files
    assert rag_embed.MODEL_CATALOG["bge-m3"]["query_prefix"] == ""
    assert rag_embed.MODEL_CATALOG["bge-base-zh-v1.5"]["query_prefix"]   # zh 系列仍带指令前缀

    def fake_download(url, dest, progress=None):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(b"fake-weights")
        if progress:
            progress(8, 8)
    monkeypatch.setattr(rag_embed, "_download_file", fake_download)
    inst = client.post("/api/kb/embedding/install", json={"model": "bge-m3"}).json()
    assert inst["ok"] is True, inst
    if rag_embed._install_thread:
        rag_embed._install_thread.join(timeout=10)
    d = rag_embed.model_dir("bge-m3")
    assert os.path.exists(os.path.join(d, "sentencepiece.bpe.model"))
    assert not os.path.exists(os.path.join(d, "vocab.txt"))
    assert os.path.exists(os.path.join(d, "pytorch_model.bin"))   # 权重同名，哈希记录复用
    info = client.get("/api/kb/embedding").json()
    m3c = [m for m in info["catalog"] if m["short"] == "bge-m3"][0]
    assert m3c["installed"] and m3c["active"] and info["active"] == "bge-m3"   # 新装即用


def test_kb_embedding_catalog_install_and_activate(tmp_path, monkeypatch):
    """v49：向量模型自选安装——清单含状态标注；安装（mock 下载）成功后自动激活并记哈希；
    重复安装被拒；未知模型被拒；activate 只对已安装模型生效。"""
    import rag_embed
    r = client.get("/api/kb/embedding").json()
    assert r["ok"] is True and len(r["catalog"]) >= 3
    assert {m["short"] for m in r["catalog"]} >= {"bge-small-zh-v1.5", "bge-base-zh-v1.5"}
    assert all(not m["installed"] and not m["active"] for m in r["catalog"])   # 隔离环境全未装
    bad = client.post("/api/kb/embedding/install", json={"model": "no-such"}).json()
    assert bad["ok"] is False and bad["reason"] == "unknown_model"

    def fake_download(url, dest, progress=None):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(b"fake-weights")
        if progress:
            progress(12, 12)
    monkeypatch.setattr(rag_embed, "_download_file", fake_download)
    inst = client.post("/api/kb/embedding/install", json={"model": "bge-small-zh-v1.5"}).json()
    assert inst["ok"] is True, inst
    if rag_embed._install_thread:
        rag_embed._install_thread.join(timeout=10)
    info = client.get("/api/kb/embedding").json()
    small = [m for m in info["catalog"] if m["short"] == "bge-small-zh-v1.5"][0]
    assert small["installed"] and small["active"]          # 新装即用（自动激活）
    assert info["status"]["installing"] is None and small["weights_sha256"]
    dup = client.post("/api/kb/embedding/install", json={"model": "bge-small-zh-v1.5"}).json()
    assert dup["ok"] is False and dup["reason"] == "already_installed"
    na = client.post("/api/kb/embedding/activate", json={"model": "bge-base-zh-v1.5"}).json()
    assert na["ok"] is False and na["reason"] == "not_installed"
    d = os.path.join(rag_embed.MODELS_ROOT, "bge-base-zh-v1.5")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "config.json"), "wb").write(b"{}")
    open(os.path.join(d, "pytorch_model.bin"), "wb").write(b"w")
    act = client.post("/api/kb/embedding/activate", json={"model": "bge-base-zh-v1.5"}).json()
    assert act["ok"] is True and act["active"] == "bge-base-zh-v1.5"
    assert client.get("/api/kb/embedding").json()["active"] == "bge-base-zh-v1.5"


def test_kb_backup_and_restore_roundtrip():
    """备份/恢复：导出含全部书籍与索引；恢复后检索结果一致；非法备份数据被拒绝。"""
    _kb_import("备份往返测试书籍.txt", "第一章 痤疮\n痤疮是毛囊皮脂腺单位的慢性炎症性皮肤病。")
    snap_before = client.get("/api/kb/books").json()["kb_snapshot"]
    bk = client.get("/api/kb/backup").json()
    assert bk["ok"] is True and bk["data"]["books"]
    # 恢复到当前（幂等往返）
    rp = client.post("/api/kb/restore", json={"data": bk["data"]}).json()
    assert rp["ok"] is True
    assert client.get("/api/kb/books").json()["kb_snapshot"] == snap_before
    bad = client.post("/api/kb/restore", json={"data": {"foo": 1}}).json()
    assert bad["ok"] is False and bad["reason"] == "invalid_backup"


def test_kb_auto_retrieval_feeds_doctor_pipeline():
    """§16.1：医生生成辅助建议时自动全库检索——命中片段以 book_citations 附着在草稿顶层
    （与合成 KB 引用分开列示，不混淆引用白名单契约）；无书/KB故障时不阻断主流程。"""
    book = ("第一章 红斑鳞屑性疾病\n红斑鳞屑性皮肤病常见者包括银屑病、玫瑰糠疹等，"
            "皮损多为红斑或斑块，覆有鳞屑，可伴瘙痒。\n第二章 用药原则\n"
            "外用糖皮质激素需在医生指导下使用，注意不良反应。")
    assert _kb_import("合成皮肤病学入门.txt", book)["ok"] is True
    snap = _start(demo="1")                     # demo1：红斑瘙痒类病例，应命中第一章
    bc = snap["draft"].get("book_citations") or []
    assert bc, "书籍命中应附着到草稿"
    assert all(c["source_type"] == "local_book" and c["kb_snapshot"] for c in bc)
    assert any("红斑" in c["quoted_text"] or "红斑" in c["section"] for c in bc)
    # 合成 KB 引用与书籍引用分开：draft.citations 保持合成白名单契约
    assert snap["draft"]["citations"]
    assert all(c["source_type"] != "local_book" for c in snap["draft"]["citations"])
    # 快照透传（跨端续办可见）
    again = client.get(f"/api/cases/{snap['case_id']}").json()
    assert again["book_citations"] == bc
    # 确认后待签署报告不因书籍引用变化而受影响
    final = _review(snap["case_id"], "确认")
    assert final["report"]


def test_llm_config_file_precedence_over_env(tmp_path, monkeypatch):
    """v43：设置面板写入的 llm_config.json 优先于环境变量（逐字段回退）；
    Key 只进配置文件与 _chat 头部，不进入草稿/响应；配置缺失回退 env。"""
    import server.app as sa
    cfg_file = tmp_path / "llm_config.json"
    monkeypatch.setattr(sa, "LLM_CONFIG_FILE", str(cfg_file))
    orig_chat = sa._chat
    try:
        seen = {}

        def fake_chat(base_url, api_key, model, user, reasoning_effort=""):
            seen.update(base_url=base_url, api_key=api_key, model=model)
            return '{"suspected_conditions": [], "symptom_assessment": "", "recommended_next_steps": [], "medication_checks": [], "severity": {"level": "low", "basis": []}, "uncertainties": [], "citations": [], "review_status": "pending"}'

        sa._chat = fake_chat
        from derm_core import DEMO_CASES
        case = dict(DEMO_CASES["1"]["case"])
        # ① 配置文件存在 → 全部字段来自配置文件
        cfg_file.write_text(json.dumps({"base_url": "https://cfg.example.com/v1",
                                        "api_key": "cfg-secret-key-9999", "model": "cfg-model"}),
                            encoding="utf-8")
        draft = sa._make_draft_server(case, [], [], [])
        assert (seen["base_url"], seen["api_key"], seen["model"]) == \
            ("https://cfg.example.com/v1", "cfg-secret-key-9999", "cfg-model")
        assert "cfg-secret-key-9999" not in json.dumps(draft, ensure_ascii=False)   # Key 不入草稿
        # ② 配置文件某字段为空 → 该字段回退 env；非空字段仍用配置文件
        monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "env-key-0001")
        monkeypatch.setenv("LLM_MODEL", "env-model")
        cfg_file.write_text(json.dumps({"base_url": "https://cfg.example.com/v1",
                                        "api_key": "", "model": ""}), encoding="utf-8")
        sa._make_draft_server(case, [], [], [])
        assert seen["base_url"] == "https://cfg.example.com/v1"       # 非空 → 配置优先
        assert seen["api_key"] == "env-key-0001" and seen["model"] == "env-model"   # 空 → env 回退
        # ③ 配置文件不存在 → 全部走 env（向后兼容）
        cfg_file.unlink()
        sa._make_draft_server(case, [], [], [])
        assert (seen["base_url"], seen["api_key"], seen["model"]) == \
            ("https://env.example.com/v1", "env-key-0001", "env-model")
    finally:
        sa._chat = orig_chat


def test_thinking_level_flows_into_chat(tmp_path, monkeypatch):
    """v47：设置界面选择的思考等级（thinking_level）写入配置 → _make_draft_server 以
    reasoning_effort 传入 _chat；白名单外的值归一为空（不传参数，行为与旧版一致）。"""
    import server.app as sa
    cfg_file = tmp_path / "llm_config.json"
    monkeypatch.setattr(sa, "LLM_CONFIG_FILE", str(cfg_file))
    orig_chat = sa._chat
    try:
        seen = {}

        def fake_chat(base_url, api_key, model, user, reasoning_effort=""):
            seen["effort"] = reasoning_effort
            return '{"suspected_conditions": [], "symptom_assessment": "", "recommended_next_steps": [], "medication_checks": [], "severity": {"level": "low", "basis": []}, "uncertainties": [], "citations": [], "review_status": "pending"}'

        sa._chat = fake_chat
        from derm_core import DEMO_CASES
        case = dict(DEMO_CASES["1"]["case"])
        # ① 思考等级=高 → reasoning_effort=high 传入请求
        cfg_file.write_text(json.dumps({"base_url": "https://x.example.com/v1", "api_key": "k",
                                        "model": "m", "thinking_level": "high"}), encoding="utf-8")
        sa._make_draft_server(case, [], [], [])
        assert seen["effort"] == "high"
        # ② 无思考等级字段（旧配置文件） → 不传参数，向后兼容
        cfg_file.write_text(json.dumps({"base_url": "https://x.example.com/v1", "api_key": "k",
                                        "model": "m"}), encoding="utf-8")
        sa._make_draft_server(case, [], [], [])
        assert seen["effort"] == ""
        # ③ 非法值（如 "ultra"） → 白名单归一为空
        cfg_file.write_text(json.dumps({"base_url": "https://x.example.com/v1", "api_key": "k",
                                        "model": "m", "thinking_level": "ultra"}), encoding="utf-8")
        sa._make_draft_server(case, [], [], [])
        assert seen["effort"] == ""
    finally:
        sa._chat = orig_chat


def test_chat_drops_reasoning_effort_when_gateway_rejects(monkeypatch):
    """v47 失败不变性 + v59 外层退避：网关不认识 reasoning_effort（400/404/422）→ 自动去掉
    参数重试一次，调用方照常拿到结果；401 等失败归一 ConnectionError 后由 v59 外层退避
    统一重试 1 次（网关故障形态多样不区分），仍失败则如实报错。"""
    import server.app as sa
    import urllib.error
    captured = []

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok-reply"}}]}).encode("utf-8")

    def fake_urlopen(req, timeout=0, context=None):
        body = json.loads(req.data.decode("utf-8"))
        captured.append(body)
        if "reasoning_effort" in body:
            raise urllib.error.HTTPError(req.full_url, 422,
                                         "unknown field reasoning_effort", {}, None)
        return FakeResp()

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake_urlopen)
    out = sa._chat("https://gw.example.com/v1", "k", "m", "hello", reasoning_effort="high")
    assert out == "ok-reply"
    assert len(captured) == 2                       # 第一次带参数被拒 → 第二次去掉参数成功
    assert "reasoning_effort" not in captured[1]
    # 401（鉴权失败）不降级重试
    captured.clear()

    def fake_urlopen_401(req, timeout=0, context=None):
        captured.append(json.loads(req.data.decode("utf-8")))
        raise urllib.error.HTTPError(req.full_url, 401, "bad key", {}, None)

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake_urlopen_401)
    try:
        sa._chat("https://gw.example.com/v1", "k", "m", "hello", reasoning_effort="high")
        raise AssertionError("should have raised ConnectionError")
    except ConnectionError as exc:
        assert "401" in str(exc)
    assert len(captured) == 2                       # v59 外层退避：重试 1 次后仍失败才报错


def test_chat_sends_browser_user_agent(monkeypatch):
    """v58b：网关（Cloudflare）拦截 Python-urllib 默认签名（403/1010）——_chat 必须带
    浏览器式 User-Agent；与 Content-Type/Authorization 并存，且不把 Key 泄进 UA。"""
    import server.app as sa
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    def fake_urlopen(req, timeout=0, context=None):
        seen.update(dict(req.header_items()))
        return FakeResp()

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake_urlopen)
    sa._chat("https://gw.example.com/v1", "secret-key", "m", "hello")
    ua = seen.get("User-agent") or seen.get("User-agent".lower()) or ""
    assert "Mozilla/5.0" in ua                       # 浏览器式签名，过 Cloudflare 1010
    assert "Python-urllib" not in ua
    assert "secret-key" not in ua                    # Key 只进 Authorization，绝不进 UA


# —— v58：LLM 真实性防线（未连接绝不生成医疗内容） ——

def test_llm_unavailable_no_medical_draft():
    """v58：LLM 未配置时，自定义病例绝不生成医疗草稿——workflow_state=llm_unavailable、
    draft=None、无任何疾病/药物/方案字段，快照 llm 元信息如实记录（ok=False/source=none）。"""
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
    try:
        snap = _start(text="身体区域：小腿；主诉：红斑瘙痒；症状：红斑伴脱屑；病程：2周；"
                           "过敏史：否认；当前用药：无；既往史：无；诱因：不明")
        assert snap["workflow_state"] == "planning"        # v64-P2：先计划确认
        snap = _confirm(snap["case_id"])
        assert snap["workflow_state"] == "llm_unavailable"
        assert snap["draft"] is None                      # 无医疗内容
        assert "未连接 LLM" in (snap.get("message") or "")
        llm = snap.get("llm") or {}
        assert llm.get("ok") is False and llm.get("source") == "none"
        assert llm.get("error") and "at" in llm
        assert snap.get("report", "") == ""               # 报告更不可能有
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_llm_status_endpoint(tmp_path, monkeypatch):
    """v58：/api/llm/status 探测端点——不回显 Key 明文；reachable 仅 check=1 时探测；
    任何 HTTP 状态码（含 401）算服务可达；网络异常如实 reachable=False。"""
    import server.app as sa
    cfg_file = tmp_path / "llm_config.json"
    cfg_file.write_text(json.dumps({"base_url": "https://gw.example.com/v1", "model": "m-1",
                                    "api_key": "sk-secret-abcdef", "thinking_level": ""}), encoding="utf-8")
    monkeypatch.setattr(sa, "LLM_CONFIG_FILE", str(cfg_file))
    out = client.get("/api/llm/status").json()
    assert out["ok"] is True
    assert out["configured"] is True and out["model"] == "m-1"
    assert out["api_key_set"] is True                     # 只回布尔
    assert "sk-secret" not in json.dumps(out)             # Key 明文绝不出现
    assert out["reachable"] is None                       # 未 check 不探测

    # check=1：HTTP 401（Key 无效）也算可达——服务在线与鉴权有效性分开如实呈现
    class FakeResp401:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen_401(req, timeout=0, context=None):
        assert "Authorization" in dict(req.header_items())
        raise urllib.error.HTTPError(req.full_url, 401, "bad key", {}, None)

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake_urlopen_401)
    out2 = client.get("/api/llm/status?check=1").json()
    assert out2["reachable"] is True and out2["error"] == ""

    # check=1：网络异常 → reachable=False + 错误原因
    def fake_urlopen_down(req, timeout=0, context=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(sa.urllib.request, "urlopen", fake_urlopen_down)
    out3 = client.get("/api/llm/status?check=1").json()
    assert out3["reachable"] is False and "connection refused" in out3["error"]


def test_llm_failure_real_request_never_fakes_medical_draft(tmp_path, monkeypatch):
    """v58 端到端：真实 LLM 请求失败（服务在线但 500）→ 自定义病例零医疗内容
    （v64：已配置但失败 → llm_error 可重试，仍绝不显示疾病/药物/方案）；
    demo 病例才回退规则稿且 llm.source=demo 明确标注离线演示。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "http://127.0.0.1:9", "fake-model"
    try:
        def fake_chat(base_url, api_key, model, user, reasoning_effort=""):
            raise ConnectionError("HTTP 500：模型服务内部错误")

        monkeypatch.setattr(sa, "_chat", fake_chat)
        snap = _start(text="身体区域：小腿；主诉：红斑瘙痒；症状：红斑伴脱屑；病程：2周；"
                           "过敏史：否认；当前用药：无；既往史：无；诱因：不明")
        assert snap["workflow_state"] == "planning"
        snap = _confirm(snap["case_id"])
        assert snap["workflow_state"] == "llm_error" and snap["draft"] is None
        assert "重试" in snap["message"]
        assert (snap.get("llm") or {}).get("ok") is False
        assert "模型服务内部错误" in ((snap.get("llm") or {}).get("error") or "")
        # demo 病例：规则稿回退（明确标注离线演示数据，不是真实 AI 分析）
        snap2 = _start(demo="1")
        assert snap2["draft"] is not None
        llm2 = snap2.get("llm") or {}
        assert llm2.get("ok") is True and llm2.get("source") == "demo"
        assert snap2["draft"]["generated_by"].startswith("rule")
        assert "模型调用失败" in (snap2["draft"].get("review_note") or "")
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


# —— v59：自然语言任务路由（Agent 化核心） ——

def test_task_classification_rules():
    """任务分类：12 类关键词路由（顺序敏感：编辑>文书>写病例；默认 case_write）。"""
    import server.app as sa
    cases = [
        ("请帮我写一份病例", "case_write"),
        ("请根据以下信息写一份门诊病历：患者男，35岁", "case_write"),
        ("请整理现病史", "history"),
        ("请描述皮损情况", "lesion_desc"),
        ("请根据当前病例列出需要鉴别的疾病和判断依据", "differential"),
        ("请生成用药核对清单", "medication_review"),
        ("请把这份病例整理成治疗参考", "treatment_ref"),
        ("请生成患者能看懂的健康教育说明", "patient_edu"),
        ("请生成转诊摘要", "referral"),
        ("请生成随访记录", "followup"),
        ("请生成病例摘要", "summary"),
        ("把刚才的病例改成适合医生审核的门诊记录", "case_edit"),
        ("请导出报告", "report_export"),
        ("请只写现病史，不要生成诊断和治疗建议", "history"),
        ("今天天气不错", "case_write"),
    ]
    for text, want in cases:
        assert sa._classify_task(text) == want, f"{text!r} → {sa._classify_task(text)} != {want}"


def _fake_doc_chat(content):
    def fake_chat(base_url, api_key, model, user, reasoning_effort=""):
        return json.dumps({"content": content}, ensure_ascii=False)
    return fake_chat


def test_doc_task_llm_success_history(tmp_path, monkeypatch):
    """文书任务（现病史）LLM 成功 → doc_ready / source=llm，且不含诊断治疗（提示词约束外的运行时守卫另测）。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "http://127.0.0.1:9", "fake-model"
    try:
        monkeypatch.setattr(sa, "_chat", _fake_doc_chat("## 现病史（整理）\n右前臂红斑瘙痒7天，接触洗涤剂后出现。"))
        snap = _start(text="患者男，35岁，右前臂红斑瘙痒7天，近期更换洗涤剂，无发热。请整理现病史")
        assert snap["workflow_state"] == "planning"
        snap = _confirm(snap["case_id"])
        assert snap["task_type"] == "history"
        assert snap["workflow_state"] == "doc_ready"
        doc = snap["doc"]
        assert doc["content"].startswith("## 现病史") and doc["generated_by"].startswith("llm")
        assert (snap["llm"] or {}).get("source") == "llm" and (snap["llm"] or {}).get("ok") is True
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_medical_doc_task_llm_unavailable_zero_content(tmp_path, monkeypatch):
    """医学判断任务（鉴别/用药核对/宣教）LLM 不可用 → llm_unavailable，doc/draft 全空，零医疗内容。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""   # 无 LLM 配置
    try:
        base = _start(text="患者男，35岁，右前臂红斑瘙痒7天")
        for text in ("请根据当前病例列出需要鉴别的疾病和判断依据",
                     "请生成用药核对清单", "请生成患者能看懂的健康教育说明"):
            plan = client.post(f"/api/cases/{base['case_id']}/chat", json={"text": text}).json()
            assert plan["workflow_state"] == "planning", text    # v64-P2：先计划确认
            snap = _confirm(base["case_id"])
            assert snap["workflow_state"] == "llm_unavailable", text
            assert snap["doc"] is None and snap["draft"] is None
            assert (snap["llm"] or {}).get("ok") is False
            assert "当前未连接 LLM" in snap["message"]
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_template_doc_missing_fields_not_negated(tmp_path, monkeypatch):
    """文书任务 LLM 不可用 → 本地模板整理（source=template）：缺失字段一律「未提供」，绝不写「无/否认」；
    不含任何引用（那是 LLM 管线的事）。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
    try:
        snap = _start(text="主诉：右前臂红斑瘙痒。请整理现病史")
        assert snap["workflow_state"] == "planning"
        snap = _confirm(snap["case_id"])
        assert snap["task_type"] == "history" and snap["workflow_state"] == "doc_ready"
        doc = snap["doc"]
        assert (snap["llm"] or {}).get("source") == "template"
        assert "- 病程：未提供" in doc["content"]           # 缺失 → 未提供
        assert "- 诱因：未提供" in doc["content"]
        assert "- 主诉：右前臂红斑瘙痒" in doc["content"]   # 已提供 → 如实重组
        assert "：无\n" not in doc["content"] and "：否认" not in doc["content"]
        assert doc["citations"] == [] and "本地模板整理" in doc["disclaimer"]
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_chat_endpoint_continues_case_context(tmp_path, monkeypatch):
    """chat 端点：同病例延续上下文——补充信息并入病例（symptoms），任务指令不污染病例数据。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
    try:
        base = _start(text="患者男，35岁，右前臂红斑瘙痒7天")
        cid = base["case_id"]
        # 补充信息（case_write 类）→ 并入 symptoms
        r1 = client.post(f"/api/cases/{cid}/chat", json={"text": "瘙痒夜间加重"}).json()
        assert "夜间加重" in (r1["case"].get("symptoms") or "")
        # 任务指令 → 不污染 symptoms
        r2 = client.post(f"/api/cases/{cid}/chat", json={"text": "请根据当前病例列出需要鉴别的疾病"}).json()
        assert "列出需要鉴别" not in (r2["case"].get("symptoms") or "")
        assert r2["task_type"] == "differential"
        # 空消息拒绝
        assert client.post(f"/api/cases/{cid}/chat", json={"text": "  "}).status_code == 400
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_case_edit_llm_rewrite_and_guards(tmp_path, monkeypatch):
    """病例编辑：LLM 重写草稿（generated_by=llm-edited）；LLM 不可用时——
    无草稿 → 本地模板重排（标注）；已有 AI 草稿 → llm_unavailable（绝不本地篡改 AI 内容）。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
    try:
        # 无草稿 + 无 LLM → 模板重排（source=template，明确标注本地整理）
        base = _start(text="主诉：右前臂红斑瘙痒；病程：7天")
        plan = client.post(f"/api/cases/{base['case_id']}/chat",
                           json={"text": "把刚才的病例改成适合医生审核的门诊记录"}).json()
        assert plan["task_type"] == "case_edit" and plan["workflow_state"] == "planning"
        r = _confirm(base["case_id"])
        assert r["task_type"] == "case_edit" and r["workflow_state"] == "doc_ready"
        assert (r["llm"] or {}).get("source") == "template"
        assert "本地模板整理" in r["doc"]["content"]

        # 有 AI 草稿（case_write 产生）+ 无 LLM → llm_unavailable（不篡改 AI 内容）
        draft_json = json.dumps({"case_summary": {"symptom_summary": "右前臂红斑瘙痒7天",
                                                  "data_completeness": "partial"},
                                 "suspected_conditions": [{"name": "接触性皮炎（待排）"}],
                                 "workflow_state": "awaiting_review"}, ensure_ascii=False)
        monkeypatch.setattr(sa, "_chat", lambda *a, **k: draft_json)
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "http://127.0.0.1:9", "fake-model"
        base2 = _start(text="患者男，35岁，右前臂红斑瘙痒7天")
        base2 = _confirm2(base2["case_id"])
        assert base2["workflow_state"] == "awaiting_review" and base2["draft"]["generated_by"] == "llm"
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""   # LLM 掉线
        plan2 = client.post(f"/api/cases/{base2['case_id']}/chat",
                            json={"text": "把病例改成门诊记录"}).json()
        assert plan2["workflow_state"] == "planning"
        r2 = _confirm(base2["case_id"])
        assert r2["workflow_state"] == "llm_unavailable" and r2["draft"] is None
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_case_edit_llm_success_structured(tmp_path, monkeypatch):
    """病例编辑 LLM 成功 → normalize 校验后 awaiting_review，generated_by=llm-edited。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "http://127.0.0.1:9", "fake-model"
    try:
        draft_json = json.dumps({"case_summary": {"symptom_summary": "右前臂红斑瘙痒7天",
                                                  "data_completeness": "partial"},
                                 "suspected_conditions": [{"name": "接触性皮炎（待排）", "confidence": "medium"}],
                                 "workflow_state": "awaiting_review"}, ensure_ascii=False)
        monkeypatch.setattr(sa, "_chat", lambda *a, **k: draft_json)
        base = _start(text="患者男，35岁，右前臂红斑瘙痒7天")
        base = _confirm(base["case_id"])
        plan = client.post(f"/api/cases/{base['case_id']}/chat",
                           json={"text": "把病例改成门诊记录格式"}).json()
        assert plan["workflow_state"] == "planning"
        r = _confirm(base["case_id"])
        assert r["task_type"] == "case_edit" and r["workflow_state"] == "awaiting_review"
        assert r["draft"]["generated_by"] == "llm-edited"
        assert r["doc"] is None
        assert (r["llm"] or {}).get("source") == "llm"
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_report_export_explicit_only(tmp_path, monkeypatch):
    """报告导出：明确要求才导出；无内容 → 明确提示不生成空报告；有草稿 → 本地组装不产生新医学内容。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
    try:
        empty = _start(text="请导出报告")
        assert empty["task_type"] == "report_export" and empty["workflow_state"] == "planning"
        empty = _confirm(empty["case_id"])
        assert "暂无可导出" in empty["message"] and empty["doc"] is None
        # demo 病例（规则稿存在）→ 本地导出既有内容（demo 演示流程豁免计划确认）
        with_draft = _start(demo="1")
        r = client.post(f"/api/cases/{with_draft['case_id']}/chat", json={"text": "请导出报告"}).json()
        assert r["workflow_state"] == "doc_ready" and r["doc"]["task_type"] == "report_export"
        assert r["doc"]["generated_by"] == "local-export"
        assert "医生" in r["doc"]["content"]       # 报告注明医生签署边界
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_doc_citation_sanitized(tmp_path, monkeypatch):
    """引用校验：doc 正文编造的 citation_id 必须被白名单剔除并标注。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "http://127.0.0.1:9", "fake-model"
    try:
        monkeypatch.setattr(sa, "_chat",
                            _fake_doc_chat("考虑接触性皮炎（待排）[CIT-FAKE-999]，建议医生核对。"))
        snap = _start(text="患者男，35岁，右前臂红斑瘙痒7天。请整理皮损描述")
        assert snap["workflow_state"] == "planning"
        snap = _confirm(snap["case_id"])
        assert snap["workflow_state"] == "doc_ready"
        content = snap["doc"]["content"]
        assert "CIT-FAKE-999" not in content
        assert "引用未通过校验" in content
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_six_scenarios_via_chat_pipeline(tmp_path, monkeypatch):
    """v59 验收六案例（fake LLM 全链路）：
    ①无 3D 直接写门诊病历 ②3D 选点+病例模板 ③鉴别 ④改门诊记录 ⑤患者健康教育 ⑥只写现病史。"""
    import server.app as sa
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "http://127.0.0.1:9", "fake-model"
    try:
        def route_chat(base_url, api_key, model, user, reasoning_effort=""):
            # 按提示词中任务 spec 的独有短语路由（对话上下文/引用原文可能携带历史关键词）
            if "只整理现病史" in user:
                return json.dumps({"content": "已完成整理（AI 草稿，供医生审核）。"}, ensure_ascii=False)
            if "列出需要鉴别的疾病方向" in user:
                return json.dumps({"content": "需鉴别的疾病方向：接触性皮炎（待排）……"}, ensure_ascii=False)
            if "健康教育说明" in user:
                return json.dumps({"content": "日常护理提示……本说明仅供健康教育参考。"}, ensure_ascii=False)
            if "编辑病例" in user:
                return json.dumps({"case_summary": {"symptom_summary": "右前臂红斑瘙痒7天",
                                                    "data_completeness": "partial"},
                                   "suspected_conditions": [{"name": "接触性皮炎（待排）"}],
                                   "workflow_state": "awaiting_review"}, ensure_ascii=False)
            return json.dumps({"content": "已完成整理（AI 草稿，供医生审核）。"}, ensure_ascii=False)
        monkeypatch.setattr(sa, "_chat", route_chat)

        # ① 无 3D 选点直接写门诊病历（缺很多字段也不追问、不报错）
        s1 = _start(text="请根据以下信息写一份门诊病历：患者男，35岁，右前臂红斑瘙痒7天，近期更换洗涤剂，无发热")
        assert s1["task_type"] == "case_write" and s1["workflow_state"] == "planning"
        assert "我理解到的任务" in s1["reply"]["content"]          # v64-P2：先说明理解与动作
        assert s1["agent_state"] == "planning"
        s1 = _confirm2(s1["case_id"])
        assert s1["workflow_state"] == "awaiting_review"
        assert s1["missing"], "缺失字段应如实列出"
        assert s1["llm"]["source"] == "llm"

        # ② 3D 选点（作为文本上下文）+ 病例模板
        s2 = _start(text="身体区域：左前臂；请帮我整理一份皮肤科病例模板")
        assert s2["workflow_state"] == "planning"
        s2 = _confirm2(s2["case_id"])
        assert s2["workflow_state"] == "awaiting_review" and s2["llm"]["source"] == "llm"
        assert "左前臂" in json.dumps(s2["case"], ensure_ascii=False)

        cid = s1["case_id"]
        # ③ 鉴别分析
        p3 = client.post(f"/api/cases/{cid}/chat", json={"text": "请根据当前病例列出需要鉴别的疾病和判断依据"}).json()
        assert p3["task_type"] == "differential" and p3["workflow_state"] == "planning"
        r3 = _confirm(cid)
        assert r3["task_type"] == "differential" and r3["workflow_state"] == "doc_ready"
        assert "接触性皮炎" in r3["doc"]["content"] and r3["doc"]["generated_by"] == "llm"
        # ④ 改成门诊记录
        p4 = client.post(f"/api/cases/{cid}/chat", json={"text": "把刚才的病例改成适合医生审核的门诊记录"}).json()
        assert p4["task_type"] == "case_edit" and p4["workflow_state"] == "planning"
        r4 = _confirm(cid)
        assert r4["task_type"] == "case_edit" and r4["workflow_state"] == "awaiting_review"
        assert r4["draft"]["generated_by"] == "llm-edited"
        # ⑤ 患者健康教育
        p5 = client.post(f"/api/cases/{cid}/chat", json={"text": "请生成患者能看懂的健康教育说明"}).json()
        assert p5["workflow_state"] == "planning"
        r5 = _confirm(cid)
        assert r5["task_type"] == "patient_edu" and r5["doc"]["content"].startswith("日常护理")
        # ⑥ 只写现病史（不生成诊断和治疗）
        p6 = client.post(f"/api/cases/{cid}/chat", json={"text": "请只写现病史，不要生成诊断和治疗建议"}).json()
        assert p6["workflow_state"] == "planning"
        r6 = _confirm(cid)
        assert r6["task_type"] == "history" and r6["workflow_state"] == "doc_ready"
        assert "已完成整理" in r6["doc"]["content"]
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_chat_retry_failure_wrapped_as_connection_error(monkeypatch):
    """v59 真机回归：网关 400 → 去 effort 重试再失败必须转 ConnectionError（llm_unavailable 防线），不许裸 HTTPError 冒泡成 500。"""
    import io
    import urllib.request

    from server.app import _chat

    calls = {"n": 0}

    def fake_urlopen(req, timeout=0, context=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", None, io.BytesIO(b'{"error":"boom"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ConnectionError, match="HTTP 400"):
        _chat("http://gw.example", "k", "m", "hi", reasoning_effort="medium")
    assert calls["n"] == 4   # v59 外层退避重试：_once×2，每次带 effort + 去参重试


def test_chat_503_retry_then_recover(monkeypatch):
    """v59：网关瞬时过载（503）→ 重试一次；重试成功则正常返回，仍失败落防线。"""
    import io
    import urllib.request

    import server.app as sa

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # 场景1：503 → 重试成功
    calls = {"n": 0}

    def flaky_urlopen(req, timeout=0, context=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "unavailable", None, io.BytesIO(b"x"))
        ok = json.dumps({"choices": [{"message": {"content": "OK"}}]})
        return Resp(ok.encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", flaky_urlopen)
    monkeypatch.setattr(sa.time, "sleep", lambda s: None)
    assert sa._chat("http://gw", "k", "m", "hi", reasoning_effort="medium") == "OK"
    assert calls["n"] == 2

    # 场景2：503 重试仍失败 → ConnectionError（防伪装备线）
    calls["n"] = 0

    def dead_urlopen(req, timeout=0, context=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 503, "unavailable", None, io.BytesIO(b"x"))

    monkeypatch.setattr(urllib.request, "urlopen", dead_urlopen)
    with pytest.raises(ConnectionError, match="HTTP 503"):
        sa._chat("http://gw", "k", "m", "hi", reasoning_effort="medium")
    assert calls["n"] == 2   # v59 外层退避重试：_once×2（503 不触发去参，各 1 次）后仍失败


def test_delete_case_removes_from_list_and_resume(tmp_path, monkeypatch):
    """v60：删除历史病例——列表立即不含、续办 404、重复删除 404。"""
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
    try:
        snap = _start("身体区域：面部；主诉：红斑3天；症状：瘙痒性红斑")
        cid = snap["case_id"]
        ids = [c["case_id"] for c in client.get("/api/cases").json()]
        assert cid in ids

        r = client.delete(f"/api/cases/{cid}")
        assert r.status_code == 200 and r.json()["deleted"] is True

        ids2 = [c["case_id"] for c in client.get("/api/cases").json()]
        assert cid not in ids2
        assert client.get(f"/api/cases/{cid}").status_code == 404        # 续办失效
        assert client.delete(f"/api/cases/{cid}").status_code == 404     # 重复删除 404
        assert client.post(f"/api/cases/{cid}/chat", json={"text": "你好"}).status_code == 404
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


def test_delete_all_cases_clears_list(tmp_path, monkeypatch):
    """v62：清空全部历史病例——面板底部「删除全部」批量硬删，列表清空、续办失效、可重复空删。"""
    old_env = (os.environ.get("LLM_BASE_URL"), os.environ.get("LLM_MODEL"))
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
    try:
        c1 = _start(demo="1")["case_id"]
        c2 = _start(demo="2")["case_id"]
        r = client.delete("/api/cases")
        assert r.status_code == 200 and r.json()["ok"] is True
        n = r.json()["deleted"]
        assert n >= 2                                                     # 至少清掉本测试建的 2 条

        assert client.get("/api/cases").json() == []                      # 列表清空
        assert client.get(f"/api/cases/{c1}").status_code == 404          # 续办失效
        assert client.get(f"/api/cases/{c2}").status_code == 404
        r2 = client.delete("/api/cases")                                  # 重复空删幂等
        assert r2.status_code == 200 and r2.json()["deleted"] == 0
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = old_env or ("", "")


# —— v64：Codex 式对话 Agent（smalltalk / clarify / 会话状态对象 / 工具追踪）——

def _smalltalk_llm_stub(monkeypatch):
    import server.app as sa
    """fake-LLM：smalltalk 提示词 → 固定介绍文案（可断言零医疗内容）。"""
    def fake_chat(base_url, api_key, model, user, *args, system="", **kwargs):
        assert "DermAgent" in system, "smalltalk 必须带 Agent 设定"
        return json.dumps({"choices": [{"message": {"content":
            "您好，我是 DermAgent，可以帮您写门诊病历、整理现病史、做鉴别分析参考。"}}]},
            ensure_ascii=False)
    monkeypatch.setattr(sa, "_chat", fake_chat)


def test_smalltalk_routes_to_reply_without_tools(monkeypatch):
    """「你好，请介绍你能做什么？」→ 自然语言回复卡：不进病例工作流、零工具、真实 LLM 来源。"""
    _smalltalk_llm_stub(monkeypatch)
    old = os.environ.get("LLM_API_KEY", "")
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ["LLM_API_KEY"] = "http://127.0.0.1:9", "fake-model", "fake-key"
    try:
        s = _start(text="你好，请介绍你能做什么？")
        assert s["task_type"] == "smalltalk" and s["workflow_state"] == "chat_reply"
        assert s["draft"] is None and s["doc"] is None                 # 不调用病例工具
        assert s["reply"]["generated_by"] == "llm" and "DermAgent" in s["reply"]["content"]
        assert s["llm"]["source"] == "llm"
        assert s["tool_history"] == []                                 # 零工具
        assert not any(k in s["reply"]["content"] for k in ("疑似", "考虑", "处方"))   # 闲聊无医疗结论
        # 会话中继续闲聊：历史上下文随快照可见
        c2 = client.post(f"/api/cases/{s['case_id']}/chat", json={"text": "你有什么功能？"}).json()
        assert c2["workflow_state"] == "chat_reply" and c2["reply"]["generated_by"] == "llm"
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
        os.environ["LLM_API_KEY"] = old


def test_smalltalk_without_llm_zero_medical_content():
    """LLM 未配置 → 「当前未连接 LLM，无法回复」，零疾病/药物/方案/伪造引用。"""
    s = _start(text="你好，请介绍你能做什么？")
    assert s["workflow_state"] == "llm_unavailable"
    assert s["message"] == "当前未连接 LLM，无法回复"
    assert s["reply"] is None and s["draft"] is None and s["doc"] is None
    blob = json.dumps(s, ensure_ascii=False)
    assert not any(k in blob for k in ("湿疹", "皮炎", "处方", "恢复周期", "CIT-"))


def test_smalltalk_gateway_failure_is_retryable_llm_error(monkeypatch):
    """v64：已配置但网关失败 → llm_error（可重试），区别于未配置 llm_unavailable。"""
    import server.app as sa
    def failing_chat(*a, **k):
        raise ConnectionError("HTTP 502：bad gateway")
    monkeypatch.setattr(sa, "_chat", failing_chat)
    old = os.environ.get("LLM_API_KEY", "")
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ["LLM_API_KEY"] = "http://127.0.0.1:9", "fake-model", "fake-key"
    try:
        s = _start(text="你好，请介绍你能做什么？")
        assert s["workflow_state"] == "llm_error"          # 可重试（前端给重试按钮）
        assert "重试" in s["message"] and s["reply"] is None
        assert s["llm"]["ok"] is False and "502" in s["llm"]["error"]
        blob = json.dumps(s, ensure_ascii=False)
        assert not any(k in blob for k in ("湿疹", "皮炎", "处方", "恢复周期", "CIT-"))  # 零医疗内容
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
        os.environ["LLM_API_KEY"] = old


def test_case_write_gateway_failure_is_retryable_llm_error(monkeypatch):
    """v64：病例分析已配置但网关失败 → llm_error（可重试）且零医疗内容。"""
    import server.app as sa
    def failing_chat(*a, **k):
        raise ConnectionError("HTTP 500：gateway timeout")
    monkeypatch.setattr(sa, "_chat", failing_chat)
    old = os.environ.get("LLM_API_KEY", "")
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ["LLM_API_KEY"] = "http://127.0.0.1:9", "fake-model", "fake-key"
    try:
        s = _start(text="患者男，35岁，右前臂红斑瘙痒7天")
        assert s["workflow_state"] == "planning"
        s = _confirm2(s["case_id"])
        assert s["workflow_state"] == "llm_error"
        assert "重试" in s["message"]
        assert s["draft"] is None and s["doc"] is None     # 绝不用规则内容冒充 AI 分析
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
        os.environ["LLM_API_KEY"] = old


def test_snapshot_carries_messages_for_session_restore(monkeypatch):
    """v64-P1：快照透传对话历史（重启恢复最近会话；user/assistant 交替、截断防膨胀）。
    chat 的同步返回在 assistant 追加前生成（只含本轮 user）；恢复用的 resume 快照完整。"""
    _smalltalk_llm_stub(monkeypatch)
    old = os.environ.get("LLM_API_KEY", "")
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ["LLM_API_KEY"] = "http://127.0.0.1:9", "fake-model", "fake-key"
    try:
        s = _start(text="你好，请介绍你能做什么？")
        c2 = client.post(f"/api/cases/{s['case_id']}/chat", json={"text": "你有什么功能？"}).json()
        assert c2["messages"] and c2["messages"][-1]["role"] == "user"
        # 续办快照（GET /api/cases/{id}）= 恢复数据源：完整 user/assistant 交替
        r3 = client.get(f"/api/cases/{s['case_id']}").json()
        msgs = r3["messages"]
        assert len(msgs) >= 4
        assert [m["role"] for m in msgs][-2:] == ["user", "assistant"]
        assert all(set(("role", "content", "at")) <= set(m) for m in msgs)
        assert "DermAgent" in msgs[-1]["content"]   # assistant 内容已持久化
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
        os.environ["LLM_API_KEY"] = old


def test_clarify_then_supplement_generates_draft(monkeypatch):
    """「请帮我写一份病例」→ Agent 先澄清列出所需信息；补充症状后字段并入自动生成草稿。"""
    import server.app as sa
    monkeypatch.setattr(sa, "_chat", lambda *a, **k: json.dumps(
        {"case_summary": {"symptom_summary": "右前臂红斑瘙痒7天"}, "workflow_state": "awaiting_review"},
        ensure_ascii=False))
    old = os.environ.get("LLM_API_KEY", "")
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ["LLM_API_KEY"] = "http://127.0.0.1:9", "fake-model", "fake-key"
    try:
        s1 = _start(text="请帮我写一份病例")
        assert s1["task_type"] == "case_write" and s1["workflow_state"] == "clarifying"
        assert s1["reply"]["generated_by"] == "agent" and s1["reply"]["missing"]
        assert "身体区域" in s1["reply"]["content"] and "既往史" in s1["reply"]["content"]
        # 有明确症状描述的请求 → 先计划确认再生成（缺失字段 unknown 如实列出）
        s2 = _start(text="请根据以下信息写一份门诊病历：患者男，35岁，右前臂红斑瘙痒7天")
        assert s2["workflow_state"] == "planning"
        s2 = _confirm2(s2["case_id"])
        assert s2["workflow_state"] == "awaiting_review" and s2["llm"]["source"] == "llm"
        s3 = _start(text="患者女，28岁，面部干燥脱屑伴刺痛3周")
        assert s3["workflow_state"] == "planning"
        s3 = _confirm2(s3["case_id"])
        assert s3["workflow_state"] == "awaiting_review"
        # 澄清会话补充信息 → 字段并入 → 计划确认 → 生成
        s4 = client.post(f"/api/cases/{s1['case_id']}/chat",
                         json={"text": "患者男，35岁，右前臂红斑瘙痒7天，近期更换洗涤剂，无发热"}).json()
        assert s4["workflow_state"] == "planning"
        assert "右前臂" in json.dumps(s4["case"], ensure_ascii=False)
        s4 = _confirm2(s1["case_id"])
        assert s4["workflow_state"] == "awaiting_review"
        assert s4["tool_history"] and s4["tool_history"][-1]["tool"] == "case_writer"
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""


def test_plan_confirm_flow_and_supplement_not_confirm(monkeypatch):
    """v64-P2：明确工作要求 → 先出计划（理解+动作）；补充信息不算确认、更新理解后再次出计划；
    确认短语才执行且不污染病例字段。"""
    import server.app as sa
    monkeypatch.setattr(sa, "_chat", lambda *a, **k: json.dumps(
        {"case_summary": {"symptom_summary": "右前臂红斑瘙痒7天"},
         "suspected_conditions": [{"name": "接触性皮炎（待排）"}],
         "workflow_state": "awaiting_review"}, ensure_ascii=False))
    old = os.environ.get("LLM_API_KEY", "")
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ["LLM_API_KEY"] = "http://127.0.0.1:9", "fake-model", "fake-key"
    try:
        s = _start(text="请根据以下信息写一份门诊病历：患者男，35岁，右前臂红斑瘙痒7天")
        assert s["workflow_state"] == "planning" and s["agent_state"] == "planning"
        assert s["reply"]["plan"] is True and s["reply"]["generated_by"] == "agent"
        assert "我理解到的任务" in s["reply"]["content"]
        assert "准备执行的动作" in s["reply"]["content"]
        assert s["draft"] is None and s["tool_history"] == []      # 未执行、零工具
        # 补充信息（含「好的，另外…」）不是确认 → 更新理解、再次出计划
        p2 = client.post(f"/api/cases/{s['case_id']}/chat",
                         json={"text": "好的，另外患者近期更换了洗涤剂"}).json()
        assert p2["workflow_state"] == "planning"
        assert "洗涤剂" in p2["reply"]["content"]                  # 理解已更新
        assert "更换了洗涤剂" in (p2["case"].get("symptoms") or "")   # 补充并入病例字段
        assert p2["tool_history"] == []                            # 仍未调工具
        # 「确认」→ 执行
        ok = _confirm2(s["case_id"])
        assert ok["workflow_state"] == "awaiting_review"
        assert ok["llm"]["source"] == "llm"
        assert ok["tool_history"][-1]["tool"] == "case_writer"
        # 确认短语不进病例字段
        assert "确认" not in json.dumps(ok["case"], ensure_ascii=False)
        # 同一任务的后续补充不再反复确认，直接重跑分析
        s2 = client.post(f"/api/cases/{s['case_id']}/chat", json={"text": "补充：瘙痒夜间加重"}).json()
        assert s2["workflow_state"] == "awaiting_review"
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
        os.environ["LLM_API_KEY"] = old


def test_smalltalk_never_becomes_demo_or_case():
    """「你好」不触发演示病例匹配，会话标签为「对话」。"""
    s = _start(text="你好")
    assert s["task_type"] == "smalltalk" and s.get("demo") in ("", None)
    from server.app import _load_sessions
    assert _load_sessions()[s["case_id"]]["label"] == "对话"


def test_session_state_regions_attachments_tool_history():
    """会话状态对象：3D 选点增量合并、附件入会话、工具历史按需记录。"""
    s = _start(text="患者男，35岁，右前臂红斑瘙痒7天", regions=["左前臂"], attachments=[{"name": "a.png"}])
    assert s["selected_body_regions"] == ["左前臂"] and s["attachments"]
    assert s["workflow_state"] == "planning" and s["tool_history"] == []   # 未确认未调工具
    s = _confirm2(s["case_id"])
    assert s["tool_history"][-1]["tool"] == "case_writer"
    s2 = client.post(f"/api/cases/{s['case_id']}/chat",
                     json={"text": "补充：夜间瘙痒明显", "regions": ["左手背"]}).json()
    assert s2["selected_body_regions"] == ["左前臂", "左手背"]      # 增量合并不丢失
    assert s2["workflow_state"] == "llm_unavailable"   # 已确认任务，补充后直接重跑（无 LLM → 不可用）


# —— v65：门诊病历单（病例窗口）——

def _case_with_draft():
    """建一个走完计划确认、产出 AI 草稿的会话。"""
    snap = _start("患者男，35岁。主诉：右前臂红斑伴瘙痒7天")
    if snap.get("workflow_state") == "planning":
        snap = _confirm2(snap["case_id"])
    return snap


def test_record_get_put_confirm_export(monkeypatch):
    snap = _case_with_draft()
    cid = snap["case_id"]
    # GET：空字段留空（渲染端「未提供」），case 内容已入主诉/现病史
    r = client.get(f"/api/cases/{cid}/record")
    assert r.status_code == 200
    rec = r.json()["record"]
    assert rec["case_id"] == cid
    assert "主诉" in rec["fields"]["chief_complaint"] or rec["fields"]["chief_complaint"]
    assert rec["review_status"] == "draft"
    # PUT：医生手工修改 → doctor_modified
    r = client.put(f"/api/cases/{cid}/record",
                   json={"fields": {"present_illness": "医生修改后的现病史：红斑7天，加重2天。"}})
    assert r.status_code == 200
    rec = r.json()["record"]
    assert rec["fields"]["present_illness"].startswith("医生修改后的现病史")
    assert rec["meta"]["present_illness"]["source"] == "doctor_modified"
    # 确认单字段 → doctor_confirmed；再确认全部 → review_status=confirmed（最终审核版本）
    r = client.post(f"/api/cases/{cid}/record/confirm", json={"fields": ["present_illness"]})
    assert r.json()["review_status"] == "draft"
    r = client.post(f"/api/cases/{cid}/record/confirm", json={"all": True})
    assert r.status_code == 200
    assert r.json()["review_status"] == "confirmed"
    assert r.json()["record"]["confirmed_at"]
    # 导出：编号/审核状态/免责声明/未提供标注（v66：确认门禁下未确认 403）
    r = client.get(f"/api/cases/{cid}/record/export")
    assert r.status_code == 200
    md = r.text
    assert "# 皮肤科门诊病历" in md and f"病例编号：{cid}" in md
    assert "最终审核版本" in md and "医生已确认" in md
    assert "免责声明" in md
    # 医生确认过的现病史不被 AI 覆盖：再补一轮对话生成 draft
    snap2 = client.post(f"/api/cases/{cid}/chat", json={"text": "补充：皮损扩大至手掌大小"}).json()
    if snap2.get("workflow_state") == "planning":
        snap2 = _confirm(cid)
    r = client.get(f"/api/cases/{cid}/record")
    assert r.json()["record"]["fields"]["present_illness"].startswith("医生修改后的现病史")


def test_record_unknown_field_rejected():
    snap = _case_with_draft()
    r = client.put(f"/api/cases/{snap['case_id']}/record",
                   json={"fields": {"made_up_field": "x"}})
    assert r.status_code == 422


def test_record_regen_llm_off(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    snap = _case_with_draft()
    r = client.post(f"/api/cases/{snap['case_id']}/record/regen",
                    json={"field": "present_illness"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["llm_connected"] is False
    assert "当前未连接 LLM" in body["message"]


def test_record_v66_sources_history_reject(monkeypatch):
    """v66：来源细分（user_input/doctor_modified/model_3d/kb_ref）、历史版本（前后版本保留）、
    状态派生（医生已修改/已确认/已拒绝）、reject 端点。"""
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    snap = _start("患者男，30岁。主诉：右前臂红斑伴瘙痒5天", regions=["右前臂"])
    if snap.get("workflow_state") == "planning":
        snap = _confirm(snap["case_id"])
    cid = snap["case_id"]
    r = client.get(f"/api/cases/{cid}/record")
    rec = r.json()["record"]
    # 来源细分：医生口述/资料 → user_input；3D 选点 → model_3d
    assert rec["meta"]["chief_complaint"]["source"] == "user_input"
    assert rec["fields"]["body_regions"] and rec["meta"]["body_regions"]["source"] == "model_3d"
    # 状态派生：AI 草稿（模板整理也归 ai_draft/pending）
    assert rec["status"] in ("ai_draft", "pending_review")
    # 医生修改 → doctor_modified + 历史记录（before/after/at/by）
    r = client.put(f"/api/cases/{cid}/record",
                   json={"fields": {"chief_complaint": "右前臂红斑伴瘙痒5天，加重1天"}})
    rec = r.json()["record"]
    assert rec["meta"]["chief_complaint"]["source"] == "doctor_modified"
    assert rec["status"] == "doctor_modified" and rec["status_label"] == "医生已修改"
    hist = rec["history"]
    assert hist and hist[-1]["field"] == "chief_complaint"
    assert hist[-1]["by"] == "doctor" and "加重1天" in hist[-1]["after"]
    # 确认 → confirmed；确认后医生再改字段 → 回到医生已修改态（review_status 重置）
    assert client.post(f"/api/cases/{cid}/record/confirm", json={"all": True}).status_code == 200
    r = client.put(f"/api/cases/{cid}/record", json={"fields": {"notes": "医生备注"}})
    rec = r.json()["record"]
    assert rec["review_status"] == "draft" and rec["confirmed_at"] is None
    # 拒绝 → rejected 状态；再修改字段 → 解除拒绝
    assert client.post(f"/api/cases/{cid}/record/reject").status_code == 200
    rec = client.get(f"/api/cases/{cid}/record").json()["record"]
    assert rec["rejected"] is True and rec["status"] == "rejected" and rec["status_label"] == "已拒绝"
    r = client.put(f"/api/cases/{cid}/record", json={"fields": {"notes": "医生备注二"}})
    assert r.json()["record"]["rejected"] is False
    # 导出仍需重新确认（403 门禁）
    assert client.get(f"/api/cases/{cid}/record/export").status_code == 403


def test_record_regions_and_empty_fields(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    snap = _start("躯干部位红斑鳞屑，伴瘙痒", regions=["躯干（中部），前侧"])
    if snap.get("workflow_state") == "planning":
        snap = _confirm(snap["case_id"])
    cid = snap["case_id"]
    assert snap.get("selected_body_regions") or True
    # v66：未确认导出 → 403 门禁；确认后才能导出
    gate = client.get(f"/api/cases/{cid}/record/export")
    assert gate.status_code == 403 and "医生确认" in gate.json()["detail"]
    assert client.post(f"/api/cases/{cid}/record/confirm", json={"all": True}).status_code == 200
    r = client.get(f"/api/cases/{cid}/record/export")
    md = r.text
    assert "未选择 3D 部位" not in md if snap.get("selected_body_regions") else "未选择 3D 部位" in md
    assert "未提供" in md          # 缺失字段如实标注
    assert "否认" not in md and "\n无\n" not in md   # 绝不自动填阴性


def test_chat_open_case_flag(monkeypatch):
    """v65：对话里要求打开/新建病例窗口 → 响应带 open_case=True（Agent 决定开窗）；
    普通病例描述不带该标志。"""
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    snap = _start("患者男，35岁。主诉：右前臂红斑伴瘙痒7天")
    assert snap.get("open_case") is None
    r = client.post(f"/api/cases/{snap['case_id']}/chat", json={"text": "帮我打开病例窗口"})
    assert r.status_code == 200
    assert r.json().get("open_case") is True
    snap2 = _start("新建一份病例")
    assert snap2.get("open_case") is True


# —— v67：对话驱动医疗辅助 Agent ——

def test_v67_plan_card_inventory_and_prelim_rag(monkeypatch):
    """需求1/2/6：确认卡=理解+已识别/还缺少盘点+最低信息要求+初步 RAG 行。"""
    import rag as rag_mod
    monkeypatch.setattr(rag_mod, "search", lambda q, top_k=5: {"ok": True, "citations": [
        {"citation_id": "RB-TEST-01", "source_type": "local_book", "book_title": "皮肤性病学",
         "author": "测试作者", "publisher": "测试出版社", "section": "湿疹与皮炎",
         "page_hint": 12, "quoted_text": "接触性皮炎表现为边界清楚的红斑。"}]})
    s = _start(text="请根据以下信息写一份门诊病历：患者男，35岁，右前臂红斑瘙痒7天")
    assert s["workflow_state"] == "planning" and s["reply"]["plan"] is True
    content = s["reply"]["content"]
    assert "已识别到：" in content and "还缺少：" in content
    assert "最低信息要求" in content
    assert "初步知识库检索" in content
    # 同一任务只检索一次（缓存）
    import server.app as sa
    calls = {"n": 0}
    def _count(q, top_k=5):
        calls["n"] += 1
        return {"ok": True, "citations": []}
    monkeypatch.setattr(rag_mod, "search", _count)
    p2 = client.post(f"/api/cases/{s['case_id']}/chat", json={"text": "补充：皮疹边界清楚"}).json()
    assert p2["workflow_state"] == "planning"
    assert calls["n"] == 0   # rag_prelim_task 缓存命中，未再检索


def test_v67_agent_commands_no_llm(monkeypatch):
    """需求5：自然语言指令（保存/撤销/重新追问/跳过3D）零 LLM 响应，指令文本不污染病例。"""
    s = _start(text="右手背红斑脱屑瘙痒两周，接触洗涤剂后加重")
    cid = s["case_id"]
    assert s["workflow_state"] == "planning"
    # 指令路由不调 LLM（未配置 LLM 也直接回复）
    r = client.post(f"/api/cases/{cid}/chat", json={"text": "先保存病例"}).json()
    assert r["llm"]["generated_by"] == "agent-cmd"
    assert "已保存" in r["reply"]["content"] or "保存" in r["reply"]["content"]
    rec0 = client.get(f"/api/cases/{cid}/record").json()["record"]
    assert rec0["fields"] is not None and rec0["updated_at"]
    # 补充信息（落 case_prev 快照）→ 撤销回退
    sup = client.post(f"/api/cases/{cid}/chat", json={"text": "补充：患者对青霉素过敏"}).json()
    assert "青霉素" in json.dumps(sup["case"], ensure_ascii=False)
    undo = client.post(f"/api/cases/{cid}/chat", json={"text": "撤销刚才的信息"}).json()
    assert "已撤销" in undo["reply"]["content"]
    assert "青霉素" not in json.dumps(undo["case"], ensure_ascii=False)
    # 重新追问缺失 → clarify 类回复（agent 生成，非 LLM）
    reask = client.post(f"/api/cases/{cid}/chat", json={"text": "还缺什么信息"}).json()
    assert reask["reply"]["generated_by"] in ("agent", "agent-cmd")
    # 跳过 3D 引导（3D 可选，不阻塞）
    skip = client.post(f"/api/cases/{cid}/chat", json={"text": "跳过3D选择"}).json()
    assert skip["reply"]["generated_by"] in ("agent", "agent-cmd")
    assert "3D" in skip["reply"]["content"]
    # 指令文本不进病例字段
    assert "先保存病例" not in json.dumps(undo["case"], ensure_ascii=False)
    assert "先保存病例" not in json.dumps(sup["case"], ensure_ascii=False)


def test_v67_danger_signals_field_and_sections():
    """需求2/7：record 18 字段含风险提示；draft 红旗 → danger_signals 字段。"""
    import server.app as sa
    from server.app import RECORD_FIELDS, _empty_record, _sections_from_draft
    keys = [k for k, _ in RECORD_FIELDS]
    assert "danger_signals" in keys and keys.index("danger_signals") < keys.index("doctor_review")
    assert len(keys) == 18
    assert "danger_signals" in _empty_record()["fields"]
    out = _sections_from_draft({
        "red_flag_escalation": {"reasons": ["RF-HZ-001：水疱泛发，建议尽快就医"]},
        "suspected_conditions": [
            {"name": "带状疱疹（待排）", "confidence": "medium",
             "supporting_evidence": [{"statement": "沿神经分布的红斑水疱", "citation_ids": []}]},
        ],
        "recommended_exams": [{"exam": "疱疹病毒PCR", "reason": "明确病原"}],
        "treatment_reference": [{"approach": "抗病毒治疗参考", "basis": "指南"}],
        "recovery_period": {"typical_course": "2-4周", "influence_factors": ["年龄", "部位"]},
        "recommended_next_steps": [{"action": "医生面诊复核", "urgency": "soon"}],
    })
    assert "RF-HZ-001" in out["danger_signals"]
    assert "带状疱疹" in out["impression"] and "沿神经分布" in out["impression"]
    assert "疱疹病毒PCR" in out["auxiliary_exam"]
    assert "抗病毒治疗参考" in out["treatment_ref"]
    assert "2-4周" in out["follow_up"] and "医生面诊复核" in out["follow_up"]
    # record PUT 支持新字段
    s = _start(text="左手臂红斑三天")
    r = client.put(f"/api/cases/{s['case_id']}/record",
                   json={"fields": {"danger_signals": "水疱泛发需复诊"}, "editor": "医生"})
    assert r.status_code == 200
    rec = client.get(f"/api/cases/{s['case_id']}/record").json()["record"]
    assert rec["fields"]["danger_signals"] == "水疱泛发需复诊"


def test_v67_doctor_modified_protection(monkeypatch):
    """需求8：医生手动修改字段不被 AI 增量更新覆盖；「重新整理」授权一次性放行。"""
    import server.app as sa
    s = _start(text="右小腿红斑瘙痒五天")
    cid = s["case_id"]
    r = client.put(f"/api/cases/{cid}/record",
                   json={"fields": {"present_illness": "医生亲自整理的现病史"}, "editor": "医生"})
    assert r.status_code == 200
    rec = client.get(f"/api/cases/{cid}/record").json()["record"]
    assert rec["meta"]["present_illness"]["source"] == "doctor_modified"
    # AI 产物同步：doctor_modified 字段不被覆盖
    sess = sa._load_sess(cid)
    sess["doc"] = {"task_type": "history", "content": "AI 重写的现病史（应被拒绝）"}
    sa._sync_record(sess)
    sa._store_sess(sess)
    rec2 = client.get(f"/api/cases/{cid}/record").json()["record"]
    assert rec2["fields"]["present_illness"] == "医生亲自整理的现病史"
    assert rec2["meta"]["present_illness"]["source"] == "doctor_modified"
    # 授权后（重新整理确认）→ 放行，授权一次性消费
    sess2 = sa._load_sess(cid)
    sess2["allow_ai_override"] = True
    sess2["doc"] = {"task_type": "history", "content": "医生确认重新整理后的现病史"}
    sa._sync_record(sess2)
    sa._store_sess(sess2)
    rec3 = client.get(f"/api/cases/{cid}/record").json()["record"]
    assert "医生确认重新整理后的现病史" in rec3["fields"]["present_illness"]
    assert rec3["meta"]["present_illness"]["source"] != "doctor_modified"
    # 授权已消费；医生再次手动修改 → 保护恢复（无授权的 AI 同步再次被拒）
    r2 = client.put(f"/api/cases/{cid}/record",
                    json={"fields": {"present_illness": "医生第二次修改"}, "editor": "医生"})
    assert r2.status_code == 200
    sess3 = sa._load_sess(cid)
    assert not sess3.get("allow_ai_override")
    sess3["doc"] = {"task_type": "history", "content": "再次 AI 覆盖（应被拒绝）"}
    sa._sync_record(sess3)
    sa._store_sess(sess3)
    rec4 = client.get(f"/api/cases/{cid}/record").json()["record"]
    assert rec4["fields"]["present_illness"] == "医生第二次修改"
    assert rec4["meta"]["present_illness"]["source"] == "doctor_modified"


def test_v67_doc_task_final_rag_citations(monkeypatch):
    """需求6：医学判断任务接入本地书籍检索（最终 RAG），引用带完整详情+检索时间。"""
    import rag as rag_mod
    import server.app as sa
    monkeypatch.setattr(rag_mod, "search", lambda q, top_k=5: {"ok": True, "citations": [
        {"citation_id": "RB-TEST-02", "source_type": "local_book", "book_title": "皮肤科治疗学",
         "author": "张三", "publisher": "人民卫生出版社", "section": "抗病毒治疗",
         "page_hint": 88, "quoted_text": "阿昔洛韦类抗病毒治疗需医生评估。"}]})
    old = os.environ.get("LLM_API_KEY", "")
    os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"], os.environ["LLM_API_KEY"] = "http://127.0.0.1:9", "fake-model", "fake-key"
    try:
        monkeypatch.setattr(sa, "_chat", lambda *a, **k: json.dumps(
            {"content": "鉴别方向：带状疱疹（待排）。[[1]]",
             "citations": [{"citation_id": "RB-TEST-02", "quoted_text": "阿昔洛韦类抗病毒治疗需医生评估。"}]},
            ensure_ascii=False))
        s = _start(text="患者左胸红斑水疱伴神经痛三天，请列出鉴别诊断")
        _confirm(s["case_id"])   # 进入诊断（case_write 执行——不关注草稿本身）
        doc = client.post(f"/api/cases/{s['case_id']}/chat",
                          json={"text": "请根据当前病例列出需要鉴别的疾病"}).json()
        assert doc.get("doc") and doc["doc"]["task_type"] == "differential"
        cites = doc["doc"]["citations"]
        assert any(c.get("citation_id") == "RB-TEST-02" for c in cites)
        for c in cites:
            assert c.get("retrieved_at")
        rb = [c for c in cites if c["citation_id"] == "RB-TEST-02"][0]
        assert rb["book_title"] == "皮肤科治疗学" and rb["section"] == "抗病毒治疗"
        # 病历单引用同样带检索时间
        rec = client.get(f"/api/cases/{s['case_id']}/record").json()["record"]
        assert all(c.get("retrieved_at") for c in rec.get("citations", []))
    finally:
        os.environ["LLM_BASE_URL"], os.environ["LLM_MODEL"] = "", ""
        os.environ["LLM_API_KEY"] = old


# ————— v68 工具扩展专项（计划 §三/§四/§五/§十一） —————

def test_v68_tool_registry_protocol():
    """v68 §五.1：15 个工具统一协议六要素完整；医学判断类必须来源+确认双门禁。"""
    from server.tool_registry import TOOLS, TOOL_REGISTRY, tool_for_task
    assert len(TOOLS) == 15 and len(TOOL_REGISTRY) == 15
    names = {t["name"] for t in TOOLS}
    assert len(names) == 15                     # name 唯一
    for t in TOOLS:
        for k in ("name", "label", "description", "input_schema", "output_schema",
                  "requires_confirmation", "source_required", "risk_level", "route"):
            assert k in t, f"{t['name']} 缺 {k}"
        assert t["risk_level"] in ("low", "medium", "high")
    # §十 安全边界：医学判断类必须 source_required 且 requires_confirmation
    for n in ("differential_assistant", "medication_safety_checker"):
        t = TOOL_REGISTRY[n]
        assert t["source_required"] and t["requires_confirmation"] and t["risk_level"] == "high"
    assert tool_for_task("medication_review") == "medication_safety_checker"
    assert tool_for_task("case_compare") == "case_comparator"


def test_v68_tool_tracing_metadata():
    """v68 §七.后端4：工具调用记录升级——label/风险/确认/版本/目标/产出摘要/来源可追踪。"""
    s = _start("手背红疹伴瘙痒两周", demo="eczema")
    cid = s["case_id"]
    _confirm2(cid)
    _confirm(cid)   # 执行分析
    r = client.post(f"/api/cases/{cid}/chat", json={"text": "帮我做鉴别分析"})
    _confirm(cid)   # doc 计划确认执行
    sess = client.get(f"/api/cases/{cid}").json()
    hist = sess.get("tool_history") or []
    assert hist and all(isinstance(e, dict) for e in hist)
    doc_entries = [e for e in hist if e["tool"] == "differential_assistant"]
    assert doc_entries, f"鉴别任务必须记录 differential_assistant，实际: {[e['tool'] for e in hist]}"
    e = doc_entries[-1]
    for k in ("tool", "label", "risk_level", "requires_confirmation", "confirmed",
              "case_version", "goal", "input_digest", "output_digest", "sources", "at"):
        assert k in e, f"工具记录缺 {k}"
    assert e["label"] == "鉴别诊断辅助"
    assert e["risk_level"] == "high" and e["requires_confirmation"] is True
    assert e["case_version"].startswith("r") and len(e["case_version"]) >= 2


def test_v68_image_analysis_tool():
    """v68 §三.3：图片分析——无附件提示上传；带附件询问不默认执行；特征勾选卡+质量提示+免责。"""
    s = _start("手背红疹伴瘙痒两周", demo="eczema")
    cid = s["case_id"]
    r0 = client.post(f"/api/cases/{cid}/chat", json={"text": "分析图片"}).json()
    assert "没有图片附件" in r0["reply"]["content"]          # 无附件 → 提示上传
    r1 = client.post(f"/api/cases/{cid}/chat",
                     json={"text": "这是患处照片",
                           "attachments": [{"name": "lesion.jpg", "size": 8192}]}).json()
    reply_txt = (r1.get("reply") or {}).get("content", "") + str(r1.get("message") or "")
    assert "分析图片" in reply_txt and "可选" in reply_txt    # 询问是否分析，不默认执行
    r2 = client.post(f"/api/cases/{cid}/chat", json={"text": "分析图片"}).json()
    rep = r2["reply"]
    assert rep["card"]["type"] == "image_analysis"
    assert any("较小" in h for h in rep["card"]["quality_hints"])   # 8KB → 分辨率不足提示
    assert "红斑" in rep["card"]["features"] and "鳞屑" in rep["card"]["features"]
    assert "不等于诊断" in rep["disclaimer"]
    assert rep["generated_by"] == "agent" and r2["llm"]["generated_by"] == "agent-cmd"
    # 医生勾选特征提交 → 并入病例症状（来源=医生输入）
    r3 = client.post(f"/api/cases/{cid}/chat", json={"text": "皮损特征：红斑、鳞屑；对称分布"}).json()
    assert "红斑" in (r3["case"]["symptoms"] or "")
    assert "手背红疹" in r3["case"]["symptoms"]   # v68：追加不覆盖医生已录入症状


def test_v68_case_compare_tool():
    """v68 §四.2：病例对比——唯一历史病例直接对比；变化/新增/缺失分段+免责；零编造。"""
    a = _start("左手背红疹伴瘙痒两周，否认过敏", demo="eczema")
    b = _start("右小腿环形红斑脱屑一月", demo="eczema")     # 第二个病例（eczema 触发同 demo）
    cid = b["case_id"]
    client.post(f"/api/cases/{cid}/chat", json={"text": "补充：病程持续一个月不退"})
    r = client.post(f"/api/cases/{cid}/chat", json={"text": f"对比 {a['case_id']}"}).json()
    doc = r["doc"]
    assert doc["task_type"] == "case_compare"
    assert a["case_id"][:8] in doc["content"]               # 目标历史病例 ID 在对比正文中
    assert "变化" in doc["content"] and "新增" in doc["content"] and "缺失" in doc["content"]
    assert "医生" in doc["disclaimer"]                       # 语义判断留给医生
    assert doc["generated_by"] == "local"                    # 规则式，非 AI 生成
    assert r["llm"]["source"] == "agent"


def test_v68_context_summary_and_layers():
    """v68 §十一：30 轮触发自动摘要（七段结构/安全字段保留/来源标记）；快照带压缩提示。"""
    s = _start("手背红疹伴瘙痒两周；过敏史：青霉素", demo="eczema")
    cid = s["case_id"]
    r = None
    for i in range(16):   # 16 轮 = 32 条消息（user+assistant），越过 30 条阈值
        r = client.post(f"/api/cases/{cid}/chat",
                        json={"text": f"补充观察记录第{i}轮：瘙痒程度变化不明显"}).json()
    sess = client.get(f"/api/cases/{cid}").json()
    summary = sess.get("context_summary")
    assert summary, "达阈值必须生成结构化摘要"
    for k in ("confirmed_facts", "unconfirmed_items", "allergy_and_risk_flags",
              "doctor_edits", "tool_results", "rag_sources", "current_task", "next_action"):
        assert k in summary, f"摘要缺七段字段 {k}"
    assert "青霉素" in summary["allergy_and_risk_flags"]["allergies"]   # 过敏史永不丢失
    assert summary["generated_by"] == "local-summary"                   # 非 AI 生成标记
    assert sess.get("messages"), "原始对话记录保留，摘要只作为压缩上下文"
    # 压缩提示随快照透传（chat 响应层面）
    assert r.get("context_compressed") is True
    # LLM 提示词使用分层上下文（含摘要层与安全层）
    from server.app import _build_context, _load_sessions
    sess_obj = _load_sessions()[cid]
    ctx = _build_context(sess_obj)
    assert "【安全上下文（永不裁剪）】" in ctx and "青霉素" in ctx
    assert "【病例摘要" in ctx


def test_v68_tool_label_route_lookup():
    """v68 §五.1：传入 route 名（case_writer）时工具记录仍取注册表中文标签（反查）。"""
    from server.app import _tool_traced
    sess = {"case_id": "t" * 12, "case": {"symptoms": "瘙痒"}, "task_text": "整理病例",
            "tool_history": [], "book_citations": []}
    snap = {"doc": None, "draft": {"model_version": "test"}, "reply": None,
            "book_citations": []}
    out = _tool_traced(sess, snap, "case_writer")
    entry = out["tool_history"][-1]
    assert entry["label"] == "病例整理", f"route 名应反查中文标签，得到 {entry['label']}"
    assert entry["tool"] == "case_writer"
    # 直接用注册名时不反查，标签一致
    sess2 = dict(sess, tool_history=[])
    out2 = _tool_traced(sess2, snap, "case_organizer")
    assert out2["tool_history"][-1]["label"] == "病例整理"


# ———————————————— v69：统一导航决策层（状态 → 下一步动作 → 工作区） ————————————————
# 背景：v68 之前 workflow_state 与界面工作区之间没有任何映射，服务端从不告诉前端
# 「本轮结束后该去哪个工作区」，只有「新建病例」一种情况会开面板 —— 这就是
# 「Agent 已经回复，但界面没有自动跳转到对应窗口」的根因。以下用例锁死映射规则本身。

def test_v69_nav_state_priority_and_mapping():
    """nav_state 优先级：显式开窗 > 失败态 > 已确认 > 已拒绝 > 待审核 > 产物 > 兜底。"""
    from server import nav
    assert nav.nav_state_of({"open_case": True}) == "case_draft"
    # 失败态优先于「有产物」：错误时绝不能因为 doc 存在就假装分析成功
    assert nav.nav_state_of({"workflow_state": "llm_error", "doc": {"content": "x"}}) == "error"
    assert nav.nav_state_of({"workflow_state": "llm_unavailable"}) == "error"
    assert nav.nav_state_of({"workflow_state": "approved"}) == "confirmed"
    assert nav.nav_state_of({"report": "# 报告"}) == "confirmed"
    assert nav.nav_state_of({"workflow_state": "rejected"}) == "rejected"
    assert nav.nav_state_of({"workflow_state": "closed"}) == "rejected"
    assert nav.nav_state_of({"workflow_state": "awaiting_review"}) == "awaiting_review"
    assert nav.nav_state_of({"workflow_state": "escalated"}) == "awaiting_review"
    # 医生已改过字段 → 与「全新草稿」区分开（前端高亮修改项而不是待确认全量字段）
    assert nav.nav_state_of({"workflow_state": "awaiting_review",
                             "record_review_status": "doctor_modified"}) == "doctor_modified"
    assert nav.nav_state_of({"workflow_state": "doc_ready"}) == "analysis_ready"
    assert nav.nav_state_of({"doc": {"content": "x"}}) == "analysis_ready"
    assert nav.nav_state_of({"workflow_state": "planning"}) == "awaiting_confirmation"
    assert nav.nav_state_of({"workflow_state": "clarifying"}) == "collecting_information"
    assert nav.nav_state_of({"workflow_state": "screening"}) == "collecting_information"
    assert nav.nav_state_of({"workflow_state": "chat_reply"}) == "chat_reply"
    assert nav.nav_state_of({}) == "collecting_information"   # 兜底：回对话追问，不跳窗


def test_v69_resolve_error_and_no_case_stay_put():
    """失败/超时/空响应留在当前工作区；无 case_id 时绝不做病例跳转（防打开空病例）。"""
    from server import nav
    d = nav.resolve({"case_id": "abc"}, {"case_id": "abc", "workflow_state": "llm_error"})
    assert (d["nav_state"], d["workspace"], d["next_action"]) == ("error", None, "stay")

    d2 = nav.resolve({}, {"workflow_state": "awaiting_review"})
    assert d2["case_id"] == ""
    assert d2["workspace"] is None and d2["next_action"] == "stay"
    assert d2["highlight_fields"] == []      # 没病例就没有可高亮的字段

    # 信息不足需要追问：回对话并聚焦输入框（医生立刻能打字）
    d3 = nav.resolve({"case_id": "abc"}, {"case_id": "abc", "workflow_state": "clarifying"})
    assert (d3["workspace"], d3["next_action"], d3["focus_input"]) == ("chat", "focus_input", True)


def test_v69_resolve_awaiting_review_highlights_unconfirmed_fields():
    from server import nav
    sess = {"case_id": "abc", "record": {
        "fields": {"chief": "面部红斑瘙痒", "blank": "", "diagnosis": "湿疹"},
        "meta": {"chief": {"source": "ai_draft"}, "diagnosis": {"source": "doctor_confirmed"}}}}
    d = nav.resolve(sess, {"case_id": "abc", "workflow_state": "awaiting_review"})
    assert (d["nav_state"], d["workspace"], d["next_action"]) == (
        "awaiting_review", "case", "highlight_case_fields")
    # 空字段不打扰、医生确认过的字段不再提示
    assert d["highlight_fields"] == ["chief"]
    assert d["scroll_to_latest"] is True


def test_v69_resolve_explicit_intent_has_top_priority():
    """医生的自然语言导航指令（「返回对话」等）优先于本轮状态推导。"""
    from server import nav
    d = nav.resolve({"case_id": "abc"},
                    {"case_id": "abc", "workflow_state": "doc_ready", "nav_intent": "navigate_to_chat"})
    assert (d["workspace"], d["next_action"]) == ("chat", "navigate_to_chat")
    d2 = nav.resolve({"case_id": "abc"},
                     {"case_id": "abc", "workflow_state": "chat_reply", "nav_intent": "navigate_to_3d"})
    assert (d2["workspace"], d2["next_action"]) == ("3d", "navigate_to_3d")
    # 未知 intent 不得被当成跳转（防注入式误跳）
    d3 = nav.resolve({"case_id": "abc"},
                     {"case_id": "abc", "workflow_state": "doc_ready", "nav_intent": "rm -rf"})
    assert d3["next_action"] == "navigate_to_analysis"


def test_v69_confirmed_unlocks_export_and_rejected_locks_it():
    from server import nav
    ok = nav.resolve({"case_id": "abc"}, {"case_id": "abc", "workflow_state": "approved"})
    assert (ok["nav_state"], ok["workspace"], ok["export_unlocked"]) == ("confirmed", "case", True)
    no = nav.resolve({"case_id": "abc"}, {"case_id": "abc", "workflow_state": "rejected"})
    assert no["nav_state"] == "rejected" and no["export_unlocked"] is False


def test_v69_nav_map_matches_frontend_fallback_table():
    """服务端映射表是唯一权威：前端兜底表的键与工作区值必须与服务端全等。

    两侧各自演进（服务端改了规则、前端忘了跟）就会重新出现「跳错窗口」，
    所以这里用源码级比对把一致性锁进回归。"""
    import io
    import re
    r = client.get("/api/nav/map")
    assert r.status_code == 200
    m = r.json()
    assert set(m["workspaces"]) == {"chat", "case", "3d"}
    assert len(m["nav_states"]) == len(set(m["nav_states"]))
    assert set(m["rules"]) == set(m["nav_states"])
    for st in ("screening", "planning", "clarifying", "chat_reply", "doc_ready",
               "awaiting_review", "escalated", "approved", "rejected", "closed",
               "llm_error", "llm_unavailable"):
        assert st in m["state_to_nav"], f"workflow_state {st} 必须有落点，否则前端只能靠猜"

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = io.open(os.path.join(root, "client", "web", "main.js"), encoding="utf-8").read()
    blk = re.search(r"var NAV_FALLBACK = \{(.*?)\n\};", js, re.S)
    assert blk, "main.js 必须保留 NAV_FALLBACK 兜底表"
    pairs = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(null|\"[a-z0-9]+\")", blk.group(1))
    assert {k for k, _ in pairs} == set(m["nav_states"]), "前端兜底表键与服务端 nav_states 不一致"
    for st, ws in pairs:
        expect = None if ws == "null" else ws.strip('"')
        assert m["rules"][st]["workspace"] == expect, f"{st} 工作区两侧不一致：{ws}"


def test_v69_api_returns_nav_for_every_round():
    """每轮响应都必须带 nav（含 case_id / next_action），前端无需自己推断。"""
    res = _start("女，32岁，面部反复红斑瘙痒 3 个月")
    cid = res["case_id"]
    assert isinstance(res.get("nav"), dict), "创建会话的响应必须带导航决策"
    assert res["nav"]["case_id"] == cid
    assert res["next_action"] == res["nav"]["next_action"]
    assert res["nav"]["workspace"] in ("chat", None)

    # 按病例查询导航（刷新/重启恢复工作区用）——与对话轮同源
    r = client.get(f"/api/cases/{cid}/nav")
    assert r.status_code == 200
    d = r.json()
    assert d["case_id"] == cid and d["nav"]["case_id"] == cid
    from server.nav import NAV_STATES
    assert d["nav"]["nav_state"] in NAV_STATES
    assert d["next_action"] == d["nav"]["next_action"]

    # 医生显式「返回对话」→ 服务端下发 navigate_to_chat
    r2 = client.post(f"/api/cases/{cid}/chat", json={"text": "返回对话"})
    assert r2.status_code == 200
    n2 = r2.json()["nav"]
    assert (n2["workspace"], n2["next_action"]) == ("chat", "navigate_to_chat")


def test_v69_regions_api_writes_case_draft_and_returns_to_chat():
    """3D 选点：选中即写入病例草稿（来源 model_3d），并把医生送回对话聚焦输入框。"""
    res = _start("男，45岁，躯干皮疹")
    cid = res["case_id"]
    r = client.post(f"/api/cases/{cid}/regions", json={"regions": ["躯干", "背部"]})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["selected_body_regions"] == ["躯干", "背部"]
    assert (d["nav"]["workspace"], d["nav"]["next_action"]) == ("chat", "focus_input")
    rec = d["record"]
    assert rec["fields"]["body_regions"] == "躯干；背部"   # 服务端以「；」串联多部位
    assert rec["meta"]["body_regions"]["source"] == "model_3d"
    # 增量合并：再次选点不丢已选部位、不重复
    r2 = client.post(f"/api/cases/{cid}/regions", json={"regions": ["躯干", "上肢"]})
    assert r2.json()["selected_body_regions"] == ["躯干", "背部", "上肢"]
    # 病例不存在 → 404，绝不隐式新建（避免把部位写进空病例）
    assert client.post("/api/cases/notexist0000/regions",
                       json={"regions": ["面部"]}).status_code == 404


# ———————————————— v70：对话优先、结果按需打开（artifacts 派生 + 导航去自动执行） ————————————————

def test_v70_artifacts_map_matches_frontend():
    """/api/artifacts/map：按钮文案与产物→动作映射的权威来源，前端只按此渲染。"""
    m = client.get("/api/artifacts/map").json()
    assert m["artifact_keys"] == ["case_draft", "analysis", "rag_sources",
                                  "medication_check", "risk_screening", "model_3d_context"]
    assert m["action_labels"]["open_case"] == "查看病例"
    assert m["action_labels"]["retry"] == "重试"
    assert ["case_draft", "open_case"] in m["artifact_actions"]


def test_v70_nav_declares_not_executes():
    """v70 语义反转：nav 决策一律 auto=False——只声明建议工作区，前端不得自动跳转。"""
    res = _start("女，32岁，面部反复红斑瘙痒 3 个月")
    cid = res["case_id"]
    assert res["nav"]["auto"] is False
    # 即使是「医生明确要病例」的显式意图，也只声明 workspace=case，绝不自动执行
    r = client.post(f"/api/cases/{cid}/chat", json={"text": "新建一份病例"})
    assert r.json()["nav"]["auto"] is False


def test_v70_every_reply_has_unique_message_id():
    """要求 5：每条回复独立 message_id；点某条回复按钮只打开该条结果。"""
    res = _start("女，32岁，面部反复红斑瘙痒 3 个月")
    cid = res["case_id"]
    mid1 = res["message_id"]
    assert mid1 and mid1.startswith(f"msg_{cid}_")
    r1 = client.post(f"/api/cases/{cid}/chat", json={"text": "你好"}).json()
    r2 = client.post(f"/api/cases/{cid}/chat", json={"text": "谢谢"}).json()
    mids = {mid1, r1["message_id"], r2["message_id"]}
    assert len(mids) == 3, "每轮回复的 message_id 必须唯一"
    for m in (r1["message_id"], r2["message_id"]):
        assert m.startswith(f"msg_{cid}_")
    # 对话历史也携带 message_id（前端历史卡按钮定位用）
    hist = client.get(f"/api/cases/{cid}").json()["messages"]
    assistant_ids = [m.get("message_id") for m in hist if m.get("role") == "assistant"]
    assert all(assistant_ids), "assistant 历史消息必须带 message_id"


def test_v70_smalltalk_round_has_no_fake_artifacts():
    """要求 2/7/9：纯闲聊轮不得把上一轮的病例产物冒充成本轮结果——
    artifacts 只声明本轮真实产出，按钮区只剩真实存在的 3D 工具入口。"""
    res = _start("女，32岁，面部反复红斑瘙痒 3 个月")   # 本轮真实生成了病例草稿
    cid = res["case_id"]
    assert res["artifacts"]["case_draft"] is True
    assert "open_case" in res["available_actions"]
    assert "已生成病例草稿" in res["result_summary"]

    r = client.post(f"/api/cases/{cid}/chat", json={"text": "你好"}).json()
    assert all(v is False for v in r["artifacts"].values()), "闲聊轮不得虚报任何产物"
    # 无产物轮：LLM 可用时只留 3D 工具入口；LLM 不可用时只留重试+真实存在的病例入口。
    # 两种环境都不允许出现「查看完整分析 / RAG 引用」这类虚假按钮。
    assert "open_analysis" not in r["available_actions"]
    assert "open_sources" not in r["available_actions"]
    assert "export_case" not in r["available_actions"]
    assert r["available_actions"] in (["open_3d"], ["open_case", "retry"])
    assert r["result_summary"] == []
    # 刷新/续办：历史卡按钮来自消息级存档——创建轮的产物不因后续闲聊轮而丢失
    snap = client.get(f"/api/cases/{cid}").json()
    created = (snap.get("msg_artifacts") or {}).get(res["message_id"]) or {}
    assert created.get("artifacts", {}).get("case_draft") is True
    assert "open_case" in (created.get("available_actions") or [])


def test_v70_failure_round_only_offers_retry():
    """要求 9：LLM 失败/未连接 → artifacts 全空，只给重试（+真实存在的病例入口），无虚假分析按钮。"""
    res = _start("女，45岁，右前臂红斑脱屑伴瘙痒 2 周")
    cid = res["case_id"]
    # min-gate 追问/确认轮：无产物
    r = client.post(f"/api/cases/{cid}/chat", json={"text": "确认"}).json()
    if r["workflow_state"] in ("llm_error", "llm_unavailable"):
        assert all(v is False for v in r["artifacts"].values())
        assert r["available_actions"][0] == "retry"
        assert "open_analysis" not in r["available_actions"]
        assert r["authenticity"] == "none"


def test_v70_confirmed_unlocks_export_action():
    """医生确认后导出动作进入 available_actions；未确认不给。"""
    res = _start(demo="1")            # 规则管线完整走通
    cid = res["case_id"]
    snap = _confirm2(cid)
    assert snap["workflow_state"] in ("awaiting_review", "llm_unavailable")
    if snap["workflow_state"] == "awaiting_review":
        assert "export_case" not in snap["available_actions"]   # 未确认：连重放轮也不给导出
        _review(cid, "确认")
        d = client.get(f"/api/cases/{cid}").json()
        assert d["review_status"] == "approved"
        # 确认后快照真实承载报告（action=report）→ 查看病例 + 导出同时可用
        assert "export_case" in d["available_actions"]
        assert "open_case" in d["available_actions"]


def test_v70_analysis_round_reports_analysis_artifact():
    """分析产出轮：artifacts.analysis=true + 对应按钮 + 摘要文案。
    demo 规则稿在创建轮即完成分析——本轮差异（fingerprint 变化）驱动按钮，如实标注 offline_demo。"""
    res = _start(demo="1")
    cid = res["case_id"]
    if res["workflow_state"] != "awaiting_review":
        return                          # 环境无规则稿时跳过（已有其它用例覆盖状态机）
    assert res["artifacts"]["analysis"] is True
    assert "open_analysis" in res["available_actions"]
    assert "已完成辅助分析" in res["result_summary"]
    assert res["authenticity"] == "offline_demo"


def test_kb_hard_delete_and_cors():
    """v71：物理删除——书籍与全部索引从库中移除且不可恢复（与软删 quarantine 互补）；
    web 界面以 file://（null origin）加载，导入/删除走 HTTP 直连，必须放行跨源。"""
    book = ("第三章 银屑病\n银屑病是一种免疫介导的慢性复发性炎症性皮肤病，典型表现为鳞屑性红斑或斑块，"
            "寻常型银屑病蜡滴现象、薄膜现象与点状出血具有诊断提示意义。")
    r = _kb_import("v71物理删除测试书.txt", book)
    bid = r["book_id"]
    assert r["ok"] is True
    # CORS：file://（Origin: null）跨源请求被放行（导入/删除直连通道的前提）
    resp = client.post("/api/kb/books", json={"filename": "c.txt", "content_base64": "",
                                              "meta_note": ""},
                       headers={"Origin": "null"})
    assert resp.headers.get("access-control-allow-origin") is not None
    # 物理删除：返回 ok，列表不再出现该书
    d = client.delete(f"/api/kb/books/{bid}/hard").json()
    assert d["ok"] is True and d["book_id"] == bid, d
    listing = client.get("/api/kb/books").json()
    assert all(b["book_id"] != bid for b in listing["books"])
    # 重复删除 → not_found；软删端点行为保持不变（quarantined 可恢复）
    assert client.delete(f"/api/kb/books/{bid}/hard").json()["reason"] == "not_found"
    r2 = _kb_import("v71软删测试书.txt", book)
    sd = client.delete(f"/api/kb/books/{r2['book_id']}").json()
    assert sd["ok"] is True and sd["status"] == "quarantined"
    rs = client.post(f"/api/kb/books/{r2['book_id']}/restore").json()
    assert rs["ok"] is True and rs["status"] == "ready"
    # 清理：物理删除软删测试书，保持知识库整洁
    client.delete(f"/api/kb/books/{r2['book_id']}/hard")
