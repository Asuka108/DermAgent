# 肤智（DermAgent） Windows 演示客户端（阶段 C MVP）

三栏工作台：**左**就诊资料（九要素表单 + 脱敏照片上传 + 合成案例一键导入）· **中** 3D 人体定位（**真实人体模型 Cesium Man，CC BY 4.0**，内嵌零网络加载；旋转/缩放/视角/点击区域标记/截图入报告；GLB 失败自动回落程序化人台）· **右** 智能体结果（§6.1 固定 JSON 草稿卡片 → 医生确认/修改/拒绝 → 报告导出）。

> ⚠️ 仅供医生决策支持演示，不构成诊疗建议或处方；全部使用合成/脱敏数据；**密钥不出客户端**。

## 本地运行

```bash
cd client
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

- **mock 模式（默认）**：完全离线的确定性演示管线（九要素校验/红旗/知识库检索/§6.1 草稿/报告），断网可演示、结果可复现。
- **llm 模式 · 直连大模型**：三个输入框——**Base URL / API Key / 模型名**（OpenAI 兼容 `/chat/completions`，temperature=0）。LLM 负责草稿生成与按意见修订；本地保留必填校验、红旗筛查、知识库检索与**引用白名单强制**（编造引用剔除并计数）、非法 JSON 受限修复一次 → 失败标记「未通过系统校验」并降级规则稿。API Key 仅存本机 `data/settings.json`（**已 gitignore**；演示时粘贴，勿截图/勿提交）。
- **http 模式**：`AGENT_HTTP_URL=https://<桥接服务>` 指向部署方服务端桥接（平台密钥只在服务端）；连接失败时客户端明确提示并可切回 mock（断网降级）。

## 演示脚本（对应 5 分钟视频 1:05–3:25 段）

1. 点击「案例2 红旗」→「开始结构化分析」→ 草稿卡片出现 🚩 红旗升级提示（severity=needs_review）；
2. 3D 面板：旋转/缩放，点击左侧胸背部区域 → 身体区域字段自动填充；「截图入报告」；
3. 「按意见修订」输入 `请补充疼痛评估与眼科会诊提示` → 修订后再审 →「确认生成报告」；
4. 「案例3 资料不足」→ 追问卡片 → 按「字段：值」补充 → 草稿 → 「拒绝」演示不生成报告；
5. 「案例4 无答案」→ 草稿明确「知识库未覆盖/不足以判断」，不编造引用。

## 打包（Windows，PyInstaller）

```bash
pip install pyinstaller
pyinstaller --name Fuzhi3D --windowed --add-data "web:web" main.py
# 产物 dist/Fuzhi3D/；复制 data/（空目录）后即可分发；密钥/审计均不打入安装包
```

> 打包须在 Windows 上执行（PyInstaller 不支持交叉打包）；macOS 开发机负责功能开发与 pytest 回归。

## 测试

```bash
python -m pytest tests/ -q          # 26 例：校验/红旗/检索否定词感知/引用/§6.1 契约/四案例端到端/LLM 直连四路径
node --check web/main.js            # 3D 面板脚本语法校验
python tools/verify_3d.py           # 3D 真机验证（QtWebEngine 实渲染）：三角形数/拾取/高亮互斥 + 正背面截图
```

3D 真机验证最近一次结果（v2 仿真人台）：`triangles=50416, drawCalls=35, regionGroups=13`，点击拾取与高亮互斥均正确，截图输出 `data/verify3d_front.png` / `verify3d_back.png`。

⚠️ 已知坑：QtWebEngine/Chromium 阻止 `file://` 下的 ES Module（CORS）——因此 vendor 使用 three.js r147 **UMD** 版（含 CapsuleGeometry/sheen）；若未来升级 ESM 需经本地 http 服务提供页面。

## 目录

```
client/
  main.py            PySide6 主窗口（三栏工作台 + 审计 + 断网降级）
  derm_logic.py      纯业务逻辑（TS logic.ts 同构移植）
  derm_kb.py         合成知识库 + 四合成案例（与 TS 侧同源）
  agent_client.py    mock（离线）/ http（服务端桥接）双模式
  web/               Three.js 3D 面板（vendor/three.min.js r128 + OrbitControls，MIT）
  data/              审计 audit.jsonl + uploads/（运行时生成，不入库）
  tests/             pytest 纯逻辑与端到端
```
