# -*- coding: utf-8 -*-
"""短期记忆：Redis 对话窗口（最近 N 轮）+ sessions.json 回退。

边界（计划 §3.1）：Redis 只是提示词热窗口，不是病例权威；任何 Redis 故障都不得
影响病例状态，窗口读取回退本地会话并如实标记 degraded。
"""
import json
import time

from . import config

_PREFIX = "derm:mem:v1:case:"


def _keys(case_id: str) -> dict:
    base = f"{_PREFIX}{case_id}"
    return {"msgs": f"{base}:msgs", "summary": f"{base}:summary", "seq": f"{base}:seq"}


class ShortTermMemory:
    """Redis 窗口；连接失败在 cooldown 内直接走回退，不拖慢每轮对话。"""

    def __init__(self, reader=None, client_factory=None, cooldown: float = 30.0):
        self.reader = reader                  # callable(case_id) -> list[dict]（sessions.json）
        self._client_factory = client_factory or self._default_client
        self._client = None
        self._retry_at = 0.0
        self._cooldown = cooldown
        self.degraded = False
        self.last_error = ""

    @staticmethod
    def _default_client():
        import redis
        client = redis.Redis.from_url(
            config.redis_url(), socket_timeout=0.5, socket_connect_timeout=0.5,
            decode_responses=True)
        client.ping()
        return client

    def _conn(self):
        if time.time() < self._retry_at:
            return None
        if self._client is not None:
            return self._client
        try:
            self._client = self._client_factory()
            self.degraded = False
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001 — 不可用即回退，不抛给调用方
            self._retry_at = time.time() + self._cooldown
            self.degraded = True
            self.last_error = str(exc)[:160]
            self._client = None
        return self._client

    def _drop(self, exc: Exception) -> None:
        self._client = None
        self._retry_at = time.time() + self._cooldown
        self.degraded = True
        self.last_error = str(exc)[:160]

    # —— 写 ——
    def append(self, case_id: str, message: dict) -> bool:
        if not case_id:
            return False
        client = self._conn()
        if client is None:
            return False
        keys = _keys(case_id)
        limit = config.short_rounds() * 2
        try:
            pipe = client.pipeline()
            pipe.rpush(keys["msgs"], json.dumps(message, ensure_ascii=False))
            pipe.ltrim(keys["msgs"], -limit, -1)
            pipe.expire(keys["msgs"], config.short_ttl())
            try:
                seq = int(message.get("seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq:
                pipe.set(keys["seq"], seq)
                pipe.expire(keys["seq"], config.short_ttl())
            pipe.execute()
            return True
        except Exception as exc:  # noqa: BLE001
            self._drop(exc)
            return False

    def set_summary(self, case_id: str, summary) -> bool:
        if not case_id or not isinstance(summary, dict):
            return False
        client = self._conn()
        if client is None:
            return False
        try:
            client.set(_keys(case_id)["summary"], json.dumps(summary, ensure_ascii=False),
                       ex=config.short_ttl())
            return True
        except Exception as exc:  # noqa: BLE001
            self._drop(exc)
            return False

    # —— 读 ——
    def window(self, case_id: str, rounds: int = None, fallback=None):
        """返回 (messages, degraded)。Redis 空/不可用 → 本地会话消息。

        degraded 只表示 Redis 连接层不可用：新病例（key 尚未建立）或 key 过期后
        回退 sessions.json 属于正常读路径，不能误报为「短期记忆不可用」。
        """
        rounds = int(rounds or config.short_rounds())
        limit = max(1, rounds) * 2
        client = self._conn() if case_id else None
        if client is not None:
            try:
                raw = client.lrange(_keys(case_id)["msgs"], -limit, -1)
                out = []
                for item in raw or []:
                    try:
                        msg = json.loads(item)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(msg, dict):
                        out.append(msg)
                if out:
                    return out, self.degraded
            except Exception as exc:  # noqa: BLE001
                self._drop(exc)
        src = fallback if fallback is not None else self.reader
        msgs = []
        if callable(src) and case_id:
            try:
                raw_msgs = src(case_id) or []
                if isinstance(raw_msgs, dict):       # 应用侧 reader 返回会话对象
                    raw_msgs = raw_msgs.get("messages") or []
                msgs = [m for m in raw_msgs if isinstance(m, dict)]
            except Exception:  # noqa: BLE001 — 回退失败等同于空窗口
                msgs = []
        return msgs[-limit:], self.degraded

    def summary(self, case_id: str):
        client = self._conn() if case_id else None
        if client is None:
            return None
        try:
            raw = client.get(_keys(case_id)["summary"])
        except Exception as exc:  # noqa: BLE001
            self._drop(exc)
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def seq(self, case_id: str) -> int:
        client = self._conn() if case_id else None
        if client is None:
            return 0
        try:
            return int(client.get(_keys(case_id)["seq"]) or 0)
        except Exception as exc:  # noqa: BLE001
            self._drop(exc)
            return 0

    # —— 清理 / 健康 ——
    def purge(self, case_id: str) -> int:
        client = self._conn() if case_id else None
        if client is None:
            return 0
        try:
            return int(client.delete(*_keys(case_id).values()))
        except Exception as exc:  # noqa: BLE001
            self._drop(exc)
            return 0

    def health(self) -> dict:
        t0 = time.perf_counter()
        client = self._conn()
        if client is None:
            return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": self.last_error or "unavailable"}
        try:
            client.ping()
            return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": "redis"}
        except Exception as exc:  # noqa: BLE001
            self._drop(exc)
            return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": self.last_error}
