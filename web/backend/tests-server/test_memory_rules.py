# -*- coding: utf-8 -*-
"""记忆层地基单测：白名单/版本化规则、脱敏、Redis 窗口回退、患者 CRUD、装配降级。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import memory  # noqa: E402
from server.memory import inject, redact  # noqa: E402
from server.memory.profile import (  # noqa: E402
    InMemoryProfileStore, normalize_value, plan_write, validate_facts,
)
from server.memory.short_term import ShortTermMemory  # noqa: E402

from memory_fakes import FakeRedis  # noqa: E402


def _msgs(n, case_id):
    return {"case_id": case_id, "messages": [{"role": "user", "content": f"m{i}"} for i in range(n)]}


# —— 规则层 ——

def test_validate_facts_whitelist_confidence_and_length():
    facts = [
        {"category": "allergy", "field": "allergen", "value": "青霉素", "confidence": 0.9},
        {"category": "identity", "field": "name", "value": "张三", "confidence": 0.9},
        {"category": "allergy", "field": "unknown_field", "value": "x", "confidence": 0.9},
        {"category": "allergy", "field": "allergen", "value": "头孢", "confidence": 0.2},
        {"category": "medication", "field": "name", "value": "氯" * 300, "confidence": 0.9},
    ]
    out = validate_facts(facts, "llm_extract")
    assert [f["value"][:3] for f in out] == ["青霉素", "氯氯氯"]
    assert len(out[1]["value"]) == 200


def test_validate_facts_doctor_sources_skip_confidence_gate():
    out = validate_facts([{"category": "allergy", "field": "allergen",
                           "value": "磺胺", "confidence": 0}], "doctor_modified")
    assert out and out[0]["confidence"] == 1.0


def test_plan_write_multi_dedupes_and_upgrades():
    rows = [{"fact_id": 1, "category": "allergy", "field": "allergen",
             "normalized_value": normalize_value("青霉素"), "source": "llm_extract",
             "status": "active"}]
    fact = {"category": "allergy", "field": "allergen", "value": " 青霉素 "}
    assert plan_write(rows, fact, "llm_extract", multi=True) == []
    assert plan_write(rows, fact, "doctor_modified", multi=True) == [("upgrade", 1)]


def test_plan_write_single_supersede_and_disputed():
    rows = [{"fact_id": 2, "category": "personal_history", "field": "smoking",
             "normalized_value": normalize_value("每天一包"), "source": "doctor_modified",
             "status": "active"}]
    lower = {"category": "personal_history", "field": "smoking", "value": "已戒烟"}
    assert plan_write(rows, lower, "llm_extract", multi=False) == [("insert_disputed", None)]
    higher = {"category": "personal_history", "field": "smoking", "value": "已戒烟"}
    assert plan_write(rows, higher, "doctor_modified", multi=False) == [("insert", None)]
    same = {"category": "personal_history", "field": "smoking", "value": "每天一包"}
    assert plan_write(rows, same, "doctor_input", multi=False) == []


# —— 脱敏 ——

def test_redact_identity_placeholders_and_names():
    text = "患者姓名：张三，电话 13812345678，身份证 110101199001011234，门诊号：MZ-8899"
    out = redact.redact_identity(text, ["张三"])
    for leak in ("张三", "13812345678", "110101199001011234", "MZ-8899"):
        assert leak not in out
    assert "[患者]" in out and "[手机号]" in out and "[身份证]" in out
    assert redact.has_identity(text, ["张三"]) is True
    assert redact.has_identity(out, ["张三"]) is False


# —— 短期层 ——

def test_short_term_window_trim_ttl_and_seq():
    fake = FakeRedis()
    st = ShortTermMemory(reader=lambda cid: _msgs(30, cid)["messages"],
                         client_factory=lambda: fake)
    for i in range(50):
        assert st.append("c1", {"seq": i + 1, "role": "user", "content": f"m{i}"})
    raw = fake.lists["derm:mem:v1:case:c1:msgs"]
    assert len(raw) == 40
    assert fake.ttls["derm:mem:v1:case:c1:msgs"] == 604800
    assert fake.ttls["derm:mem:v1:case:c1:seq"] == 604800   # 三个 key 都带 TTL，避免 seq 永不过期
    assert int(fake.values["derm:mem:v1:case:c1:seq"]) == 50
    window, degraded = st.window("c1")
    assert not degraded and len(window) == 40
    assert window[-1]["content"] == "m49" and window[-1]["seq"] == 50


def test_short_term_falls_back_to_sessions_when_redis_down():
    def boom():
        raise ConnectionError("redis down")

    st = ShortTermMemory(reader=lambda cid: _msgs(30, cid)["messages"], client_factory=boom)
    window, degraded = st.window("c9")
    assert degraded is True
    assert len(window) == 30
    assert st.append("c9", {"seq": 1, "role": "user", "content": "x"}) is False
    assert st.health()["ok"] is False


def test_short_term_empty_but_healthy_is_not_degraded():
    """新病例 Redis key 尚未建立：回退 sessions 是正常路径，不能误报短期记忆不可用。"""
    fake = FakeRedis()
    st = ShortTermMemory(reader=lambda cid: _msgs(2, cid)["messages"],
                         client_factory=lambda: fake)
    window, degraded = st.window("brand-new")
    assert degraded is False
    assert [m["content"] for m in window] == ["m0", "m1"]
    assert st.health()["ok"] is True


def test_short_term_summary_and_purge():
    fake = FakeRedis()
    st = ShortTermMemory(client_factory=lambda: fake)
    assert st.set_summary("c2", {"current_task": "case_write"}) is True
    assert st.summary("c2")["current_task"] == "case_write"
    st.append("c2", {"seq": 1, "role": "user", "content": "x"})
    assert st.purge("c2") >= 2
    assert st.summary("c2") is None


# —— 患者画像 ——

def test_profile_store_crud_versioning_and_delete():
    store = InMemoryProfileStore()
    p = store.create_patient(display_name="张三", sex="male", birth_year=1980)
    assert p["patient_id"].startswith("pt") and p["sex"] == "male"
    assert store.get_patient(p["patient_id"])["display_name"] == "张三"
    assert [x["patient_id"] for x in store.list_patients("张")] == [p["patient_id"]]

    pid = p["patient_id"]
    out = store.write_facts(pid, [{"category": "allergy", "field": "allergen", "value": "青霉素",
                                   "confidence": 0.9},
                                  {"category": "allergy", "field": "allergen", "value": "青霉素",
                                   "confidence": 0.9}],
                            "llm_extract")
    assert out == {"inserted": 1, "upgraded": 0, "disputed": 0, "skipped": 1}

    store.write_facts(pid, [{"category": "allergy", "field": "allergen", "value": "青霉素",
                             "confidence": 0.9}], "doctor_modified")
    active = store.active_facts(pid)
    assert len(active) == 1 and active[0]["source"] == "doctor_modified"

    store.write_facts(pid, [{"category": "personal_history", "field": "smoking",
                             "value": "每天一包"}], "doctor_modified")
    store.write_facts(pid, [{"category": "personal_history", "field": "smoking",
                             "value": "已戒烟", "confidence": 0.9}], "llm_extract")
    assert len([f for f in store.active_facts(pid)
                if f["category"] == "personal_history"]) == 1   # 低优先级冲突只记 disputed
    assert len([f for f in store.all_facts(pid) if f["status"] == "disputed"]) == 1

    store.link_case("case1", pid)
    assert store.patient_for_case("case1") == pid
    assert store.cases_for_patient(pid) == ["case1"]
    store.purge_case("case1")
    assert store.patient_for_case("case1") is None

    store.link_case("case2", pid)
    assert store.delete_patient(pid) >= 1
    assert store.get_patient(pid) is None
    assert store.active_facts(pid) == []


def test_profile_store_merge_and_jobs():
    store = InMemoryProfileStore()
    a = store.create_patient(display_name="甲")["patient_id"]
    b = store.create_patient(display_name="乙")["patient_id"]
    store.write_facts(a, [{"category": "past_history", "field": "disease", "value": "银屑病"}],
                      "case_import")
    store.link_case("c1", a)
    assert store.merge_patients(a, b) == 1
    assert store.patient_for_case("c1") == b
    assert store.patients[a]["status"] == "merged"
    assert store.active_facts(b)[0]["value"] == "银屑病"

    j1 = store.enqueue_job("c1", 0, 4)
    assert j1 and store.enqueue_job("c1", 0, 4) is None      # UNIQUE(case_id, upto_seq)
    assert store.claim_job(j1) is True
    store.set_cursor("c1", 4)
    assert store.get_cursor("c1") == 4
    store.finish_job(j1, True)
    assert store.last_job("c1")["status"] == "done"
    j2 = store.enqueue_job("c1", 5, 8)
    store.purge_case("c1")
    assert store.last_job("c1")["job_id"] == j1              # 未完成任务被清理，已完成的留痕
    assert j2 not in [j["job_id"] for j in store.jobs]


# —— 渲染与装配 ——

def test_render_profile_budget_and_source_labels():
    facts = [{"fact_id": 1, "category": "allergy", "field": "allergen", "value": "青霉素",
              "source": "doctor_modified"},
             {"fact_id": 2, "category": "medication", "field": "name", "value": "氯雷他定",
              "source": "llm_extract"}]
    text = inject.render_profile(facts, 800)
    assert "过敏史·过敏原：青霉素（医生修改）" in text
    assert "用药·名称：氯雷他定（自动抽取，需医生核对）" in text
    clipped = inject.render_profile(facts, 60)
    assert "已截断" in clipped
    assert len(clipped) <= 60                     # 标题与截断标记都算在预算内


def test_render_episodic_budget_includes_header_and_marker():
    hits = [{"scope": "case", "title": "湿疹", "text": "甲" * 400, "date": "2026-10-01"},
            {"scope": "doctor", "title": "渗出", "text": "乙" * 400, "date": "2026-10-02"}]
    text = inject.render_episodic(hits, 300)
    assert "已截断" in text
    assert len(text) <= 300
    assert text.startswith("【相关历史话题")


def test_assemble_degrades_without_patient_and_uses_store(monkeypatch):
    monkeypatch.setenv("DERM_MEMORY_DISABLED", "0")
    memory.reset()
    store = InMemoryProfileStore()
    memory.set_profile_store(store)
    fake = FakeRedis()
    st = ShortTermMemory(reader=lambda cid: _msgs(2, cid), client_factory=lambda: fake)
    monkeypatch.setattr(memory, "short_term", lambda: st)

    sess = {"case_id": "caseA", "context_summary": {"current_task": "case_write"},
            "messages": [{"role": "user", "content": "hi"}]}
    out = memory.assemble(sess, query="湿疹", task_type="case_write")
    assert out["summary"]["current_task"] == "case_write"
    assert out["meta"]["degraded"]["profile"] is True
    assert out["meta"]["degraded"]["episodic"] is True

    pid = store.create_patient(display_name="张三")["patient_id"]
    store.link_case("caseA", pid)
    store.write_facts(pid, [{"category": "allergy", "field": "allergen", "value": "青霉素"}],
                      "doctor_modified")
    out = memory.assemble(sess, query="湿疹", task_type="case_write")
    assert out["patient_id"] == pid
    assert "青霉素" in out["profile_text"]
    assert out["meta"]["profile_facts"] == 1
    assert out["meta"]["degraded"]["profile"] is False

    memory.set_episodic(recall=lambda pid, cid, q, t, exclude=None: [
        {"scope": "case", "text": "上次讨论过渗出处理", "date": "2026-10-01", "title": "湿疹"}])
    out = memory.assemble(sess)
    assert "上次讨论过渗出处理" in out["episodic_text"]
    assert out["meta"]["degraded"]["episodic"] is False
    memory.reset()


def test_assemble_redacts_identity_inside_fact_values(monkeypatch):
    """事实值可能夹带姓名/电话（医生手录、病例导入）：进入提示词前必须脱敏。"""
    monkeypatch.setenv("DERM_MEMORY_DISABLED", "0")
    memory.reset()
    store = InMemoryProfileStore()
    memory.set_profile_store(store)
    fake = FakeRedis()
    monkeypatch.setattr(memory, "short_term",
                        lambda: ShortTermMemory(client_factory=lambda: fake))
    pid = store.create_patient(display_name="张三", phone="13800138000")["patient_id"]
    store.link_case("caseR", pid)
    store.write_facts(pid, [{"category": "past_history", "field": "disease",
                             "value": "张三患高血压，电话13800138000"}], "doctor_modified")
    out = memory.assemble({"case_id": "caseR"})
    assert out["patient_id"] == pid
    assert "高血压" in out["profile_text"]
    assert "张三" not in out["profile_text"]
    assert "13800138000" not in out["profile_text"]
    memory.reset()


def test_append_message_disabled_and_enabled(monkeypatch):
    monkeypatch.setenv("DERM_MEMORY_DISABLED", "1")
    assert memory.append_message("c", {"role": "user", "content": "x"}) is False
    assert memory.assemble({"case_id": "c"})["meta"]["disabled"] is True

    monkeypatch.setenv("DERM_MEMORY_DISABLED", "0")
    fake = FakeRedis()
    st = ShortTermMemory(client_factory=lambda: fake)
    monkeypatch.setattr(memory, "short_term", lambda: st)
    assert memory.append_message("c", {"seq": 1, "role": "user", "content": "x"}) is True
    assert json.loads(fake.lists["derm:mem:v1:case:c:msgs"][0])["content"] == "x"


def test_log_msg_assigns_monotonic_seq_and_mirrors(monkeypatch):
    monkeypatch.setenv("DERM_MEMORY_DISABLED", "0")
    fake = FakeRedis()
    st = ShortTermMemory(client_factory=lambda: fake)
    monkeypatch.setattr(memory, "short_term", lambda: st)
    from server.app import _log_msg

    sess = {"case_id": "caseZ", "messages": [{"role": "user", "content": "旧消息"}]}
    first = _log_msg(sess, "user", "医生口述")
    second = _log_msg(sess, "assistant", "助手回复", message_id="m1")
    assert (first["seq"], second["seq"]) == (1, 2)
    assert sess["msg_seq"] == 2 and second["message_id"] == "m1"
    raw = fake.lists["derm:mem:v1:case:caseZ:msgs"]
    assert [json.loads(x)["seq"] for x in raw] == [1, 2]
    assert int(fake.values["derm:mem:v1:case:caseZ:seq"]) == 2
