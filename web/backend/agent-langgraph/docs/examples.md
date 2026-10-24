# 扩展范式与可复制骨架

本包**不提供**可切换的内置场景 demo：无 `examples/` 实体、无 `libs/topologies/` 预设图、无 scaffold 生成 flow。产品入口默认只有 **ReAct default flow**（`flow.active: "default"`，见 [`src/app/graph.ts`](../src/app/graph.ts)）。

本文的作用是给开发 Agent / 人类开发者一个不走偏的落地索引：先判断是否真的需要改图；需要时再按骨架改 `src/app/graph.ts` / `src/app/state.ts` / `src/app/default-flow.ts`。Factory API 详见 [node-kit.md](./node-kit.md)，节点选型见 [node-catalog.md](./node-catalog.md)，Send / Command / interrupt 进阶见 [flow-patterns.md](./flow-patterns.md)。

## 先判定：default 是否已经够用

| 需求 | 默认做法 | 是否改图 |
|------|----------|----------|
| 开放追问、客服、通用助手、搜索总结；以及模糊/未指明形态 | `flow.active: "default"` + systemPrompt + 平台能力登记 | 否 |
| 让模型按需调用平台工具 / MCP 工具 | 平台登记后由宿主注入会话，默认图 `think.bindTools(runtime.allTools)` | 否 |
| 必须固定阶段顺序（先 A 再 B 再 C） | 手写固定管道 | 是 |
| 必须 Send 并行、多源聚合、条件重试 | 手写图或子图 | 是 |
| 必须 multi-turn HITL（人审 / 审批 / 定稿） | 手写 HITL 图，走 interrupt/resume | 是 |

> 说不清“default 为什么不够”时，不要改图。默认图已经具备多轮记忆、checkpoint、自动压缩、流式响应和工具调用回路。

## 交付底座：所有范式都要保留

| 能力 | 机制 | 默认路径 |
|------|------|----------|
| 多轮持续会话 | 稳定 `threadId` + Messages 历史累积 | `createStatefulFlow({ conversational: true })`，默认图已开 |
| checkpoint | `FileCheckpointSaver` 落盘，跨重启恢复 | runtime 注入 `checkpointer`，图 `.compile({ checkpointer })` |
| 自动压缩 | `applyCompaction` 超阈值摘要写回 | `createStatefulFlow` + `appConfig` + `config.compaction` |
| 工具事件 | `createToolExecNode` / `runTool` 三态回调 | surface 注入 callbacks |

自建图时仍应经 `src/index.ts` 的 `materializeFlow → createStatefulFlow` 物化，禁止手写外层 run-loop。

## 自建图改哪些文件

| 文件 | 何时改 | 注意 |
|------|--------|------|
| `src/app/state.ts` | 新增 `draft` / `docs` / `feedback` 等业务状态 | Send 并行通道必须加 reducer；节点名不要与 channel 同名（R-G007） |
| `src/app/graph.ts` | 改节点和边 | 用户可见大段输出用 `createLlmStreamNode` + `r.text`（R-G009） |
| `src/app/default-flow.ts` | `toInput` / `toResult` / 额外 runtime 注入变化 | 需要 MCP 主动检索时，可把 `runtime.ctx.mcpServerConfigs` 注入图配置 |
| `docs/*` | 图语义变了 | 同步相关说明（R-G003，SHOULD） |

常用模型 helper：

```ts
import { requireModel } from "../libs/nodes/model-resolver.js";
import { resolveLlmResilience } from "../runtime/services/llm-resilience.js";

const model = requireModel(cfg.config, "my-flow");
const { longTimeoutMs } = resolveLlmResilience(cfg.config);
```

`requireModel` 会在缺凭证时直接报错；不要在真实业务固定管道里回落 demo 假结果。

## 默认 ReAct + 平台能力

开放对话 + 工具调用优先走默认图，不写 graph：

1. 在平台侧登记 Plugin / Workflow / Knowledge / MCP。
2. 已登记工具进入运行时：**宿主注入**，或开发期把真实 schema **固化为** `FlowDef.platformToolRefs` 再经 `createFlowRuntime` 装配为 `StructuredTool`。图内按需用于独立节点（`createPlatformToolActionNode`）、局部工具集合（`pickTools` → `createToolExecNode`），或并入 `runtime.allTools` 供默认 ReAct `bindTools`。
3. 把领域说明写进 systemPrompt，例如“需要实时信息时优先调用已登记搜索工具，汇总来源后回答”。
4. 本地快检：`pnpm flow "…"`；端到端在平台预览会话经 ACP 验证。

