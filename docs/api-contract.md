# 接口契约（v77）

服务端 `web/backend/server/app.py`（FastAPI）。v77 的改造原则是**增量兼容**：所有既有路径、请求体、成功响应体一律不变，只补充机器可读的错误封套、请求 ID、分页头、OpenAPI 元数据与契约端点。

- 契约实现：`web/backend/server/api_contract.py`
- 工具真源：`web/backend/server/tool_spec.py`
- 在线文档：`GET /docs`（Swagger UI）、`GET /openapi.json`
- 契约总入口：`GET /api/meta/contract`

## 响应约定

### 成功

成功响应保持各端点原有形态（裸数组 / 裸 dict / `{ok:true,...}` / 病例快照），未做统一包装——这是为了三端旧客户端零改动。

所有响应都带：

| 头 | 说明 |
|---|---|
| `X-Request-Id` | 客户端可传入（≤128 字符）；未传则服务端生成 `req_<12hex>` |
| `X-API-Version` | 当前契约版本（`1.0`） |
| `X-Total-Count` | 仅分页端点：裁剪前的总条数 |

### 失败（错误封套）

```json
{
  "ok": false,
  "detail": "病例不存在：abc123",
  "error": {
    "code": "case_not_found",
    "message": "病例不存在：abc123",
    "retryable": false,
    "request_id": "req_9f2c1a7b3d4e"
  }
}
```

- `detail` 与旧版**完全一致**（字符串或 dict 原样保留），旧客户端按文本展示不受影响；
- `error` 是新增的机器可读层；推荐客户端优先读 `error.message`，取不到再回退 `detail`；
- 参数校验失败（422）额外带 `error.fields`：

```json
{"ok": false, "detail": "参数校验失败：text Input should be a valid string",
 "error": {"code": "validation_error", "fields": [
   {"loc": "text", "msg": "Input should be a valid string", "type": "string_type"}],
   "message": "…", "retryable": false, "request_id": "req_…"}}
```

## 错误码目录

| code | HTTP | 可重试 | 说明 |
|---|---|---|---|
| `bad_request` | 400 | 否 | 请求参数不合法 |
| `unauthorized` | 401 | 否 | 未通过鉴权（MCP token 等） |
| `forbidden` | 403 | 否 | 当前状态不允许该操作 |
| `record_export_forbidden` | 403 | 否 | 病历未确认，禁止导出 |
| `mcp_policy_denied` | 403 | 否 | MCP 策略拒绝（写工具不可达） |
| `not_found` | 404 | 否 | 资源不存在（通用） |
| `case_not_found` | 404 | 否 | 病例不存在 |
| `turn_expired` | 404 | 否 | turn 不存在或已过期 |
| `stream_disabled` | 404 | 否 | 流式接口未启用 |
| `mcp_disabled` | 404 | 否 | MCP 接口未启用 |
| `method_not_allowed` | 405 | 否 | 请求方法不允许 |
| `conflict` | 409 | 否 | 状态冲突 |
| `turn_in_progress` | 409 | 是 | 该病例已有进行中的对话 |
| `intent_conflict` | 409 | 否 | 确认卡不存在或已失效 |
| `plan_not_confirmed` | 409 | 否 | 计划尚未确认 |
| `payload_too_large` | 413 | 否 | 上传内容过大 |
| `validation_error` | 422 | 否 | 请求体校验失败 |
| `invalid_field` | 422 | 否 | 包含未知病例字段 |
| `internal` | 500 | 是 | 服务端处理失败 |
| `memory_unavailable` | 503 | 是 | 记忆层不可用 |
| `kb_unavailable` | 503 | 是 | 知识库不可用 |
| `llm_unavailable` | 503 | 是 | LLM 未配置或不可达 |
| `unavailable` | 503 | 是 | 其他依赖不可用 |
| `multipart_unavailable` | 400 | 否 | 缺少 multipart 上传支持 |

完整机器可读目录：`GET /api/meta/contract` → `error_codes`（含状态码/可重试/默认文案）。

## 分页

