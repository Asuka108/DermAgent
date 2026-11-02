# -*- coding: utf-8 -*-
"""桌面离线记忆（memory_local）与 MockAgentClient 接入：端口语义、降级与去标识。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "shared"))
sys.path.insert(0, os.path.dirname(HERE))

from agent_client import MockAgentClient  # noqa: E402
from memory_local import LocalMemory  # noqa: E402


def test_patient_and_fact_priority(tmp_path):
    mem = LocalMemory(str(tmp_path))
    p = mem.create_patient({"display_name": "张三", "sex": "male", "birth_year": 1980})
    assert p["patient_id"].startswith("pt_") and p["display_name"] == "张三"
    assert mem.list_patients("张")[0]["patient_id"] == p["patient_id"]

    pid = p["patient_id"]
    assert mem.add_fact(pid, {"category": "allergy", "field": "allergen", "value": "青霉素"},
                        source="case_import")["inserted"] == 1
    # 同值去重（但更高来源要升级来源，不能被旧的一次性导入压住）
    dup = mem.add_fact(pid, {"category": "allergy", "field": "allergen", "value": " 青霉素 "},
                       source="doctor_modified")
    assert dup["inserted"] == 0 and dup.get("upgraded") is True
    assert mem.facts(pid)[0]["source"] == "doctor_modified"
    # 多值类别并存
    mem.add_fact(pid, {"category": "allergy", "field": "allergen", "value": "磺胺"}, source="doctor_modified")
    assert len([f for f in mem.facts(pid) if f["category"] == "allergy"]) == 2
    # 单值类别：低优先级不得覆盖医生修改
    mem.add_fact(pid, {"category": "past_history", "field": "disease", "value": "高血压"},
                 source="doctor_modified")
    low = mem.add_fact(pid, {"category": "past_history", "field": "disease", "value": "糖尿病"},
                       source="case_import")
    assert low["status"] == "disputed"
    active = [f for f in mem.facts(pid) if f["category"] == "past_history"]
    assert len(active) == 1 and active[0]["value"] == "高血压"
    # 软删除
    assert mem.delete_fact(pid, active[0]["fact_id"])["deleted"] == 1
    assert not [f for f in mem.facts(pid) if f["category"] == "past_history"]


def test_link_and_delete_patient_cascades(tmp_path):
    mem = LocalMemory(str(tmp_path))
    p = mem.create_patient({"display_name": "李四"})
    mem.link_case("c1", p["patient_id"])
    assert mem.patient_for_case("c1")["patient_id"] == p["patient_id"]
    mem.add_fact(p["patient_id"], {"category": "medication", "field": "name", "value": "氯雷他定"})
    mem.append("c1", "user", "医生口述")
    mem.set_summary("c1", {"headline": "概要"})
    out = mem.delete_patient(p["patient_id"])
    assert out["cases"] == ["c1"] and mem.patient_for_case("c1") is None
    assert mem.facts(p["patient_id"]) == []
    # 计划 §9：删患者连带清理其病例窗口与摘要
    assert mem.window("c1") == [] and mem.summary("c1") is None


def test_update_patient_accepts_birth_year(tmp_path):
    mem = LocalMemory(str(tmp_path))
    p = mem.create_patient({"display_name": "王五", "birth_year": 1980})
    updated = mem.update_patient(p["patient_id"], {"birth_year": 1990, "note": "复诊"})
    assert updated["birth_year"] == 1990 and updated["note"] == "复诊"
    cleared = mem.update_patient(p["patient_id"], {"birth_year": ""})
    assert cleared["birth_year"] is None


def test_window_trim_and_summary(tmp_path):
    mem = LocalMemory(str(tmp_path))
    for i in range(50):
        mem.append("c1", "user" if i % 2 == 0 else "assistant", f"m{i}")
    win = mem.window("c1", rounds=20)
    assert len(win) == 40 and win[-1]["content"] == "m49" and win[0]["content"] == "m10"
    mem.set_summary("c1", {"headline": "概要"})
    assert mem.summary("c1")["headline"] == "概要"


def test_episodic_recall_and_redaction(tmp_path):
    mem = LocalMemory(str(tmp_path))
    mem.episodic_write({"scope": "case", "case_id": "c1", "msg_id": "m1",
                        "title": "湿疹渗出处理", "text": "医生：患者姓名：王五 湿疹渗液怎么换药？"})
    mem.episodic_write({"scope": "case", "case_id": "c1", "msg_id": "m2",
                        "title": "银屑病鉴别", "text": "医生：银屑病与湿疹如何鉴别"})
    mem.episodic_write({"scope": "case", "case_id": "c2", "msg_id": "m3",
                        "title": "湿疹", "text": "医生：湿疹"})
    chunks = mem._chunks()
    assert all("王五" not in c["text"] for c in chunks) and "[患者]" in chunks[0]["text"]
    # 幂等
    assert mem.episodic_write({"chunk_id": chunks[0]["chunk_id"], "scope": "case",
                               "case_id": "c1", "text": "重复"}) is False
    hits = mem.recall("湿疹 渗出 换药", case_id="c1")
    assert hits and hits[0]["case_id"] == "c1"
    assert all(h["msg_id"] != "m2" for h in hits)      # 无关内容不过阈值
    excl = mem.recall("湿疹渗出换药", case_id="c1", exclude_msg_ids=["m1"])
    assert all(h["msg_id"] != "m1" for h in excl)
    assert mem.episodic_count(case_id="c2") == 1
    assert mem.episodic_delete(case_id="c1") == 2
    assert mem.episodic_count(case_id="c1") == 0


def test_mock_client_writes_local_memory(tmp_path):
    client = MockAgentClient(memory_dir=str(tmp_path))
    res = client.start("案例1")
    assert res and client.case_id and client.state["case_id"] == client.case_id
    patient = client.memory.patient_for_case(client.case_id)
    assert patient and patient["patient_id"].startswith("pt_")

    client.chat("请整理现病史")
    win = client.memory.window(client.case_id)
    assert [m["role"] for m in win][:2] == ["user", "assistant"]
    assert client.memory.episodic_count(case_id=client.case_id) >= 1

    rec = client.get_record()["record"]
    assert rec["patient"]["patient_id"] == patient["patient_id"]
    assert rec["memory"]["degraded"]["episodic"] is True   # 无 BGE → 词法降级如实标注
    assert rec["memory"]["degraded"]["worker"] is True

    # 手工事实：医生修改优先
    assert client.patient_add_fact(patient["patient_id"], {
        "category": "allergy", "field": "allergen", "value": "青霉素"})["inserted"] == 1
    facts = client.patient_facts(patient["patient_id"])["facts"]
    assert any(f["value"] == "青霉素" and f["source"] == "doctor_modified" for f in facts)
    # 离线合并明确拒绝
    assert client.patient_merge(patient["patient_id"], "pt_x")["ok"] is False
    assert client.memory_health()["degraded"]["episodic"] is True


def test_mock_without_dir_keeps_ram():
    a = MockAgentClient()
    b = MockAgentClient()
    a.start("案例1")
    assert a.case_id and not b.case_id
