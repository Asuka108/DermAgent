# -*- coding: utf-8 -*-
"""KB（本地医学书籍 RAG）配置：环境变量优先，凭据不落日志。"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(HERE)
DATA_DIR = os.environ.get("DERM_DATA_DIR") or os.path.join(SERVER_DIR, "data")
KB_DIR = os.path.join(DATA_DIR, "kb")

try:  # 复用记忆层的 MySQL/Redis 连接配置，避免两套凭据来源
    from ..memory import config as memory_config
except ImportError:  # 以 server 目录为 sys.path 运行
    try:
        from memory import config as memory_config
    except ImportError:  # 单测只导入 kb 子模块
        memory_config = None


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def enabled() -> bool:
    if _env("DERM_KB_DISABLED").lower() in ("1", "true", "yes"):
        return False
    return _env("DERM_KB_ENABLED", "1").lower() not in ("0", "false", "no")


def backend() -> str:
    """auto（真实服务）| fake（进程内假实现，测试）| sqlite（仅镜像只读）。"""
    return _env("DERM_KB_BACKEND", "auto").lower() or "auto"


def autostart_worker() -> bool:
    return _env("DERM_KB_AUTOSTART_WORKER", "1").lower() not in ("0", "false", "no")


def mysql_settings() -> dict:
    if memory_config is not None:
        return memory_config.mysql_settings()
    return {"host": _env("DERM_MYSQL_HOST"), "port": int(_env("DERM_MYSQL_PORT") or 3306),
            "user": _env("DERM_MYSQL_USER"), "password": _env("DERM_MYSQL_PASSWORD"),
            "database": _env("DERM_MYSQL_DB", "dermagent"),
            "read_default_file": os.path.expanduser("~/.my.cnf")}


def redis_url() -> str:
    if memory_config is not None:
        return memory_config.redis_url()
    return _env("DERM_REDIS_URL", "redis://127.0.0.1:6379/0")


def milvus_uri() -> str:
    if memory_config is not None:
        return memory_config.milvus_uri()
    return _env("DERM_MILVUS_URI", "http://127.0.0.1:19530")


def milvus_token() -> str:
    if memory_config is not None:
        return memory_config.milvus_token()
    return _env("DERM_MILVUS_TOKEN")


def uploads_dir() -> str:
    return os.path.join(KB_DIR, "uploads")


def mineru_dir() -> str:
    return os.path.join(KB_DIR, "mineru")


def cleaned_dir() -> str:
    return os.path.join(KB_DIR, "cleaned")


def exports_dir() -> str:
    return os.path.join(KB_DIR, "exports")


def mirror_path() -> str:
    return os.path.join(KB_DIR, "mirror.sqlite3")


def upload_dir(book_id: str) -> str:
    return os.path.join(uploads_dir(), book_id)


def mineru_gen_dir(book_id: str, generation: int) -> str:
    return os.path.join(mineru_dir(), book_id, f"g{int(generation)}")


def cleaned_file(book_id: str, generation: int) -> str:
    return os.path.join(cleaned_dir(), book_id, f"g{int(generation)}.md")


def schema_file() -> str:
    return os.path.join(HERE, "schema.sql")


def collection() -> str:
    return _env("DERM_KB_COLLECTION", "derm_kb_chunk_v1")


def milvus_alias() -> str:
    return _env("DERM_KB_MILVUS_ALIAS", "derm_kb")


def mineru_mode() -> str:
    """auto（有 token 走云端，否则本地 SDK）| cloud | local。"""
    return (_env("DERM_KB_MINERU_MODE", "auto").lower() or "auto")


def mineru_token() -> str:
    return _env("DERM_MINERU_TOKEN") or _env("MINERU_TOKEN")


def mineru_base_url() -> str:
    return _env("DERM_MINERU_BASE_URL", "https://mineru.net/api/v4").rstrip("/")


def mineru_model_version() -> str:
    """云端 model_version：pipeline | vlm | MinerU-HTML。"""
    return _env("DERM_MINERU_MODEL_VERSION", "vlm")


def mineru_language() -> str:
    return _env("DERM_MINERU_LANGUAGE", "ch")


def mineru_poll_seconds() -> float:
    return float(_env("DERM_MINERU_POLL", "5") or 5)


def mineru_timeout_seconds() -> float:
    return float(_env("DERM_MINERU_TIMEOUT", "1800") or 1800)


def embed_model() -> str:
    return _env("DERM_KB_EMBED_MODEL", "bge-base-zh-v1.5")


def embed_query_prefix() -> str:
    return _env("DERM_KB_QUERY_PREFIX", "为这个句子生成表示以用于检索相关文章：")


def rerank_model() -> str:
    return _env("DERM_KB_RERANK_MODEL", "bge-reranker-base")


def rerank_dir() -> str:
    try:
        import rag_embed  # noqa: PLC0415
        return os.path.join(rag_embed.MODELS_ROOT, rerank_model())
    except Exception:  # noqa: BLE001
        return os.path.join(DATA_DIR, "models", rerank_model())


def max_book_bytes() -> int:
    return int(_env("DERM_KB_MAX_BYTES", str(200 * 1024 * 1024)))


def supported_formats() -> set:
    return {"pdf", "txt", "md", "markdown", "html", "htm", "docx", "pptx", "xlsx", "epub"}


def direct_formats() -> set:
    """MinerU 之前可直接读取的纯文本格式。"""
    return {"txt", "md", "markdown"}


def chunk_target() -> int:
    return int(_env("DERM_KB_CHUNK_TARGET", "450"))


def chunk_overlap() -> int:
    return int(_env("DERM_KB_CHUNK_OVERLAP", "60"))


def chunk_max() -> int:
    return int(_env("DERM_KB_CHUNK_MAX", "600"))


def k_default() -> int:
    return int(_env("DERM_KB_K_DEFAULT", "20"))


def k_max() -> int:
    return int(_env("DERM_KB_K_MAX", "50"))


def n_min() -> int:
    return int(_env("DERM_KB_N_MIN", "3"))


def n_max() -> int:
    return int(_env("DERM_KB_N_MAX", "8"))


def gap_rel() -> float:
    return float(_env("DERM_KB_GAP_REL", "0.30"))


def gap_abs() -> float:
    return float(_env("DERM_KB_GAP_ABS", "0.15"))


def cluster_count() -> int:
    return int(_env("DERM_KB_CLUSTER_COUNT", "4"))


def cluster_min_sim() -> float:
    return float(_env("DERM_KB_CLUSTER_MIN_SIM", "0.65"))


def cluster_min_samples() -> int:
    return int(_env("DERM_KB_CLUSTER_MIN_SAMPLES", "3"))


def context_chars() -> int:
    return int(_env("DERM_KB_CONTEXT_CHARS", "2600"))


def citation_chars() -> int:
    return int(_env("DERM_KB_CITATION_CHARS", "350"))


def query_max_chars() -> int:
    return int(_env("DERM_KB_QUERY_MAX", "300"))


def job_max_attempts() -> int:
    return int(_env("DERM_KB_JOB_ATTEMPTS", "3"))


def job_retry_delay() -> int:
    return int(_env("DERM_KB_JOB_RETRY_DELAY", "5"))


def job_ttl() -> int:
    return int(_env("DERM_KB_JOB_TTL", "604800"))


def progress_ttl() -> int:
    return int(_env("DERM_KB_PROGRESS_TTL", "86400"))


def timeout_s() -> float:
    return float(_env("DERM_KB_TIMEOUT", "3"))