`GET /api/cases`、`GET /api/records` 支持：

- `?limit=N&offset=M`
- 默认 `limit` 缺省 = **全量返回**（兼容旧客户端）
- 总数始终在响应头 `X-Total-Count`，响应体仍是裸数组

```bash
curl -sD - 'localhost:8765/api/cases?limit=20&offset=0' | head
```

## 契约端点

### `GET /api/tools`

工具目录：内置 19 条 + 已导入的外部工具（v79）。保留旧字段 `tools` / `available` /
`llm_tools` / `mcp_tools`，新增：

| 字段 | 说明 |
|---|---|
| `sources` | `{builtin, mcp, http, py}` 计数 |
| `mcp_inbound` | 对外只读 MCP 桥 `{enabled, protocol}`（与设置里的外部 MCP 接入无关） |
| `config_error` | 工具配置损坏时的中文说明（正常为空串） |

每条工具含 `name/title/description/input_schema/risk/source/builtin/enabled/read_only/available/executor/status/error/origin`。

```bash
curl -s localhost:8765/api/tools | python -m json.tool
```

### `/api/tools/**`（v79 工具管理）

| 方法 | 路径 | 请求体 / 说明 |
|---|---|---|
| PATCH | `/api/tools/builtin/{name}` | `{enabled: bool}`；核心工具停用返回 `warning` |
| GET | `/api/tools/servers` | 服务器摘要 + 工具（密钥只给掩码） |
| POST | `/api/tools/servers` | `name/transport(stdio·http·sse)/command/args/url/cwd/timeout_s/enabled/env/headers` |
| PUT | `/api/tools/servers/{id}` | 同上；`env`/`headers` 留空=保留，`clear_secrets:true` 才清除 |
| DELETE | `/api/tools/servers/{id}` | 删除并终止连接/子进程 |
| POST | `/api/tools/servers/{id}/test` | 同步重连，返回 `{ok, server, error}` |
| PATCH | `/api/tools/servers/{id}/tools/{tool}` | `{enabled}`；写类工具 400 |
| DELETE | `/api/tools/servers/{id}/tools/{tool}` | 移除并写入 `removed_tools` |
| POST | `/api/tools/servers/{id}/tools/{tool}/restore` | 恢复被移除工具 |
| POST | `/api/tools/import/mcp` | `{"mcpServers": {...}}` 或 `{"servers": {...}}`；返回 `created/errors` |
| GET/POST | `/api/tools/http` | GET 列表；POST 新建（`url` 必须 HTTPS 或本机 http，`method` 仅 GET/POST） |
| PUT/DELETE | `/api/tools/http/{id}` | 编辑 / 删除 |
| GET | `/api/tools/python` | 已导入 Python 工具 |
| POST | `/api/tools/import/python` | JSON `{filename, content_b64}`（首选，无额外依赖）或 multipart `file`；`--inspect` 校验 `TOOL_SPEC` + `run` |
| PATCH | `/api/tools/dynamic/{name}` | `{enabled}`（MCP 工具映射回服务器状态） |
| DELETE | `/api/tools/dynamic/{name}` | 删除（MCP 进入 `removed_tools`） |
| POST | `/api/tools/test` | `{name, args}`；仅动态工具，内置返回 400 |
| GET | `/api/tools/audit?limit=50` | `tool_call/tool_test/mcp_server_connect/mcp_tool_call/tool_import/tool_delete`，已脱敏 |

失败统一走错误封套：校验/只读门禁/非法负载 → 400 `detail`（中文）；不存在的内置工具或动态工具 → 404。
响应中**没有** `env` / `headers` 明文，只有 `env_keys` / `header_keys` / `masked_*` / `has_secrets`。
完整说明见 `docs/tools-management.md`。

### `GET /api/meta/contract`

跨端唯一契约入口：

