# -*- coding: utf-8 -*-
"""情景记忆补建：从 sessions.json 重建病例级切片（计划 §3.3 / §4）。

用法（在 web/backend 下）：
    python tools/memory_backfill.py --dry-run
    python tools/memory_backfill.py                     # 写 Milvus（需 BGE + Milvus）
    python tools/memory_backfill.py --case <case_id>    # 只补指定病例（可重复）

退出码：0 = 完成或如实跳过（Milvus/BGE 未就绪）；2 = 数据文件缺失/损坏或病例不存在。
不写病例、不推进游标、不产生患者事实；chunk_id 由内容哈希生成，重复执行幂等（upsert）。
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from memory import config as mem_config          # noqa: E402
from memory import extract                        # noqa: E402


def _load_sessions(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError:
        print(f"跳过：找不到 {path}")
        return None
    except json.JSONDecodeError as exc:
        print(f"失败：{path} 不是合法 JSON（{exc}）")
        return None
    if not isinstance(data, dict):
        print(f"失败：{path} 结构不是病例字典")
        return None
    return data


def _profile_store():
    """补建必须能读患者档案：姓名清单缺失时无法保证去标识，宁可跳过。"""
    try:
        from memory.profile import MySQLProfileStore
        store = MySQLProfileStore()
        store.health()
        return store
    except Exception as exc:  # noqa: BLE001
        print(f"跳过：患者库不可用（{str(exc)[:120]}）；缺姓名清单无法保证去标识，未写入任何切片")
        return None


def _patient(store, case_id: str):
    if store is None:
        return "", None
    try:
        pid = store.patient_for_case(case_id)
        return (pid or ""), (store.get_patient(pid) if pid else None)
    except Exception:  # noqa: BLE001
        return "", None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从 sessions.json 补建病例级情景切片")
    ap.add_argument("--case", action="append", default=[], help="只处理指定病例（可重复）")
    ap.add_argument("--data-dir", default="", help="会话数据目录（默认 server/data 或 DERM_DATA_DIR）")
    ap.add_argument("--dry-run", action="store_true", help="只统计切片数，不写入")
    args = ap.parse_args(argv)

    path = os.path.join(args.data_dir or mem_config.data_dir(), "sessions.json")
    sessions = _load_sessions(path)
    if sessions is None:
        return 2

    wanted = set(args.case)
    unknown = wanted - set(sessions)
    if unknown:
        print(f"失败：病例不存在：{', '.join(sorted(unknown))}")
        return 2

    store = _profile_store()
    if store is None:
        return 0
    total_cases = total_chunks = 0
    payload = []
    for case_id, sess in sessions.items():
        if wanted and case_id not in wanted:
            continue
        if not isinstance(sess, dict):
            continue
        messages = sess.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        patient_id, patient = _patient(store, case_id)
        if not patient_id:
            print(f"{case_id}: 跳过（未关联患者，无法核对姓名做去标识）")
            continue
        chunks = extract.dedupe_chunks(extract.build_turn_chunks(
            messages, case_id, patient_id, patient, str(sess.get("task_type") or "")))
        if not chunks:
            continue
        total_cases += 1
        total_chunks += len(chunks)
        print(f"{case_id}: {len(chunks)} 片")
        payload.extend(chunks)

    if args.dry_run:
        print(f"dry-run：{total_cases} 个病例，共 {total_chunks} 片；未写入")
        return 0
    if not payload:
        print("没有可写入的切片")
        return 0

    try:
        from memory.episodic import MilvusEpisodic
        written = MilvusEpisodic().write(payload)
    except Exception as exc:  # noqa: BLE001 — Milvus/BGE 未就绪如实跳过，不伪造写入
        print(f"跳过：Milvus/BGE 不可用（{str(exc)[:160]}）；未写入任何切片，安装后重跑即可")
        return 0
    print(f"完成：{total_cases} 个病例 / 写入或更新 {written} 片（幂等，可重复执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
