# 能力分层与配置

当前工作目录把 Agent 的每一项能力归入一个**来源层**，明确「谁能改、从哪来」。
项目内配置文件、环境变量、内置运行时各司其职。

## 来源层

| 层 | 来源 | 例子 | 可编辑 |
| --- | --- | --- | --- |
| `workspace-config` | 项目内配置文件 | systemPrompt、mcpServers、skills、model、subagents | ✅（改 `config/` / `prompts/` / `builtin/` / `.agents/`） |
| `agent-builtin` | 项目内置 | bash / 文件读写 / grep·glob / http / json / load_skill / task / compaction / demo（native MCP 见下） | ❌（改 `src/` 源码） |
| `env-builtin` | 环境变量 | API key、base URL、`LOG_LEVEL`、`LOG_DIR` | env / `.env` |
| `agent-builtin-file` | 用户级会话目录（文件，无 DB） | sessionStore（默认 `~/.flowagents/sessions/<workspace 散列>/`，可经 `config.memory.dir` opt-out 回 `./.flow-sessions`） | ❌ |
| `package-placeholder` | 平台安装占位（开发期忽略） | `${INSTALL_ROOT}`、`${PACKAGE_VERSION}` | ❌ |

完整映射：[.nuwax-agent/capability-sources.json](../.nuwax-agent/capability-sources.json)（平台侧元数据；源码开发无需阅读）。

## 查询

在本仓库根目录执行：

```bash
pnpm capabilities   # 无凭证，输出工具/MCP/skills/能力分层
pnpm sessions       # 已持久化的会话
```

## 每项能力从哪进图

`FlowRuntime`（接口 [src/runtime/flow-runtime.ts](../src/runtime/flow-runtime.ts)，装配工厂 `createFlowRuntime` 见 [src/index.ts](../src/index.ts)）在启动时把各层组装成一处，注入图节点：

- **systemPrompt** — `resolveSystemPrompt`：无 ACP session 时 `config.agent.systemPrompt` / `prompts/flow.base.md` > inline fallback；有 ACP session 时保留本地 `prompts/flow.base.md` 身份，**追加** host 在 `session/new` 下发的补充指令，再追加 `PLATFORM_CONVENTIONS`（不覆盖本地提示词）。
- **mcpServers** — `config/mcp.default.json`（`config.mcp.configPath`），默认内置 `ask-question`（结构化提问 fallback，图内 HITL 即 **平台问答卡片**，见 [glossary.md](./glossary.md)）；native 工具经 `@langchain/mcp-adapters`（`MultiServerMCPClient`）由 runtime-context 加载，支持 **stdio / Streamable HTTP / SSE**（有 url 时默认 Streamable HTTP，`automaticSSEFallback` 失败后再试 SSE；连接成功与否以 **tools/list** 为准，session 结束 `destroyRuntimeContext` 关闭连接）。ACP session 可合并追加（`session-wins`，平台同名覆盖内置），日常扩展改 `config/mcp.default.json` 即可。
- **model** — `resolveModel`（env > `config.model` > 默认）。协议优先级：`API_PROTOCOL` > `LLM_PROVIDER` > 凭证启发式 > `config.model.provider`（`anthropic` | `openai`）。
- **skills** — `discoverSkills` 发现 `agentsDirectories` 下各 `<root>/skills/`（默认含 `builtin/skills/`、`.agents/skills/`）及 `config.skills.directories` 的 SKILL.md；经 `renderSkillsSection` 注入清单，模型用 `load_skill(name)` 渐进式读正文。
- **subagents** — `discoverSubAgents` 发现 `agentsDirectories` 下各 `<root>/agents/`；默认 ReAct 图经 `task({ subagent_type, description })` 委派。自定义图可用 subgraph（见 [flow-patterns.md](flow-patterns.md)）。
- **builtInTools** — `createFlowTools(ctx)`（[src/app/flow-tools.ts](../src/app/flow-tools.ts)）组装 http/json + bash/fs/grep·glob + `load_skill`/`task` + native MCP + demo，`bindTools` 绑给模型、`ToolNode` 执行。`http_request` 默认拦截私有/loopback/链路本地/云元数据端点（防 SSRF）+ 响应字节上限（防 OOM）；需访问内网改用 `createHttpRequestTool({ allowPrivateNetwork: true })`。
- **platformToolRefs** — `FlowDef.platformToolRefs`（或 `createFlowRuntime({ platformToolRefs })`）传入的平台 Plugin / Workflow / Knowledge 引用；经 `createPlatformToolDescriptors` + `createPlatformStructuredTool` 生成 `StructuredTool`。schema **不是** `*.flow.json` / 旧 scaffold `spec.tools`；须用平台已登记工具的真实配置固化（`targetType` / `targetId` / `schema` 等）。运行期也可由宿主直接注入等价工具。二者可并用；**不必**全部并入 `allTools`——图内也可把固化/注入得到的 `StructuredTool` 用于独立节点（`createPlatformToolActionNode`）或局部工具集合（`pickTools` → `createToolExecNode`）；并入 `allTools` 是默认 ReAct 的便捷路径。
- **compaction** — [src/libs/compaction.ts](../src/libs/compaction.ts)，消费 `config.compaction`。
- **sessionStore** — `FileCheckpointSaver`（继承 `MemorySaver`），默认持久化到 `~/.flowagents/<workspace 散列>/`（`resolveSessionDir` 按 workspace 隔离）；设 `config.memory.dir` 为相对路径可 opt-out 回项目内。CLI：`sessions` 列出、`sessions delete <id>` 删除。

