# -*- coding: utf-8 -*-
"""v76 会话存储并发：按病例锁放开并行后，读盘不得看到半写文件、写盘不得覆盖其它病例。"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app as app_mod  # noqa: E402


def test_save_is_atomic_and_leaves_no_tmp():
    app_mod._save_sessions({"case_a": {"case_id": "case_a"}})
    assert app_mod._load_sessions()["case_a"]["case_id"] == "case_a"
    assert not os.path.exists(app_mod.SESSIONS_FILE + ".tmp")
    with open(app_mod.SESSIONS_FILE, encoding="utf-8") as fh:
        assert "case_a" in fh.read()          # 磁盘上是完整 JSON


def test_concurrent_readers_never_see_empty_or_partial():
    app_mod._save_sessions({"case_a": {"case_id": "case_a"}})
    stop = threading.Event()
    errors = []

    def writer():
        for i in range(60):
            sessions = app_mod._load_sessions()
            sessions["case_b"] = {"case_id": "case_b", "n": i}
            app_mod._save_sessions(sessions)
        stop.set()

    def reader():
        while not stop.is_set():
            data = app_mod._load_sessions()
            if "case_a" not in data:
                errors.append("case_a 在读盘时丢失")
                return
            if "case_b" in data and not isinstance(data["case_b"], dict):
                errors.append("case_b 结构损坏")
                return

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    final = app_mod._load_sessions()
    assert final["case_a"]["case_id"] == "case_a"
    assert final["case_b"]["n"] == 59
