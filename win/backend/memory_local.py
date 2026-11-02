# -*- coding: utf-8 -*-
"""桌面离线记忆（mock 模式）：SQLite 患者事实 + JSONL 情景切片 + 2-gram Dice 召回。

与服务端 memory 包同一端口语义（窗口 / 患者事实 / 情景 / 健康），但完全本地：
- 无 Redis：窗口直接落 SQLite（最近 20 轮），失败时调用方可回落会话消息；
- 无 MySQL：事实落 SQLite，来源只用 case_import / doctor_modified（不伪造 llm_extract）；
- 无 Milvus / BGE：情景切片落 JSONL，召回用 2-gram Dice 词法打分，health 标 degraded。

身份字段（姓名/电话）只允许存在于本地 SQLite 与界面；情景切片写入前做去标识。
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(os.path.dirname(HERE), "frontend", "data")

SOURCE_PRIORITY = {"doctor_modified": 4, "doctor_input": 3, "case_import": 2, "llm_extract": 1}
_MULTI = {"allergy", "medication", "family_history", "diagnosis_history", "lab"}
_MAX_WINDOW = 40

ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
NAME_RE = re.compile(r"((?:患者)?姓名)\s*[:：]\s*([\u4e00-\u9fa5A-Za-z·]{1,20})")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
  patient_id TEXT PRIMARY KEY, display_name TEXT DEFAULT '', sex TEXT DEFAULT 'unknown',
  birth_year INTEGER, note TEXT DEFAULT '', status TEXT DEFAULT 'active',
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS facts (
  fact_id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL,
  category TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL,
  normalized_value TEXT NOT NULL, evidence TEXT DEFAULT '', source TEXT NOT NULL,
  status TEXT DEFAULT 'active', superseded_by INTEGER, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_facts ON facts(patient_id, category, field, status);
CREATE TABLE IF NOT EXISTS case_patients (
  case_id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, linked_at TEXT);
CREATE TABLE IF NOT EXISTS messages (
  case_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  message_id TEXT DEFAULT '', task_type TEXT DEFAULT '', at TEXT,
  PRIMARY KEY (case_id, seq));
CREATE TABLE IF NOT EXISTS summaries (case_id TEXT PRIMARY KEY, data TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()[:255]


def redact(text: str) -> str:
    """情景切片去标识：证件/手机/姓名占位（与 server/memory/redact.py 同口径）。"""
    out = str(text or "")
    out = ID_RE.sub("[身份证]", out)
    out = PHONE_RE.sub("[手机号]", out)
    out = NAME_RE.sub(lambda m: f"{m.group(1)}：[患者]", out)
    return out


def _bigrams(text: str) -> set:
    t = re.sub(r"[\s\W_]+", "", str(text or "").lower())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _dice(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _lexical_score(query: set, text: set) -> float:
    """词法相关度：Dice 与「查询覆盖率」各半。

    纯 Dice 对长切片惩罚过重（无关内容也会因为分母大而接近 0），
    覆盖率高说明问题关键词确实出现在该切片里；两者平均在中文短问句上更稳。
    """
    if not query or not text:
        return 0.0
    return 0.5 * _dice(query, text) + 0.5 * (len(query & text) / len(query))


class LocalMemory:
    """mock 模式记忆层：四个端口与 server 同语义，任何异常都只降级不抛出。"""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir or os.environ.get("DERM_DATA_DIR") or DEFAULT_DIR
        self._memory_only = self.data_dir == ":memory:"
        if not self._memory_only:
            os.makedirs(self.data_dir, exist_ok=True)
        self._db = ":memory:" if self._memory_only else os.path.join(self.data_dir, "memory.sqlite3")
        self._epi_path = "" if self._memory_only else os.path.join(self.data_dir, "episodic.jsonl")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # —— 连接 / 健康 ——

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def health(self) -> dict:
        out = {"ok": True, "latency_ms": 0, "detail": "local-sqlite",
               "short_term": {"ok": True, "latency_ms": 0, "detail": "local-sqlite"},
               "profile": {"ok": True, "latency_ms": 0, "detail": "local-sqlite"},
               "episodic": {"ok": True, "latency_ms": 0, "detail": "local-lexical-degraded"},
               "worker": {"ok": False, "detail": "local_extract_unsupported"}}
        out["degraded"] = {"short_term": False, "profile": False, "episodic": True, "worker": True}
        return out

    def _rows(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # —— 短期窗口（SQLite 代替 Redis） ——

    def append(self, case_id: str, role: str, content: str, message_id: str = "",
               task_type: str = "", at: str = "") -> int:
        cid = str(case_id or "")
        if not cid:
            return 0
        with self._lock:
            seq = int(self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE case_id = ?", (cid,)).fetchone()[0]) + 1
            self._conn.execute(
                "INSERT INTO messages (case_id, seq, role, content, message_id, task_type, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, seq, role, str(content or "")[:4000], message_id, task_type, at or _now()))
            self._conn.execute(
                "DELETE FROM messages WHERE case_id = ? AND seq <= ?",
                (cid, seq - _MAX_WINDOW))
            self._conn.commit()
        return seq

    def window(self, case_id: str, rounds: int = 20) -> list:
        limit = max(2, int(rounds) * 2)
        rows = self._rows(
            "SELECT seq, role, content, message_id, task_type, at FROM messages "
            "WHERE case_id = ? ORDER BY seq DESC LIMIT ?", (str(case_id or ""), limit))
        return list(reversed(rows))

    def set_summary(self, case_id: str, data: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO summaries (case_id, data) VALUES (?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET data = excluded.data",
                (str(case_id or ""), json.dumps(data or {}, ensure_ascii=False)))
            self._conn.commit()

    def summary(self, case_id: str):
        rows = self._rows("SELECT data FROM summaries WHERE case_id = ?", (str(case_id or ""),))
        if not rows:
            return None
        try:
            return json.loads(rows[0]["data"])
        except ValueError:
            return None

    # —— 患者档案 ——

    @staticmethod
    def _patient_out(row: dict) -> dict:
        return {"patient_id": row.get("patient_id"), "display_name": row.get("display_name") or "",
                "sex": row.get("sex") or "unknown", "birth_year": row.get("birth_year"),
                "note": row.get("note") or "", "status": row.get("status") or "active",
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at")}

    def ensure_patient(self, case_id: str, label: str = "") -> dict:
        """mock 每病例自动建匿名患者 pt_<case_id>（跨病例合并只在 server 模式提供）。"""
        cid = str(case_id or "")
        if not cid:
            return {}
        linked = self.patient_for_case(cid)
        if linked:
            return linked
        pid = ("pt_" + re.sub(r"[^0-9A-Za-z_-]", "", cid))[:12]
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO patients (patient_id, display_name, sex, status, created_at, updated_at) "
                "VALUES (?, ?, 'unknown', 'active', ?, ?)",
                (pid, (label or "")[:64], now, now))
            self._conn.execute(
                "INSERT INTO case_patients (case_id, patient_id, linked_at) VALUES (?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET patient_id = excluded.patient_id, linked_at = excluded.linked_at",
                (cid, pid, now))
            self._conn.commit()
        return self.patient_for_case(cid) or {}

    def create_patient(self, fields: dict) -> dict:
        fields = fields or {}
        pid = "pt_" + hashlib.sha1(f"{_now()}{id(fields)}".encode()).hexdigest()[:10]
        now = _now()
        sex = str(fields.get("sex") or "unknown").lower()
        if sex not in ("male", "female", "other", "unknown"):
            sex = "unknown"
        year = fields.get("birth_year")
        try:
            year = int(year) if year not in (None, "") else None
        except (TypeError, ValueError):
            year = None
        with self._lock:
            self._conn.execute(
                "INSERT INTO patients (patient_id, display_name, sex, birth_year, note, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                (pid, str(fields.get("display_name") or "")[:64], sex, year,
                 str(fields.get("note") or "")[:500], now, now))
            self._conn.commit()
        return self.get_patient(pid)

    def list_patients(self, query: str = "") -> list:
        q = str(query or "").strip()
        if q:
            rows = self._rows(
                "SELECT * FROM patients WHERE status = 'active' AND (display_name LIKE ? OR patient_id LIKE ?) "
                "ORDER BY updated_at DESC LIMIT 50", (f"%{q}%", f"%{q}%"))
        else:
            rows = self._rows("SELECT * FROM patients WHERE status = 'active' ORDER BY updated_at DESC LIMIT 50")
        return [self._patient_out(r) for r in rows]

    def get_patient(self, patient_id: str) -> dict:
        rows = self._rows("SELECT * FROM patients WHERE patient_id = ?", (str(patient_id or ""),))
        if not rows:
            raise KeyError("patient not found")
        return self._patient_out(rows[0])

    def update_patient(self, patient_id: str, fields: dict) -> dict:
        p = self.get_patient(patient_id)
        fields = fields or {}
        name = str(fields.get("display_name", p["display_name"]) or "")[:64]
        sex = str(fields.get("sex", p["sex"]) or "unknown").lower()
        if sex not in ("male", "female", "other", "unknown"):
            sex = p["sex"]
        note = str(fields.get("note", p["note"]) or "")[:500]
        year = p.get("birth_year")
        if "birth_year" in fields:
            raw = fields.get("birth_year")
            if raw in (None, ""):
                year = None
            else:
                try:
                    year = int(raw)
                except (TypeError, ValueError):
                    year = p.get("birth_year")
                else:
                    if not 1900 <= year <= 2100:
                        year = p.get("birth_year")
        with self._lock:
            self._conn.execute(
                "UPDATE patients SET display_name = ?, sex = ?, birth_year = ?, note = ?, "
                "updated_at = ? WHERE patient_id = ?",
                (name, sex, year, note, _now(), p["patient_id"]))
            self._conn.commit()
        return self.get_patient(p["patient_id"])

    def patient_for_case(self, case_id: str):
        rows = self._rows(
            "SELECT p.* FROM case_patients c JOIN patients p ON p.patient_id = c.patient_id "
            "WHERE c.case_id = ? AND p.status = 'active'", (str(case_id or ""),))
        return self._patient_out(rows[0]) if rows else None

    def patient_brief(self, case_id: str):
        p = self.patient_for_case(case_id)
        if not p:
            return None
        out = {"patient_id": p["patient_id"], "display_name": p["display_name"], "sex": p["sex"]}
        try:
            if p.get("birth_year"):
                out["age"] = max(0, datetime.now(timezone.utc).year - int(p["birth_year"]))
        except (TypeError, ValueError):
            pass
        return out

    def link_case(self, case_id: str, patient_id: str) -> dict:
        self.get_patient(patient_id)
        with self._lock:
            self._conn.execute(
                "INSERT INTO case_patients (case_id, patient_id, linked_at) VALUES (?, ?, ?) "
                "ON CONFLICT(case_id) DO UPDATE SET patient_id = excluded.patient_id, linked_at = excluded.linked_at",
                (str(case_id), str(patient_id), _now()))
            self._conn.commit()
        return {"ok": True, "case_id": case_id, "patient_id": patient_id}

    def unlink_case(self, case_id: str) -> dict:
        with self._lock:
            self._conn.execute("DELETE FROM case_patients WHERE case_id = ?", (str(case_id),))
            self._conn.commit()
        return {"ok": True, "case_id": case_id, "patient_id": ""}

    def facts(self, patient_id: str, active_only: bool = True) -> list:
        sql = "SELECT * FROM facts WHERE patient_id = ?"
        if active_only:
            sql += " AND status = 'active'"
        return self._rows(sql + " ORDER BY fact_id", (str(patient_id or ""),))

    def add_fact(self, patient_id: str, fact: dict, source: str = "doctor_modified") -> dict:
        self.get_patient(patient_id)
        fact = fact or {}
        category = str(fact.get("category") or "")
        field = str(fact.get("field") or "")
        value = str(fact.get("value") or "").strip()[:200]
        if not category or not field or not value:
            raise ValueError("category/field/value 不能为空")
        src = source if source in SOURCE_PRIORITY else "doctor_input"
        norm = _norm(value)
        now = _now()
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM facts WHERE patient_id = ? AND category = ? AND field = ? AND status = 'active'",
                (str(patient_id), category, field)).fetchall()]
            dup = [r for r in rows if r["normalized_value"] == norm]
            if dup:
                row = dup[0]
                # 与 server plan_write 同口径：同值重复不新增，但更高来源要升级来源/状态
                if SOURCE_PRIORITY[src] > SOURCE_PRIORITY.get(row["source"], 0):
                    self._conn.execute(
                        "UPDATE facts SET source = ?, status = 'active', created_at = ? WHERE fact_id = ?",
                        (src, now, row["fact_id"]))
                    self._conn.execute("UPDATE patients SET updated_at = ? WHERE patient_id = ?",
                                       (now, str(patient_id)))
                    self._conn.commit()
                    return {"ok": True, "inserted": 0, "fact_id": row["fact_id"],
                            "status": "active", "upgraded": True}
                return {"ok": True, "inserted": 0, "fact_id": row["fact_id"], "status": "active"}
            status = "active"
            if category not in _MULTI:
                best = max((SOURCE_PRIORITY.get(r["source"], 0) for r in rows), default=0)
                status = "active" if SOURCE_PRIORITY[src] >= best else "disputed"
                cur = self._conn.execute(
                    "INSERT INTO facts (patient_id, category, field, value, normalized_value, evidence, "
                    "source, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(patient_id), category, field, value, norm,
                     str(fact.get("evidence") or "")[:500], src, status, now))
                if status == "active":
                    for r in rows:
                        self._conn.execute(
                            "UPDATE facts SET status = 'superseded', superseded_by = ? WHERE fact_id = ?",
                            (cur.lastrowid, r["fact_id"]))
            else:
                cur = self._conn.execute(
                    "INSERT INTO facts (patient_id, category, field, value, normalized_value, evidence, "
                    "source, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                    (str(patient_id), category, field, value, norm,
                     str(fact.get("evidence") or "")[:500], src, now))
            self._conn.execute("UPDATE patients SET updated_at = ? WHERE patient_id = ?", (now, str(patient_id)))
            self._conn.commit()
        return {"ok": True, "inserted": 1, "fact_id": cur.lastrowid, "status": status}

    def delete_fact(self, patient_id: str, fact_id) -> dict:
        try:
            fid = int(fact_id)
        except (TypeError, ValueError):
            raise ValueError("fact_id 无效") from None
        with self._lock:
            cur = self._conn.execute(
                "UPDATE facts SET status = 'deleted' WHERE fact_id = ? AND patient_id = ?",
                (fid, str(patient_id or "")))
            self._conn.commit()
        if not cur.rowcount:
            raise KeyError("fact not found")
        return {"ok": True, "deleted": 1}

    def import_case_facts(self, case_id: str, fields: dict) -> dict:
        """病例记录字段 → case_import 事实（mock 无 LLM 抽取，只搬运医生已填内容）。"""
        patient = self.patient_for_case(case_id)
        if not patient:
            return {"ok": False, "imported": 0}
        mapping = [("allergy_history", "allergy", "allergen"), ("past_history", "past_history", "disease"),
                   ("medication_history", "medication", "name"), ("personal_history", "personal_history", "occupation")]
        imported = 0
        for key, cat, field in mapping:
            val = str((fields or {}).get(key) or "").strip()
            if not val or val in ("无", "否认", "未提供"):
                continue
            res = self.add_fact(patient["patient_id"], {"category": cat, "field": field, "value": val[:200],
                                                        "evidence": "病例记录：" + key}, source="case_import")
            imported += int(res.get("inserted") or 0)
        return {"ok": True, "imported": imported}

    def delete_patient(self, patient_id: str) -> dict:
        self.get_patient(patient_id)
        pid = str(patient_id)
        with self._lock:
            cases = [r["case_id"] for r in self._conn.execute(
                "SELECT case_id FROM case_patients WHERE patient_id = ?", (pid,)).fetchall()]
            self._conn.execute("UPDATE facts SET status = 'deleted' WHERE patient_id = ?", (pid,))
            self._conn.execute("UPDATE patients SET status = 'deleted', updated_at = ? WHERE patient_id = ?",
                               (_now(), pid))
            self._conn.execute("DELETE FROM case_patients WHERE patient_id = ?", (pid,))
            for cid in cases:                 # 计划 §9：删患者连带清理其病例窗口与摘要
                self._conn.execute("DELETE FROM messages WHERE case_id = ?", (cid,))
                self._conn.execute("DELETE FROM summaries WHERE case_id = ?", (cid,))
            self._conn.commit()
        self.episodic_delete(patient_id=pid)
        for cid in cases:
            self.episodic_delete(case_id=cid)
        return {"ok": True, "patient_id": pid, "cases": cases, "deleted": 1}

    def merge(self, source_id: str, target_id: str) -> dict:
        if str(source_id) == str(target_id):
            raise ValueError("不能合并到自身")
        self.get_patient(source_id)
        self.get_patient(target_id)
        src, dst = str(source_id), str(target_id)
        with self._lock:
            self._conn.execute("UPDATE facts SET patient_id = ? WHERE patient_id = ? AND status = 'active'",
                               (dst, src))
            self._conn.execute("UPDATE case_patients SET patient_id = ? WHERE patient_id = ?", (dst, src))
            self._conn.execute("UPDATE patients SET status = 'merged', updated_at = ? WHERE patient_id = ?",
                               (_now(), src))
            self._conn.commit()
        return {"ok": True, "source_id": src, "target_id": dst, "merged": 1}

    # —— 情景（JSONL + 词法召回） ——

    def _chunks(self) -> list:
        if not self._epi_path or not os.path.exists(self._epi_path):
            return []
        out = []
        try:
            with open(self._epi_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        out.append(row)
        except OSError:
            return []
        return out

    def episodic_write(self, chunk: dict) -> bool:
        chunk = dict(chunk or {})
        cid = str(chunk.get("chunk_id") or "")
        if not cid:
            cid = hashlib.sha1(f"{chunk.get('case_id')}|{chunk.get('scope')}|"
                               f"{chunk.get('msg_id')}|{chunk.get('text')}".encode()).hexdigest()[:32]
            chunk["chunk_id"] = cid
        existing = {c.get("chunk_id") for c in self._chunks()}
        if cid in existing:
            return False
        chunk["text"] = redact(str(chunk.get("text") or ""))[:1200]
        chunk["title"] = redact(str(chunk.get("title") or ""))[:64]
        chunk["created_at"] = chunk.get("created_at") or int(datetime.now(timezone.utc).timestamp())
        if not self._epi_path:
            return False
        try:
            with self._lock, open(self._epi_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False

    def episodic_delete(self, case_id: str = "", patient_id: str = "") -> int:
        if not self._epi_path or not os.path.exists(self._epi_path):
            return 0
        doomed = set()
        kept = []
        for chunk in self._chunks():
            if ((case_id and chunk.get("case_id") == case_id and chunk.get("scope") == "case")
                    or (patient_id and chunk.get("patient_id") == patient_id)):
                doomed.add(chunk.get("chunk_id"))
            else:
                kept.append(chunk)
        if not doomed:
            return 0
        try:
            with self._lock, open(self._epi_path, "w", encoding="utf-8") as fh:
                for chunk in kept:
                    fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")
        except OSError:
            return 0
        return len(doomed)

    def recall(self, query: str, case_id: str = "", patient_id: str = "", exclude_msg_ids=None,
               top_k_case: int = 3, top_k_doctor: int = 3, threshold: float = 0.25,
               max_injected: int = 5) -> list:
        excluded = {str(x) for x in (exclude_msg_ids or []) if x}
        q = _bigrams(query)
        scored = []
        for chunk in self._chunks():
            if str(chunk.get("msg_id") or "") in excluded:
                continue
            if chunk.get("scope") == "case" and case_id and chunk.get("case_id") != case_id:
                continue
            score = _lexical_score(q, _bigrams(str(chunk.get("text") or "") + " " + str(chunk.get("title") or "")))
            if score < threshold:
                continue
            scored.append((score, chunk))
        scored.sort(key=lambda kv: -kv[0])
        case_hits = [x for x in scored if x[1].get("scope") == "case"][:max(0, top_k_case)]
        doctor_hits = [x for x in scored if x[1].get("scope") != "case"][:max(0, top_k_doctor)]
        out, seen = [], set()
        for score, chunk in sorted(case_hits + doctor_hits, key=lambda kv: -kv[0]):
            cid = chunk.get("chunk_id")
            if cid in seen:
                continue
            seen.add(cid)
            out.append({**{k: chunk.get(k) for k in
                           ("chunk_id", "scope", "case_id", "patient_id", "msg_id",
                            "title", "text", "created_at")}, "score": round(score, 4)})
            if len(out) >= max_injected:
                break
        return out

    def episodic_count(self, case_id: str = "", patient_id: str = "") -> int:
        return sum(1 for c in self._chunks()
                   if (not case_id or (c.get("case_id") == case_id and c.get("scope") == "case"))
                   and (not patient_id or c.get("patient_id") == patient_id))
