# node-catalog —— 节点类型目录(选型入口)

> **建 flow 先看这里**：有哪些节点类型、何时选哪个。
> 各 factory 的**完整 API / 用法**见 [node-kit.md](node-kit.md)（本文件是选型 + 分类，不重复 API 细节）。
>
> 心智模型：先有**节点库**（本目录），再在 `src/app/graph.ts` 编排成图。产品入口仅 **default ReAct**；扩展范式见 [examples.md](examples.md)。

## 三层分类

| 层 | 是什么 | 能否 `addNode` |
|---|---|---|
| **① FACTORY 节点** | 框架封装的参数化节点,`addNode("x", createYyy({...}))` | ✅ |
| **② BESPOKE 模式** | 无 factory,但有命名惯例 + 支撑原语;手写 | ✅(手写) |
| **③ 支撑原语** | 节点**体内**调用的纯函数/工具(不是节点本身) | —(节点内用) |

---

## ① FACTORY 节点

### LLM 类

| 惯用名 | factory | 语义 | 何时用 |
|---|---|---|---|
| `llm` | `createLlmNode` | 一次调 LLM,写回**纯文本**（默认）或结构化（`write` 读 `r.parsed` 时才加 `parse`） | plan / rewrite / grade / 路由裁决；**非**用户可见大段终稿 |
| `llm-stream` | `createLlmStreamNode` | **流式**调 LLM,逐 token 给用户 | compose / aggregate / generate / draft / respond(用户可见大段输出) |
| `llm-router` | `createLlmRouterNode` | LLM 裁决后**返回 Command goto**(节点内路由) | reflection / evaluator:评审 → 重做或放行 |

### HITL(人审)类

| 惯用名 | factory | 语义 | 何时用 |
|---|---|---|---|
| `approval` | `createHumanApprovalNode` | **前置**审批:`interrupt` 暂停 → 收 feedback(通过/打回) | review / approve / confirm（**跨 turn**） |
| `permission-approval` | `createPermissionApprovalNode` | **同步弹窗**审批:同 turn 调 `onApprovalRequest` → allow/reject | 秒级 yes/no；不 interrupt、不结束 turn |
| `approval-finalize` | `createApprovalFinalizeNode` | **后置**定稿:按已收 feedback 短路 / 修订 | HITL 流的最后一公里 |

> `approval` 与 `approval-finalize` **互补**,常配对。
> **工具级审批**由 `createToolExecNode` + `permissions.interruptOn` 自动门控（见 `config/flow-agent.config.json`）。

### 工具/检索类

| 惯用名 | factory | 语义 | 何时用 |
|---|---|---|---|
| `tool-exec` | `createToolExecNode` | 执行模型 `tool_calls`(ReAct 工具步)+ 三态透出 | 默认图 tools 节点 |
| `platform-tool` | `createPlatformToolActionNode` | 主动调用已注入的平台/工具集合 | 固定管道里点名调 Plugin |
| `mcp-retrieval` | `createMcpRetrievalNode` | 调真实 MCP 检索(rateLimited + 三态) | RAG retrieve（接外部知识源） |

### 结构类

| 惯用名 | factory | 语义 | 何时用 |
|---|---|---|---|
| `prepare` | `createPrepareNode` | input → 首条 HumanMessage | 图入口归一化用户输入 |
| `fanout` | `createFanout` | **Send map-reduce 扇出**(返回条件边函数,非节点) | 并行调研聚合 |
| `subgraph` | `createSubgraphNode` | 把一张小图编译后当节点用 | 子任务封装 |

---

## ② BESPOKE 模式（无 factory，手写）

| 模式 | 为何定制 | 出路 |
|---|---|---|
| **bindTools 的 ReAct think** | think↔tools 循环与 ToolNode 耦合 | 用默认 ReAct 图,不手写 |
| **文件交付** | interrupt 收路径 + 写文件 | 手写节点 |
| **多源检索取优** | 多 MCP 源启发式合并 | 手写 subgraph（`createMcpRetrievalNode` 仅单源/简单多源） |
| **自定义 reducer** | 业务特定聚合 | Annotation reducer 手写 |
| **含 emitPlan/emitStage** | 节点内发结构化事件 | `createLlmNode`/`Stream` 的 `write` 内 `emit*` |

> 「图是契约」：bespoke 保留手写是设计选择（注释「为何不用 factory」），不是遗漏。范式名（RAG / HITL / Send）见 [examples.md](examples.md)，**不是**可切换内置场景。

---

## ③ 支撑原语(节点体内调用)