## 扩展（不改 `src/libs/` 保护区）

- **加平台能力 / MCP**：搜索、文档、业务 API 等须在**平台侧**登记。把已登记工具的真实 schema 写入 **`FlowDef.platformToolRefs`**（字段：`targetType` / `targetId` / `schema` 等；工具名运行时按 `targetType_targetId` 或 `toolName` 拼），再经 `createFlowRuntime({ platformToolRefs })` 构建 `StructuredTool`；或依赖宿主会话直接注入。图侧按需接线：**独立节点** `createPlatformToolActionNode`、**局部集合** `createToolExecNode` / `pickTools`、**可选** default ReAct `bindTools(allTools)`——**零包装代码**。勿再写旧 scaffold 的 `spec.tools` / `*.flow.json`。本地 MCP 调试可参考 [config/mcp.examples.json](../config/mcp.examples.json)（chrome-devtools / filesystem / bash 等），复制到 `servers` 或经会话下发。
- **加 Skill**：
  - **项目内置（推荐）**：`builtin/skills/<name>/SKILL.md`（`agentsDirectories` 含 `./builtin`）。
  - **工作区扩展**：`.agents/skills/<name>/SKILL.md`，或在 `config.skills.directories` 增加目录。
- **加 Subagent**：
  - **项目内置**：`builtin/agents/<name>/AGENT.md`。
  - **工作区扩展**：`.agents/agents/<name>/AGENT.md`。
  - **代码级复用** → subgraph（见 [flow-patterns.md](flow-patterns.md) § subgraph）。
- **改系统提示词**：编辑 `prompts/flow.base.md`，或设 `config.agent.systemPrompt` / `config.agent.systemPromptPath`。
- **换模型**：改 `config.model` 或设 `ANTHROPIC_MODEL` / `OPENAI_MODEL`（见 [`.env.example`](../.env.example)）。
- **换协议**：设 `API_PROTOCOL=anthropic|openai`，或 `LLM_PROVIDER`（同义别名）。`loadConfig` 会打 `platformModelEnv` / `resolveModelProvider` 诊断日志（密钥脱敏）；未替换的 `{MODEL_PROVIDER_*}` 占位符会 `warn`。

