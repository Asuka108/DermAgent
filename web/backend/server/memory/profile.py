# -*- coding: utf-8 -*-
"""实体画像层：患者档案 + 版本化事实（MySQL / 内存假实现共用同一冲突规则）。"""
import os
import re
import time
import uuid
from datetime import datetime, timezone

from . import config

SOURCE_PRIORITY = {"doctor_modified": 4, "doctor_input": 3, "case_import": 2, "llm_extract": 1}
_SEX = {"male", "female", "other", "unknown"}
_MAX_NAME = 64


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _age_seconds(stamp, now: datetime) -> float:
    """updated_at（UTC 字符串）距现在的秒数；解析失败视为已过期（允许重试）。"""
    try:
        dt = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


def normalize_value(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()[:255]


def _multi_valued(category: str) -> bool:
    return category in ((config.profile_rule().get("multi_valued")) or [])


def _valid_sex(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in _SEX else "unknown"


def _fact_out(row: dict) -> dict:
    return {
        "fact_id": row.get("fact_id"),
        "patient_id": row.get("patient_id"),
        "category": row.get("category"),
        "field": row.get("field"),
        "value": row.get("value"),
        "normalized_value": row.get("normalized_value"),
        "evidence": row.get("evidence") or "",
        "source": row.get("source"),
        "confidence": float(row.get("confidence") or 0),
        "source_case_id": row.get("source_case_id") or "",
        "source_msg_id": row.get("source_msg_id") or "",
        "status": row.get("status"),
        "effective_from": row.get("effective_from") or "",
        "superseded_by": row.get("superseded_by"),
        "created_at": row.get("created_at") or "",
    }


def plan_write(active_rows: list, fact: dict, source: str, multi: bool) -> list:
    """纯规则：返回 [("insert"|"insert_disputed"|"upgrade", fact_id|None)]。

    - 多值类按归一化值去重；已存在同值时不新增，来源更高时升级来源。
    - 单值类：更高来源生效并让旧行 superseded；更低来源只记 disputed，不生效。
    """
    category, field = fact["category"], fact["field"]
    norm = normalize_value(fact["value"])
    same = [r for r in active_rows
            if r.get("category") == category and r.get("field") == field
            and r.get("status") == "active"]
    incoming = SOURCE_PRIORITY.get(source, 0)
    if multi:
        match = [r for r in same if r.get("normalized_value") == norm]
        if match:
            best = max(match, key=lambda r: SOURCE_PRIORITY.get(r.get("source"), 0))
            if incoming > SOURCE_PRIORITY.get(best.get("source"), 0):
                return [("upgrade", best.get("fact_id"))]
            return []
        return [("insert", None)]
    if not same:
        return [("insert", None)]
    best = max(same, key=lambda r: SOURCE_PRIORITY.get(r.get("source"), 0))
    if all(r.get("normalized_value") == norm for r in same):
        if incoming > SOURCE_PRIORITY.get(best.get("source"), 0):
            return [("upgrade", best.get("fact_id"))]
        return []
    if incoming < SOURCE_PRIORITY.get(best.get("source"), 0):
        return [("insert_disputed", None)]
    return [("insert", None)]


def validate_facts(facts: list, source: str) -> list:
    """白名单 / 置信度 / 长度 / 证据校验；非法项静默丢弃（调用方审计计数）。"""
    rule = config.profile_rule()
    cats = rule.get("categories") or {}
    min_conf = float(rule.get("min_confidence") or 0.6)
    max_value = int(rule.get("max_value_chars") or 200)
    max_ev = int(rule.get("max_evidence_chars") or 500)
    out = []
    for raw in facts or []:
        if not isinstance(raw, dict):
            continue
        cat = str(raw.get("category") or "").strip()
        field = str(raw.get("field") or "").strip()
        value = str(raw.get("value") or "").strip()
        if cat not in cats or field not in set(cats.get(cat) or []):
            continue
        if not value:
            continue
        try:
            conf = float(raw.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if source == "llm_extract" and conf < min_conf:
            continue
        out.append({
            "category": cat, "field": field, "value": value[:max_value],
            "evidence": str(raw.get("evidence") or "")[:max_ev],
            "confidence": max(0.0, min(1.0, conf if source == "llm_extract" else 1.0)),
        })
    return out


class ProfileStore:
    """协议实现：所有方法失败抛异常，由调用方统一降级/审计。"""

    def health(self) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        pass

    # —— 患者 ——
    def create_patient(self, **fields) -> dict:
        raise NotImplementedError

    def get_patient(self, patient_id: str) -> dict | None:
        raise NotImplementedError

    def list_patients(self, query: str = "") -> list:
        raise NotImplementedError

    def update_patient(self, patient_id: str, **fields) -> dict | None:
        raise NotImplementedError

    def delete_patient(self, patient_id: str) -> int:
        raise NotImplementedError

    def merge_patients(self, source_id: str, target_id: str) -> int:
        raise NotImplementedError

    # —— 病例关联 ——
    def link_case(self, case_id: str, patient_id: str) -> dict:
        raise NotImplementedError

    def unlink_case(self, case_id: str) -> None:
        raise NotImplementedError

    def patient_for_case(self, case_id: str) -> str | None:
        raise NotImplementedError

    def cases_for_patient(self, patient_id: str) -> list:
        raise NotImplementedError

    # —— 事实 ——
    def active_facts(self, patient_id: str) -> list:
        raise NotImplementedError

    def all_facts(self, patient_id: str) -> list:
        raise NotImplementedError

    def write_facts(self, patient_id: str, facts: list, source: str,
                    case_id: str = "", msg_id: str = "") -> dict:
        raise NotImplementedError

    def delete_fact(self, fact_id: int) -> bool:
        raise NotImplementedError

    # —— 抽取游标 / 任务 ——
    def get_cursor(self, case_id: str) -> int:
        raise NotImplementedError

    def set_cursor(self, case_id: str, upto_seq: int) -> None:
        raise NotImplementedError

    def enqueue_job(self, case_id: str, from_seq: int, upto_seq: int) -> str | None:
        raise NotImplementedError

    def pending_jobs(self) -> list:
        raise NotImplementedError

    def claim_job(self, job_id: str) -> bool:
        raise NotImplementedError

    def finish_job(self, job_id: str, ok: bool, error: str = "") -> None:
        raise NotImplementedError

    def last_job(self, case_id: str) -> dict | None:
        raise NotImplementedError

    def get_job(self, job_id: str) -> dict | None:
        raise NotImplementedError

    def purge_case(self, case_id: str) -> None:
        raise NotImplementedError


class InMemoryProfileStore(ProfileStore):
    """单测/离线用：与 MySQL 实现共用 plan_write，行为一致。"""

    def __init__(self):
        self.patients: dict[str, dict] = {}
        self.facts: list[dict] = []
        self.links: dict[str, str] = {}
        self.cursors: dict[str, int] = {}
        self.jobs: list[dict] = []
        self._seq = 0

    def health(self):
        return {"ok": True, "latency_ms": 0, "detail": "memory"}

    def create_patient(self, **fields):
        pid = "pt" + uuid.uuid4().hex[:10]
        now = _now()
        row = {"patient_id": pid, "display_name": str(fields.get("display_name") or "")[:_MAX_NAME] or None,
               "sex": _valid_sex(fields.get("sex")), "birth_year": fields.get("birth_year"),
               "phone": fields.get("phone") or None, "id_no": fields.get("id_no") or None,
               "note": (str(fields.get("note") or "")[:500] or None),
               "status": "active", "merged_into": None, "created_at": now, "updated_at": now}
        self.patients[pid] = row
        return dict(row)

    def get_patient(self, patient_id):
        row = self.patients.get(patient_id)
        if not row or row.get("status") == "deleted":
            return None
        return dict(row)

    def list_patients(self, query=""):
        q = str(query or "").strip().lower()
        out = []
        for row in self.patients.values():
            if row.get("status") != "active":
                continue
            hay = " ".join(str(row.get(k) or "") for k in ("display_name", "phone", "id_no")).lower()
            if q and q not in hay:
                continue
            out.append(dict(row))
        return sorted(out, key=lambda r: r.get("updated_at") or "", reverse=True)[:50]

    def update_patient(self, patient_id, **fields):
        row = self.patients.get(patient_id)
        if not row or row.get("status") == "deleted":
            return None
        for key in ("display_name", "sex", "birth_year", "phone", "id_no", "note"):
            if key in fields:
                row[key] = _valid_sex(fields[key]) if key == "sex" else fields[key]
        row["updated_at"] = _now()
        return dict(row)

    def delete_patient(self, patient_id):
        row = self.patients.get(patient_id)
        if not row:
            return 0
        row["status"] = "deleted"
        row["updated_at"] = _now()
        n = 0
        for fact in self.facts:
            if fact["patient_id"] == patient_id and fact["status"] != "deleted":
                fact["status"] = "deleted"
                n += 1
        for cid in [c for c, p in self.links.items() if p == patient_id]:
            self.links.pop(cid, None)
        return n

    def merge_patients(self, source_id, target_id):
        if source_id not in self.patients or target_id not in self.patients:
            raise ValueError("patient not found")
        n = 0
        for fact in self.facts:
            if fact["patient_id"] == source_id and fact["status"] == "active":
                fact["patient_id"] = target_id
                n += 1
        for cid, pid in list(self.links.items()):
            if pid == source_id:
                self.links[cid] = target_id
        self.patients[source_id]["status"] = "merged"
        self.patients[source_id]["merged_into"] = target_id
        return n

    def link_case(self, case_id, patient_id):
        if patient_id not in self.patients:
            raise ValueError("patient not found")
        self.links[case_id] = patient_id
        return {"case_id": case_id, "patient_id": patient_id, "linked_at": _now()}

    def unlink_case(self, case_id):
        self.links.pop(case_id, None)

    def patient_for_case(self, case_id):
        return self.links.get(case_id)

    def cases_for_patient(self, patient_id):
        return [c for c, p in self.links.items() if p == patient_id]

    def active_facts(self, patient_id):
        return [_fact_out(f) for f in self.facts
                if f["patient_id"] == patient_id and f["status"] == "active"]

    def all_facts(self, patient_id):
        return [_fact_out(f) for f in self.facts
                if f["patient_id"] == patient_id and f["status"] != "deleted"]

    def write_facts(self, patient_id, facts, source, case_id="", msg_id=""):
        active = [f for f in self.facts if f["patient_id"] == patient_id and f["status"] == "active"]
        inserted = upgraded = disputed = skipped = 0
        for fact in validate_facts(facts, source):
            multi = _multi_valued(fact["category"])
            plan = plan_write(active, fact, source, multi)
            if not plan:
                skipped += 1
                continue
            action, fid = plan[0]
            if action == "upgrade":
                for row in self.facts:
                    if row["fact_id"] == fid:
                        row.update(value=fact["value"], normalized_value=normalize_value(fact["value"]),
                                   evidence=fact["evidence"], source=source,
                                   confidence=fact["confidence"], source_case_id=case_id,
                                   source_msg_id=msg_id, effective_from=_now())
                        upgraded += 1
                        break
                continue
            self._seq += 1
            new = {"fact_id": self._seq, "patient_id": patient_id, **fact,
                   "normalized_value": normalize_value(fact["value"]),
                   "source": source, "source_case_id": case_id, "source_msg_id": msg_id,
                   "status": "disputed" if action == "insert_disputed" else "active",
                   "effective_from": _now(), "superseded_by": None, "created_at": _now()}
            self.facts.append(new)
            if action == "insert_disputed":
                disputed += 1
                continue
            inserted += 1
            if not multi:
                for row in self.facts:
                    if (row["patient_id"] == patient_id and row["category"] == fact["category"]
                            and row["field"] == fact["field"] and row["status"] == "active"
                            and row["fact_id"] != new["fact_id"]):
                        row["status"] = "superseded"
                        row["superseded_by"] = new["fact_id"]
            active = [f for f in self.facts if f["patient_id"] == patient_id and f["status"] == "active"]
        return {"inserted": inserted, "upgraded": upgraded, "disputed": disputed, "skipped": skipped}

    def delete_fact(self, fact_id):
        for row in self.facts:
            if row["fact_id"] == fact_id:
                row["status"] = "deleted"
                return True
        return False

    def get_cursor(self, case_id):
        return int(self.cursors.get(case_id) or 0)

    def set_cursor(self, case_id, upto_seq):
        self.cursors[case_id] = int(upto_seq)

    def enqueue_job(self, case_id, from_seq, upto_seq):
        if any(j["case_id"] == case_id and j["upto_seq"] == upto_seq for j in self.jobs):
            return None
        jid = uuid.uuid4().hex[:32]
        self.jobs.append({"job_id": jid, "case_id": case_id, "from_seq": from_seq,
                          "upto_seq": upto_seq, "status": "queued", "attempts": 0,
                          "last_error": "", "created_at": _now(), "updated_at": _now()})
        return jid

    def pending_jobs(self):
        return [dict(j) for j in self.jobs if j["status"] in ("queued", "running")]

    def claim_job(self, job_id, stale_seconds: int = 600):
        now = datetime.now(timezone.utc)
        for j in self.jobs:
            if j["job_id"] != job_id:
                continue
            if j["status"] == "queued" or (
                    j["status"] == "running"
                    and _age_seconds(j.get("updated_at"), now) > stale_seconds):
                j["status"] = "running"
                j["attempts"] = int(j["attempts"]) + 1
                j["updated_at"] = _now()
                return True
        return False

    def finish_job(self, job_id, ok, error=""):
        for j in self.jobs:
            if j["job_id"] == job_id:
                j["status"] = "done" if ok else "failed"
                j["last_error"] = "" if ok else str(error)[:500]
                j["updated_at"] = _now()

    def last_job(self, case_id):
        rows = [j for j in self.jobs if j["case_id"] == case_id]
        return dict(rows[-1]) if rows else None

    def get_job(self, job_id):
        for row in self.jobs:
            if row["job_id"] == job_id:
                return dict(row)
        return None

    def purge_case(self, case_id):
        self.links.pop(case_id, None)
        self.cursors.pop(case_id, None)
        self.jobs = [j for j in self.jobs
                     if not (j["case_id"] == case_id and j["status"] in ("queued", "running"))]


class MySQLProfileStore(ProfileStore):
    def __init__(self, settings: dict | None = None):
        self.settings = dict(settings or config.mysql_settings())
        self.db = self.settings.get("database") or "dermagent"
        self._ensure_schema()

    # —— 连接 ——
    def _connect(self, with_db: bool = True):
        import pymysql
        from pymysql.cursors import DictCursor
        kw = {k: v for k, v in self.settings.items() if v not in ("", None)}
        kw.pop("database", None)
        conn = pymysql.connect(
            database=self.db if with_db else None,
            charset="utf8mb4", autocommit=False, cursorclass=DictCursor,
            connect_timeout=2, read_timeout=5, write_timeout=5,
            init_command="SET time_zone='+00:00'",   # 与 _now() 的 UTC 字符串一致，任务窗口判定才正确
            **kw)
        return conn

    def _ensure_schema(self):
        sql_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
        with open(sql_file, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if not ln.strip().startswith("--")]
        ddl = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
        conn = self._connect(with_db=False)
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.db}` "
                            "DEFAULT CHARACTER SET utf8mb4")
                cur.execute(f"USE `{self.db}`")
                for stmt in ddl:
                    cur.execute(stmt)
            conn.commit()
        finally:
            conn.close()

    def health(self):
        t0 = time.perf_counter()
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            finally:
                conn.close()
            return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "detail": "mysql"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": str(exc)[:160]}

    # —— 患者 ——
    def create_patient(self, **fields):
        pid = "pt" + uuid.uuid4().hex[:10]
        now = _now()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO patients (patient_id, display_name, sex, birth_year, phone, id_no, note,"
                    " status, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)",
                    (pid, (str(fields.get("display_name") or "")[:_MAX_NAME] or None),
                     _valid_sex(fields.get("sex")), fields.get("birth_year"),
                     fields.get("phone") or None, fields.get("id_no") or None,
                     (str(fields.get("note") or "")[:500] or None), now, now))
            conn.commit()
        finally:
            conn.close()
        return self.get_patient(pid)

    def _patient_row(self, cur, patient_id):
        cur.execute("SELECT * FROM patients WHERE patient_id=%s AND status<>'deleted'", (patient_id,))
        return cur.fetchone()

    def get_patient(self, patient_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                row = self._patient_row(cur, patient_id)
            return dict(row) if row else None
        finally:
            conn.close()

    def list_patients(self, query=""):
        q = str(query or "").strip()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                if q:
                    like = f"%{q}%"
                    cur.execute("SELECT * FROM patients WHERE status='active' AND (display_name LIKE %s"
                                " OR phone LIKE %s OR id_no LIKE %s) ORDER BY updated_at DESC LIMIT 50",
                                (like, like, like))
                else:
                    cur.execute("SELECT * FROM patients WHERE status='active'"
                                " ORDER BY updated_at DESC LIMIT 50")
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def update_patient(self, patient_id, **fields):
        allowed = {k: fields[k] for k in ("display_name", "sex", "birth_year", "phone", "id_no", "note")
                   if k in fields}
        if "sex" in allowed:
            allowed["sex"] = _valid_sex(allowed["sex"])
        if not allowed:
            return self.get_patient(patient_id)
        sets = ", ".join(f"`{k}`=%s" for k in allowed)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE patients SET {sets}, updated_at=%s WHERE patient_id=%s AND status<>'deleted'",
                            (*allowed.values(), _now(), patient_id))
            conn.commit()
        finally:
            conn.close()
        return self.get_patient(patient_id)

    def delete_patient(self, patient_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE patient_facts SET status='deleted' WHERE patient_id=%s AND status<>'deleted'",
                            (patient_id,))
                n = cur.rowcount
                cur.execute("DELETE FROM case_memory_cursor WHERE case_id IN"
                            " (SELECT case_id FROM case_patients WHERE patient_id=%s)", (patient_id,))
                cur.execute("DELETE FROM case_patients WHERE patient_id=%s", (patient_id,))
                cur.execute("UPDATE patients SET status='deleted', updated_at=%s WHERE patient_id=%s",
                            (_now(), patient_id))
            conn.commit()
            return int(n)
        finally:
            conn.close()

    def merge_patients(self, source_id, target_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE patient_facts SET patient_id=%s WHERE patient_id=%s AND status='active'",
                            (target_id, source_id))
                n = cur.rowcount
                cur.execute("UPDATE case_patients SET patient_id=%s WHERE patient_id=%s",
                            (target_id, source_id))
                cur.execute("UPDATE patients SET status='merged', merged_into=%s, updated_at=%s"
                            " WHERE patient_id=%s", (target_id, _now(), source_id))
            conn.commit()
            return int(n)
        finally:
            conn.close()

    # —— 病例关联 ——
    def link_case(self, case_id, patient_id):
        now = _now()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO case_patients (case_id, patient_id, linked_at, linked_by)"
                            " VALUES (%s,%s,%s,'doctor') ON DUPLICATE KEY UPDATE"
                            " patient_id=VALUES(patient_id), linked_at=VALUES(linked_at)",
                            (case_id, patient_id, now))
                cur.execute("INSERT INTO case_memory_cursor (case_id, upto_seq, updated_at)"
                            " VALUES (%s,0,%s) ON DUPLICATE KEY UPDATE updated_at=VALUES(updated_at)",
                            (case_id, now))
            conn.commit()
        finally:
            conn.close()
        return {"case_id": case_id, "patient_id": patient_id, "linked_at": now}

    def unlink_case(self, case_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM case_patients WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM case_memory_cursor WHERE case_id=%s", (case_id,))
            conn.commit()
        finally:
            conn.close()

    def patient_for_case(self, case_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT patient_id FROM case_patients WHERE case_id=%s", (case_id,))
                row = cur.fetchone()
            return row["patient_id"] if row else None
        finally:
            conn.close()

    def cases_for_patient(self, patient_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT case_id FROM case_patients WHERE patient_id=%s", (patient_id,))
                return [r["case_id"] for r in cur.fetchall()]
        finally:
            conn.close()

    # —— 事实 ——
    def _facts(self, patient_id, statuses):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                ph = ",".join(["%s"] * len(statuses))
                cur.execute(f"SELECT * FROM patient_facts WHERE patient_id=%s AND status IN ({ph})"
                            " ORDER BY fact_id", (patient_id, *statuses))
                return [_fact_out(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def active_facts(self, patient_id):
        return self._facts(patient_id, ("active",))

    def all_facts(self, patient_id):
        return self._facts(patient_id, ("active", "superseded", "disputed"))

    def write_facts(self, patient_id, facts, source, case_id="", msg_id=""):
        facts = validate_facts(facts, source)
        if not facts:
            return {"inserted": 0, "upgraded": 0, "disputed": 0, "skipped": 0}
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM patient_facts WHERE patient_id=%s AND status='active'",
                            (patient_id,))
                active = [_fact_out(r) for r in cur.fetchall()]
                inserted = upgraded = disputed = skipped = 0
                for fact in facts:
                    multi = _multi_valued(fact["category"])
                    plan = plan_write(active, fact, source, multi)
                    if not plan:
                        skipped += 1
                        continue
                    action, fid = plan[0]
                    now = _now()
                    if action == "upgrade":
                        cur.execute(
                            "UPDATE patient_facts SET value=%s, normalized_value=%s, evidence=%s, source=%s,"
                            " confidence=%s, source_case_id=%s, source_msg_id=%s, effective_from=%s"
                            " WHERE fact_id=%s",
                            (fact["value"], normalize_value(fact["value"]), fact["evidence"], source,
                             fact["confidence"], case_id or None, msg_id or None, now, fid))
                        upgraded += 1
                    else:
                        cur.execute(
                            "INSERT INTO patient_facts (patient_id, category, field, value, normalized_value,"
                            " evidence, source, confidence, source_case_id, source_msg_id, status,"
                            " effective_from, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (patient_id, fact["category"], fact["field"], fact["value"],
                             normalize_value(fact["value"]), fact["evidence"], source, fact["confidence"],
                             case_id or None, msg_id or None,
                             "disputed" if action == "insert_disputed" else "active", now, now))
                        new_id = cur.lastrowid
                        if action == "insert_disputed":
                            disputed += 1
                        else:
                            inserted += 1
                            if not multi:
                                cur.execute(
                                    "UPDATE patient_facts SET status='superseded', superseded_by=%s"
                                    " WHERE patient_id=%s AND category=%s AND field=%s"
                                    " AND status='active' AND fact_id<>%s",
                                    (new_id, patient_id, fact["category"], fact["field"], new_id))
                    cur.execute("SELECT * FROM patient_facts WHERE patient_id=%s AND status='active'",
                                (patient_id,))
                    active = [_fact_out(r) for r in cur.fetchall()]
            conn.commit()
            return {"inserted": inserted, "upgraded": upgraded, "disputed": disputed, "skipped": skipped}
        finally:
            conn.close()

    def delete_fact(self, fact_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE patient_facts SET status='deleted' WHERE fact_id=%s", (fact_id,))
                n = cur.rowcount
            conn.commit()
            return bool(n)
        finally:
            conn.close()

    # —— 游标 / 任务 ——
    def get_cursor(self, case_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT upto_seq FROM case_memory_cursor WHERE case_id=%s", (case_id,))
                row = cur.fetchone()
            return int(row["upto_seq"]) if row else 0
        finally:
            conn.close()

    def set_cursor(self, case_id, upto_seq):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO case_memory_cursor (case_id, upto_seq, updated_at)"
                            " VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE"
                            " upto_seq=GREATEST(upto_seq, VALUES(upto_seq)), updated_at=VALUES(updated_at)",
                            (case_id, int(upto_seq), _now()))
            conn.commit()
        finally:
            conn.close()

    def enqueue_job(self, case_id, from_seq, upto_seq):
        jid = uuid.uuid4().hex[:32]
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT IGNORE INTO memory_jobs (job_id, case_id, from_seq, upto_seq, status,"
                            " attempts, last_error, created_at, updated_at)"
                            " VALUES (%s,%s,%s,%s,'queued',0,'',%s,%s)",
                            (jid, case_id, int(from_seq), int(upto_seq), _now(), _now()))
                created = cur.rowcount > 0
            conn.commit()
            return jid if created else None
        finally:
            conn.close()

    def pending_jobs(self):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM memory_jobs WHERE status IN ('queued','running')"
                            " AND updated_at > (NOW(3) - INTERVAL 10 MINUTE)"
                            " ORDER BY created_at LIMIT 100")
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def claim_job(self, job_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE memory_jobs SET status='running', attempts=attempts+1, updated_at=%s"
                            " WHERE job_id=%s AND (status='queued' OR (status='running'"
                            " AND updated_at < (NOW(3) - INTERVAL 10 MINUTE)))",
                            (_now(), job_id))
                ok = cur.rowcount > 0
            conn.commit()
            return bool(ok)
        finally:
            conn.close()

    def finish_job(self, job_id, ok, error=""):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE memory_jobs SET status=%s, last_error=%s, updated_at=%s WHERE job_id=%s",
                            ("done" if ok else "failed", "" if ok else str(error)[:500], _now(), job_id))
            conn.commit()
        finally:
            conn.close()

    def last_job(self, case_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM memory_jobs WHERE case_id=%s ORDER BY created_at DESC LIMIT 1",
                            (case_id,))
                row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_job(self, job_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM memory_jobs WHERE job_id=%s", (job_id,))
                row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def purge_case(self, case_id):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM case_memory_cursor WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM case_patients WHERE case_id=%s", (case_id,))
                cur.execute("DELETE FROM memory_jobs WHERE case_id=%s"
                            " AND status IN ('queued','running')", (case_id,))
            conn.commit()
        finally:
            conn.close()
