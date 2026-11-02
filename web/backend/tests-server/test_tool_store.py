# -*- coding: utf-8 -*-
"""工具管理配置：Fernet 真加密、掩码视图、原子写入与输入校验。"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import tool_store as ts  # noqa: E402


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("DERM_DATA_DIR", str(tmp_path))
    return tmp_path


def _server(**extra):
    payload = {"name": "demo", "transport": "stdio", "command": "python3",
               "args": ["-m", "demo_mcp"], "timeout_s": 20}
    payload.update(extra)
    return payload


def test_encrypt_roundtrip_and_key_mode(data):
    token = ts.encrypt_secrets({"API_TOKEN": "top-secret"}, {"Authorization": "Bearer abc"})
    assert token and "top-secret" not in token
    secrets = ts.decrypt_secrets(token)
    assert secrets["env"]["API_TOKEN"] == "top-secret"
    assert secrets["headers"]["Authorization"] == "Bearer abc"
    mode = stat.S_IMODE(os.stat(ts.key_file()).st_mode)
    assert mode == 0o600
    cfg_mode = stat.S_IMODE(os.stat(ts.config_file()).st_mode) if os.path.exists(ts.config_file()) else 0o600
    assert cfg_mode == 0o600


def test_masked_server_and_public_config_hide_values(data):
    server = ts.upsert_server(_server(env={"API_TOKEN": "top-secret"}))
    assert "secrets_enc" not in server
    assert server["has_secrets"] is True
    assert server["env_keys"] == ["API_TOKEN"]
    assert server["masked_env"]["API_TOKEN"] == "••••"
    blob = json.dumps(ts.public_config(), ensure_ascii=False)
    assert "top-secret" not in blob
    assert "secrets_enc" not in blob


def test_blank_secret_keeps_existing_and_clear_removes(data):
    server = ts.upsert_server(_server(env={"API_TOKEN": "top-secret"}))
    ts.upsert_server({"env": {"API_TOKEN": ""}}, server_id=server["id"])
    kept = ts.server_secrets(ts.load()["servers"][0])
    assert kept["env"]["API_TOKEN"] == "top-secret"

    ts.upsert_server({"clear_secrets": True}, server_id=server["id"])
    assert ts.load()["servers"][0]["secrets_enc"] == ""


def test_http_tool_secrets_masked(data):
    tool = ts.upsert_http_tool({
        "name": "pubmed", "description": "检索 PubMed",
        "url": "https://example.org/api", "method": "GET",
        "input_schema": {"type": "object", "properties": {}},
        "headers": {"X-Api-Key": "k-123"}})
    assert tool["has_secrets"] is True
    assert "k-123" not in json.dumps(tool, ensure_ascii=False)
    assert ts.http_secrets(ts.load()["http_tools"][0])["headers"]["X-Api-Key"] == "k-123"


def test_atomic_write_and_corrupt_json_error(data):
    ts.upsert_server(_server())
    assert os.path.getsize(ts.config_file()) > 0
    assert not [p for p in os.listdir(ts.tools_dir()) if ".tmp." in p]

    with open(ts.config_file(), "w", encoding="utf-8") as fh:
        fh.write("{not-json")
    cfg = ts.load()
    assert cfg["servers"] == [] and cfg["http_tools"] == []
    assert "配置" in ts.last_error() or ts.last_error()


def test_invalid_inputs_raise_value_error(data):
    with pytest.raises(ValueError):
        ts.upsert_server({"name": "x", "transport": "udp"})
    with pytest.raises(ValueError):
        ts.upsert_server({"name": "x", "transport": "stdio", "command": ""})
    with pytest.raises(ValueError):
        ts.upsert_server({"name": "x", "transport": "stdio", "command": "run",
                          "args": [1, 2]})
    with pytest.raises(ValueError):
        ts.upsert_server({"name": "x", "transport": "http", "url": "http://example.com/mcp"})
    with pytest.raises(ValueError):
        ts.upsert_http_tool({"name": "t", "description": "d", "url": "https://e.org",
                             "method": "DELETE",
                             "input_schema": {"type": "object"}})
    with pytest.raises(ValueError):
        ts.upsert_http_tool({"name": "t", "description": "d", "url": "https://e.org",
                             "method": "GET", "input_schema": ["not-object"]})
    with pytest.raises(ValueError):
        ts.upsert_http_tool({"name": "t", "description": "d",
                             "url": "https://169.254.169.254/latest/meta-data",
                             "method": "GET", "input_schema": {"type": "object"}})


def test_builtin_toggle_known_and_unknown(data):
    item = ts.set_builtin_enabled("case_comparator", False)
    assert item["enabled"] is False
    assert "case_comparator" in ts.load()["builtin_disabled"]
    with pytest.raises(ValueError):
        ts.set_builtin_enabled("not_a_tool", False)


def test_mcp_removed_tools_not_resurrected(data):
    server = ts.upsert_server(_server())
    ts.remove_mcp_tool(server["id"], "write_thing")
    assert "write_thing" in ts.load()["servers"][0]["removed_tools"]
    with pytest.raises(ValueError):
        ts.set_mcp_tool_state(server["id"], "write_thing", True)
    ts.restore_mcp_tool(server["id"], "write_thing")
    assert "write_thing" not in ts.load()["servers"][0]["removed_tools"]