## 业务场景：平台能力如何进图

这一节是平台能力接入的**选型权威**。先在平台登记并取得真实配置，再根据业务控制方式选择注入或固化、以及工具在图中的位置；不要因为“能调用”就一律塞进 `allTools`。

| 业务场景 | 工具来源 | 图内接线 | 取舍 |
|---|---|---|---|
| 用户开放提问，模型自行决定是否查搜索、知识库或业务 API | 优先宿主注入；需要可复现配置时固化 `platformToolRefs` | 并入 `runtime.allTools`，`think.bindTools(...)` | 适合聊天助手；不改图即可让模型按需调用 |
| 每次流程都必须调用某个能力，例如“先查额度再审批” | 固化 `platformToolRefs`，使用真实 `targetType` / `targetId` / `schema` | `createPlatformToolActionNode(...)` 或节点内 `StructuredTool.invoke(...)` | 调用顺序由图保证，不依赖模型是否产生 tool call |
| 某个子图或 ReAct 阶段只能使用少量能力 | 宿主注入集合或固化产物的子集 | `pickTools(...)` → `createToolExecNode(...)`，或 `bindTools(selected)` | 缩小权限与模型选择空间，避免无关工具干扰 |
| 不同租户、会话或平台环境决定可用能力，代码不应保存其 schema | 宿主会话注入 | 使用注入集合；运行时检查工具是否存在 | 保持动态性；不要把短期或环境专属 schema 写入项目 |
| 平台能力需要随代码版本稳定复现，或固定管道必须依赖它 | `get-config --key tools --full` 返回的真实配置固化为 `FlowDef.platformToolRefs` | runtime 的 `createPlatformStructuredTool` 生成 `StructuredTool`，再按上述方式接线 | schema 声明即接入；不另写 HTTP / `tool()` 包装 |
| 平台搜索和登记后确认没有能力可用 | 在 `src/app/` 自写业务工具 | 按 [node-kit.md](node-kit.md) 的 `tool()` + Zod 注册到 `createFlowTools()` | 这是最后手段；记录搜索依据，密钥只从环境读取 |

选型顺序：先问“**调用由模型决定还是流程决定**”，再问“**是否只应暴露给局部节点**”，最后问“**配置需不需要随代码稳定复现**”。前两问决定 `allTools`、局部集合或独立节点；最后一问决定宿主注入还是 `platformToolRefs` 固化。纯 LLM 对话不需要为了预防性扩展而接入工具。

固化时必须使用平台已登记工具的完整真实配置（包括 `toolName`、`targetType`、`targetId`、完整 `schema` 及运行所需字段），而不是搜索结果的片段。`FlowDef.platformToolRefs` 经 `createPlatformToolDescriptors` / `createPlatformStructuredTool` 生成的是 LangChain `StructuredTool`，供 LangGraph 节点和 `bindTools` 消费；它不是旧 `*.flow.json` 或 `spec.tools` 配置。

## 工具接入优先级

在本仓库内扩展业务能力时，按下列顺序判断（扩展方式见上文 [扩展（不改 src/libs/ 保护区）](#扩展不改-srclibs-保护区)）：

1. **平台能力（schema 声明即接入）** — `FlowDef.platformToolRefs`（或宿主注入的等价工具，**非** flow.json）声明的 Plugin / Workflow / Knowledge，runtime 按 schema 转成 `StructuredTool`；可按需用于独立节点、局部工具集合，或并入 `FlowRuntime.allTools` 供 conversational ReAct bind——**零包装代码**
2. **内置 `libs/tools`** — bash / 读写 / grep·glob / http / json / load_skill / task / demo
3. **自写 `src/app/`** — 最后手段（仅平台确无命中的真外部 API），在 [flow-tools.ts](../src/app/flow-tools.ts) 注册

密钥用环境变量或 `.env`，禁止硬编码；平台内部端点/envelope 一律不得猜测或硬编码。
