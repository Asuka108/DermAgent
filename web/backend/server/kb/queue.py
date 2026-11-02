# -*- coding: utf-8 -*-
"""KB 任务队列：Redis 列表 + job hash + 进度/租约/延迟重试；测试用同构内存实现。

纪律：Redis 不可用时入队直接抛 QueueUnavailable（API 503），不在内存里假装排队；
worker 崩溃时 processing 列表在启动恢复中回灌。
"""
import time
import uuid

from . import config

PREFIX = "derm:kb:v1:"
QUEUE = PREFIX + "q"
PROCESSING = PREFIX + "q:processing"
DELAYED = PREFIX + "delayed"
LEADER = PREFIX + "leader"
LEASE_TTL_MS = 3600000
LEADER_TTL_MS = 30000
ACTIVE_STATES = ("queued", "running")


class QueueUnavailable(Exception):
    pass


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _job_key(job_id: str) -> str:
    return f"{PREFIX}job:{job_id}"


def _progress_key(book_id: str) -> str:
    return f"{PREFIX}progress:{book_id}"


def _active_key(book_id: str, kind: str) -> str:
    return f"{PREFIX}active:{book_id}:{kind}"


def _lease_key(book_id: str) -> str:
    return f"{PREFIX}lease:{book_id}"


def _kspec_key(version: str) -> str:
    return f"{PREFIX}kspec:{version}"


