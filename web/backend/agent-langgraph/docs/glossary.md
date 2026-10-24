# Glossary

> 术语权威源。写文档/注释只查「用」列。实现细节 → `node-kit.md`；systemPrompt 解析 → `src/runtime/context/prompt.ts`。改图判定 → `docs/examples.md` § 先判定。

## 编排范式（强制 / 禁止）

| 弃用 | 用 | 定义 |
|---|---|---|
| 自由工具循环、自由 tool loop、其它非图范式 | **LangGraph TS / `StateGraph` 图编排** | 本模板唯一编排范式：`StateGraph` + node/edge；**禁止**自由 tool loop（无显式图、模型自驱调工具循环） |

## 编排 / 流程

| 弃用 | 用 | 定义 |
|---|---|---|
| 长任务、长任务硬化（持久化义） | **durable stateful flow** | `createStatefulFlow` 基座；多阶段 + multi-turn HITL + cross-restart resume |
| 长任务（耗时/token 义） | **long-running** | 仅运行耗时长；无跨重启持久化语义 |
| 跨重启续跑 | **cross-restart resume** | checkpointer 落盘；进程/IDE 重启后续跑 |
| 阶段进度 | **stage progress** | `emitStage` → `onStage` |
| 单步护栏、递归护栏（非 recursion 语境） | **recursion guard** | 仅指 `recursionLimit` 防 reflection 回边死循环 |
| 一个会话一个主题 | **one session, one topic** | `hasStarted` 推断续跑 vs 新开 |
| 多模式 stream | **multi-mode stream** | `streamMode: ["messages","tools","custom","updates"]` + `mapStreamChunk` |
| 上下文压缩 | **context compaction** | `compactHistory` + `RemoveMessage` |
| 跨 turn 人审、多轮人审 | **multi-turn HITL** | 跨用户回合的人审循环（interrupt → 用户回复 → resume）；须 checkpointer |
| interrupt+resume、人审断点 | **interrupt / resume** | LangGraph `interrupt()` 暂停；surface 用 `Command({ resume })` 续跑；见 `flow-patterns.md` |
| Send 并行、fan-out、多源聚合 | **Send / fan-out** | `Send` 并行派发多份子 state，reducer 聚合；见 `flow-patterns.md` |
| 条件重试、自纠正回边 | **条件重试** | `addConditionalEdges`（或 router）按评分/门控回到 rewrite 等节点；返回值须 ∈ targets（R-G004） |
| 工具回路 | **ReAct** | 默认图标准回路：`START → prepare → think(bindTools) ⇄ tools(ToolNode) → respond → END`；`toolsCondition` 有 tool_calls 回 think，否则进 respond；**不是**自由 tool loop |
| 完成闸门 | **completion gate**（完成闸门） | 强制验证门；按模板 README 的工程验证矩阵选择与改动范围匹配的验证项 |

## 默认图（default ReAct）

唯一开箱产品入口。标准 ReAct，经 **StatefulFlow conversational** 运行（稳定 threadId + checkpointer 多轮记忆 + `graph.stream` 真流式；见 `src/app/default-flow.ts`）。权威叙述 → README § 默认图 + `src/app/graph.ts`。

```
START → prepare → think(model.bindTools) ──(toolsCondition)──┐
                      ▲                                      ├─ 有 tool_calls → tools(ToolNode + onToolCall) → think
                      └──────────────────────────────────────┘
                                               └─ 无 tool_calls → respond(流式) → END
```

| 弃用 | 用 | 定义 |
|---|---|---|
| - | **prepare** | input → HumanMessage，追加到 Messages 历史（`MessagesAnnotation`） |
| - | **think** | `bindTools`，模型决定调工具或回答（原生 function-calling） |
| - | **tools** | 执行 tool_calls + `onToolCall` 三态透出；prebuilt `ToolNode` + `toolsCondition` |
| - | **respond** | 取回答流式输出（onToken）后 END |

> **会话压缩**不在 `prepare` 内：由 `createStatefulFlow` 在每轮新 `query` 入口调用 `applyCompaction`（消费 `config.compaction`）。

## Flow 交互形态

面向用户仅两类形态（「用」列中文）：**聊天助手型** / **固定流程型**。`chat` / `pipeline` 为对应机器值；`approval` 是固定流程型内**人审编排**的机器值（不作独立形态问用户）。

