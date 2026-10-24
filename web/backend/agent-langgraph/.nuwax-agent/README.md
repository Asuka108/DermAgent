# .nuwax-agent 开发配置

本目录存放工作流编排模板的**平台侧**（主平台）元数据：开发配置、能力分层、打包契约。

它与 `config/` 有意分开：

- `config/` 是运行时应用配置，由 `loadFlowConfig` → 本仓 config-loader 加载（flow-ts 自包含，不依赖 app-ts）。
- `.nuwax-agent/` 是平台侧元数据，供打包流程、`flow capabilities` CLI 使用。

这里不放真实密钥 —— 用占位符如 `${SECRET_OPENAI_API_KEY}`，最终值由 ACP / 环境变量注入。

## 能力来源分层

| 层 | 示例 | 归属 |
| --- | --- | --- |
| 工作区配置 | 系统提示词、MCP 服务、skills、模型、subagent（`config/`、`prompts/`、`builtin/`、`.agents/`） | 项目内配置文件 |
| Agent 内置 | 运行时工具（bash/fs/grep·glob/http/json/load_skill/task）、压缩、demo 工具 | 项目包 |
| 环境内置 | API key、base URL、日志路径 | 云计算机 / 本机 / 安装器 |
| Agent 内置文件 | 会话存储（文件 JSON checkpointer） | 用户目录（`~/.flowagents/<workspace 散列>/`） |
| 包占位符 | `${INSTALL_ROOT}`、`${PACKAGE_VERSION}` | 构建与安装流水线 |

## 文件

- `capability-sources.json` —— 把每项能力映射到其来源层（workspace-config / agent-builtin / env-builtin / agent-builtin-file / package-placeholder）。这是 `flow capabilities` 读取的契约。
- `package.config.json` —— 声明打包目标、esbuild-bundle 依赖策略、安装时占位符替换、平台矩阵，以及供平台读取的 `include`/`exclude` 元数据。**注意**：实际打进压缩包的文件由 [`scripts/lib/staging.mjs`](../scripts/lib/staging.mjs) 的 `STAGING_EXCLUDES` 黑名单决定（`package.mjs` 用 `copyPackageTree` 复制整树后排除）；此处 `include`/`exclude` 仅为平台侧元数据，**改它不改变实际打包内容**，二者需手动保持一致。
- `placeholders.json` —— 列出打包时与安装时的占位符（OpenAI 兼容与 Anthropic 两套模型 env、安装根路径）。

## 运行时各层如何被消费

- **systemPrompt** —— `resolveSystemPrompt(appConfig, sessionConfig, root)`：无 session 时 `config.agent.systemPrompt` / `prompts/flow.base.md` > 内联 fallback；有 ACP session 时本地身份提示词保留，host 补充指令**追加**其后（不覆盖）。
- **mcpServers** —— runtime-context 加载 `config/mcp.default.json`；native 工具经 `@langchain/mcp-adapters` 的 `MultiServerMCPClient.getTools()` 注入 `ctx.mcpTools` 并合并进 `allTools`。平台登记的 Plugin / Workflow / Knowledge 等能力运行期同样经会话/MCP 适配后进入 `allTools`（与本地 MCP server 同路径）。ACP session 可合并追加（`session-wins`）。
- **model** —— `resolveModel(appConfig)` 取自 `config.model`（ACP session / env / config / defaults）。
- **skills** —— `resolveSkillsPaths(appConfig)` 发现 `agentsDirectories` 下各 `<root>/skills/`（含 `builtin/skills/`、`.agents/skills/`）及 `config.skills.directories`。
- **subagents** —— `resolveSubagentPaths` / `discoverSubAgents` 发现 `agentsDirectories` 下各 `<root>/agents/`（含 `builtin/agents/`、`.agents/agents/`）；可选 flat `subagents.directories`。
- **sessionStore** —— `FileCheckpointSaver`（继承 `MemorySaver`）持久化到 `config.memory.dir`（默认 `~/.flowagents/sessions/<workspace 散列>/`，可显式 opt-out 回 `./.flow-sessions`）；线程隔离、重启存活、恢复 interrupt/resume。
- **builtInTools** —— `createFlowTools(ctx)` 组合 bash/fs/grep·glob/http/json + `load_skill`/`task`/`write_todos` + demo 工具 + `ctx.mcpTools`（含 MCP 与平台适配工具）；经 `bindTools` 绑定到模型，由 `ToolNode` 执行。固定管道节点可用 `pickTools(allTools, names)` 或 `createPlatformToolActionNode` 按工具名选取子集。

运行时查询：`pnpm capabilities`（工作区开发，无需凭证）；平台安装包产品名为 **nuwax-flow-ts**。