def job_out(data: dict) -> dict:
    if not data:
        return None
    def as_int(key):
        try:
            return int(data.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    reuse = data.get("reuse_cleaned")
    return {
        "job_id": data.get("job_id"),
        "book_id": data.get("book_id"),
        "kind": data.get("kind") or "index",
        "state": data.get("state") or "queued",
        "generation": as_int("generation"),
        "reuse_cleaned": reuse in (True, 1, "1", "true", "True"),
        "phase": data.get("phase") or "queued",
        "progress": as_int("progress"),
        "attempts": as_int("attempts"),
        "error_code": data.get("error_code") or "",
        "error_detail": data.get("error_detail") or "",
        "heartbeat_at": data.get("heartbeat_at") or "",
        "created_at": data.get("created_at") or "",
        "updated_at": data.get("updated_at") or "",
    }


class BaseQueue:
    """公共语义（幂等入队、租约、进度）；存储原语由子类实现。"""

    def health(self) -> dict:
        raise NotImplementedError

    # —— 存储原语 ——
    def _rpush(self, key, value):
        raise NotImplementedError

    def _blmove(self, src, dst, timeout):
        raise NotImplementedError

    def _lrem(self, key, value):
        raise NotImplementedError

    def _lrange(self, key):
        raise NotImplementedError

    def _hset(self, key, mapping):
        raise NotImplementedError

    def _hgetall(self, key):
        raise NotImplementedError

    def _hget(self, key, field):
        raise NotImplementedError

    def _expire(self, key, seconds):
        raise NotImplementedError

    def _get(self, key):
        raise NotImplementedError

    def _set_nx_ex(self, key, value, ttl):
        raise NotImplementedError

    def _set_ex(self, key, value, ttl):
        raise NotImplementedError

    def _pexpire_if(self, key, value, ms):
        raise NotImplementedError

    def _delete(self, key):
        raise NotImplementedError

    def _zadd(self, key, score, member):
        raise NotImplementedError

    def _zrangebyscore(self, key, upto):
        raise NotImplementedError

    def _zrem(self, key, member):
        raise NotImplementedError

    def _exists(self, key):
        raise NotImplementedError

    # —— job ——
    def enqueue(self, book_id: str, kind: str = "index", generation: int = 0,
                reuse_cleaned: bool = False) -> str:
        active = _active_key(book_id, kind)
        existing = self._get(active)
        if existing:
            job = self.get_job(existing)
            if job and job["state"] in ACTIVE_STATES:
                return existing
        job_id = uuid.uuid4().hex
        if not self._set_nx_ex(active, job_id, config.job_ttl()):
            existing = self._get(active)
            job = self.get_job(existing) if existing else None
            if job and job["state"] in ACTIVE_STATES:
                return existing
            self._set_ex(active, job_id, config.job_ttl())
        now = _now()
        self._hset(_job_key(job_id), {
            "job_id": job_id, "book_id": str(book_id)[:16], "kind": str(kind)[:16],
            "state": "queued", "generation": int(generation or 0),
            "reuse_cleaned": "1" if reuse_cleaned else "0",
            "phase": "queued", "progress": 0, "attempts": 0,
            "error_code": "", "error_detail": "", "heartbeat_at": "",
            "created_at": now, "updated_at": now})
        self._expire(_job_key(job_id), config.job_ttl())
        self._expire(active, config.job_ttl())
        self._rpush(QUEUE, job_id)
        return job_id

    def get_job(self, job_id: str):
        if not job_id:
            return None
        return job_out(self._hgetall(_job_key(job_id)))

    def claim(self, timeout: int = 2):
        job_id = self._blmove(QUEUE, PROCESSING, timeout)
        if not job_id:
            return None
        job = self.get_job(job_id)
        if not job:
            self._lrem(PROCESSING, job_id)
            return None
        self._hset(_job_key(job_id), {
            "state": "running", "attempts": job["attempts"] + 1,
            "heartbeat_at": _now(), "updated_at": _now()})
        return job_id

    def ack(self, job_id: str) -> None:
        self._lrem(PROCESSING, job_id)

    def heartbeat(self, job_id: str) -> None:
        self._hset(_job_key(job_id), {"heartbeat_at": _now(), "updated_at": _now()})

    def update(self, job_id: str, **fields) -> None:
        data = {key: ("" if value is None else value) for key, value in fields.items()}
        if "reuse_cleaned" in data:
            data["reuse_cleaned"] = "1" if data["reuse_cleaned"] else "0"
        data["updated_at"] = _now()
        self._hset(_job_key(job_id), data)

    def finish(self, job_id: str, ok: bool, error_code: str = "", detail: str = "") -> None:
        job = self.get_job(job_id)
        self._hset(_job_key(job_id), {
            "state": "done" if ok else "failed",
            "phase": "ready" if ok else "failed",
            "progress": 100 if ok else 0,
            "error_code": "" if ok else str(error_code)[:48],
            "error_detail": "" if ok else str(detail)[:500],
            "updated_at": _now()})
        self.ack(job_id)
        if job:
            self._delete(_active_key(job["book_id"], job["kind"]))

    def retry(self, job_id: str, delay: int = 0) -> None:
        self._hset(_job_key(job_id), {
            "state": "queued", "phase": "queued",
            "heartbeat_at": "", "updated_at": _now()})
        self.ack(job_id)
        self._zadd(DELAYED, time.time() + max(0, int(delay)), job_id)

    def promote_delayed(self, now: float = None) -> int:
        now = time.time() if now is None else now
        moved = 0
        for job_id in self._zrangebyscore(DELAYED, now):
            self._zrem(DELAYED, job_id)
            job = self.get_job(job_id)
            if job and job["state"] == "queued":
                self._rpush(QUEUE, job_id)
                moved += 1
        return moved

    def reclaim(self) -> int:
        moved = 0
        for job_id in list(self._lrange(PROCESSING)):
            self._lrem(PROCESSING, job_id)
            job = self.get_job(job_id)
            if job and job["state"] in ACTIVE_STATES:
                self._hset(_job_key(job_id), {"state": "queued", "phase": "queued",
                                              "updated_at": _now()})
                self._rpush(QUEUE, job_id)
                moved += 1
        return moved

    # —— 进度 / 租约 / leader / k 规格缓存 ——
    def progress(self, book_id: str) -> dict:
        data = self._hgetall(_progress_key(book_id))
        if not data:
            return {}
        try:
            pct = int(data.get("pct") or 0)
        except (TypeError, ValueError):
            pct = 0
        return {"phase": data.get("phase") or "", "pct": pct,
                "message": data.get("message") or ""}

    def set_progress(self, book_id: str, phase: str, pct: int, message: str = "",
                     job_id: str = "") -> None:
        self._hset(_progress_key(book_id), {"phase": str(phase)[:24],
                                            "pct": max(0, min(100, int(pct))),
                                            "message": str(message)[:200]})
        self._expire(_progress_key(book_id), config.progress_ttl())
        if job_id:
            self.heartbeat(job_id)

    def acquire_lease(self, book_id: str, token: str) -> bool:
        return bool(self._set_nx_ex(_lease_key(book_id), token, LEASE_TTL_MS // 1000))

    def release_lease(self, book_id: str) -> None:
        self._delete(_lease_key(book_id))

    def acquire_leader(self, token: str) -> bool:
        if self._set_nx_ex(LEADER, token, LEADER_TTL_MS // 1000):
            return True
        return bool(self._pexpire_if(LEADER, token, LEADER_TTL_MS))

    def cache_kspec(self, version: str, payload: str, ttl: int = 300) -> None:
        self._set_ex(_kspec_key(version), payload, ttl)

    def cached_kspec(self, version: str):
        return self._get(_kspec_key(version))


class RedisQueue(BaseQueue):
    def __init__(self, url: str = None):
        self.url = url or config.redis_url()
        self._redis = None

    def _client(self):
        if self._redis is None:
            try:
                import redis  # noqa: PLC0415
            except ImportError as exc:
                raise QueueUnavailable("redis 依赖未安装") from exc
            self._redis = redis.Redis.from_url(
                self.url, decode_responses=True, socket_timeout=3, socket_connect_timeout=2)
        return self._redis

    def health(self):
        t0 = time.perf_counter()
        try:
            self._client().ping()
            return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": "redis"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": str(exc)[:160]}

    def _rpush(self, key, value):
        self._client().rpush(key, value)

    def _blmove(self, src, dst, timeout):
        client = self._client()
        if timeout and timeout > 0:
            return client.blmove(src, dst, timeout=max(1, int(timeout)),
                                 src="LEFT", dest="RIGHT")
        return client.lmove(src, dst, "LEFT", "RIGHT")

    def _lrem(self, key, value):
        self._client().lrem(key, 0, value)

    def _lrange(self, key):
        return self._client().lrange(key, 0, -1)

    def _hset(self, key, mapping):
        self._client().hset(key, mapping=mapping)

    def _hgetall(self, key):
        return self._client().hgetall(key)

    def _hget(self, key, field):
        return self._client().hget(key, field)

    def _expire(self, key, seconds):
        self._client().expire(key, int(seconds))

    def _get(self, key):
        return self._client().get(key)

    def _set_nx_ex(self, key, value, ttl):
        return bool(self._client().set(key, value, nx=True, ex=max(1, int(ttl))))

    def _set_ex(self, key, value, ttl):
        self._client().set(key, value, ex=max(1, int(ttl)))

    def _pexpire_if(self, key, value, ms):
        client = self._client()
        if client.get(key) == str(value):
            client.pexpire(key, int(ms))
            return True
        return False

    def _delete(self, key):
        self._client().delete(key)

    def _zadd(self, key, score, member):
        self._client().zadd(key, {member: score})

    def _zrangebyscore(self, key, upto):
        return self._client().zrangebyscore(key, "-inf", upto)

    def _zrem(self, key, member):
        self._client().zrem(key, member)

    def _exists(self, key):
        return bool(self._client().exists(key))


class MemoryQueue(BaseQueue):
    """单测/假后端：与 Redis 同语义，TTL 以惰性过期实现。"""

    def __init__(self):
        self._lists: dict[str, list] = {}
        self._hashes: dict[str, dict] = {}
        self._strings: dict[str, list] = {}
        self._zsets: dict[str, dict] = {}
        self._expiry: dict[str, float] = {}

    def _alive(self, key):
        exp = self._expiry.get(key)
        if exp is not None and exp < time.time():
            self._lists.pop(key, None)
            self._hashes.pop(key, None)
            self._strings.pop(key, None)
            self._zsets.pop(key, None)
            self._expiry.pop(key, None)
            return False
        return True

    def health(self):
        return {"ok": True, "latency_ms": 0, "detail": "memory-queue"}

    def _rpush(self, key, value):
        self._lists.setdefault(key, []).append(value)

    def _blmove(self, src, dst, timeout):
        deadline = time.time() + max(0, int(timeout))
        while True:
            queue = self._lists.get(src) or []
            if queue:
                value = queue.pop(0)
                self._lists.setdefault(dst, []).append(value)
                return value
            if time.time() >= deadline:
                return None
            time.sleep(0.02)

    def _lrem(self, key, value):
        self._lists[key] = [x for x in (self._lists.get(key) or []) if x != value]

    def _lrange(self, key):
        return list(self._lists.get(key) or [])

    def _hset(self, key, mapping):
        if self._alive(key):
            self._hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})

    def _hgetall(self, key):
        return dict(self._hashes.get(key) or {}) if self._alive(key) else {}

    def _hget(self, key, field):
        return (self._hashes.get(key) or {}).get(field) if self._alive(key) else None

    def _expire(self, key, seconds):
        if self._alive(key):
            self._expiry[key] = time.time() + int(seconds)

    def _get(self, key):
        row = self._strings.get(key) if self._alive(key) else None
        return row[0] if row else None

    def _set_nx_ex(self, key, value, ttl):
        if self._get(key) is not None:
            return False
        self._strings[key] = [str(value)]
        self._expiry[key] = time.time() + int(ttl)
        return True

    def _set_ex(self, key, value, ttl):
        self._strings[key] = [str(value)]
        self._expiry[key] = time.time() + int(ttl)

    def _pexpire_if(self, key, value, ms):
        if self._get(key) != str(value):
            return False
        self._expiry[key] = time.time() + ms / 1000.0
        return True

    def _delete(self, key):
        self._lists.pop(key, None)
        self._hashes.pop(key, None)
        self._strings.pop(key, None)
        self._zsets.pop(key, None)
        self._expiry.pop(key, None)

    def _zadd(self, key, score, member):
        self._zsets.setdefault(key, {})[member] = float(score)

    def _zrangebyscore(self, key, upto):
        zset = self._zsets.get(key) or {}
        return [m for m, s in sorted(zset.items(), key=lambda kv: kv[1]) if s <= upto]

    def _zrem(self, key, member):
        (self._zsets.get(key) or {}).pop(member, None)

    def _exists(self, key):
        return self._alive(key) and (key in self._lists or key in self._hashes
                                     or key in self._strings or key in self._zsets)
