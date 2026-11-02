# -*- coding: utf-8 -*-
"""LLM 配置落盘：并发写入必须原子且互不踩临时文件。"""
import json
import threading

from server import llm_config as lc


def test_concurrent_writes_are_atomic(tmp_path, monkeypatch):
    target = tmp_path / "llm_config.json"
    monkeypatch.setattr(lc, "CONFIG_FILE", str(target))
    errors = []

    def worker():
        try:
            for _ in range(20):
                lc._write({"schema_version": 2, "active_id": "", "connections": [],
                           "pad": "x" * 20000})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert json.loads(target.read_text(encoding="utf-8"))["schema_version"] == 2
    assert not list(tmp_path.glob("*.tmp"))
    assert oct(target.stat().st_mode & 0o777) == "0o600"
