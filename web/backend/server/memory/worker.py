# -*- coding: utf-8 -*-
"""异步记忆抽取 worker：有界队列 + 守护线程（计划 §5.2）。

纪律：
- LLM 未配置时任务保持 queued，不扣减、不失败（下一轮或重启后可重放）；
- 抽取只写记忆层，绝不回写当前病例/文书/审核状态；
- 任何失败只置 failed + 审计，不影响对话与后续任务；
- 情景写入失败（Milvus/BGE 不可用）不拖垮已成功的事实写入。
"""
import queue
import threading
import time
from datetime import datetime, timezone

from . import extract

_QUEUE_MAX = 256
_CLAIM_TIMEOUT_S = 600


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seq(msg: dict, index: int) -> int:
    try:
        return int(msg.get("seq") or 0) or index + 1
    except (TypeError, ValueError):
        return index + 1


def _case_brief(sess: dict) -> dict:
    """抽取提示词里的病例摘要：只取临床字段，不取身份字段。"""
    if not isinstance(sess, dict):
        return {}
    case = sess.get("case") if isinstance(sess.get("case"), dict) else {}
    record = ((sess.get("record") or {}).get("fields")
              if isinstance(sess.get("record"), dict) else {}) or {}
    out = {}
    for key in ("chief_complaint", "present_illness", "symptoms", "allergies",
                "past_history", "current_medications", "body_region", "diagnosis"):
        value = case.get(key) or record.get(key)
        if str(value or "").strip():
            out[key] = str(value)[:200]
    if str(sess.get("task_text") or "").strip():
        out["task"] = str(sess.get("task_text"))[:120]
    return out


