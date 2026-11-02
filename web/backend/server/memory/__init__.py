# -*- coding: utf-8 -*-
"""三层记忆统一入口：短期窗口 / 患者画像 / 情景召回装配与降级。

应用侧只依赖本模块暴露的少数函数（configure / append_message / assemble / enqueue /
patients / health / purge_case），底层适配器可替换为假实现，单测不连真实服务。
"""
import threading
import time
from datetime import datetime, timezone

from . import config, inject, redact
from .patients import PatientService
from .profile import InMemoryProfileStore, MySQLProfileStore
from .short_term import ShortTermMemory

__all__ = [
    "config", "configure", "enabled", "append_message", "assemble", "enqueue",
    "patients", "health", "purge_case", "purge_patient", "patient_summary",
    "snapshot_meta", "set_profile_store", "set_episodic", "reset",
    "after_turn", "start_worker", "use_milvus", "worker",
]

_LOCK = threading.RLock()
_PROFILE_RETRY_SECONDS = 30.0
_state = {
    "reader": None,          # callable(case_id) -> session dict（或 messages 列表）
    "audit": None,           # callable(event, detail)
    "llm": None,             # callable() -> (base_url, api_key, model, effort)
    "short": None,
    "profile": None,
    "profile_retry_at": 0.0,
    "profile_error": "",
    "episodic_store": None,
    "episodic_recall": None,   # callable(patient_id, case_id, query, task_type, exclude) -> list[dict]
    "episodic_health": None,   # callable() -> {ok, latency_ms, detail}
    "episodic_delete": None,   # callable(case_id=, patient_id=)
    "worker": None,
    "worker_health": None,     # callable() -> dict
    "worker_notify": None,     # callable(job_id)
    "last_extract": {},        # case_id -> iso 时间（worker 完成后写入，快照展示）
    "patient_cache": {},       # case_id -> (expires, row)；快照高频读取用 60s 缓存
}
_PATIENT_TTL = 60.0


def enabled() -> bool:
    return config.enabled()


def configure(reader=None, audit=None, llm=None, worker_notify=None) -> None:
    with _LOCK:
        if reader is not None:
            _state["reader"] = reader
            if _state["short"] is not None:
                _state["short"].reader = reader
        if audit is not None:
            _state["audit"] = audit
        if llm is not None:
            _state["llm"] = llm
        if worker_notify is not None:
            _state["worker_notify"] = worker_notify


def _audit(event: str, detail="") -> None:
    fn = _state.get("audit")
    if callable(fn):
        try:
            fn(event, detail)
        except Exception:  # noqa: BLE001
            pass


def reset() -> None:
    """测试用：清空单例（不关闭外部连接）。"""
    worker = _state.get("worker")
    if worker is not None:
        try:
            worker.stop()
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        for key in ("short", "profile", "worker", "episodic_store"):
            _state[key] = None
        _state["profile_retry_at"] = 0.0
        _state["profile_error"] = ""
        _state["episodic_recall"] = None
        _state["episodic_health"] = None
        _state["episodic_delete"] = None
        _state["worker_health"] = None
        _state["worker_notify"] = None
        _state["last_extract"].clear()
        _state["patient_cache"].clear()


def short_term() -> ShortTermMemory:
    with _LOCK:
        if _state["short"] is None:
            _state["short"] = ShortTermMemory(reader=_fallback_messages)
        return _state["short"]


def _fallback_messages(case_id: str) -> list:
    reader = _state.get("reader")
    if not callable(reader):
        return []
    try:
        out = reader(case_id)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(out, dict):
        return list(out.get("messages") or [])
    return list(out or [])


def set_profile_store(store) -> None:
    with _LOCK:
        _state["profile"] = store
        _state["profile_retry_at"] = 0.0
        _state["profile_error"] = ""
        _state["patient_cache"].clear()


def _profile_fail(exc: Exception) -> None:
    with _LOCK:
        _state["profile"] = None
        _state["profile_retry_at"] = time.time() + _PROFILE_RETRY_SECONDS
        _state["profile_error"] = str(exc)[:160]
    _audit("memory_profile_unavailable", {"error": str(exc)[:160]})


