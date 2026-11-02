# -*- coding: utf-8 -*-
"""记忆层自检与清理（计划 §3.4 / §9）。

用法（在 web/backend 下）：
    python tools/memory_check.py                  # 三层状态 + 计数（只读）
    python tools/memory_check.py --case <id>      # 追加该病例窗口/患者/最近任务
    python tools/memory_check.py --purge-case <id> --yes
    python tools/memory_check.py --purge-patient <id> --yes
    python tools/memory_check.py --json

输出只含状态、计数与 ID，不含事实正文与身份字段；清理按 §9 删除矩阵级联。
退出码：0 正常；2 参数/环境问题（如清理时患者库不可用或对象不存在）。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import memory as memory_mod                       # noqa: E402
from memory import config as mem_config           # noqa: E402


def _audit(event: str, detail="") -> None:
    path = os.path.join(mem_config.data_dir(), "audit.jsonl")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event,
                                 "detail": body[:500]}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load_sessions() -> dict:
    path = os.path.join(mem_config.data_dir(), "sessions.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _wire() -> None:
    sessions = _load_sessions()
    memory_mod.configure(reader=lambda cid: sessions.get(cid) or {}, audit=_audit)
    if memory_mod.enabled():
        memory_mod.use_milvus()          # 惰性连接；未就绪时 delete/health 自行降级


def _counts(case_id: str) -> dict:
    out = {"sessions": len(_load_sessions())}
    store = memory_mod.profile() if memory_mod.enabled() else None
    if store is not None:
        try:
            out["patients"] = len(store.list_patients())
            out["pending_jobs"] = len(store.pending_jobs())
        except Exception as exc:  # noqa: BLE001
            out["profile_error"] = str(exc)[:120]
    if case_id and memory_mod.enabled():
        try:
            msgs, degraded = memory_mod.short_term().window(case_id)
            out["case"] = {"case_id": case_id, "window": len(msgs),
                           "short_term_degraded": bool(degraded)}
            if store is not None:
                pid = store.patient_for_case(case_id)
                out["case"]["patient_id"] = pid or ""
                out["case"]["active_facts"] = len(store.active_facts(pid)) if pid else 0
                out["case"]["last_job"] = store.last_job(case_id)
        except Exception as exc:  # noqa: BLE001
            out["case"] = {"case_id": case_id, "error": str(exc)[:120]}
    return out


def _purge_case(case_id: str, assume_yes: bool) -> int:
    if not assume_yes:
        print(f"将删除病例 {case_id} 的 Redis 窗口、关联/游标与病例级情景切片；加 --yes 执行")
        return 0
    out = memory_mod.purge_case(case_id)
    memory_mod.invalidate_patient(case_id)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _purge_patient(patient_id: str, assume_yes: bool) -> int:
    if not assume_yes:
        print(f"将删除患者 {patient_id} 的档案事实、关联、其病例窗口与全部情景切片；加 --yes 执行")
        return 0
    svc = memory_mod.patients()
    if svc is None:
        print("失败：患者库不可用（需要 MySQL）；未删除任何数据")
        return 2
    try:
        out = svc.delete(patient_id)
    except KeyError:
        print(f"失败：患者不存在：{patient_id}")
        return 2
    for cid in out.get("cases") or []:
        memory_mod.purge_case(cid)
        memory_mod.invalidate_patient(cid)
    episodic = memory_mod.purge_patient(patient_id)
    out["episodic"] = episodic
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="记忆层 health/计数/清理")
    ap.add_argument("--case", default="", help="附加该病例的窗口/患者/任务摘要")
    ap.add_argument("--purge-case", metavar="CASE_ID", default="")
    ap.add_argument("--purge-patient", metavar="PATIENT_ID", default="")
    ap.add_argument("--yes", action="store_true", help="确认执行清理")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    _wire()
    if args.purge_case:
        return _purge_case(args.purge_case, args.yes)
    if args.purge_patient:
        return _purge_patient(args.purge_patient, args.yes)

    report = {"health": memory_mod.health(), "counts": _counts(args.case)}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    health = report["health"]
    print(f"记忆层：{'启用' if health.get('enabled') else '已关闭'}")
    for layer in ("short_term", "profile", "episodic", "worker"):
        row = health.get(layer) or {}
        state = "ok" if row.get("ok") else "degraded"
        print(f"  {layer:<10} {state:<8} {row.get('latency_ms', '-')}ms {row.get('detail', '')}".rstrip())
    counts = report["counts"]
    print(f"病例 {counts['sessions']} 个"
          + (f"｜患者 {counts['patients']} 个｜待处理任务 {counts['pending_jobs']} 个"
             if "patients" in counts else "｜患者库不可用"))
    case = counts.get("case")
    if case:
        print(f"病例 {case.get('case_id')}：窗口 {case.get('window', '-')} 条"
              f"｜患者 {case.get('patient_id') or '未关联'}"
              f"｜active 事实 {case.get('active_facts', '-')} 条")
        job = case.get("last_job") or {}
        if job:
            print(f"  最近任务 {job.get('job_id')} {job.get('status')} "
                  f"{job.get('last_error') or ''}".rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
