# -*- coding: utf-8 -*-
"""异步抽取与情景切片：证据校验、脱敏、worker 队列、幂等与降级。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from server import memory  # noqa: E402
from server.memory import extract  # noqa: E402
from server.memory.episodic import InMemoryEpisodic  # noqa: E402
from server.memory.profile import InMemoryProfileStore  # noqa: E402
from server.memory.worker import MemoryWorker  # noqa: E402

PATIENT = {"patient_id": "p1", "display_name": "张三", "sex": "male", "birth_year": 1985}


def _sess(case_id="c1", task_type="case_write"):
    return {
        "case_id": case_id, "task_type": task_type,
        "case": {"chief_complaint": "面部红斑瘙痒", "symptoms": "渗出 3 天"},
        "messages": [
            {"seq": 1, "role": "user",
             "content": "患者张三青霉素过敏，去年起用氯雷他定10mg每天"},
            {"seq": 2, "role": "assistant", "content": "已记录过敏史与用药",
             "message_id": "m2"},
        ],
    }


def _llm_json(**overrides):
    data = {
        "facts": [{"category": "allergy", "field": "allergen", "value": "青霉素",
                   "evidence": "青霉素过敏", "confidence": 0.95},
                  {"category": "medication", "field": "name", "value": "氯雷他定",
                   "evidence": "去年起用氯雷他定10mg每天", "confidence": 0.9}],
        "topic": {"title": "湿疹渗出处理", "summary": "先收敛渗出再抗炎", "generalizable": True},
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _worker(store, epi=None, llm=("http://llm", "k", "m", ""), sess=None, events=None, on_done=None):
    events = events if events is not None else []
    return MemoryWorker(
        profile_getter=lambda: store,
        episodic_getter=lambda: epi,
        llm_getter=lambda: llm,
        reader=lambda case_id: sess if sess is not None else _sess(case_id),
        audit=lambda event, detail="": events.append((event, detail)),
        on_done=on_done), events


def test_parse_extraction_requires_evidence_and_drops_identity_parts():
    raw = json.dumps({
        "facts": [
            {"category": "allergy", "field": "allergen", "value": "青霉素",
             "evidence": "青霉素过敏", "confidence": 0.9},
            {"category": "allergy", "field": "allergen", "value": "头孢",
             "evidence": "助手说过头孢过敏", "confidence": 0.9},          # 原文没有 → 丢弃
            {"category": "allergy", "field": "allergen", "value": "磺胺",
             "evidence": "患者张三青霉素过敏", "confidence": 0.9},        # 含姓名 → 丢弃
            {"category": "identity", "field": "name", "value": "张三",
             "evidence": "患者张三", "confidence": 0.9},                  # 越权类别 → 丢弃
            {"category": "medication", "field": "name", "value": "氯雷他定",
             "evidence": "去年起用氯雷他定", "confidence": 0.2},          # 低置信 → 丢弃
        ],
        "topic": {"title": "张三的湿疹", "summary": "张三渗出处理", "generalizable": True},
    }, ensure_ascii=False)
    out = extract.parse_extraction(raw, "患者张三青霉素过敏，去年起用氯雷他定10mg每天", PATIENT)
    assert [f["value"] for f in out["facts"]] == ["青霉素"]
    assert "张三" not in out["topic"]["title"] + out["topic"]["summary"]
    assert out["topic"]["generalizable"] is False


def test_parse_extraction_rejects_value_not_grounded_in_evidence():
    """证据能在原话里找到，但事实值不在证据里 = 编造，必须丢弃。"""
    raw = json.dumps({"facts": [
        {"category": "allergy", "field": "allergen", "value": "头孢",
         "evidence": "青霉素过敏", "confidence": 0.9},
        {"category": "medication", "field": "name", "value": "氯雷他定",
         "evidence": "去年起用氯雷他定10mg每天", "confidence": 0.9},
    ]}, ensure_ascii=False)
    out = extract.parse_extraction(raw, "患者青霉素过敏，去年起用氯雷他定10mg每天")
    assert [f["value"] for f in out["facts"]] == ["氯雷他定"]


def test_build_turn_chunks_pairing_redaction_and_cap():
    sess = _sess()
    chunks = extract.build_turn_chunks(sess["messages"], "c1", "p1", PATIENT)
    assert len(chunks) == 1
    text = chunks[0]["text"]
    assert text.startswith("医生：") and "助手：" in text
    assert "张三" not in text and "患者" in text
    assert chunks[0]["msg_id"] == "m2" and chunks[0]["scope"] == "case"
    # 助手缺回复的孤立医生轮不切片
    assert extract.build_turn_chunks([{"seq": 3, "role": "user", "content": "补充"}], "c1", "p1", PATIENT) == []


def test_build_topic_chunk_only_generalizable():
    assert extract.build_topic_chunk({"title": "x", "summary": "y", "generalizable": False},
                                     "c1", "p1", "m2", PATIENT) is None
    chunk = extract.build_topic_chunk({"title": "湿疹渗出", "summary": "先收敛再抗炎",
                                       "generalizable": True}, "c1", "p1", "m2", PATIENT)
    assert chunk and chunk["scope"] == "doctor"
    named = extract.build_topic_chunk({"title": "张三湿疹", "summary": "张三的处置",
                                       "generalizable": True}, "c1", "p1", "m2", PATIENT)
    assert named is None


def test_worker_writes_facts_cursor_and_episodic(monkeypatch):
    store = InMemoryProfileStore()
    patient = store.create_patient(display_name="张三")
    store.link_case("c1", patient["patient_id"])
    epi = InMemoryEpisodic()
    job = store.enqueue_job("c1", 0, 2)
    monkeypatch.setattr(extract, "call_llm", lambda llm, messages, timeout=20: _llm_json())
    done = []
    w, events = _worker(store, epi, sess=_sess(), on_done=lambda cid, seq: done.append((cid, seq)))

    assert w.process_job(job) is True
    facts = store.active_facts(patient["patient_id"])
    assert {f["value"] for f in facts} == {"青霉素", "氯雷他定"}
    assert all(f["source"] == "llm_extract" for f in facts)
    assert store.get_cursor("c1") == 2
    assert store.get_job(job)["status"] == "done"
    assert done == [("c1", 2)]
    assert epi.count(case_id="c1") == 1 and epi.count(patient_id=patient["patient_id"]) == 2
    assert any(e == "memory_profile_write" for e, _ in events)
    assert any(e == "memory_extract_done" for e, _ in events)


def test_worker_llm_unavailable_keeps_job_queued(monkeypatch):
    store = InMemoryProfileStore()
    patient = store.create_patient(display_name="张三")
    store.link_case("c1", patient["patient_id"])
    job = store.enqueue_job("c1", 0, 2)
    called = []
    monkeypatch.setattr(extract, "call_llm", lambda *a, **kw: called.append(1) or _llm_json())
    w, _ = _worker(store, InMemoryEpisodic(), llm=None, sess=_sess())

    assert w.process_job(job) is False
    assert store.get_job(job)["status"] == "queued"
    assert called == [] and store.get_cursor("c1") == 0


def test_worker_without_patient_finishes_without_facts(monkeypatch):
    store = InMemoryProfileStore()
    job = store.enqueue_job("c1", 0, 2)
    monkeypatch.setattr(extract, "call_llm", lambda *a, **kw: _llm_json())
    w, events = _worker(store, InMemoryEpisodic(), sess=_sess())
    assert w.process_job(job) is True
    assert store.get_job(job)["status"] == "done"
    assert store.get_cursor("c1") == 2 and store.facts == []
    assert any(e == "memory_extract_skipped" for e, _ in events)


def test_worker_episodic_failure_keeps_facts(monkeypatch):
    class BrokenEpisodic(InMemoryEpisodic):
        def write(self, chunks):
            raise RuntimeError("milvus down")

    store = InMemoryProfileStore()
    patient = store.create_patient(display_name="张三")
    store.link_case("c1", patient["patient_id"])
    job = store.enqueue_job("c1", 0, 2)
    monkeypatch.setattr(extract, "call_llm", lambda *a, **kw: _llm_json())
    w, events = _worker(store, BrokenEpisodic(), sess=_sess())
    assert w.process_job(job) is True
    assert len(store.active_facts(patient["patient_id"])) == 2
    assert store.get_job(job)["status"] == "done"
    assert any(e == "memory_episodic_skipped" for e, _ in events)


def test_worker_invalid_json_marks_failed(monkeypatch):
    store = InMemoryProfileStore()
    patient = store.create_patient(display_name="张三")
    store.link_case("c1", patient["patient_id"])
    job = store.enqueue_job("c1", 0, 2)
    monkeypatch.setattr(extract, "call_llm", lambda *a, **kw: "不是 JSON")
    w, events = _worker(store, InMemoryEpisodic(), sess=_sess())
    assert w.process_job(job) is True
    assert store.get_job(job)["status"] == "failed"
    assert store.get_cursor("c1") == 0
    assert any(e == "memory_extract_failed" for e, _ in events)


def test_worker_requires_doctor_text(monkeypatch):
    store = InMemoryProfileStore()
    patient = store.create_patient(display_name="张三")
    store.link_case("c1", patient["patient_id"])
    job = store.enqueue_job("c1", 2, 2)     # 区间内只有助手消息
    called = []
    monkeypatch.setattr(extract, "call_llm", lambda *a, **kw: called.append(1) or _llm_json())
    w, _ = _worker(store, InMemoryEpisodic(), sess=_sess())
    assert w.process_job(job) is True
    assert called == [] and store.get_cursor("c1") == 2
    assert store.get_job(job)["status"] == "done"


class _Notify:
    def __init__(self):
        self.jobs = []

    def __call__(self, job_id=None):
        if job_id:
            self.jobs.append(job_id)
        return True


@pytest.fixture
def mem(monkeypatch):
    monkeypatch.setenv("DERM_MEMORY_DISABLED", "0")
    memory.reset()
    store = InMemoryProfileStore()
    memory.set_profile_store(store)
    notify = _Notify()
    memory.configure(worker_notify=notify)
    yield store, notify
    memory.reset()


def test_after_turn_enqueues_once(mem):
    store, notify = mem
    patient = store.create_patient(display_name="张三")
    store.link_case("c1", patient["patient_id"])
    sess = _sess()
    job = memory.after_turn(sess)
    assert job and notify.jobs == [job]
    assert store.last_job("c1")["upto_seq"] == 2
    assert memory.after_turn(sess) is None          # 同一区间不重复登记


def test_after_turn_skips_without_patient_or_assistant(mem):
    store, notify = mem
    sess = _sess()
    assert memory.after_turn(sess) is None          # 未关联患者
    assert notify.jobs == []

    patient = store.create_patient(display_name="张三")
    store.link_case("c1", patient["patient_id"])
    sess["messages"] = sess["messages"][:1]         # 只有医生消息，未收口
    assert memory.after_turn(sess) is None and store.last_job("c1") is None


def test_after_turn_smalltalk_advances_cursor_without_job(mem):
    store, notify = mem
    patient = store.create_patient(display_name="张三")
    store.link_case("c1", patient["patient_id"])
    sess = _sess(task_type="smalltalk")
    assert memory.after_turn(sess) is None
    assert notify.jobs == [] and store.get_cursor("c1") == 2