def _invalidate_patient(case_id: str = "") -> None:
    with _LOCK:
        if case_id:
            _state["patient_cache"].pop(case_id, None)
        else:
            _state["patient_cache"].clear()


def profile():
    """惰性创建 MySQL 存储；失败后 cooldown 内直接返回 None（每轮对话不重试拖慢）。"""
    with _LOCK:
        store = _state["profile"]
        if store is not None:
            return store
        if time.time() < _state["profile_retry_at"]:
            return None
        try:
            store = MySQLProfileStore()
            _state["profile"] = store
            _state["profile_error"] = ""
            return store
        except Exception as exc:  # noqa: BLE001 — MySQL/pymysql 缺失都走降级
            _profile_fail(exc)
            return None


def _profile_store_or_none():
    if not enabled():
        return None
    return profile()


def set_episodic(recall=None, health=None, delete=None) -> None:
    with _LOCK:
        _state["episodic_recall"] = recall
        _state["episodic_health"] = health
        _state["episodic_delete"] = delete


def set_worker_health(fn, notify=None) -> None:
    with _LOCK:
        _state["worker_health"] = fn
        if notify is not None:
            _state["worker_notify"] = notify


def use_milvus(episodic=None) -> object | None:
    """启用 Milvus 情景层（惰性连接；pymilvus 缺失/服务未起动时调用方自动降级）。"""
    if episodic is None:
        try:
            from .episodic import MilvusEpisodic
            episodic = MilvusEpisodic()
        except Exception as exc:  # noqa: BLE001
            _audit("memory_episodic_unavailable", {"error": str(exc)[:160]})
            return None
    rule = config.episodic_rule()

    def recall(patient_id, case_id, query, task_type="", exclude=None):
        return episodic.recall(
            query, patient_id=patient_id or "", case_id=case_id or "",
            exclude_msg_ids=list(exclude or []),
            top_k_case=int(rule.get("top_k_case") or 3),
            top_k_doctor=int(rule.get("top_k_doctor") or 3),
            threshold=float(rule.get("score_threshold") or 0.35),
            max_injected=int(rule.get("max_injected") or 5))

    with _LOCK:
        _state["episodic_store"] = episodic
    set_episodic(recall=recall, health=episodic.health, delete=episodic.delete)
    return episodic


def _on_extract_done(case_id: str, upto_seq: int) -> None:
    with _LOCK:
        _state["last_extract"][case_id] = datetime.now(timezone.utc).isoformat()


def start_worker():
    """启动进程内后台 worker（幂等）；测试可先 set_profile_store 再调用。"""
    with _LOCK:
        if _state.get("worker") is not None:
            return _state["worker"]
        from .worker import MemoryWorker
        worker = MemoryWorker(
            profile_getter=lambda: _profile_store_or_none(),
            episodic_getter=lambda: _state.get("episodic_store"),
            llm_getter=(lambda: _state["llm"]() if callable(_state.get("llm")) else None),
            reader=(lambda cid: _state["reader"](cid) if callable(_state.get("reader")) else None),
            audit=_audit,
            on_done=_on_extract_done)
        _state["worker"] = worker
        _state["worker_health"] = worker.health
        _state["worker_notify"] = worker.notify
    worker.start()
    return worker


def worker():
    return _state.get("worker")


# —— 写 ——

def append_message(case_id: str, message: dict) -> bool:
    if not enabled() or not case_id:
        return False
    try:
        return short_term().append(case_id, message)
    except Exception:  # noqa: BLE001
        return False


def set_summary(case_id: str, summary) -> None:
    if not enabled() or not case_id:
        return
    try:
        short_term().set_summary(case_id, summary)
    except Exception:  # noqa: BLE001
        pass


def enqueue(case_id: str, from_seq: int, upto_seq: int, has_user: bool = True):
    """收口后登记抽取任务：仅已关联患者且区间含医生消息时入队。"""
    if not enabled() or not case_id or not has_user:
        return None
    store = _profile_store_or_none()
    if store is None:
        return None
    try:
        if not store.patient_for_case(case_id):
            return None
        job_id = store.enqueue_job(case_id, int(from_seq), int(upto_seq))
        notify = _state.get("worker_notify")
        if job_id and not callable(notify):
            try:                        # 启动窗口内首轮：懒启动 worker，避免任务滞留到重启
                notify = start_worker().notify
            except Exception:  # noqa: BLE001
                notify = None
        if job_id and callable(notify):
            try:
                notify(job_id)
            except Exception:  # noqa: BLE001
                pass
        return job_id
    except Exception as exc:  # noqa: BLE001
        _audit("memory_enqueue_failed", {"case_id": case_id, "error": str(exc)[:120]})
        return None


