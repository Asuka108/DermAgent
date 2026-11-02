# MCP 只读接口

DermAgent 服务端内置一个 **MCP（Model Context Protocol）只读桥**，让外部 Agent / IDE / 桌面客户端查询病例状态与检索本地知识库。写病历、生成诊断、导出等操作**不在 MCP 暴露范围**。

> **两条独立的 MCP 链路**：
> 1. 本文档描述的是**对外**只读桥（其他程序连 DermAgent，`DERM_MCP` 开关，`mcp_bridge.py`）；
> 2. 设置里的「🧰 工具」让**本机 Agent 作为客户端**接入外部 MCP 服务器（`tool_mcp.py`），
>    仅用于给 Agent 增加只读工具，与本文开关、端口、token 完全无关。详见 `docs/tools-management.md`。

- 协议：JSON-RPC 2.0，MCP `2024-11-05`，无状态（stateless）
- 默认：关闭（`DERM_MCP=0`）
- 代码：`web/backend/server/mcp_bridge.py`；工具真源：`tool_spec.py`

## 开关与安全

| 变量 | 默认 | 说明 |
|---|---|---|
| `DERM_MCP` | `0` | `1/true/on` 开启；关闭时 POST/GET `/mcp` 与 `/api/mcp/manifest` 均 404 |
| `DERM_MCP_TOKEN` | 空 | 非空时要求 `Authorization: Bearer <token>`；空表示仅本机可达（不额外鉴权） |

边界：

- 只暴露 `tool_spec.mcp_tools()`：`risk == "read"`、`requires_confirmation == False`、`expose_mcp == True`；
- 当前只有 2 个：`get_case_status`、`search_knowledge_base`；
- 写工具（`update_record_fields` / `complete_record_intake` / `finish_supplement_and_analyze`）与需医生确认的工具**永远不可达**，调用返回 `-32001 policy_denied`；
- 病例类工具必须带 `case_id`；病例不存在返回 `-32002`；
- 每次调用写审计 `mcp_tool_call`（工具名、病例 ID、参数摘要、耗时、成败；不记病例内容）；
- 工具 schema 不含姓名/身份证/手机号等身份字段。

> 本接口仅用于医生工作站内的辅助查询，不构成诊疗建议或处方。

## HTTP（POST /mcp）

```bash
export DERM_MCP=1
curl -s -X POST localhost:8765/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}'
```

响应：

```json
{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2024-11-05",
  "capabilities":{"tools":{"listChanged":false}},
  "serverInfo":{"name":"dermagent","version":"1.0.0"}}}
```

列工具：

```bash
curl -s -X POST localhost:8765/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

调工具：

```bash
curl -s -X POST localhost:8765/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
        "name":"get_case_status","arguments":{"case_id":"<case_id>"}}}'
```

返回 MCP 标准 `content` 数组，文本内容是工具结果的 JSON 字符串：

```json
{"jsonrpc":"2.0","id":3,"result":{
  "content":[{"type":"text","text":"{\"ok\":true,\"phase\":\"...\",...}"}],
  "isError":false}}
```

调试清单：`GET /api/mcp/manifest`（受同一开关控制）。

通知（无 `id`，如 `notifications/initialized`）返回 HTTP 202 空体。批量请求按 JSON 数组处理。

## stdio（桌面客户端 / Claude Desktop）

```bash
DERM_MCP=1 python -m server.mcp_bridge --stdio
```

从 stdin 逐行读 JSON-RPC，向 stdout 逐行写响应。适合 `command` 型 MCP 客户端配置：

```json
{
  "mcpServers": {
    "dermagent": {
      "command": "/path/to/python",
      "args": ["-m", "server.mcp_bridge", "--stdio"],
      "env": {"DERM_MCP": "1", "DERM_DATA_DIR": "/path/to/server/data"}
    }
  }
}
```

## 错误码

| code | 含义 |
|---|---|
| `-32700` | JSON 解析失败 |
| `-32600` | 非法 JSON-RPC 请求 |
| `-32601` | 方法不存在 |
| `-32602` | 参数缺失/类型错误 |
| `-32603` | 内部错误 |
| `-32001` | 策略拒绝（工具未暴露给 MCP） |
| `-32002` | 病例不存在 |

## 扩展新工具

1. 在 `tool_spec.py` 增加 `ToolSpec`，给出真 JSON Schema 与 `executor`；
2. 在 `tool_exec.py` 注册执行器（只读）；
3. 设置 `expose_mcp=True` 且 `risk="read"`、`requires_confirmation=False`；
4. 补 `test_mcp.py`：`tools/list` 出现、`tools/call` 正常、写工具仍被拒。