class MemoryWorker:
    """单例后台线程；process_job 同步可调用（单测用假 profile/episodic/LLM）。"""

    def __init__(self, profile_getter, episodic_getter=None, llm_getter=None,
                 reader=None, audit=None, on_done=None, queue_max: int = _QUEUE_MAX):
        self._profile = profile_getter
        self._episodic = episodic_getter or (lambda: None)
        self._llm = llm_getter or (lambda: None)
        self._reader = reader or (lambda case_id: None)
        self._audit = audit or (lambda event, detail="": None)
        self._on_done = on_done
        self._q: "queue.Queue[str]" = queue.Queue(maxsize=max(1, int(queue_max)))
        self._thread = None
        self._stop = threading.Event()
        self._last_error = ""
        self._last_done_at = ""
        self._started_at = 0.0
        self._processed = 0
        self._failed = 0

    # —— 生命周期 ——
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True, name="memory-worker")
        self._thread.start()
        self.replay()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.process_job(job_id)
            except Exception as exc:  # noqa: BLE001 — 单任务异常绝不终止守护线程
                self._last_error = str(exc)[:200]
                self._audit("memory_worker_error", {"job_id": job_id, "error": self._last_error})
            finally:
                self._q.task_done()

    def replay(self) -> int:
        """启动/重启后重放 queued/running 任务（10 分钟内）。"""
        try:
            store = self._profile()
            jobs = store.pending_jobs() if store is not None else []
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)[:200]
            return 0
        queued = 0
        for job in jobs:
            if self.notify(job.get("job_id")):
                queued += 1
        return queued

    def notify(self, job_id: str = None) -> bool:
        if not job_id:
            return False
        try:
            self._q.put_nowait(str(job_id))
            return True
        except queue.Full:
            self._audit("memory_queue_full", {"job_id": job_id})
            return False

    def health(self) -> dict:
        alive = bool(self._thread is not None and self._thread.is_alive())
        return {"ok": alive and not self._last_error, "thread": alive,
                "queued": self._q.qsize(), "processed": self._processed,
                "failed": self._failed, "last_error": self._last_error,
                "last_done_at": self._last_done_at,
                "uptime_s": round(time.time() - self._started_at, 1) if self._started_at else 0}

    def drain(self, timeout: float = 10.0) -> bool:
        """单测用：等待队列清空。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return self._q.unfinished_tasks == 0

    # —— 任务处理 ——
    def process_job(self, job_id: str) -> bool:
        """返回 True 表示任务已终态（done/failed/skip），False 表示保留队列稍后重试。"""
        store = self._profile()
        if store is None:
            return False
        job = store.get_job(job_id)
        if not job or job.get("status") not in ("queued", "running"):
            return True
        case_id = str(job.get("case_id") or "")
        try:
            from_seq = int(job.get("from_seq") or 0)
            upto_seq = int(job.get("upto_seq") or 0)
        except (TypeError, ValueError):
            store.finish_job(job_id, False, "bad_job_range")
            return True
        try:
            self._process(store, job_id, case_id, from_seq, upto_seq)
            return True
        except _RetryLater:
            return False

    def _process(self, store, job_id: str, case_id: str, from_seq: int, upto_seq: int) -> None:
        patient_id = None
        try:
            patient_id = store.patient_for_case(case_id)
        except Exception as exc:  # noqa: BLE001
            self._fail(store, job_id, case_id, f"patient_lookup_failed: {exc}")
            raise _RetryLater() from exc
        if not patient_id:
            # 未关联患者：不写画像，任务直接终结（不伪造事实）
            store.set_cursor(case_id, upto_seq)
            store.finish_job(job_id, True)
            self._audit("memory_extract_skipped", {"case_id": case_id, "reason": "no_patient"})
            return

        llm = None
        try:
            llm = self._llm()
        except Exception:  # noqa: BLE001
            llm = None
        if not llm or not llm[0] or not llm[2]:
            # LLM 不可用：保持 queued，等重放；不计失败、不推进游标
            raise _RetryLater()

        if not store.claim_job(job_id):
            return

        sess = self._session(case_id)
        all_msgs = (sess.get("messages") if isinstance(sess, dict) else sess) or []
        window = []
        for idx, msg in enumerate(all_msgs):
            if isinstance(msg, dict) and from_seq < _seq(msg, idx) <= upto_seq:
                window.append(msg)
        user_msgs = [m for m in window
                     if str(m.get("role") or "") == "user" and str(m.get("content") or "").strip()]
        if not user_msgs:
            store.set_cursor(case_id, upto_seq)
            store.finish_job(job_id, True)
            return

        user_text = "\n".join(str(m.get("content") or "").strip()[:500] for m in user_msgs)
        try:
            patient = store.get_patient(patient_id) or {}
            active = store.active_facts(patient_id)
        except Exception as exc:  # noqa: BLE001
            self._fail(store, job_id, case_id, f"profile_read_failed: {exc}")
            return

        messages = extract.build_messages(user_text, _case_brief(sess), active, patient)
        raw = extract.call_llm(llm, messages)
        if raw is None:
            self._fail(store, job_id, case_id, "llm_call_failed")
            return
        parsed = extract.parse_extraction(raw, user_text, patient)
        if parsed is None:
            self._fail(store, job_id, case_id, "invalid_extraction_json")
            return

        last_msg = window[-1] if window else {}
        msg_id = str(last_msg.get("message_id") or "") or f"seq-{_seq(last_msg, len(all_msgs))}"
        counts = {"inserted": 0, "upgraded": 0, "disputed": 0, "skipped": 0}
        try:
            if parsed["facts"]:
                counts = store.write_facts(patient_id, parsed["facts"], "llm_extract",
                                           case_id=case_id, msg_id=msg_id)
        except Exception as exc:  # noqa: BLE001
            self._fail(store, job_id, case_id, f"profile_write_failed: {str(exc)[:160]}")
            return

        store.set_cursor(case_id, upto_seq)
        self._audit("memory_profile_write", {"case_id": case_id, "patient_id": patient_id,
                                             "facts": counts,
                                             "total": sum(counts.values())})
        self._write_episodic(case_id, patient_id, patient, window, parsed.get("topic"), msg_id)
        store.finish_job(job_id, True)
        self._processed += 1
        self._last_done_at = _iso()
        self._audit("memory_extract_done", {"job_id": job_id, "case_id": case_id,
                                            "facts": counts})
        if callable(self._on_done):
            try:
                self._on_done(case_id, upto_seq)
            except Exception:  # noqa: BLE001
                pass

    def _write_episodic(self, case_id, patient_id, patient, window, topic, msg_id) -> None:
        try:
            epi = self._episodic()
        except Exception:  # noqa: BLE001
            epi = None
        if epi is None:
            self._audit("memory_episodic_skipped", {"case_id": case_id, "reason": "unavailable"})
            return
        chunks = extract.dedupe_chunks(
            extract.build_turn_chunks(window, case_id, patient_id, patient) +
            ([extract.build_topic_chunk(topic, case_id, patient_id, msg_id, patient)]
             if topic else []))
        if not chunks:
            return
        try:
            written = epi.write(chunks)
            self._audit("memory_episodic_write", {"case_id": case_id, "count": written})
        except Exception as exc:  # noqa: BLE001 — 情景失败不影响已写入的事实
            self._audit("memory_episodic_skipped", {"case_id": case_id,
                                                    "reason": str(exc)[:160]})

    def _session(self, case_id: str):
        try:
            return self._reader(case_id) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _fail(self, store, job_id: str, case_id: str, error: str) -> None:
        self._failed += 1
        self._last_error = str(error)[:200]
        try:
            store.finish_job(job_id, False, error)
        except Exception:  # noqa: BLE001
            pass
        self._audit("memory_extract_failed", {"case_id": case_id, "job_id": job_id,
                                              "error": str(error)[:160]})


class _RetryLater(Exception):
    """内部信号：任务保留 queued，不记为失败。"""
