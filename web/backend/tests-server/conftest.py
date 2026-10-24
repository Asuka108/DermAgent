"""tests-server 数据隔离：pytest 一律不得触碰真实 server/data/。

v63 教训——tests-server 无隔离时，test_delete_all_cases_clears_list 曾通过
in-process TestClient 直写真实 sessions.json，把医生当晚刚创建的病例连同
验证数据一并清空（审计留痕 case_delete_all count=4，数据无法找回）。

conftest 顶层先于任何测试模块导入 server.app 执行：把 DERM_DATA_DIR 指到
临时目录，sessions/audit 全部落隔离区，会话结束自动清理。
RAG 侧各用例已自行 monkeypatch BOOKS_FILE/MODELS_ROOT 等，无需在此处理。
"""

import os
import shutil
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="derm-tests-data-")
os.environ["DERM_DATA_DIR"] = _TMP_DATA


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP_DATA, ignore_errors=True)
