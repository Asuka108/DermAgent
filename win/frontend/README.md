# 桌面端（macOS / Windows）

部署、运行与测试说明统一见仓库根目录 [`README.md`](../../README.md)，此处不再单独维护，避免版本过期。

- 快速启动：`pip install -r requirements.txt -r ../../web/backend/requirements.txt`，然后 `python main.py`（自动拉起本机服务端）。
- 3D 模型（`.glb`）不随仓库分发，需自备授权模型放入 `web/frontend/vendor/models/`；缺失时仅 3D 面板不可用。
- 测试：`python -m pytest tests -q`（39 passed）。