**改图判定**以 `docs/examples.md` § 先判定为唯一权威：说不清「default 为什么不够」就不要改图；命中能力门槛（固定阶段顺序、Send 并行/多源聚合/条件重试、multi-turn HITL）再手写 `src/app/graph.ts`。

| 弃用 | 用 | 定义 |
|---|---|---|
| 无须写图的聊天模式名 | **聊天助手型** / **default flow** | `flow.active: "default"`；标准 **ReAct** + StatefulFlow conversational；**唯一默认路径**；开放追问 / 客服 / 通用助手等多数场景只改 systemPrompt + 按需登记平台能力，勿手写图 |
| 管线、pipeline（对用户） | **固定流程型** | 机器值 `pipeline`；须能说明 default 不够（固定阶段 / Send / 条件重试等）才手写 `src/app/graph.ts`；流程内人审用 HITL interrupt/resume（机器值 `approval`） |
| ~~人工确认型~~（已并入固定流程型） | **HITL / 人审编排** | 非独立用户形态；`createHumanApprovalNode` + interrupt/resume；含审批 / 人工复核 / 定稿 |
| flow 类型、图类型 | **flow profile** | `flows --json`；含 `interaction` / `implementation` / `userLabel` |
| 为了写图而写图 | **graphReason** | 手写图时须说明 default 为何不够（能力门槛，非「用户念出固定流程四字」） |

| 用户中文 | 机器值 | 默认落点 |
|---|---|---|
| 聊天助手型 | `chat` | **`default`（唯一默认）**；追问/客服/问答/搜索总结/模糊未指明形态 → 不写图 |
| 固定流程型 | `pipeline`（人审步骤 `approval`） | 命中能力门槛 → 手写 `src/app/graph.ts`；需审批/复核/定稿则加 multi-turn HITL（interrupt/resume） |

| 弃用 | 用 | 定义 |
|---|---|---|
| - | **`flow.active`** | config 正式 flow 字段；缺省 `default` |

## 架构 / 接入层

| 弃用 | 用 | 定义 |
|---|---|---|
| 运行时胶水代码 | **接入逻辑**（plumbing） | surface 侧 ACP/CLI 复用连接代码 |
| - | **接入层**（seam） | surface 与图解耦接缝 |
| - | **surface seam** | surface 侧 seam |
| - | **可运行 flow 挂载点**（app/flows） | app 层注册入口；**仅 default**；图逻辑在 `graph.ts` |
| 硬塞进 factory、强行 factory 化 | **Bespoke nodes**（定制节点） | 不宜塞进 `src/libs/nodes/` factory 的手写节点（如多源检索合并、文件交付、对话路由等）；保持手写；见 `docs/node-catalog.md` § BESPOKE |

## 平台侧

| 弃用 | 用 | 定义 |
|---|---|---|
| 平台面、厂商产品名指主平台 | **平台侧** | 主平台元数据/接口：部署、在线配置、`mcpServers`、能力登记 |
| dockpanel、结构化审阅卡片 | **平台问答卡片** | 主平台 ACP 侧 `nuwax_ask_question` + `rawInput.ui` 问答 UI；展示层，不替代 interrupt/resume |
| 模版 | **模板** | 错别字订正 |

## 保留中文（勿机械英文化）

| 弃用 | 用 | 定义 |
|---|---|---|
| topology（对用户分类） | **拓扑** | 内部图实现；用户分类见 §Flow 交互形态；本包无内置拓扑库 |
| guard（统称，无语境） | **护栏** | recursion → recursion guard；超时 → timeout guard（`withTimeout`）；校验 → input-validation guard |
| - | **流水线** | pipeline；多阶段编排 |

## systemPrompt

| 弃用 | 用 | 定义 |
|---|---|---|
| persona、人设、统一人格 | **场景系统提示词** | `agent.systemPrompt` / ACP 下发；角色/风格/输出约束 |
| - | **runtime.systemPrompt** | `resolveSystemPrompt`：本地 `prompts/flow.base.md` + ACP 追加 + `PLATFORM_CONVENTIONS` |
| - | **领域 prompt** | 图内各 LLM 节点 SystemMessage；与旧 scaffold spec 无关 |
| 「systemPrompt 注入图」（无来源/节点） | 写明 **runtime/config** + **目标节点** | 例：`runtime.systemPrompt → think` |

| 图 | 注入点 | runtime | 备注 |
|---|---|---|---|
| default | prepare / think（优先） | config / ACP 兜底 | 单 ReAct conversational |

## 格式

中文行内标点全角：`：` `（）` `、`；代码/路径/英文短语半角。
