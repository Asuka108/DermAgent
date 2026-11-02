# -*- coding: utf-8 -*-
"""KB 向量化：复用 rag_embed 的本地 bge 模型，doc 不加前缀、query 加检索前缀。"""
import importlib.util
import threading
import time

from . import config

DIM = 768
_DOC_TIMEOUT_MS = 60000
_QUERY_TIMEOUT_MS = 5000
_RETRY_S = 30


def _rag_embed():
    try:
        from .. import rag_embed  # 以 server 包导入
    except ImportError:
        import rag_embed  # 以 server 目录为 sys.path
    return rag_embed


_state = {"ok": False, "state": "unknown", "detail": "", "model": config.embed_model(), "dim": DIM}
_state_at = 0.0
_lock = threading.Lock()


def status() -> dict:
    return dict(_state)


def ready() -> bool:
    return _state.get("state") == "ready"


def reset() -> None:
    global _state, _state_at
    with _lock:
        _state = {"ok": False, "state": "unknown", "detail": "",
                  "model": config.embed_model(), "dim": DIM}
        _state_at = 0.0


def ensure(force: bool = False) -> dict:
    """加载/确认指定模型；失败缓存 30s，避免每次检索都重复 import torch。"""
    global _state, _state_at
    model = config.embed_model()
    with _lock:
        fresh = time.time() - _state_at < _RETRY_S
        if not force and _state.get("state") == "ready" and _state.get("model") == model:
            return dict(_state)
        if not force and fresh and _state.get("state") in ("not_installed", "error"):
            return dict(_state)
        try:
            info = _rag_embed().ensure_specific(model)
        except Exception as exc:  # noqa: BLE001
            _state = {"ok": False, "state": "error", "model": model, "dim": DIM,
                      "detail": str(exc)[:160]}
            _state_at = time.time()
            return dict(_state)
        if not info.get("ok"):
            detail = str(info.get("detail") or "模型不可用")[:160]
            _state = {"ok": False, "state": "not_installed", "model": model,
                      "dim": DIM, "detail": detail}
            _state_at = time.time()
            return dict(_state)
        dim = int(info.get("dim") or DIM)
        if dim != DIM:
            _state = {"ok": False, "state": "error", "model": model, "dim": dim,
                      "detail": f"维度不符：期望 {DIM}，实际 {dim}"}
            _state_at = time.time()
            return dict(_state)
        _state = {"ok": True, "state": "ready", "model": model, "dim": DIM, "detail": ""}
        _state_at = time.time()
        return dict(_state)


def health() -> dict:
    """不触发加载：已就绪直接返回，否则报告模型/依赖是否安装。"""
    if ready():
        return {"ok": True, "state": "ready", "model": _state.get("model"),
                "dim": DIM, "detail": ""}
    try:
        installed = bool(_rag_embed().is_installed(config.embed_model()))
    except Exception:  # noqa: BLE001
        installed = False
    deps = importlib.util.find_spec("sentence_transformers") is not None
    detail = ("依赖 sentence-transformers 未安装" if not deps else
              "模型目录未安装" if not installed else "待加载")
    state = "not_installed" if (not deps or not installed) else "unloaded"
    return {"ok": False, "state": state, "model": config.embed_model(),
            "dim": DIM, "detail": detail}


def _valid(vecs, count: int):
    if not isinstance(vecs, list) or len(vecs) != count:
        return None
    for vec in vecs:
        if not isinstance(vec, list) or len(vec) != DIM:
            return None
    return vecs


def embed_documents(texts: list):
    texts = [str(t or "") for t in (texts or [])]
    if not texts:
        return []
    if not ensure().get("ok"):
        return None
    try:
        vecs = _rag_embed().embed_specific(texts, config.embed_model(),
                                           prefix="", timeout_ms=_DOC_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        return None
    return _valid(vecs, len(texts))


def embed_query(text: str):
    text = str(text or "").strip()[:config.query_max_chars()]
    if not text:
        return None
    if not ensure().get("ok"):
        return None
    try:
        vecs = _rag_embed().embed_specific([text], config.embed_model(),
                                           prefix=config.embed_query_prefix(),
                                           timeout_ms=_QUERY_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        return None
    out = _valid(vecs, 1)
    return out[0] if out else None
