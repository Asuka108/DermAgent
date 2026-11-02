# -*- coding: utf-8 -*-
"""工具管理配置：外部 MCP 服务器 / HTTP 工具 / Python 工具 / 内置工具启停。

安全边界：
- 配置文件与 Fernet 密钥均在 server/data/tools 下，权限 0600，原子写入；
- 对外的 public 视图只给 key 名与掩码，绝不返回 env / header 明文；
- 所有校验失败抛中文 ValueError，由 API 层转 400，不做静默截断。
"""
import copy
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    import tool_spec
except ImportError:  # pragma: no cover - 包导入路径
    from server import tool_spec

_LOCK = threading.RLock()
_LAST_ERROR = ""

TRANSPORTS = ("stdio", "http", "sse")
HTTP_METHODS = ("GET", "POST")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_BLOCKED_HOSTS = {"169.254.169.254", "metadata.google.internal"}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_SECRET_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,79}$")
_SECRET_MASK = "••••"

DEFAULT_CONFIG = {
    "version": 1,
    "builtin_disabled": [],
    "servers": [],
    "http_tools": [],
    "py_tools": [],
}


def _data_dir() -> str:
    return os.environ.get("DERM_DATA_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data")


def tools_dir() -> str:
    return os.path.join(_data_dir(), "tools")


def config_file() -> str:
    return os.path.join(tools_dir(), "config.json")


def key_file() -> str:
    return os.path.join(tools_dir(), "fernet.key")


def py_dir() -> str:
    return os.path.join(tools_dir(), "py")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def last_error() -> str:
    return _LAST_ERROR