| 原语 | 用途 |
|---|---|
| `requireModel(appConfig, label)` | 「无 demo fallback」模型凭证(真实接入用) |
| `extractText(content)` | LLM content → 纯文本 |
| `parseJson<T>(text)` | LLM 文本 → JSON(容忍 ```json 围栏) |
| `streamLLMText(...)` | 韧性流式调用(factory 内用,bespoke 也可) |
| `emitStage` / `emitPlan` / `emitTextToken` | surface 事件生产(writer + callback 双发) |
| `runTool(name, args, fn, onToolCall?)` | 执行一个工具 + 三态透出 |
| `isApproval(feedback, opts?)` | HITL「通过」判定 |
| `callResolvedMcpTool` / `rateLimited` / `McpServerConfig` | MCP 访问层(见 `src/libs/mcp/mcp-access.ts`) |

> 原语是节点的「建材」,不是节点本身——不要 `addNode("x", emitPlan)`。

---

## 选型决策树(4 问)

```
Q1 节点要等人审批吗?
  是 → Q1a 同 turn 弹窗(秒级 yes/no)?
        是 → permission-approval (createPermissionApprovalNode)
        否 → Q1b 定稿(按已收 feedback 决定输出)?
              是 → approval-finalize (createApprovalFinalizeNode)
              否 → approval (createHumanApprovalNode, interrupt 跨 turn)
  否 → Q2

Q2 节点要调 LLM 吗?
  是 → Q2a 裁决后要节点内路由(reflection/evaluator)?
        是 → llm-router (createLlmRouterNode)
        否 → Q2b 用户可见大段输出 + 模型支持 stream?
                是 → llm-stream (createLlmStreamNode)
                否 → Q2c write 需要读结构化字段(r.parsed)?
                        是 → llm + parse（write 读 r.parsed；见 node-kit § parse 契约）
                        否 → llm（仅文本，**不加 parse**）
  否 → Q3

Q3 节点要检索外部知识(MCP)吗?
  是 → mcp-retrieval (createMcpRetrievalNode)
  否 → Q4

Q4 结构类?
  input → 首条 HumanMessage       → prepare (createPrepareNode)
  并行扇出 map-reduce              → fanout (createFanout,⚠️ 边函数非节点)
  一张小图当节点                   → subgraph (createSubgraphNode)
  执行 tool_calls                  → tool-exec (createToolExecNode)
  主动调已注入平台工具               → platform-tool (createPlatformToolActionNode)
  都不是                           → bespoke(保留手写,见②)
```

---

## 命名注意：`prepare`

| 名称 | 含义 |
|---|---|
| factory `createPrepareNode` / 惯用名 `prepare` | input → HumanMessage（默认 ReAct 图入口） |
| 节点**名叫** `prepare` 但实际是 `createLlmNode` | 仍是 LLM 节点，**不要**因名字误加 `parseJson`；是否 `parse` 只看 `write` 是否读 `r.parsed` |

---

## factory 惯用名一览

手写图对照用（`tests/node-catalog.test.ts` 断言下列名出现在本文）：

```
llm | llm-stream | llm-router | approval | approval-finalize | platform-tool | tool-exec | mcp-retrieval | prepare | passthrough
```

另有手写常用、无上表惯用名行：`permission-approval`（`createPermissionApprovalNode`）、`subgraph`（`createSubgraphNode`）、`fanout`（边函数，非节点）。

- `platform-tool`：主动调用已对齐到该节点的工具
- `tool-exec`：执行上一条 AIMessage 的 `tool_calls`
- `passthrough`：纯数据变换 / 占位（无需 factory）

---

## 边约束（手写图）

- **conditional 边**：`condition` 的返回值必须 ∈ 其 `targets`，否则运行时 LangGraph 抛 `Invalid edge`（**R-G004**）。`pnpm graph` 静态反射**不执行** condition，需人工核对。
- **llm-router**：`route` 的 `goto` 目标须在 `addNode(..., { ends: [...] })` 声明，否则反射会丢掉 Command 路由边。

---

## 关系

- **[flow-graph-rules.md](flow-graph-rules.md)**:图编排硬性规则（R-G001+，**新增约定优先落此**）。
- **[node-kit.md](node-kit.md)**:各 factory 的完整 API + 用法 snippet(本文件只管选型/分类)；parse 摘要链到 R-G001。
- **[troubleshooting.md](troubleshooting.md)**:常见运行时错误索引（含 `LLM 未返回 JSON`）。
- **[flow-patterns.md](flow-patterns.md)**:Send/interrupt/Command/subgraph/checkpointer 的原生细节。
- **[examples.md](examples.md)**：扩展范式 + 会话底座（仅文档，无内置 demo）。
