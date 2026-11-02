# -*- coding: utf-8 -*-
"""记忆层 API/流程：患者 CRUD、病例关联、快照透传、删除级联、降级不 500。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_MODEL"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import memory  # noqa: E402
from server.app import app  # noqa: E402
from server.memory.profile import InMemoryProfileStore  # noqa: E402
from server.memory.short_term import ShortTermMemory  # noqa: E402

from memory_fakes import FakeRedis  # noqa: E402

client = TestClient(app)


@pytest.fixture
def mem(monkeypatch):
    monkeypatch.setenv("DERM_MEMORY_DISABLED", "0")
    memory.reset()
    store = InMemoryProfileStore()
    memory.set_profile_store(store)
    fake = FakeRedis()
    st = ShortTermMemory(client_factory=lambda: fake)
    monkeypatch.setattr(memory, "short_term", lambda: st)
    yield store, fake
    memory.reset()


def _new_patient(**extra):
    body = {"display_name": "张三", "sex": "male", "birth_year": 1985, **extra}
    r = client.post("/api/patients", json=body)
    assert r.status_code == 200, r.text
    return r.json()["patient"]


def _start(**extra):
    r = client.post("/api/cases", json={"text": "开始诊断", **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_patient_crud_and_facts(mem):
    p = _new_patient(phone="13812345678")
    pid = p["patient_id"]
    assert client.get("/api/patients", params={"query": "张"}).json()["patients"][0]["patient_id"] == pid
    assert client.get(f"/api/patients/{pid}").json()["patient"]["display_name"] == "张三"

    r = client.post(f"/api/patients/{pid}/facts",
                    json={"category": "allergy", "field": "allergen", "value": "青霉素"})
    assert r.status_code == 200 and r.json()["inserted"] == 1
    facts = client.get(f"/api/patients/{pid}/facts", params={"active_only": True}).json()["facts"]
    assert facts[0]["value"] == "青霉素" and facts[0]["source"] == "doctor_modified"

    assert client.patch(f"/api/patients/{pid}", json={"note": "复诊"}).status_code == 200
    assert client.get(f"/api/patients/{pid}").json()["patient"]["note"] == "复诊"

    assert client.post(f"/api/patients/{pid}/facts",
                       json={"category": "identity", "field": "name", "value": "x"}).json()["inserted"] == 0
    assert client.post("/api/patients/missing/facts",
                       json={"category": "allergy", "field": "allergen", "value": "x"}).status_code == 404


def test_delete_fact_checks_patient_ownership(mem):
    a = _new_patient(display_name="甲")["patient_id"]
    b = _new_patient(display_name="乙")["patient_id"]
    client.post(f"/api/patients/{a}/facts",
                json={"category": "allergy", "field": "allergen", "value": "青霉素"})
    fid = client.get(f"/api/patients/{a}/facts").json()["facts"][0]["fact_id"]

    assert client.delete(f"/api/patients/{b}/facts/{fid}").status_code == 404
    assert client.get(f"/api/patients/{a}/facts", params={"active_only": True}).json()["facts"]
    assert client.delete(f"/api/patients/{a}/facts/{fid}").status_code == 200
    assert client.get(f"/api/patients/{a}/facts", params={"active_only": True}).json()["facts"] == []


def test_case_patient_link_snapshot_and_delete_cascade(mem):
    store, fake = mem
    p = _new_patient()
    pid = p["patient_id"]
    snap = _start(patient_id=pid)
    case_id = snap["case_id"]
    assert snap["patient"]["patient_id"] == pid
    assert snap["memory"]["degraded"]["profile"] is False
    assert store.patient_for_case(case_id) == pid
    assert fake.lists.get(f"derm:mem:v1:case:{case_id}:msgs")

    r = client.post(f"/api/cases/{case_id}/patient", json={"patient_id": ""})
    assert r.status_code == 200
    assert client.get(f"/api/cases/{case_id}").json()["patient"] is None
    client.post(f"/api/cases/{case_id}/patient", json={"patient_id": pid})

    client.delete(f"/api/cases/{case_id}")
    assert f"derm:mem:v1:case:{case_id}:msgs" not in fake.lists
    assert store.patient_for_case(case_id) is None
    # 患者事实不随病例删除（跨就诊复用）
    store.write_facts(pid, [{"category": "allergy", "field": "allergen", "value": "磺胺"}],
                      "case_import")
    assert len(store.active_facts(pid)) == 1


def test_delete_patient_purges_cases_and_facts(mem):
    store, fake = mem
    p = _new_patient()
    pid = p["patient_id"]
    snap = _start(patient_id=pid)
    case_id = snap["case_id"]
    store.write_facts(pid, [{"category": "allergy", "field": "allergen", "value": "青霉素"}],
                      "doctor_modified")
    r = client.delete(f"/api/patients/{pid}")
    assert r.status_code == 200 and case_id in r.json()["cases"]
    assert store.get_patient(pid) is None
    assert fake.lists.get(f"derm:mem:v1:case:{case_id}:msgs") is None
    assert client.get(f"/api/cases/{case_id}").json()["patient"] is None


def test_merge_patients_moves_facts_and_cases(mem):
    store, _ = mem
    a = _new_patient(display_name="甲")
    b = _new_patient(display_name="乙")
    store.write_facts(a["patient_id"], [{"category": "past_history", "field": "disease",
                                         "value": "银屑病"}], "case_import")
    snap = _start(patient_id=a["patient_id"])
    r = client.post(f"/api/patients/{a['patient_id']}/merge",
                    json={"target_id": b["patient_id"]})
    assert r.status_code == 200 and r.json()["facts_moved"] == 1
    assert store.patient_for_case(snap["case_id"]) == b["patient_id"]
    assert store.active_facts(b["patient_id"])[0]["value"] == "银屑病"


def test_no_patient_degrades_profile_but_chat_works(mem):
    snap = _start()
    assert snap["patient"] is None
    assert snap["memory"]["degraded"]["profile"] is True
    case_id = snap["case_id"]
    r = client.post(f"/api/cases/{case_id}/chat", json={"text": "患者右前臂红斑伴瘙痒3天"})
    assert r.status_code == 200


def test_patients_endpoint_503_when_store_unavailable(monkeypatch):
    monkeypatch.setenv("DERM_MEMORY_DISABLED", "0")
    memory.reset()
    monkeypatch.setattr(memory, "profile", lambda: None)
    assert client.get("/api/patients").status_code == 503
    assert client.post("/api/patients", json={"display_name": "x"}).status_code == 503


def test_memory_health_reports_layers_without_secrets(mem):
    res = client.get("/api/memory/health")
    assert res.status_code == 200
    data = res.json()
    assert data["short_term"]["ok"] is True
    assert data["profile"]["ok"] is True
    assert data["episodic"]["detail"] == "not_available"
    text = res.text.lower()
    assert "password" not in text and "api_key" not in text


def test_case_memory_debug_endpoint(mem):
    p = _new_patient()
    snap = _start(patient_id=p["patient_id"])
    data = client.get(f"/api/cases/{snap['case_id']}/memory").json()
    assert data["patient"]["patient_id"] == p["patient_id"]
    assert data["window_count"] >= 2
    assert data["short_term_degraded"] is False


def test_patient_blank_birth_year_stays_null(mem):
    """MySQL INT 列严格模式不接受 ''；未填/非法值统一落 NULL。"""
    p = _new_patient(birth_year=None)
    assert p["birth_year"] is None
    p3 = _new_patient(display_name="王五", birth_year="1980")
    assert p3["birth_year"] == 1980
