# -*- coding: utf-8 -*-
"""本机 env 文件加载：已有环境变量优先、引号/export 解析、缺失文件静默。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import local_env  # noqa: E402


def test_load_parses_without_overriding(tmp_path, monkeypatch):
    f = tmp_path / "local.env"
    f.write_text('DERM_TEST_A=1\n# comment\nexport DERM_TEST_B="two"\n'
                 "DERM_TEST_A=9\nDERM_TEST_C='three'\n", encoding="utf-8")
    monkeypatch.setenv("DERM_TEST_A", "keep")
    monkeypatch.delenv("DERM_TEST_B", raising=False)
    monkeypatch.delenv("DERM_TEST_C", raising=False)
    assert local_env.load(str(f)) is True
    assert os.environ["DERM_TEST_A"] == "keep"
    assert os.environ["DERM_TEST_B"] == "two"
    assert os.environ["DERM_TEST_C"] == "three"


def test_missing_file_is_not_an_error(tmp_path):
    assert local_env.load(str(tmp_path / "none.env")) is False


def test_path_follows_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DERM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DERM_ENV_FILE", raising=False)
    assert local_env.path() == str(tmp_path / "local.env")
