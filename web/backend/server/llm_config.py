# -*- coding: utf-8 -*-
"""本机 LLM 多连接配置：版本化、原子写入、脱敏读取。

API Key 只在本模块内部参与请求或落盘，任何 public 函数都不返回 Key 本体。
"""
import json
import os
import re
import stat
import uuid
from datetime import datetime, timezone

CONFIG_FILE = os.path.join(
    os.environ.get("DERM_DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
    "llm_config.json")
_ALLOWED_EFFORTS = {"", "low", "medium", "high"}
_PROVIDER_DEFAULTS = {
    "custom": {"name": "通用 OpenAI", "base_url": "", "models": []},
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                 "models": ["deepseek-chat", "deepseek-reasoner"]},
    "zhipu": {"name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4",
              "models": ["glm-4.5", "glm-4-air", "glm-4-flash"]},
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def normalize_base_url(value):
    value = str(value or "").strip().rstrip("/")
    value = re.sub(r"/chat/completions$", "", value, flags=re.I).rstrip("/")
    return value


def _valid_effort(value):
    value = str(value or "").strip().lower()
    return value if value in _ALLOWED_EFFORTS else ""


def _new_id():
    return uuid.uuid4().hex[:12]


def _provider(value):
    value = str(value or "custom").strip().lower()
    aliases = {"deep-seek": "deepseek", "glm": "zhipu", "zhipuai": "zhipu", "智谱": "zhipu"}
    return aliases.get(value, value) if value in _PROVIDER_DEFAULTS or value in aliases else "custom"


def _clean_connection(raw, existing=None):
    existing = existing if isinstance(existing, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    provider = _provider(raw.get("provider", existing.get("provider", "custom")))
    now = _now()
    base = normalize_base_url(raw.get("base_url", existing.get("base_url", "")))
    model = str(raw.get("model", existing.get("model", "")) or "").strip()
    name = str(raw.get("name", existing.get("name", "")) or "").strip()
    if not name:
        name = _PROVIDER_DEFAULTS.get(provider, _PROVIDER_DEFAULTS["custom"])["name"]
    return {
        "id": str(raw.get("id") or existing.get("id") or _new_id())[:64],
        "name": name[:120], "provider": provider, "base_url": base[:500], "model": model[:160],
        "api_key": str(raw.get("api_key", existing.get("api_key", "")) or "").strip(),
        "thinking_level": _valid_effort(raw.get("thinking_level", existing.get("thinking_level", ""))),
        "created_at": str(existing.get("created_at") or raw.get("created_at") or now),
        "updated_at": now,
        "last_test": existing.get("last_test") if isinstance(existing.get("last_test"), dict) else None,
    }


def _default_config():
    return {"schema_version": 2, "active_id": "", "connections": []}


def _read_raw():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load_config(migrate=True):
    raw = _read_raw()
    if raw.get("schema_version") == 2 and isinstance(raw.get("connections"), list):
        conns = [_clean_connection(c, c) for c in raw["connections"] if isinstance(c, dict)]
        active = str(raw.get("active_id") or "")
        if active not in {c["id"] for c in conns}:
            active = conns[0]["id"] if conns else ""
        data = {"schema_version": 2, "active_id": active, "connections": conns}
        if migrate and data != raw:
            _write(data)
        return data
    # v1 flat object -> one stable migrated profile. Do not fabricate a profile for an empty file.
    if any(str(raw.get(k) or "").strip() for k in ("base_url", "api_key", "model")):
        c = _clean_connection({"id": "default", "name": "默认连接", "provider": "custom",
                               "base_url": raw.get("base_url"), "api_key": raw.get("api_key"),
                               "model": raw.get("model"), "thinking_level": raw.get("thinking_level")})
        c["id"] = "default"
        data = {"schema_version": 2, "active_id": c["id"], "connections": [c]}
        if migrate:
            _write(data)
        return data
    return _default_config()


def _public(c):
    return {k: c.get(k, "") for k in
            ("id", "name", "provider", "base_url", "model", "thinking_level", "created_at", "updated_at")} | {
                "has_key": bool(c.get("api_key")),
                "key_tail": c.get("api_key", "")[-4:] if c.get("api_key") else "",
                "last_test": c.get("last_test") if isinstance(c.get("last_test"), dict) else None,
            }


def list_public():
    data = load_config()
    return {"ok": True, "schema_version": 2, "active_id": data["active_id"],
            "connections": [_public(c) for c in data["connections"]],
            "providers": {k: {"name": v["name"], "base_url": v["base_url"], "models": v["models"]}
                          for k, v in _PROVIDER_DEFAULTS.items()}}


def get_connection(connection_id):
    data = load_config()
    return next((c for c in data["connections"] if c["id"] == str(connection_id)), None)


def active_connection():
    data = load_config()
    return next((c for c in data["connections"] if c["id"] == data["active_id"]), None)


def save_connection(payload, connection_id=None, clear_key=False):
    data = load_config()
    payload = payload if isinstance(payload, dict) else {}
    old = next((c for c in data["connections"] if c["id"] == str(connection_id or payload.get("id"))), None)
    if old:
        c = _clean_connection(payload, old)
        if "api_key" not in payload or not str(payload.get("api_key") or "").strip():
            c["api_key"] = "" if clear_key else old.get("api_key", "")
        data["connections"] = [c if x["id"] == old["id"] else x for x in data["connections"]]
    else:
        c = _clean_connection(payload)
        if not c["base_url"] and not c["model"]:
            raise ValueError("base_url_or_model_required")
        data["connections"].append(c)
    if not data["active_id"]:
        data["active_id"] = c["id"]
    _write(data)
    return {"ok": True, "active_id": data["active_id"], "connection": _public(c)}


def activate(connection_id):
    data = load_config()
    connection_id = str(connection_id or "")
    if connection_id not in {c["id"] for c in data["connections"]}:
        raise KeyError("connection_not_found")
    data["active_id"] = connection_id
    _write(data)
    return {"ok": True, "active_id": connection_id, "connection": _public(get_connection(connection_id))}


def delete(connection_id):
    data = load_config()
    connection_id = str(connection_id or "")
    kept = [c for c in data["connections"] if c["id"] != connection_id]
    if len(kept) == len(data["connections"]):
        raise KeyError("connection_not_found")
    data["connections"] = kept
    if data["active_id"] == connection_id:
        data["active_id"] = kept[0]["id"] if kept else ""
    _write(data)
    return {"ok": True, "active_id": data["active_id"]}


def active_tuple():
    c = active_connection()
    if c:
        return c["base_url"], c["api_key"], c["model"], c.get("thinking_level", "")
    return "", "", "", ""