运行时关系：平台宿主可把已登记能力注入会话；固化路径经 `createPlatformStructuredTool` 生成工具。默认 ReAct 便捷路径是并入 `runtime.allTools` 后在 `think` 中 `bindTools`；固定管道不必全部进 `allTools`，按节点或集合接线即可。

## 固定线性管道

适合翻译、摘要、打分、报告生成等固定步骤。核心结构：`draft → generate`（入口 query 由 `default-flow.ts` 的 `toInput` 写入 state）。

```ts
// src/app/state.ts 节选：先声明业务 channel
import { Annotation, MessagesAnnotation } from "@langchain/langgraph";

const lastValue = <T>(_: T, n: T): T => n;

export const FlowStateAnnotation = Annotation.Root({
  ...MessagesAnnotation.spec,
  input: Annotation<string>({ value: lastValue<string>, default: () => "" }),
  output: Annotation<string>({ value: lastValue<string>, default: () => "" }),
  draft: Annotation<string>({ value: lastValue<string>, default: () => "" }),
});

export type FlowState = typeof FlowStateAnnotation.State;
```

```ts
// src/app/graph.ts 节选：draft → generate
import { StateGraph, START, END } from "@langchain/langgraph";
import { HumanMessage } from "@langchain/core/messages";
import {
  createLlmNode,
  createLlmStreamNode,
} from "../libs/nodes/index.js";
import { requireModel } from "../libs/nodes/model-resolver.js";
import { resolveLlmResilience } from "../runtime/services/llm-resilience.js";
import { FlowStateAnnotation, type FlowState } from "./state.js";

export function createFlowGraph(cfg: CreateFlowGraphConfig = {}) {
  const model = requireModel(cfg.config, "linear-flow");
  const { longTimeoutMs } = resolveLlmResilience(cfg.config);

  const draft = createLlmNode<FlowState>({
    model,
    config: cfg.config,
    prompt: (s) => [new HumanMessage(`先产出草稿：${s.input}`)],
    write: (r) => ({ draft: r.content } as Partial<FlowState>),
  });

  const generate = createLlmStreamNode<FlowState>({
    model,
    config: cfg.config,
    timeoutMs: longTimeoutMs,
    prompt: (s) => [new HumanMessage(`基于草稿输出终稿：${s.draft}`)],
    write: (r) => ({ output: r.text } as Partial<FlowState>),
  });

  return new StateGraph(FlowStateAnnotation)
    .addNode("draft", draft)
    .addNode("generate", generate)
    .addEdge(START, "draft")
    .addEdge("draft", "generate")
    .addEdge("generate", END)
    .compile({ checkpointer: cfg.checkpointer });
}
```

## 主动平台工具调用

固定管道里“每轮必搜 / 必查接口”时，不等模型 tool_calls，直接从 `allTools` 按真实工具名定位并 `invoke`。

```ts
import type { StructuredTool } from "@langchain/core/tools";

function webSearchNode(allTools: StructuredTool[]) {
  const tool = allTools.find((t) => t.name === "Plugin_<id>");
  if (!tool) throw new Error("平台工具未注入本次会话：Plugin_<id>");

  return async (s: { query: string }) => {
    const raw = await tool.invoke({ query: s.query });
    return { searchResult: raw };
  };
}
```

若你需要一个 ReAct 子回路执行上一条 `AIMessage.tool_calls`，`createToolExecNode` 的 `tools` 必须是 `StructuredTool[]`：

```ts
import { pickTools } from "./tool-bindings.js";
import { createToolExecNode } from "../libs/nodes/index.js";

const selectedTools = pickTools(allTools, ["Plugin_<id>"]);
if (selectedTools.length === 0) throw new Error("平台工具未注入本次会话：Plugin_<id>");
const toolsNode = createToolExecNode<FlowState>({ tools: selectedTools, config: cfg.config });
```

不要传 `tools: ["工具名"]`。

## 检索增强问答（RAG）

固定结构：`rewrite → retrieve → grade → prepare → generate`。只有“每轮必须先检索再答”时才写这个；开放追问 + 偶尔检索优先默认 ReAct。

`createMcpRetrievalNode` 需要 MCP server 配置。默认 `CreateFlowGraphConfig` 不含 `mcpServerConfigs`；如果要主动 MCP 检索，先扩展 app 层配置，并在 `default-flow.ts` 里从 runtime 注入：

```ts
// src/app/graph.ts
export interface CreateFlowGraphConfig {
  // ...原字段
  mcpServerConfigs?: Record<string, McpServerConfig>;
}

// src/app/default-flow.ts
buildGraph: (checkpointer) =>
  createFlowGraph({
    allTools: runtime.allTools,
    checkpointer,
    config: runtime.config,
    systemPrompt: runtime.systemPrompt,
    mcpServerConfigs: runtime.ctx.mcpServerConfigs,
  }),
```

