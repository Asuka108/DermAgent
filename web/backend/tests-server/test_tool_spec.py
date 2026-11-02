# -*- coding: utf-8 -*-
"""v77 工具单一真源：19 条规范项、5 个可调用、2 个 MCP 只读、15 条旧视图。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import tool_spec as ts  # noqa: E402

OLD_TASK_MAP = {
    "case_write": ("get_case_status", "update_record_fields", "complete_record_intake",
                   "finish_supplement_and_analyze", "search_knowledge_base"),
    "complete_analysis": ("get_case_status", "update_record_fields", "complete_record_intake",
                          "finish_supplement_and_analyze", "search_knowledge_base"),
    "case_organizer": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "history": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "lesion_desc": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "summary": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "referral": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "followup": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "differential": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "treatment_ref": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "medication_review": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "auxiliary": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "patient_edu": ("get_case_status", "update_record_fields", "search_knowledge_base"),
    "medical_kb_search": ("get_case_status", "search_knowledge_base"),
    "red_flag_screen": ("get_case_status",),
    "privacy_deidentify": ("get_case_status",),
    "body_region_3d": ("get_case_status",),
    "report_export": ("get_case_status",),
}


def test_registry_counts_and_self_check():
    ts.assert_valid_registry()
    assert len(ts.all()) == 19
    assert len(ts.llm_tools()) == 5
    assert len(ts.mcp_tools()) == 2
    assert sum(1 for s in ts.all() if s.available) == 5


def test_legacy_view_is_exactly_v68_shape():
    old = ts.legacy_specs()
    assert len(old) == 15 and len({t["name"] for t in old}) == 15
    for t in old:
        for k in ("name", "label", "description", "input_schema", "output_schema",
                  "requires_confirmation", "source_required", "risk_level", "route"):
            assert k in t, f"{t['name']} 缺 {k}"
        assert t["risk_level"] in ("low", "medium", "high")
    by_name = {t["name"]: t for t in old}
    assert by_name["differential_assistant"]["risk_level"] == "high"
    assert by_name["medication_safety_checker"]["risk_level"] == "high"
    for n in ("differential_assistant", "medication_safety_checker"):
        assert by_name[n]["source_required"] and by_name[n]["requires_confirmation"]
    assert by_name["medical_kb_search"]["label"] == "RAG 医学检索"
    assert by_name["case_organizer"]["label"] == "病例整理"


def test_task_map_is_identical_to_v76_table():
    for task, expected in OLD_TASK_MAP.items():
        assert tuple(ts.for_task(task)) == expected, task
    assert tuple(ts.for_task("unknown_task")) == ("get_case_status", "search_knowledge_base")


def test_aliases_and_routes_resolve_to_canonical():
    assert ts.get("medical_kb_search").name == "search_knowledge_base"
    assert ts.get("case_writer").name == "case_organizer"
    assert ts.get("doc_task:differential").name == "differential_assistant"
    assert ts.get("differential").name == "differential_assistant"
    assert ts.get("case_edit").name == "case_organizer"
    assert ts.get("no_such_tool") is None
    assert ts.risk_of("medical_kb_search") == "read"


def test_llm_tools_are_exactly_the_five_agent_tools():
    names = [t["function"]["name"] for t in ts.llm_tools()]
    assert names == ["get_case_status", "update_record_fields", "complete_record_intake",
                     "finish_supplement_and_analyze", "search_knowledge_base"]
    for t in ts.llm_tools():
        assert t["type"] == "function"
        assert t["function"]["parameters"]["type"] == "object"


def test_mcp_only_exposes_readonly_tools_with_case_id():
    tools = ts.mcp_tools()
    assert {t["name"] for t in tools} == {"get_case_status", "search_knowledge_base"}
    for t in tools:
        assert t["inputSchema"]["required"][0] == "case_id"
        assert t["annotations"]["readOnlyHint"] is True
    # 写工具与需确认工具永不进入 MCP
    exposed = {t["name"] for t in tools}
    assert "update_record_fields" not in exposed
    assert "finish_supplement_and_analyze" not in exposed


def test_validate_nested_object_enum_and_array():
    assert ts.validate("update_record_fields", {}) == "缺少必填参数：fields"
    assert "应为对象" in ts.validate("update_record_fields", {"fields": "x"})
    assert ts.validate("update_record_fields", {"fields": {"chief_complaint": "红斑"}}) == ""
    assert ts.validate("search_knowledge_base", {}) == "缺少必填参数：query"
    assert ts.validate("search_knowledge_base", {"query": "红斑", "top_k": "3"}) == ""
    assert "应为整数" in ts.validate("search_knowledge_base", {"query": "红斑", "top_k": "x"})
    # 声明型工具的 enum / 嵌套必填同样生效
    err = ts.validate("case_organizer", {"case": {}, "mode": "unknown"})
    assert "取值不在允许范围" in err


def test_catalog_has_no_duplicate_names_or_labels():
    cat = ts.catalog()
    assert len({c["name"] for c in cat}) == len(cat)
    labels = [c["title"] for c in cat]
    assert len(set(labels)) == len(labels)
