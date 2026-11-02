# -*- coding: utf-8 -*-
"""索引 worker：守护线程单并发消费 KB 队列，失败退避重试，崩溃后回灌恢复。

- 只有 leader 消费（服务端与桌面同时运行时避免重复解析）；
- 可重试错误最多 3 次，不可重试（格式不支持/正文为空/MinerU 缺失）直接 failed；
- 失败只改书目状态与任务记录，不影响已完成的旧 generation。
"""
import threading
import time
import uuid

from . import config
from .index import IndexFailure
from .queue import ACTIVE_STATES


class KBWorker:
    def __init__(self, queue, indexer, store, audit=None, leader_required: bool = True,
                 poll_seconds: int = 2):
        self.queue = queue
        self.indexer = indexer
        self.store = store
        self._audit_cb = audit or (lambda event, detail="": None)
        self.leader_required = bool(leader_required)
        self.poll_seconds = max(1, int(poll_seconds))
        self._token = "worker-" + uuid.uuid4().hex[:12]
        self._thread = None
        self._stop = threading.Event()
        self._processed = 0
        self._failed = 0
        self._last_error = ""
        self._last_done_at = ""

    def audit(self, event: str, detail) -> None:
        try:
            self._audit_cb(event, detail)
        except Exception:  # noqa: BLE001
            pass

    # —— 生命周期 ——
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.recover()
        self._thread = threading.Thread(target=self._run, daemon=True, name="kb-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def recover(self) -> dict:
        try:
            reclaimed = self.queue.reclaim()
            promoted = self.queue.promote_delayed()
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)[:200]
            return {"reclaimed": 0, "promoted": 0}
        if reclaimed or promoted:
            self.audit("kb_queue_recovered", {"reclaimed": reclaimed, "promoted": promoted})
        return {"reclaimed": reclaimed, "promoted": promoted}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.leader_required and not self.queue.acquire_leader(self._token):
                    self._stop.wait(5)
                    continue
                job_id = self.queue.claim(timeout=self.poll_seconds)
                if not job_id:
                    self.queue.promote_delayed()
                    continue
                self.process_job(job_id)
            except Exception as exc:  # noqa: BLE001 — 守护线程绝不能因单次异常退出
                self._last_error = f"{exc.__class__.__name__}: {exc}"[:200]
                self.audit("kb_worker_error", {"error": self._last_error})
                self._stop.wait(1)

    def health(self) -> dict:
        alive = bool(self._thread is not None and self._thread.is_alive())
        return {"ok": alive and not self._last_error, "thread": alive,
                "leader": bool(self.leader_required), "processed": self._processed,
                "failed": self._failed, "last_error": self._last_error,
                "last_done_at": self._last_done_at}

    # —— 任务 ——
    def run_once(self, timeout: int = 0) -> bool:
        """单测/手动触发：领取并处理一个任务。"""
        if self.queue.promote_delayed():
            pass
        job_id = self.queue.claim(timeout=timeout)
        if not job_id:
            return False
        self.process_job(job_id)
        return True

    def process_job(self, job_id: str) -> str:
        job = self.queue.get_job(job_id)
        if not job or job["state"] not in ACTIVE_STATES:
            self.queue.ack(job_id)
            return "skipped"
        book_id = job["book_id"]
        try:
            book = self.store.get_book(book_id)
        except Exception as exc:  # noqa: BLE001
            self._fail(job, "mysql_unavailable", str(exc), retryable=True)
            return "retry"
        if not book:
            self.queue.finish(job_id, False, "book_not_found", "书目不存在")
            self._failed += 1
            return "failed"
        try:
            if job["kind"] == "delete":
                self.indexer.delete(book)
            else:
                self.indexer.run(book,
                                 reuse_cleaned=job["reuse_cleaned"] or job["kind"] == "restore",
                                 reindex=job["kind"] == "reindex",
                                 progress=self._progress_for(job))
        except IndexFailure as exc:
            self._fail(job, exc.code, exc.detail, retryable=exc.retryable)
            return "retry" if exc.retryable and job["attempts"] < config.job_max_attempts() else "failed"
        except Exception as exc:  # noqa: BLE001
            self._fail(job, "index_crashed", f"{exc.__class__.__name__}: {exc}",
                       retryable=False)
            return "failed"
        self.queue.finish(job_id, True)
        self.queue.update(job_id, phase="ready", progress=100)
        self._processed += 1
        self._last_done_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return "done"

    def _progress_for(self, job):
        def progress(phase: str, pct: int, message: str = ""):
            self.queue.set_progress(job["book_id"], phase, pct, message, job_id=job["job_id"])
        return progress

    def _fail(self, job: dict, code: str, detail: str, retryable: bool) -> None:
        job_id = job["job_id"]
        book_id = job["book_id"]
        attempts = int(job.get("attempts") or 1)
        reindex = job.get("kind") == "reindex"
        retry = retryable and attempts < config.job_max_attempts()
        self._failed += 1
        self._last_error = f"{code}: {detail}"[:200]
        self.queue.update(job_id, error_code=code, error_detail=detail)
        fields = {"error_code": code, "error_detail": detail,
                  "retry_count": int((job.get("attempts") or 1) - 1)}
        if reindex:
            fields["index_state"] = "queued" if retry else "failed"
        else:
            fields.update({"status": "queued" if retry else "failed",
                           "index_state": "queued" if retry else "failed"})
        try:
            self.store.update_book(book_id, **fields)
        except Exception:  # noqa: BLE001 — 书目状态更新失败不回滚队列语义
            pass
        if retry:
            delay = config.job_retry_delay() * (2 ** max(0, attempts - 1))
            self.queue.retry(job_id, delay=delay)
            self.audit("kb_index_retry", {"job_id": job_id, "book_id": book_id,
                                          "attempt": attempts, "delay_s": delay,
                                          "error": code})
            return
        self.queue.finish(job_id, False, code, detail)
        self.audit("kb_index_failed", {"job_id": job_id, "book_id": book_id,
                                       "error": code, "detail": detail[:160]})
