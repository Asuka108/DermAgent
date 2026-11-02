# -*- coding: utf-8 -*-
"""bge-reranker-base 本地 CrossEncoder：logit → sigmoid，未安装如实报 not_installed。"""
import importlib.util
import math
import os
import threading
import time

from . import config

_RETRY_S = 30
_lock = threading.Lock()
_model = None
_state = {"state": "unknown", "detail": ""}
_state_at = 0.0


def _weights_present(path: str) -> bool:
    return any(os.path.exists(os.path.join(path, name)) for name in
               ("pytorch_model.bin", "model.safetensors", "onnx/model.onnx"))


def _looks_installed() -> tuple:
    path = config.rerank_dir()
    if not os.path.isdir(path):
        return False, f"模型目录未安装（{path}）"
    if not os.path.exists(os.path.join(path, "config.json")):
        return False, f"模型目录缺少 config.json（{path}）"
    if not _weights_present(path):
        return False, f"模型目录缺少权重文件（{path}）"
    return True, ""


def status() -> dict:
    return dict(_state)


def ready() -> bool:
    return _state.get("state") == "ready" and _model is not None


def reset() -> None:
    global _model, _state, _state_at
    with _lock:
        _model = None
        _state = {"state": "unknown", "detail": ""}
        _state_at = 0.0


def health() -> dict:
    if ready():
        return {"ok": True, "state": "ready", "model": config.rerank_model(), "detail": ""}
    if importlib.util.find_spec("sentence_transformers") is None:
        return {"ok": False, "state": "not_installed", "model": config.rerank_model(),
                "detail": "依赖 sentence-transformers 未安装"}
    installed, detail = _looks_installed()
    if not installed:
        return {"ok": False, "state": "not_installed", "model": config.rerank_model(),
                "detail": detail}
    return {"ok": False, "state": "unloaded" if _state["state"] != "error" else "error",
            "model": config.rerank_model(),
            "detail": _state.get("detail") or "待加载"}


def ensure() -> dict:
    global _model, _state, _state_at
    with _lock:
        if _model is not None:
            return {"ok": True, "state": "ready", "model": config.rerank_model(), "detail": ""}
        if time.time() - _state_at < _RETRY_S and _state["state"] in ("not_installed", "error"):
            return {"ok": False, **_state, "model": config.rerank_model()}
        if importlib.util.find_spec("sentence_transformers") is None:
            _state = {"state": "not_installed", "detail": "依赖 sentence-transformers 未安装"}
            _state_at = time.time()
            return {"ok": False, **_state, "model": config.rerank_model()}
        installed, detail = _looks_installed()
        if not installed:
            _state = {"state": "not_installed", "detail": detail}
            _state_at = time.time()
            return {"ok": False, **_state, "model": config.rerank_model()}
        try:
            try:
                import torch
                torch.set_num_threads(max(1, min(4, (os.cpu_count() or 4) // 2)))
            except Exception:  # noqa: BLE001
                pass
            from sentence_transformers import CrossEncoder
            _model = CrossEncoder(config.rerank_dir(), device="cpu", local_files_only=True)
        except Exception as exc:  # noqa: BLE001
            _model = None
            _state = {"state": "error", "detail": f"{exc.__class__.__name__}: {exc}"[:200]}
            _state_at = time.time()
            return {"ok": False, **_state, "model": config.rerank_model()}
        _state = {"state": "ready", "detail": ""}
        _state_at = time.time()
        return {"ok": True, **_state, "model": config.rerank_model()}


def sigmoid(value: float) -> float:
    value = max(-30.0, min(30.0, float(value)))
    return 1.0 / (1.0 + math.exp(-value))


def score(query: str, texts: list) -> list | None:
    """返回与 texts 等长的 0..1 分数；模型不可用/异常返回 None（调用方用向量分兜底）。"""
    global _state, _state_at
    texts = [str(t or "") for t in (texts or [])]
    if not texts:
        return []
    if not ensure().get("ok"):
        return None
    try:
        logits = _model.predict([[str(query or ""), text] for text in texts],
                                batch_size=8, show_progress_bar=False)
    except Exception as exc:  # noqa: BLE001
        _state = {"state": "error", "detail": f"{exc.__class__.__name__}: {exc}"[:200]}
        _state_at = time.time()
        return None
    out = []
    for item in logits:
        try:
            value = float(item[0]) if hasattr(item, "__len__") else float(item)
        except (TypeError, ValueError, IndexError):
            return None
        out.append(round(sigmoid(value), 6))
    return out
