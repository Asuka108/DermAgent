# -*- coding: utf-8 -*-
"""患者档案服务：建档/关联/合并/删除/手工事实（医生入口，来源优先 doctor_modified）。"""
from .profile import ProfileStore

_FIELDS = ("display_name", "sex", "birth_year", "phone", "id_no", "note")


def _clean(fields: dict) -> dict:
    out = {}
    for key in _FIELDS:
        if key not in (fields or {}):
            continue
        val = fields[key]
        if key == "birth_year":
            if val in (None, ""):
                out[key] = None          # MySQL INT 列不接受 ''（严格模式 1366）
                continue
            try:
                year = int(val)
            except (TypeError, ValueError):
                continue
            if 1900 <= year <= 2100:
                out[key] = year
            continue
        out[key] = str(val or "").strip()[:500]
    return out


class PatientService:
    def __init__(self, store: ProfileStore, audit=None):
        self.store = store
        self.audit = audit

    def _log(self, event: str, detail) -> None:
        if callable(self.audit):
            try:
                self.audit(event, detail)
            except Exception:  # noqa: BLE001 — 审计失败不影响业务
                pass

    def create(self, fields: dict) -> dict:
        row = self.store.create_patient(**_clean(fields))
        self._log("patient_create", {"patient_id": row.get("patient_id")})
        return row

    def list(self, query: str = "") -> list:
        return self.store.list_patients(str(query or "")[:64])

    def get(self, patient_id: str) -> dict:
        row = self.store.get_patient(patient_id)
        if not row:
            raise KeyError("patient not found")
        return row

    def update(self, patient_id: str, fields: dict) -> dict:
        self.get(patient_id)
        row = self.store.update_patient(patient_id, **_clean(fields))
        if not row:
            raise KeyError("patient not found")
        self._log("patient_update", {"patient_id": patient_id, "keys": sorted(_clean(fields))})
        return row

    def delete(self, patient_id: str) -> dict:
        self.get(patient_id)
        cases = self.store.cases_for_patient(patient_id)
        n = self.store.delete_patient(patient_id)
        self._log("patient_delete", {"patient_id": patient_id, "facts": n, "cases": len(cases)})
        return {"ok": True, "patient_id": patient_id, "facts_deleted": n, "cases": cases}

    def merge(self, source_id: str, target_id: str) -> dict:
        if source_id == target_id:
            raise ValueError("来源与目标患者相同")
        self.get(source_id)
        self.get(target_id)
        n = self.store.merge_patients(source_id, target_id)
        self._log("patient_merge", {"source": source_id, "target": target_id, "facts": n})
        return {"ok": True, "source": source_id, "target": target_id, "facts_moved": n}

    # —— 病例关联 ——
    def link_case(self, case_id: str, patient_id: str) -> dict:
        self.get(patient_id)
        out = self.store.link_case(case_id, patient_id)
        self._log("patient_link", {"case_id": case_id, "patient_id": patient_id})
        return out

    def unlink_case(self, case_id: str) -> dict:
        self.store.unlink_case(case_id)
        self._log("patient_unlink", {"case_id": case_id})
        return {"ok": True, "case_id": case_id}

    def patient_for_case(self, case_id: str):
        return self.store.patient_for_case(case_id)

    # —— 事实 ——
    def facts(self, patient_id: str, active_only: bool = False) -> list:
        self.get(patient_id)
        return self.store.active_facts(patient_id) if active_only else self.store.all_facts(patient_id)

    def add_fact(self, patient_id: str, fact: dict) -> dict:
        """医生手工录入/修正：来源 doctor_modified，优先级最高。"""
        self.get(patient_id)
        fact = dict(fact or {})
        fact.setdefault("confidence", 1.0)
        out = self.store.write_facts(patient_id, [fact], "doctor_modified")
        self._log("memory_profile_write", {"patient_id": patient_id, "source": "doctor_modified",
                                           "category": str(fact.get("category") or "")[:32],
                                           **out})
        return out

    def delete_fact(self, patient_id: str, fact_id: int) -> dict:
        """删除前校验归属：fact_id 必须属于该患者（接口里的 patient_id 不可被绕过）。"""
        self.get(patient_id)
        fid = int(fact_id)
        owned = any(int(f.get("fact_id") or 0) == fid for f in self.store.all_facts(patient_id))
        if not owned:
            raise KeyError("fact not found")
        ok = self.store.delete_fact(fid)
        if not ok:
            raise KeyError("fact not found")
        self._log("patient_fact_delete", {"patient_id": patient_id, "fact_id": fid})
        return {"ok": True, "fact_id": fid}
