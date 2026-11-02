# -*- coding: utf-8 -*-
"""本地医学书籍 RAG 管线（MinerU → 清洗 → 切分 → 向量 → 重排）。

装配入口：
- `configure(...)` 注入测试替身或应用组件；
- `service()` 懒装配：auto=MySQL+Redis+Milvus，fake=进程内假存储；
- `startup()` 启动 worker（受 DERM_KB_AUTOSTART_WORKER 控制）；
- `reset()` 停 worker 并清空装配（单测隔离用）。
"""
import threading

from . import config
from . import kselect
from .service import KBService, build_service

_service = None
_lock = threading.RLock()


def configure(store=None, mirror=None, vectors=None, queue=None, audit=None,
              autostart_worker: bool = False, worker=None) -> KBService:
    """显式装配（测试/桌面注入）。传入 None 的组件按可用性降级。"""
    global _service
    with _lock:
        if _service is not None:
            try:
                _service.stop()
            except Exception:  # noqa: BLE001
                pass
        _service = KBService(store=store, mirror=mirror, vectors=vectors, queue=queue,
                             audit=audit, autostart_worker=autostart_worker)
        if worker is not None:
            _service.worker = worker
        kselect.configure(store=store, queue=queue, mirror=mirror)
        return _service


def service() -> KBService:
    global _service
    with _lock:
        if _service is None:
            _service = build_service()
            kselect.configure(store=_service.store, queue=_service.queue,
                              mirror=_service.mirror)
        return _service


def startup(audit=None) -> KBService:
    global _service
    with _lock:
        if _service is None:
            _service = build_service(audit=audit)
            kselect.configure(store=_service.store, queue=_service.queue,
                              mirror=_service.mirror)
        if config.enabled():
            _service.start_worker()
        return _service


def reset() -> None:
    global _service
    with _lock:
        if _service is not None:
            try:
                _service.stop()
            except Exception:  # noqa: BLE001
                pass
        _service = None
        kselect.configure(None)


def health() -> dict:
    return service().health()


__all__ = ["configure", "service", "startup", "reset", "health", "KBService"]