_SKIP_EXTRACT_TASKS = ("smalltalk", "settings_control", "context_compress")


def after_turn(sess: dict):
    """回复收口后登记异步抽取（计划 §5.1）：只处理成对回合、且游标之后有医生消息。"""
    if not enabled():
        return None
    case_id = str((sess or {}).get("case_id") or "")
    msgs = (sess or {}).get("messages") or []
    if not case_id or not msgs or not isinstance(msgs[-1], dict):
        return None
    if str(msgs[-1].get("role") or "") != "assistant":
        return None
    upto = 0
    for idx, msg in enumerate(msgs):
        try:
            upto = max(upto, int((msg or {}).get("seq") or 0) or idx + 1)
        except (TypeError, ValueError, AttributeError):
            continue
    if upto <= 0:
        return None
    meta = sess.setdefault("memory_meta", {})
    try:
        if upto <= int(meta.get("enqueued_upto") or 0):
            return None
    except (TypeError, ValueError):
        pass
    store = _profile_store_or_none()
    if store is None:
        return None
    try:
        if not store.patient_for_case(case_id):
            meta["enqueued_upto"] = upto          # 未关联患者：不写画像，不做无谓重查
            return None
        if str(sess.get("task_type") or "") in _SKIP_EXTRACT_TASKS:
            store.set_cursor(case_id, upto)       # 闲聊/设置轮不产生事实，跳过区间
            meta["enqueued_upto"] = upto
            return None
        cursor = store.get_cursor(case_id)
        if upto <= cursor:
            meta["enqueued_upto"] = upto
            return None
        has_user = False
        for idx, msg in enumerate(msgs):
            if not isinstance(msg, dict) or str(msg.get("role") or "") != "user":
                continue
            try:
                seq = int(msg.get("seq") or 0) or idx + 1
            except (TypeError, ValueError):
                seq = idx + 1
            if cursor < seq <= upto:
                has_user = True
                break
        job_id = enqueue(case_id, cursor, upto, has_user)
    except Exception as exc:  # noqa: BLE001
        _audit("memory_after_turn_failed", {"case_id": case_id, "error": str(exc)[:120]})
        return None
    if job_id:
        meta["enqueued_upto"] = upto
        _audit("memory_enqueued", {"case_id": case_id, "from_seq": cursor, "upto_seq": upto})
    return job_id


# —— 读 ——

def _patient_id(store, case_id: str):
    try:
        return store.patient_for_case(case_id) if store else None
    except Exception:  # noqa: BLE001
        return None