```json
{
  "api_version": "1.0",
  "schema_version": "1.1",
  "error_format": {"ok": "boolean", "detail": "…", "error": {"code": "…"}},
  "error_codes": {"case_not_found": {"status": 404, "retryable": false, "message": "病例不存在"}},
  "request_id_header": "X-Request-Id",
  "sse_events": ["turn_started", "progress", "plan_ready", "step", "tool",
                 "message_delta", "heartbeat", "partial", "done", "error"],
  "plan_states": ["proposed", "approved", "executing", "completed", "failed",
                  "cancelled", "superseded"],
  "paginated": ["/api/cases", "/api/records"],
  "pagination": {"params": ["limit", "offset"], "total_header": "X-Total-Count", "default": "全量返回"},
  "limits": {"list_default_limit": null, "list_max_limit": 500, "chat_text_chars": 2000},
  "tools": {"total": 19, "available": ["…"], "llm": ["…"], "mcp": ["…"]},
  "mcp": {"enabled": false, "protocol": "2024-11-05", "endpoints": ["POST /mcp"], "transport": ["http", "stdio"]}
}
```

### `GET /api/health`

在原有字段上追加 `api_version`、`tools_available`、`tools_total`、`mcp`（`on/off`）。

## SSE 事件

`POST /api/cases/{case_id}/chat/stream` 的事件名与 `error.code` 均与 `GET /api/meta/contract` 目录对齐：

`turn_started` / `progress` / `plan_ready` / `step` / `tool` / `message_delta` / `heartbeat` / `partial` / `done` / `error`。

`POST /api/cases/stream` 是首条消息建案的流式版本：事件集相同，响应头额外下发 `X-Case-Id`
（设置/压缩类指令不建病例时为空串，只发 `turn_started` + `done`）。两端点均受 `DERM_AGENT_STREAM`
开关控制，`0` 时返回 404。浏览器跨源读取需 `Access-Control-Expose-Headers` 包含
`X-Turn-Id, X-Case-Id`。

`message_delta` 载荷：

```json
{"text": "逐字片段", "seq": 3, "call": 0, "final": false, "reset": false, "source": "model"}
```

| 字段 | 说明 |
|---|---|
| `text` | 增量文本；`reset:true` 时为空串 |
| `seq` | 服务端增量序号（同时写入 SSE `id:`，客户端据此去重） |
| `call` | 本轮第几次模型调用（从 0 开始） |
| `final` | `true` 表示权威终稿，通常同时 `reset:true` |
| `reset` | `true` 表示丢弃本气泡已有文本并整体替换 |
| `source` | `model`（真实模型增量）/ `final`（`done.snapshot` 的权威正文） |

语义：首个 `reset:false, final:false` 帧开始逐字追加；模型改口、修正或降级时发 `reset:true`
清空预览；`done` 之前一定有一条 `final:true`（正文为空或计划/确认卡轮次可缺省），其 `text`
与 `done.snapshot.reply.content` 完全一致。无 LLM 时**不伪造** `source:"model"` 帧，只有
`source:"final"`。

`error` 事件载荷：`{"code": "...", "message": "...", "retryable": bool}`，其中 `code` 取自错误码目录。

## OpenAPI

- 所有端点自动补 `tags`（`cases` / `patients` / `kb` / `memory` / `llm` / `imaging` / `navigation` / `meta` / `mcp`）、`operationId`、`summary`；
- 统一声明 `422` / `500` 引用 `ErrorEnvelope`；
- `x-api-version` 与 `x-error-codes` 写入文档根；
- **现有端点不启用运行时 `response_model`**，避免字段被过滤；只有 v77 新增端点使用真实响应模型。

## 兼容策略

| 项 | v77 行为 |
|---|---|
| 路径 | 不变 |
| 请求体 | 不变 |
| 成功响应体 | 不变 |
| 错误 `detail` | 不变（str/dict 原样） |
| 错误新增 | `ok:false` + `error{}`，旧客户端可忽略 |
| 分页 | 参数可选，默认全量 |
| SPA 回退 | 仅浏览器 `GET/HEAD` 的非 `/api`、`/mcp` 路径返回 `index.html` |
| 404 | `/api/*` 与 `/mcp*` 永远是 JSON |

> 本服务仅用于医生工作站的辅助决策，不构成诊疗建议或处方。
