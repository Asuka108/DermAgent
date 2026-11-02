# -*- coding: utf-8 -*-
"""v77 接口契约：错误封套、请求 ID、错误码目录、分页、OpenAPI 元数据、契约端点。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_MODEL"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from server import api_contract, tool_spec  # noqa: E402
from server.app import app  # noqa: E402

client = TestClient(app)


def _case(text="患者男，35岁，右前臂红斑伴瘙痒7天") -> str:
    r = client.post("/api/cases", json={"text": text})
    assert r.status_code == 200
    return r.json()["case_id"]


def test_health_exposes_contract_fields():
    body = client.get("/api/health").json()
    assert body["api_version"] == api_contract.API_VERSION
    assert body["tools_total"] == len(tool_spec.all())
    assert body["tools_available"] == len([s for s in tool_spec.all() if s.available])
    assert body["mcp"] in ("on", "off")


def test_tools_catalog_endpoint():
    body = client.get("/api/tools").json()
    assert body["ok"] is True
    assert len(body["tools"]) == len(tool_spec.all())
    assert set(body["llm_tools"]) == {s.name for s in tool_spec.all() if s.expose_llm}
    assert body["mcp_tools"] == [t["name"] for t in tool_spec.mcp_tools()]
    # 每个工具都带 JSON Schema 结构，不是伪 schema 字符串
    for tool in body["tools"]:
        assert tool["input_schema"].get("type") == "object"
        assert "available" in tool and "risk" in tool


def test_meta_contract_endpoint():
    body = client.get("/api/meta/contract").json()
    assert body["api_version"] == api_contract.API_VERSION
    assert "case_not_found" in body["error_codes"]
    assert body["error_codes"]["case_not_found"]["status"] == 404
    assert body["sse_events"] == list(api_contract.SSE_EVENTS)
    assert body["plan_states"] == list(api_contract.PLAN_STATES)
    assert body["pagination"]["total_header"] == "X-Total-Count"
    assert body["tools"]["total"] == len(tool_spec.all())
    assert "POST /mcp" in body["mcp"]["endpoints"]


def test_case_not_found_envelope_keeps_legacy_detail():
    r = client.get("/api/cases/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["detail"] == "病例不存在：does-not-exist"       # 旧客户端文本不变
    assert body["error"]["code"] == "case_not_found"
    assert body["error"]["retryable"] is False
    assert body["error"]["request_id"] == r.headers["X-Request-Id"]


def test_unknown_api_route_is_json_not_spa():
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "not_found"
    assert body["detail"] == "Not Found"


def test_spa_fallback_for_browser_routes():
    r = client.get("/cases/123/deep-link")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_request_id_echo_and_generation():
    echoed = client.get("/api/health", headers={"X-Request-Id": "req_test_123"})
    assert echoed.headers["X-Request-Id"] == "req_test_123"
    generated = client.get("/api/health")
    assert generated.headers["X-Request-Id"].startswith("req_")
    assert generated.headers["X-API-Version"] == api_contract.API_VERSION


def test_validation_error_fields():
    r = client.post("/api/cases", json={"text": 12345})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["fields"], "422 必须给出字段级错误"
    assert all({"loc", "msg", "type"} <= set(f) for f in body["error"]["fields"])


def test_record_export_forbidden_code():
    case_id = _case()
    r = client.get(f"/api/cases/{case_id}/record/export")
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == "record_export_forbidden"
    assert "医生确认" in body["detail"]


def test_mcp_disabled_envelope(monkeypatch):
    monkeypatch.delenv("DERM_MCP", raising=False)
    r = client.get("/api/mcp/manifest")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "mcp_disabled"


def test_pagination_headers_and_default():
    total = len(client.get("/api/cases").json())
    full = client.get("/api/cases")
    assert full.headers["X-Total-Count"] == str(total)
    assert len(full.json()) == total

    page = client.get("/api/cases", params={"limit": 1, "offset": 0})
    assert page.status_code == 200
    assert len(page.json()) <= 1
    assert page.headers["X-Total-Count"] == str(total)


def test_openapi_metadata_complete():
    schema = client.get("/openapi.json").json()
    assert schema["x-api-version"] == api_contract.API_VERSION
    assert "ErrorEnvelope" in schema["components"]["schemas"]
    assert "case_not_found" in schema["x-error-codes"]
    for path, methods in schema["paths"].items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            assert op.get("tags"), f"{method} {path} 缺少 tag"
            assert op.get("operationId"), f"{method} {path} 缺少 operationId"
            assert op.get("summary"), f"{method} {path} 缺少 summary"