def assemble(sess: dict, query: str = "", task_type: str = "", budget: dict | None = None) -> dict:
    """每轮装配一次（计划 §6.1）：窗口 + 摘要 + 患者档案 + 情景召回。"""
    t0 = time.perf_counter()
    case_id = str((sess or {}).get("case_id") or "")
    limits = {"profile": config.budget("profile_chars", 800),
              "episodic": config.budget("episodic_chars", 1200)}
    if isinstance(budget, dict):
        limits.update({k: int(v) for k, v in budget.items() if k in limits and v})
    degraded = {"short_term": False, "profile": False, "episodic": False}
    out = {"window": [], "summary": None, "profile_text": "", "episodic_text": "",
           "patient_id": None,
           "meta": {"profile_facts": 0, "episodic_hits": 0, "degraded": degraded,
                    "truncated": {"profile": False, "episodic": False}, "latency_ms": 0}}

    if not enabled():
        degraded.update({"short_term": True, "profile": True, "episodic": True})
        out["meta"]["disabled"] = True
        return out

    if case_id:
        try:
            msgs, st_degraded = short_term().window(case_id)
            out["window"] = msgs
            degraded["short_term"] = bool(st_degraded)
        except Exception:  # noqa: BLE001
            degraded["short_term"] = True
    summary = (sess or {}).get("context_summary")
    if not isinstance(summary, dict) and case_id:
        try:
            summary = short_term().summary(case_id)
        except Exception:  # noqa: BLE001
            summary = None
    out["summary"] = summary if isinstance(summary, dict) else None

    store = _profile_store_or_none()
    if store is None:
        degraded["profile"] = True
    else:
        patient_id = _patient_id(store, case_id)
        out["patient_id"] = patient_id
        if not patient_id:
            degraded["profile"] = True
            out["meta"]["profile_reason"] = "no_patient"
        else:
            try:
                facts = store.active_facts(patient_id)
                text = inject.render_profile(facts, limits["profile"])
                # 事实值可能来自医生手录/病例导入并夹带身份信息：进提示词前统一脱敏。
                try:
                    patient = store.get_patient(patient_id) or {}
                except Exception:  # noqa: BLE001
                    patient = {}
                names = redact.identity_values(patient)
                if text and redact.has_identity(text, names):
                    text = redact.redact_identity(text, names)
                    _audit("memory_profile_redacted", {"case_id": case_id,
                                                       "patient_id": patient_id})
                out["profile_text"] = text
                out["meta"]["profile_facts"] = len(facts)
                out["meta"]["truncated"]["profile"] = "已截断" in text
            except Exception as exc:  # noqa: BLE001
                degraded["profile"] = True
                _audit("memory_profile_read_failed", {"case_id": case_id, "error": str(exc)[:120]})

    recall = _state.get("episodic_recall")
    if not callable(recall):
        degraded["episodic"] = True
    else:
        recent = []
        for msg in out["window"][-4:]:
            mid = str(msg.get("message_id") or "") if isinstance(msg, dict) else ""
            if mid:
                recent.append(mid)
        try:
            hits = recall(out["patient_id"], case_id, str(query or "")[:400],
                          str(task_type or ""), recent)
            hits = [h for h in (hits or []) if isinstance(h, dict)]
            text = inject.render_episodic(hits, limits["episodic"])
            out["episodic_text"] = text
            out["meta"]["episodic_hits"] = len(hits)
            out["meta"]["truncated"]["episodic"] = "已截断" in text
        except Exception as exc:  # noqa: BLE001
            degraded["episodic"] = True
            _audit("memory_episodic_recall_failed", {"case_id": case_id, "error": str(exc)[:120]})

    out["meta"]["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    _audit("memory_recall", {"case_id": case_id, "window": len(out["window"]),
                             "profile_facts": out["meta"]["profile_facts"],
                             "episodic_hits": out["meta"]["episodic_hits"],
                             "degraded": degraded, "latency_ms": out["meta"]["latency_ms"]})
    return out


# —— 患者入口 ——

def patients() -> PatientService | None:
    store = _profile_store_or_none()
    if store is None:
        return None
    return PatientService(store, audit=_state.get("audit"))


def patient_summary(case_id: str):
    """病例关联患者的完整档案行（60s 进程缓存；供病例窗口/接口使用）。"""
    if not enabled() or not case_id:
        return None
    now = time.time()
    with _LOCK:
        hit = _state["patient_cache"].get(case_id)
        if hit and hit[0] > now:
            return dict(hit[1]) if hit[1] else None
    store = _profile_store_or_none()
    if store is None:
        return None
    try:
        pid = store.patient_for_case(case_id)
        row = store.get_patient(pid) if pid else None
    except Exception as exc:  # noqa: BLE001
        _profile_fail(exc)
        return None
    with _LOCK:
        _state["patient_cache"][case_id] = (now + _PATIENT_TTL, row)
    return dict(row) if row else None


def patient_brief(case_id: str):
    row = patient_summary(case_id)
    if not row:
        return None
    age = None
    try:
        year = int(row.get("birth_year"))
        age = datetime.now(timezone.utc).year - year if 0 < year <= 2100 else None
    except (TypeError, ValueError):
        age = None
    return {"patient_id": row.get("patient_id"), "display_name": row.get("display_name") or "",
            "sex": row.get("sex") or "unknown", "age": age}


def invalidate_patient(case_id: str = "") -> None:
    _invalidate_patient(case_id)