节点骨架：

```ts
import { StateGraph, START, END } from "@langchain/langgraph";
import { HumanMessage } from "@langchain/core/messages";
import {
  createLlmNode,
  createLlmStreamNode,
  createMcpRetrievalNode,
} from "../libs/nodes/index.js";

const afterGrade = (s: { grade?: string; retries?: number }) =>
  s.grade?.startsWith("yes") || (s.retries ?? 0) >= 2 ? "generate" : "rewrite";

const retrieve = createMcpRetrievalNode<MyState>({
  mcpServers: cfg.mcpServerConfigs ?? {},
  retrieve: (s) => ({
    server: "knowledge",
    tool: "search",
    args: { query: s.searchQuery ?? s.input },
  }),
  write: (r) => ({ docs: r.ok ? r.text : "", retrievalError: r.ok ? "" : r.text }),
});

return new StateGraph(MyStateAnnotation)
  .addNode("rewrite", rewrite)
  .addNode("retrieve", retrieve)
  .addNode("grade", grade)
  .addNode("generate", generate)
  .addEdge(START, "rewrite")
  .addEdge("rewrite", "retrieve")
  .addEdge("retrieve", "grade")
  .addConditionalEdges("grade", afterGrade, ["rewrite", "generate"])
  .addEdge("generate", END)
  .compile({ checkpointer: cfg.checkpointer });
```

规则要点：条件边返回值必须在 targets 内（R-G004）；`generate` 用 `createLlmStreamNode`。

## 人工确认（HITL）

结构：`compose → review(interrupt) → finalize`。HITL 阶段需要 checkpoint，物化时通常不设置 `conversational: true`，让 surface 暴露 `hasStarted` 并走 `resume`。

> 重要：组合根当前按 `FlowDef.profile.interaction === "chat"` 自动传 `conversational: true`。如果你把默认图改成 HITL / approval 图，必须同步调整 `src/app/flows/index.ts` 里的 profile（例如 `interaction: "approval"`），或提供独立 flow 定义；否则 surface 会按 chat 方式每轮 `query`，interrupt/resume 语义会错。

```ts
import { StateGraph, START, END } from "@langchain/langgraph";
import { HumanMessage } from "@langchain/core/messages";
import {
  createLlmStreamNode,
  createHumanApprovalNode,
  createApprovalFinalizeNode,
} from "../libs/nodes/index.js";

const compose = createLlmStreamNode<MyState>({
  model,
  config: cfg.config,
  timeoutMs: longTimeoutMs,
  prompt: (s) => [new HumanMessage(`写一版草稿：${s.input}`)],
  write: (r) => ({ draft: r.text }),
});

const review = createHumanApprovalNode<MyState>({
  question: (s) => `草稿如下，回复 ok 通过，或给出修改意见：\n\n${s.draft}`,
  write: (feedback, approved) => ({ feedback, approved }),
});

return new StateGraph(MyStateAnnotation)
  .addNode("compose", compose)
  .addNode("review", review)
  .addNode("finalize", finalize)
  .addEdge(START, "compose")
  .addEdge("compose", "review")
  .addEdge("review", "finalize")
  .addEdge("finalize", END)
  .compile({ checkpointer: cfg.checkpointer });
```

结构化表单（平台 ask-question）要拆成两节点：`present_review` direct-invoke ask-question MCP 展示卡片，`review` 用 `createHumanApprovalNode` 接收 resume。不要把展示工具当 checkpoint/resume 机制。

## Send 并行与 reflection

- 多源并行：`gather → Send research×N → aggregate`，并行写入的 channel 必须有 reducer；`aggregate` 用 `createLlmStreamNode`。
- reflection / 评审重做：优先 `createLlmRouterNode`；Command `goto` 节点必须在 `addNode(..., { ends: [...] })` 声明目标，否则 `pnpm graph` 反射会漏边。

完整 API 和注意事项见 [flow-patterns.md](./flow-patterns.md)。

## 不要做的事

- 不要恢复 `scripts/scaffold/` 或 `libs/topologies/` 作为入口。
- 不要在 `src/app/flows/` 挂场景薄封装，除非用户明确要求同一项目保留多套可切换图。
- 不要手写外层 run-loop；走 `createStatefulFlow`。
- 不要把用户可见大段输出写成 `createLlmNode`。
- 不要把平台工具名字符串直接传给 `createToolExecNode.tools`。
