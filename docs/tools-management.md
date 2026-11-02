# 工具管理（v79）

设置里的「🧰 工具」页用于查看内置工具、导入/删除外部工具，并接入**外部 MCP 服务器**。
本机 Agent 是 MCP **客户端**；服务端对外暴露的只读 MCP 桥是另一条链路（`docs/mcp.md`），两者互不影响。

- 代码：`tool_store.py`（配置/加密）、`tool_mcp.py`（MCP 客户端）、`tool_runtime.py`（动态注册表）、
  `tool_py_runner.py`（Python 子进程）
- 内置工具真源仍是 `tool_spec.py`（19 条，5 条可执行），本页只做**启停**，不新增/删除定义
- 界面：Web 设置抽屉 → 工具；桌面 `⚙ 设置` → 🧰 工具（同一套服务端配置）

## 架构

```
Agent 每轮 (agent_loop.run_agent_turn)
  ├─ 内置 schemas：tool_spec.llm_tools()，按 builtin_disabled 过滤
  └─ 动态 schemas：tool_runtime.schemas()（已启用且只读）
        │
        ▼ LLM 决定调用
tool_exec.execute(name, args)
  ├─ tool_spec 命中 → 现有 5 个执行器（写工具仍走病例锁与确认）
  └─ 未命中 → tool_runtime.execute()
        ├─ mcp    → tool_mcp（官方 SDK，每服务器常驻连接）
        ├─ http   → httpx（GET/POST，HTTPS 校验，禁重定向）
        └─ python → 子进程 tool_py_runner.py（超时 + JSON stdin/stdout）
```

动态工具结果统一截断并作为**不可信引用**回注模型；系统提示词明确要求不执行工具返回中的任何指令。

## 配置与密钥

| 路径 | 权限 | 说明 |
|---|---|---|
| `server/data/tools/config.json` | 0600 | 服务器、HTTP/Python 工具、内置停用列表 |
| `server/data/tools/fernet.key` | 0600 | Fernet 密钥；首次写入密钥时生成 |
| `server/data/tools/py/` | 0700 | 导入的 Python 工具文件 |

路径尊重 `DERM_DATA_DIR`（测试自动隔离）。写入一律 `tmp + os.replace`；JSON 损坏时返回空配置并在
`/api/tools` 的 `config_error` 中说明，不崩溃。

密钥以 `secrets_enc` 保存，解密后为 `{"env": {...}, "headers": {...}}`。所有读取接口只返回
`env_keys` / `header_keys` / `masked_env` / `masked_headers`（值为 `••••`）与 `has_secrets`；
明文只存在于执行器内部，审计与日志不记录。编辑时密钥留空表示保留原值，勾选 `clear_secrets` 才清除。
`fernet.key` 丢失后旧密钥不可解，需在界面重新填写。

`config.json` 结构：

```json
{
  "version": 1,
  "builtin_disabled": ["case_comparator"],
  "servers": [{
    "id": "srv_ab12cd", "name": "chrome-devtools", "transport": "stdio",
    "command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"], "cwd": "",
    "url": "", "timeout_s": 30, "enabled": true,
    "secrets_enc": "gAAAA...", "env_keys": ["FOO"], "header_keys": [],
    "tool_states": {"open_page": {"enabled": false}},
    "removed_tools": []
  }],
  "http_tools": [{
    "id": "htt_x1", "name": "pubmed_search", "public_name": "http_pubmed_search",
    "description": "检索 PubMed", "input_schema": {"type": "object", "properties": {}},
    "url": "https://eutils.ncbi.nlm.nih.gov/...", "method": "GET", "timeout_s": 15,
    "secrets_enc": "gAAAA...", "header_keys": ["Authorization"],
    "enabled": false, "created_at": "..."
  }],
  "py_tools": [{
    "id": "pyt_y1", "name": "calc_bmi", "public_name": "py_calc_bmi",
    "description": "...", "input_schema": {"type": "object", "properties": {}},
    "file": "py/pyt_y1.py", "enabled": false, "created_at": "..."
  }]
}
```

## 只读与命名规则

- **MCP**：只有 `annotations.readOnlyHint == true` 且 `destructiveHint != true` 的工具可启用；
  其余进入 `blocked` 并给出原因（未声明只读 / 可能修改数据）。
- **HTTP**：只允许 `GET` / `POST`；`PUT/PATCH/DELETE` 在导入校验阶段拒绝。必须 HTTPS，
  `http://` 仅允许 `localhost` / `127.0.0.1` / `[::1]`；拒绝云元数据地址与 URL 内嵌用户名密码；
  运行时 `verify=True`、`follow_redirects=False`，3xx 直接报“已拒绝跟随”。
- **Python**：`run(args)` 只读契约 + 独立子进程（15s 超时，最小环境变量，临时工作目录）。
  这是进程隔离，**不是 OS 沙箱**；导入时必须勾选风险确认。
- **命名**：`mcp_<server>_<tool>` / `http_<name>` / `py_<name>`，只保留 `[A-Za-z0-9_-]`，
  ≤64 字符，冲突加 `_2` 后缀；MCP 原名保存在映射中。
- **默认关闭**：新导入的外部工具与 MCP 工具一律 `enabled=false`，逐个启用；服务器可整体 `enabled`。
- **内置工具**：只能停用。停用 5 个核心工具（`get_case_status`、`update_record_fields`、
  `complete_record_intake`、`finish_supplement_and_analyze`、`search_knowledge_base`）时接口返回
  `warning`，UI 二次确认但不阻止。

