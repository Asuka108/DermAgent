# -*- coding: utf-8 -*-
"""记忆层配置与规格加载（env 优先；凭据不落日志、不出健康接口）。"""
import json
import os
import threading

try:  # 本机部署文件（server/data/local.env）先于配置读取
    from local_env import load as _load_local_env
except ImportError:
    try:
        from server.local_env import load as _load_local_env
    except ImportError:
        _load_local_env = None
if _load_local_env:
    _load_local_env()

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(HERE)
SHARED_DIR = os.path.abspath(os.path.join(SERVER_DIR, "..", "shared"))
SPEC_FILE = os.path.join(SHARED_DIR, "memory_spec.json")
DATA_DIR = os.environ.get("DERM_DATA_DIR") or os.path.join(SERVER_DIR, "data")

_lock = threading.Lock()
_spec_cache = None


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def enabled() -> bool:
    """显式关闭（测试默认）或全局开关。"""
    if _env("DERM_MEMORY_DISABLED").lower() in ("1", "true", "yes"):
        return False
    return _env("DERM_MEMORY_ENABLED", "1").lower() not in ("0", "false", "no")


def spec() -> dict:
    global _spec_cache
    with _lock:
        if _spec_cache is None:
            try:
                with open(SPEC_FILE, encoding="utf-8") as fh:
                    _spec_cache = json.load(fh)
            except (OSError, ValueError):
                _spec_cache = {}
        return _spec_cache


def reload_spec() -> dict:
    global _spec_cache
    with _lock:
        _spec_cache = None
    return spec()


def redis_url() -> str:
    return _env("DERM_REDIS_URL", "redis://127.0.0.1:6379/0")


def mysql_settings() -> dict:
    """优先独立环境变量；否则交给 pymysql 读 ~/.my.cnf（凭据不入仓库）。"""
    out = {
        "host": _env("DERM_MYSQL_HOST"),
        "port": int(_env("DERM_MYSQL_PORT") or 3306),
        "user": _env("DERM_MYSQL_USER"),
        "password": _env("DERM_MYSQL_PASSWORD"),
        "database": _env("DERM_MYSQL_DB", "dermagent"),
    }
    if not out["user"] and not out["host"]:
        out["read_default_file"] = os.path.expanduser("~/.my.cnf")
    return out


def milvus_uri() -> str:
    return _env("DERM_MILVUS_URI", "http://127.0.0.1:19530")


def milvus_token() -> str:
    return _env("DERM_MILVUS_TOKEN")


def embed_model() -> str:
    return _env("DERM_MEMORY_EMBED_MODEL") or str(
        (spec().get("episodic") or {}).get("embed_model") or "bge-base-zh-v1.5")


def data_dir() -> str:
    return DATA_DIR


def short_rounds() -> int:
    return int((spec().get("short_term") or {}).get("rounds") or 20)


def short_ttl() -> int:
    return int((spec().get("short_term") or {}).get("ttl_seconds") or 604800)


def budget(name: str, default: int) -> int:
    return int((spec().get("budget") or {}).get(name) or default)


def profile_rule() -> dict:
    return spec().get("profile") or {}


def episodic_rule() -> dict:
    return spec().get("episodic") or {}
