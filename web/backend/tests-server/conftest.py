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
# 记忆层默认关闭：单测不得连真实 Redis / MySQL / Milvus（集成测试自行覆盖该变量）
os.environ.setdefault("DERM_MEMORY_DISABLED", "1")
# KB 单测默认 fake 后端，且不自动拉起 worker（避免触碰真实服务）
os.environ.setdefault("DERM_KB_BACKEND", "fake")
os.environ.setdefault("DERM_KB_AUTOSTART_WORKER", "0")
# 解析默认走本地适配（云端用例自行覆盖 mode/token 并注入假网络）
os.environ.setdefault("DERM_KB_MINERU_MODE", "local")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TMP_DATA, ignore_errors=True)