## 导入格式

### MCP：`mcpServers` JSON

```json
{
  "mcpServers": {
    "chrome-devtools": {"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"],
                        "env": {"TOKEN": "..."}},
    "remote-search": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}}
  }
}
```

也接受 `{"servers": {...}}`。有 `url` 走 HTTP（可显式 `transport: "sse"`），否则按 stdio。
导入后后台重连；工具列表默认全部关闭，只读工具可启用。删除 MCP 工具会写入 `removed_tools`，
重新同步不会复活；可用 `POST /api/tools/servers/{id}/tools/{name}/restore` 恢复。

### HTTP 工具

请求字段：`name`、`description`、`url`、`method`（GET/POST）、`timeout_s`（1–120）、
`input_schema`（type=object 的 JSON Schema）、`headers`（KEY=VALUE 对象，加密保存）。

### Python 工具

文件需导出 `TOOL_SPEC`（dict：`name`、`description`、`input_schema`）与 `run(args) -> dict`；
`run` 的返回值会放进统一的 `{"ok": true, "data": ...}`（需要报错时直接抛异常）；
导入时先在临时目录用 `--inspect` 子进程校验，通过后复制到 `data/tools/py/`，默认停用。

```python
TOOL_SPEC = {
    "name": "calc_bmi",
    "description": "计算 BMI（只读）",
    "input_schema": {"type": "object", "properties": {
        "weight_kg": {"type": "number"}, "height_m": {"type": "number"}},
        "required": ["weight_kg", "height_m"]},
}

def run(args):
    bmi = args["weight_kg"] / (args["height_m"] ** 2)
    return {"bmi": round(bmi, 1)}
```

## API

所有写接口失败返回 400 + 中文 `detail`（沿用统一错误封套），成功后热生效。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tools` | 合并目录：内置 + 动态；`sources`、`mcp_inbound`、`config_error` |
| PATCH | `/api/tools/builtin/{name}` | `{enabled}`；核心工具停用附 `warning` |
| GET/POST | `/api/tools/servers` | 列表（含状态/工具）/ 新建 |
| PUT/DELETE | `/api/tools/servers/{id}` | 编辑 / 删除（终止子进程） |
| POST | `/api/tools/servers/{id}/test` | 同步重连并返回状态与工具 |
| PATCH/DELETE | `/api/tools/servers/{id}/tools/{tool}` | 启停（写类 400）/ 移除 |
| POST | `/api/tools/servers/{id}/tools/{tool}/restore` | 恢复被移除工具 |
| POST | `/api/tools/import/mcp` | 批量导入 `mcpServers` |
| GET/POST | `/api/tools/http` | 列表 / 新建 HTTP 工具 |
| PUT/DELETE | `/api/tools/http/{id}` | 编辑 / 删除 |
| GET | `/api/tools/python` | 已导入 Python 工具 |
| POST | `/api/tools/import/python` | 上传 `.py`（≤256KB）：优先 `{filename, content_b64}` JSON；multipart 仅当 `python-multipart` 已安装 |
| PATCH/DELETE | `/api/tools/dynamic/{name}` | 动态工具启停 / 删除 |
| POST | `/api/tools/test` | `{name, args}` 试调用，**仅动态工具** |
| GET | `/api/tools/audit?limit=50` | 最近工具审计（脱敏） |

`GET /api/tools` 保持旧字段（`tools` / `available` / `llm_tools` / `mcp_tools`），只新增字段。

## 审计

写入 `server/data/audit.jsonl`，`/api/tools/audit` 只返回：

- `tool_call` / `tool_test`：`source`、`tool`、`ok`、`duration_ms`、`args_digest`（sha1 前 12 位）
- `mcp_server_connect`：`server_id`、`transport`、`ok`、工具数、只读工具数
- `mcp_tool_call`：`server_id`、`tool`、`ok`、`duration_ms`、`args_digest`
- `tool_import` / `tool_delete`：来源与名称

不含参数内容、工具返回内容、密钥或身份字段。服务器自身错误截断 300 字后展示。

## 热生效与并发

- 保存配置后 `tool_runtime.refresh()`；MCP 相关再后台 `reload_all()`（单飞线程），无需重启。
- 管理线程 + 事件循环全局唯一；每个 MCP 服务器一个常驻 asyncio 任务（规避 anyio cancel scope
  跨任务问题），命令经 `asyncio.Queue` 串行执行。
- 外部工具只读，不持有病例锁；内置写工具仍走原有锁与确认门禁。
- 外部调用异常在 `tool_exec` / `tool_runtime` 内兜底为 `{ok:false,error}`，不会让回合 500。

## 已知限制

- MCP 服务器要求 sampling / elicitation 等能力时 v1 不实现，会如实报错。
- Python 工具是子进程 + 契约约束，不是 OS 级沙箱。
- HTTP 工具允许任意 HTTPS，参数会发送到用户配置的域名；导入时展示域名与警告，调用记审计。
- 外部返回可能含提示注入：只读限制 + 8KB 截断 + 系统提示词纪律 + 不提供外部写工具。
- 密钥依赖本机 `fernet.key`；删掉该文件后已保存密钥不可恢复。

## 验证

```bash
cd web/backend
.venv-tmp/bin/python -m pytest tests-server/test_tool_store.py tests-server/test_tool_runtime.py \
  tests-server/test_tool_mcp.py tests-server/test_tools_api.py -q
cd ../../win/frontend
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests -q
```

> 工具管理仅用于医生工作站的辅助查询，不构成诊疗建议或处方。