def snapshot_meta(sess: dict, has_patient: bool | None = None) -> dict:
    """快照里的记忆元数据：只报计数/耗时/降级，不含事实内容与身份信息。"""
    raw = (sess or {}).get("memory_meta")
    meta = raw if isinstance(raw, dict) else {}
    short = _state.get("short")
    recall = _state.get("episodic_recall")
    degraded = {
        "short_term": bool(short.degraded) if short is not None else False,
        "profile": _state.get("profile") is None,
        "episodic": not callable(recall),
        "worker": not callable(_state.get("worker_health")),
    }
    stored = meta.get("degraded") if isinstance(meta.get("degraded"), dict) else {}
    degraded.update({k: bool(stored[k]) for k in degraded if k in stored})
    if has_patient is False:
        degraded["profile"] = True          # 未关联患者：不写/不读画像，如实标注
    trunc = meta.get("truncated") if isinstance(meta.get("truncated"), dict) else {}
    if not enabled():
        degraded = {"short_term": True, "profile": True, "episodic": True, "worker": True}
    case_id = str((sess or {}).get("case_id") or "")
    return {"profile_facts": int(meta.get("profile_facts") or 0),
            "episodic_hits": int(meta.get("episodic_hits") or 0),
            "last_extract_at": (str(meta.get("last_extract_at") or "")
                                or _state["last_extract"].get(case_id, "")),
            "last_recall_ms": meta.get("latency_ms") or 0,
            "degraded": degraded,
            "truncated": {"profile": bool(trunc.get("profile")),
                          "episodic": bool(trunc.get("episodic"))}}


# —— 删除 / 健康 ——

def purge_patient(patient_id: str) -> bool:
    delete = _state.get("episodic_delete")
    if not enabled() or not patient_id or not callable(delete):
        return False
    try:
        delete(patient_id=patient_id)
        return True
    except Exception as exc:  # noqa: BLE001
        _audit("memory_episodic_delete_failed", {"patient_id": patient_id, "error": str(exc)[:120]})
        return False


def purge_case(case_id: str) -> dict:
    out = {"case_id": case_id, "short_term": 0, "profile": False, "episodic": False}
    if not enabled() or not case_id:
        return out
    try:
        out["short_term"] = short_term().purge(case_id)
    except Exception:  # noqa: BLE001
        pass
    store = _profile_store_or_none()
    if store is not None:
        try:
            store.purge_case(case_id)
            out["profile"] = True
        except Exception as exc:  # noqa: BLE001
            _audit("memory_purge_failed", {"case_id": case_id, "error": str(exc)[:120]})
    delete = _state.get("episodic_delete")
    if callable(delete):
        try:
            delete(case_id=case_id)
            out["episodic"] = True
        except Exception as exc:  # noqa: BLE001
            _audit("memory_episodic_delete_failed", {"case_id": case_id, "error": str(exc)[:120]})
    _audit("case_memory_purge", out)
    return out


def health() -> dict:
    out = {"enabled": enabled()}
    if not enabled():
        out.update({"short_term": {"ok": False, "latency_ms": 0, "detail": "disabled"},
                    "profile": {"ok": False, "latency_ms": 0, "detail": "disabled"},
                    "episodic": {"ok": False, "latency_ms": 0, "detail": "disabled"},
                    "worker": {"ok": False, "detail": "disabled"}})
        return out
    try:
        out["short_term"] = short_term().health()
    except Exception as exc:  # noqa: BLE001
        out["short_term"] = {"ok": False, "latency_ms": 0, "detail": str(exc)[:160]}
    store = profile()
    out["profile"] = store.health() if store is not None else {
        "ok": False, "latency_ms": 0, "detail": _state.get("profile_error") or "unavailable"}
    fn = _state.get("episodic_health")
    if callable(fn):
        try:
            out["episodic"] = fn()
        except Exception as exc:  # noqa: BLE001
            out["episodic"] = {"ok": False, "latency_ms": 0, "detail": str(exc)[:160]}
    else:
        out["episodic"] = {"ok": False, "latency_ms": 0, "detail": "not_available"}
    wfn = _state.get("worker_health")
    if callable(wfn):
        try:
            out["worker"] = wfn()
        except Exception as exc:  # noqa: BLE001
            out["worker"] = {"ok": False, "detail": str(exc)[:160]}
    else:
        out["worker"] = {"ok": False, "detail": "not_available"}
    return out