def _set_last_error(value: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = str(value or "")[:300]


def _ensure_dirs() -> None:
    for path in (tools_dir(), py_dir()):
        os.makedirs(path, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def _write_private(path: str, data: str) -> None:
    _ensure_dirs()
    tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalize_config(raw) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    disabled = raw.get("builtin_disabled")
    if isinstance(disabled, list):
        cfg["builtin_disabled"] = [str(x) for x in disabled if str(x).strip()]
    for key in ("servers", "http_tools", "py_tools"):
        items = raw.get(key)
        if isinstance(items, list):
            cfg[key] = [x for x in items if isinstance(x, dict)]
    return cfg


def load() -> dict:
    path = config_file()
    if not os.path.isfile(path):
        _set_last_error("")
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        _set_last_error("")
        return _normalize_config(raw)
    except (OSError, ValueError) as exc:
        _set_last_error(f"工具配置读取失败：{exc}")
        return copy.deepcopy(DEFAULT_CONFIG)


def save(cfg: dict) -> dict:
    cfg = _normalize_config(cfg)
    cfg["version"] = 1
    _write_private(config_file(), json.dumps(cfg, ensure_ascii=False, indent=2))
    return cfg


def _fernet():
    _ensure_dirs()
    path = key_file()
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - 依赖缺失时明确报错
        raise ValueError("缺少 cryptography 依赖，无法加密工具密钥") from exc
    if not os.path.isfile(path):
        key = Fernet.generate_key()
        _write_private(path, key.decode("ascii"))
    else:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
        return Fernet(key)
    except (OSError, ValueError) as exc:
        raise ValueError("工具密钥文件损坏，请删除 server/data/tools/fernet.key 后重新填写密钥") from exc


def encrypt_secrets(env: dict = None, headers: dict = None) -> str:
    payload = {"env": dict(env or {}), "headers": dict(headers or {})}
    return _fernet().encrypt(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")


def decrypt_secrets(token: str) -> dict:
    if not token:
        return {"env": {}, "headers": {}}
    try:
        raw = _fernet().decrypt(str(token).encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — 统一转中文可操作错误
        raise ValueError("工具密钥无法解密，请在设置中重新填写") from exc
    if not isinstance(data, dict):
        raise ValueError("工具密钥格式非法，请在设置中重新填写")
    return {"env": dict(data.get("env") or {}),
            "headers": dict(data.get("headers") or {})}


def server_secrets(server: dict) -> dict:
    return decrypt_secrets((server or {}).get("secrets_enc") or "")


def http_secrets(tool: dict) -> dict:
    return decrypt_secrets((tool or {}).get("secrets_enc") or "")


def slug(value: str, fallback: str = "tool", max_len: int = 40) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_").lower()
    text = re.sub(r"_+", "_", text)
    return (text or fallback)[:max_len]


def _validate_url(value: str, label: str = "URL") -> str:
    url = str(value or "").strip()
    if not url:
        raise ValueError(f"{label}不能为空")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{label}必须是 http(s) 地址")
    if parsed.username or parsed.password:
        raise ValueError(f"{label}不能包含用户名/密码，请改用请求头")
    host = parsed.hostname.lower()
    if host in _BLOCKED_HOSTS or host.startswith("169.254."):
        raise ValueError(f"{label}指向云元数据地址，已拒绝")
    if parsed.scheme == "http" and host not in _LOCAL_HOSTS:
        raise ValueError(f"{label}必须使用 HTTPS（本机 localhost 除外）")
    return url


def _validate_schema(schema, label: str = "input_schema") -> dict:
    if not isinstance(schema, dict) or not schema:
        raise ValueError(f"{label} 必须是非空 JSON Schema 对象")
    if schema.get("type") != "object":
        raise ValueError(f"{label} 的 type 必须是 object")
    props = schema.get("properties")
    if props is not None and not isinstance(props, dict):
        raise ValueError(f"{label}.properties 必须是对象")
    return schema


def _validate_name(value: str, label: str = "工具名") -> str:
    name = str(value or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(f"{label}只能使用字母/数字/下划线/短横线，1–40 字符，且以字母或数字开头")
    return name


def _validate_timeout(value, default: int = 30) -> int:
    try:
        timeout = int(default if value in (None, "") else value)
    except (TypeError, ValueError):
        raise ValueError("超时必须是整数秒") from None
    if timeout < 1 or timeout > 120:
        raise ValueError("超时必须在 1–120 秒之间")
    return timeout


def _merge_secret_values(existing: dict, updates, remove, label: str) -> dict:
    out = {str(k): str(v) for k, v in (existing or {}).items()}
    if isinstance(updates, dict):
        for key, value in updates.items():
            name = str(key or "").strip()
            if not name:
                continue
            if not _SECRET_KEY_RE.match(name):
                raise ValueError(f"{label}名 {name} 非法（字母/数字/下划线，最多 80 字符）")
            if value is None or str(value) == "":
                continue
            out[name] = str(value)
    if isinstance(remove, list):
        for key in remove:
            out.pop(str(key or "").strip(), None)
    return out


def masked_server(server: dict) -> dict:
    env_keys = list((server or {}).get("env_keys") or [])
    header_keys = list((server or {}).get("header_keys") or [])
    out = {k: copy.deepcopy(v) for k, v in (server or {}).items() if k != "secrets_enc"}
    out["env_keys"] = env_keys
    out["header_keys"] = header_keys
    out["masked_env"] = {k: _SECRET_MASK for k in env_keys}
    out["masked_headers"] = {k: _SECRET_MASK for k in header_keys}
    out["has_secrets"] = bool(server.get("secrets_enc"))
    return out


def masked_http_tool(tool: dict) -> dict:
    header_keys = list((tool or {}).get("header_keys") or [])
    out = {k: copy.deepcopy(v) for k, v in (tool or {}).items() if k != "secrets_enc"}
    out["header_keys"] = header_keys
    out["masked_headers"] = {k: _SECRET_MASK for k in header_keys}
    out["has_secrets"] = bool(tool.get("secrets_enc"))
    return out


def public_config() -> dict:
    cfg = load()
    return {
        "version": 1,
        "builtin_disabled": list(cfg["builtin_disabled"]),
        "servers": [masked_server(s) for s in cfg["servers"]],
        "http_tools": [masked_http_tool(t) for t in cfg["http_tools"]],
        "py_tools": [copy.deepcopy(t) for t in cfg["py_tools"]],
        "error": last_error(),
    }


def _find(items, **filters):
    for item in items:
        if all(item.get(k) == v for k, v in filters.items()):
            return item
    return None


def _server_payload(payload: dict, existing: dict = None) -> dict:
    existing = existing or {}
    payload = payload or {}
    name = str(payload.get("name", existing.get("name")) or "").strip()
    if not name or len(name) > 60:
        raise ValueError("服务器名称不能为空且不超过 60 字")
    transport = str(payload.get("transport", existing.get("transport")) or "").strip().lower()
    if transport not in TRANSPORTS:
        raise ValueError("传输方式必须是 stdio / http / sse")
    out = {
        "id": existing.get("id") or f"srv_{uuid.uuid4().hex[:8]}",
        "name": name,
        "transport": transport,
        "command": "",
        "args": [],
        "cwd": "",
        "url": "",
        "timeout_s": _validate_timeout(payload.get("timeout_s", existing.get("timeout_s")), 30),
        "enabled": bool(payload.get("enabled", existing.get("enabled", True))),
        "tool_states": copy.deepcopy(existing.get("tool_states") or {}),
        "removed_tools": list(existing.get("removed_tools") or []),
        "created_at": existing.get("created_at") or _now(),
        "updated_at": _now(),
    }
    if transport == "stdio":
        command = str(payload.get("command", existing.get("command")) or "").strip()
        if not command:
            raise ValueError("stdio 服务器必须填写启动命令")
        args = payload.get("args", existing.get("args") or [])
        if isinstance(args, str):
            args = [args]
        if not isinstance(args, list) or any(not isinstance(x, str) for x in args):
            raise ValueError("args 必须是字符串数组")
        out["command"] = command
        out["args"] = list(args)
        out["cwd"] = str(payload.get("cwd", existing.get("cwd")) or "").strip()
    else:
        out["url"] = _validate_url(payload.get("url", existing.get("url")), "MCP 服务器地址")
    old_secrets = {"env": {}, "headers": {}}
    if existing.get("secrets_enc"):
        old_secrets = decrypt_secrets(existing["secrets_enc"])
    env = _merge_secret_values(old_secrets["env"], payload.get("env"),
                               payload.get("remove_env"), "环境变量")
    headers = _merge_secret_values(old_secrets["headers"], payload.get("headers"),
                                   payload.get("remove_headers"), "请求头")
    if payload.get("clear_secrets"):
        env, headers = {}, {}
    out["secrets_enc"] = encrypt_secrets(env, headers) if (env or headers) else ""
    out["env_keys"] = sorted(env)
    out["header_keys"] = sorted(headers)
    return out


def upsert_server(payload: dict, server_id: str = None) -> dict:
    with _LOCK:
        cfg = load()
        existing = _find(cfg["servers"], id=server_id) if server_id else None
        if server_id and existing is None:
            raise ValueError("MCP 服务器不存在")
        server = _server_payload(payload or {}, existing)
        if existing is None:
            if any(s.get("name", "").lower() == server["name"].lower() for s in cfg["servers"]):
                raise ValueError("同名 MCP 服务器已存在")
            cfg["servers"].append(server)
        else:
            cfg["servers"][cfg["servers"].index(existing)] = server
        save(cfg)
        return masked_server(server)


def delete_server(server_id: str) -> dict:
    with _LOCK:
        cfg = load()
        server = _find(cfg["servers"], id=server_id)
        if server is None:
            raise ValueError("MCP 服务器不存在")
        cfg["servers"].remove(server)
        save(cfg)
        return masked_server(server)


def set_server_enabled(server_id: str, enabled: bool) -> dict:
    with _LOCK:
        cfg = load()
        server = _find(cfg["servers"], id=server_id)
        if server is None:
            raise ValueError("MCP 服务器不存在")
        server["enabled"] = bool(enabled)
        server["updated_at"] = _now()
        save(cfg)
        return masked_server(server)


def set_mcp_tool_state(server_id: str, tool_name: str, enabled: bool) -> dict:
    with _LOCK:
        cfg = load()
        server = _find(cfg["servers"], id=server_id)
        if server is None:
            raise ValueError("MCP 服务器不存在")
        name = str(tool_name or "").strip()
        if not name:
            raise ValueError("工具名不能为空")
        if name in (server.get("removed_tools") or []):
            raise ValueError("该工具已被删除，请先恢复再启用")
        states = server.setdefault("tool_states", {})
        state = states.setdefault(name, {})
        state["enabled"] = bool(enabled)
        state["updated_at"] = _now()
        save(cfg)
        return state


def remove_mcp_tool(server_id: str, tool_name: str) -> dict:
    with _LOCK:
        cfg = load()
        server = _find(cfg["servers"], id=server_id)
        if server is None:
            raise ValueError("MCP 服务器不存在")
        name = str(tool_name or "").strip()
        if not name:
            raise ValueError("工具名不能为空")
        removed = server.setdefault("removed_tools", [])
        if name not in removed:
            removed.append(name)
        (server.get("tool_states") or {}).pop(name, None)
        save(cfg)
        return {"removed": True, "tool": name}


def restore_mcp_tool(server_id: str, tool_name: str) -> dict:
    with _LOCK:
        cfg = load()
        server = _find(cfg["servers"], id=server_id)
        if server is None:
            raise ValueError("MCP 服务器不存在")
        name = str(tool_name or "").strip()
        removed = server.setdefault("removed_tools", [])
        if name in removed:
            removed.remove(name)
        server.setdefault("tool_states", {}).setdefault(name, {"enabled": False})
        save(cfg)
        return {"restored": True, "tool": name}


def _http_tool_payload(payload: dict, existing: dict = None) -> dict:
    existing = existing or {}
    payload = payload or {}
    name = _validate_name(payload.get("name", existing.get("name")), "HTTP 工具名")
    description = str(payload.get("description", existing.get("description")) or "").strip()
    if not description:
        raise ValueError("HTTP 工具描述不能为空（模型依赖描述判断何时调用）")
    method = str(payload.get("method", existing.get("method")) or "GET").upper()
    if method not in HTTP_METHODS:
        raise ValueError("HTTP 工具只允许 GET 或 POST（外部工具强制只读）")
    out = {
        "id": existing.get("id") or f"htt_{uuid.uuid4().hex[:8]}",
        "name": name,
        "public_name": existing.get("public_name") or f"http_{slug(name)}",
        "description": description[:500],
        "input_schema": _validate_schema(payload.get("input_schema",
                                                     existing.get("input_schema")),
                                         "HTTP 工具 input_schema"),
        "url": _validate_url(payload.get("url", existing.get("url")), "HTTP 工具地址"),
        "method": method,
        "timeout_s": _validate_timeout(payload.get("timeout_s", existing.get("timeout_s")), 15),
        "enabled": bool(payload.get("enabled", existing.get("enabled", False))),
        "created_at": existing.get("created_at") or _now(),
        "updated_at": _now(),
    }
    old = {"env": {}, "headers": {}}
    if existing.get("secrets_enc"):
        old = decrypt_secrets(existing["secrets_enc"])
    headers = _merge_secret_values(old["headers"], payload.get("headers"),
                                   payload.get("remove_headers"), "请求头")
    if payload.get("clear_secrets"):
        headers = {}
    out["secrets_enc"] = encrypt_secrets({}, headers) if headers else ""
    out["header_keys"] = sorted(headers)
    return out


def upsert_http_tool(payload: dict, tool_id: str = None) -> dict:
    with _LOCK:
        cfg = load()
        existing = _find(cfg["http_tools"], id=tool_id) if tool_id else None
        if tool_id and existing is None:
            raise ValueError("HTTP 工具不存在")
        tool = _http_tool_payload(payload or {}, existing)
        if existing is None:
            if any(t.get("name", "").lower() == tool["name"].lower() for t in cfg["http_tools"]):
                raise ValueError("同名 HTTP 工具已存在")
            cfg["http_tools"].append(tool)
        else:
            cfg["http_tools"][cfg["http_tools"].index(existing)] = tool
        save(cfg)
        return masked_http_tool(tool)


def delete_http_tool(tool_id: str) -> dict:
    with _LOCK:
        cfg = load()
        tool = _find(cfg["http_tools"], id=tool_id)
        if tool is None:
            raise ValueError("HTTP 工具不存在")
        cfg["http_tools"].remove(tool)
        save(cfg)
        return masked_http_tool(tool)


def path_for_py_tool(tool: dict) -> str:
    rel = str((tool or {}).get("file") or "")
    if not rel:
        raise ValueError("Python 工具文件缺失")
    root = os.path.realpath(py_dir())
    path = os.path.realpath(os.path.join(tools_dir(), rel))
    if not path.startswith(root + os.sep):
        raise ValueError("Python 工具路径非法")
    return path


def install_py_tool(filename: str, content: bytes, spec: dict) -> dict:
    spec = spec or {}
    name = _validate_name(spec.get("name"), "Python 工具名")
    description = str(spec.get("description") or "").strip()
    if not description:
        raise ValueError("Python 工具描述不能为空（模型依赖描述判断何时调用）")
    schema = _validate_schema(spec.get("input_schema"), "Python 工具 input_schema")
    if not str(filename or "").lower().endswith(".py"):
        raise ValueError("只接受 .py 文件")
    tool_id = f"pyt_{uuid.uuid4().hex[:8]}"
    rel = f"py/{tool_id}.py"
    _ensure_dirs()
    path = os.path.join(py_dir(), f"{tool_id}.py")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.chmod(path, 0o600)
        with _LOCK:
            cfg = load()
            if any(t.get("name", "").lower() == name.lower() for t in cfg["py_tools"]):
                raise ValueError("同名 Python 工具已存在")
            tool = {
                "id": tool_id,
                "name": name,
                "public_name": f"py_{slug(name)}",
                "description": description[:500],
                "input_schema": schema,
                "file": rel,
                "source_filename": os.path.basename(str(filename))[:120],
                "enabled": False,
                "created_at": _now(),
                "updated_at": _now(),
            }
            cfg["py_tools"].append(tool)
            save(cfg)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return copy.deepcopy(tool)


def delete_py_tool(tool_id: str) -> dict:
    with _LOCK:
        cfg = load()
        tool = _find(cfg["py_tools"], id=tool_id)
        if tool is None:
            raise ValueError("Python 工具不存在")
        try:
            path = path_for_py_tool(tool)
        except ValueError:
            path = ""
        cfg["py_tools"].remove(tool)
        save(cfg)
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        return copy.deepcopy(tool)


def set_dynamic_enabled(public_name: str, enabled: bool) -> dict:
    with _LOCK:
        cfg = load()
        name = str(public_name or "").strip()
        for key in ("http_tools", "py_tools"):
            tool = _find(cfg[key], public_name=name)
            if tool is not None:
                tool["enabled"] = bool(enabled)
                tool["updated_at"] = _now()
                save(cfg)
                return copy.deepcopy(tool)
        raise ValueError("工具不存在")


def delete_dynamic(public_name: str) -> dict:
    name = str(public_name or "").strip()
    cfg = load()
    for key in ("http_tools", "py_tools"):
        tool = _find(cfg[key], public_name=name)
        if tool is not None:
            if key == "http_tools":
                return delete_http_tool(tool["id"])
            return delete_py_tool(tool["id"])
    raise ValueError("工具不存在")


def set_builtin_enabled(name: str, enabled: bool) -> dict:
    spec = tool_spec.get(name)
    if spec is None:
        raise ValueError("内置工具不存在")
    with _LOCK:
        cfg = load()
        disabled = [str(x) for x in cfg["builtin_disabled"] if x != spec.name]
        if not enabled:
            disabled.append(spec.name)
        cfg["builtin_disabled"] = sorted(set(disabled))
        save(cfg)
        return {"name": spec.name, "enabled": bool(enabled),
                "disabled": list(cfg["builtin_disabled"])}


# app.py 运行脚本与 tests-server 包导入会在同一进程产生两个模块实例；
# 统一成同一个，避免配置缓存与 Fernet 密钥状态被复制成两份。
sys.modules.setdefault("tool_store", sys.modules[__name__])
sys.modules.setdefault("server.tool_store", sys.modules[__name__])
