# Flow 编排模式参考(少用 / 进阶)

默认图(`src/app/`)演示的是**常用模式**:纯逻辑节点、LLM 节点、工具调用节点、条件边+循环、state 累积、流式输出、无凭证降级(见各 `nodes/*.ts` 顶部注释)。

这一页收**少用但框架支持**的模式——平时不一定用,但需要时知道有、能照着抄。API 对齐已安装的 `@langchain/langgraph@1.x`(均已导出:`Send`、`interrupt`、`Command`、`MemorySaver`)。

> **优先用 factory**:下面的 Send / interrupt / subgraph 等模式,框架已收成 `src/libs/nodes/` 的 factory
> (`createFanout` / `createHumanApprovalNode` / `createApprovalFinalizeNode` / `createLlmRouterNode` / `createMcpRetrievalNode` / `createSubgraphNode` 等),选型见 [node-catalog.md](node-catalog.md)、API 见 [node-kit.md](node-kit.md)。
> 能用 factory 就别手写原生模式；只有定制节点（bespoke）（多源检索取优、文件交付等，见 node-catalog ②）才回这里看原生 API。

> 真实业务里把这些加到 `src/app/graph.ts` 的 `addNode`/`addEdge`/`addConditionalEdges` 即可。

## 1. Send:并行 fan-out(map-reduce)

一个节点同时派发**多个**子任务(每个一份 state),结果汇总回同一 channel。典型场景:对 N 个条目并行处理再聚合。

```ts
import { StateGraph, START, END, Annotation, Send } from "@langchain/langgraph";

// channel 用 reducer 聚合(并行写才安全):
const S = Annotation.Root({
  items: Annotation<string[]>,
  results: Annotation<string[]>({ reducer: (a, b) => [...a, ...b], default: () => [] }),
});

// fan-out:返回 Send[] 给每个条目派一个 "process" 节点实例
const fanout = (state: typeof S.State) =>
  state.items.map((it) => new Send("process", { ...state, item: it }));

const graph = new StateGraph(S)
  .addNode("process", async (s) => ({ results: [`done: ${s.item}`] }))
  .addConditionalEdges(START, fanout)
  .addEdge("process", END)
  .compile();
```

> 顺序流里默认图靠 `MessagesAnnotation` reducer 累积消息即可；**只有并行写同一 channel 时才必须用 reducer**（否则写入会互相覆盖）。

## 2. interrupt:人工介入(human-in-the-loop)

跑到某节点时**暂停**、把问题抛给用户,拿到回复后继续。

```ts
import { interrupt } from "@langchain/langgraph";

// 在节点里调用即暂停;command 里 resume 的值会作为返回值
const reviewNode = async (state) => {
  const approved = interrupt({ question: "这样可以吗?", draft: state.output });
  return { approved };
};
```

需要配合 checkpointer（见第 5 节）持久化暂停点；**surface**（`surfaces/acp` 或 `surfaces/cli`，经 `createStatefulFlow` 包装）在下一轮把用户回复作为 `resume` 传入，经 `invoke(null, { command: new Command({ resume }) })` 恢复（`Command` 见第 3 节）。

## 3. Command:节点返回式路由 / 更新

节点直接返回 goto + update,不必走 `addConditionalEdges`(适合动态、多分支路由)。

```ts
import { Command } from "@langchain/langgraph";

const router = async (state) => {
  if (state.needsTool) return new Command({ goto: "act", update: { plan: pickNext(state) } });
  return new Command({ goto: "respond" });
};
```

## 4. 子图(subgraph):把一张图当节点用

把已有图 `.compile()` 后作为另一个图的节点,实现编排的分层组合。

```ts
const sub = new StateGraph(SubState).addNode(...).compile();
const parent = new StateGraph(ParentState)
  .addNode("subStep", sub)          // 直接把编译后的图当节点
  .addEdge(START, "subStep")
  .addEdge("subStep", END)
  .compile();
```

## 5. Checkpointer:持久化 / 断点续跑 / 时间旅行

给图加 checkpointer,state 每步落盘 → 可断点恢复、回放、人工介入的暂停点。

```ts
import { MemorySaver } from "@langchain/langgraph";

const graph = createFlowGraph().compile({ checkpointer: new MemorySaver() });
// 同一 thread_id 下多次 invoke 会延续 state
await graph.invoke({ input: "..." }, { configurable: { thread_id: "t1" } });
```

> `MemorySaver` 仅进程内 —— 一旦进程/IDE 重启，暂停点就丢了。模板因此自带
> **`FileCheckpointSaver`**（`src/runtime/services/file-checkpoint-saver.ts`）：标准 checkpointer 协议 +
> 文件落盘，跨进程/重启也能 `getState`/resume。生产规模可换 sqlite/postgres saver（接口已对齐）。
>
> ⚠️ 落盘要点：MemorySaver 的 storage 里存的是 serde 产出的 `Uint8Array`；朴素 `JSON.stringify`
> 会把它变成 `{"0":..}` 普通对象、重载即报 `loadsTyped` 错。`FileCheckpointSaver` 用 base64 包装
> 保真（见该文件 `replaceBytes`/`reviveBytes`）。自定义文件型 saver 时务必处理二进制。

## 6. Durable stateful flow（`createStatefulFlow`）

多阶段、多轮 HITL 的 **durable stateful flow** 光有 `interrupt` 不够——
还要解决 cross-restart resume、stage visibility、reflection 回边/LLM 挂死跑飞。
模板把这些收进 **`createStatefulFlow`**（`src/surfaces/stateful-flow.ts`），有状态示例都基于它：

```ts
import { createStatefulFlow } from "../surfaces/stateful-flow.js";
import { createFileCheckpointer, durableCheckpointer } from "../runtime/services/file-checkpoint-saver.js";

// 组合根通常：checkpointer = createFileCheckpointer(appConfig)
// 自建示例可：durableCheckpointer(appConfig, opts.checkpointer) → 默认 FileCheckpointSaver
export function createMyFlow(appConfig?, opts: { checkpointer?: BaseCheckpointSaver } = {}) {
  return createStatefulFlow<MyState>({
    buildGraph: (cp) => createMyGraph(appConfig, cp),   // 图工厂接收 checkpointer
    toInput: (query) => ({ goal: query }),               // 新任务：query → 初始 state
    toResult: (v) => ({ answer: v.output ?? "" }),       // 终态 → 回答
    checkpointer: durableCheckpointer(appConfig, opts.checkpointer),
    configurable: { appConfig },
    recursionLimit: 50,
    // conversational: true → 多轮开放对话；HITL interrupt 图勿开（需 hasStarted/resume）
  });
}
```

它一处实现了所有有状态示例原本各自重抄的 run-loop，并叠加四个 durable-stateful-flow 能力点：

| Feature | How | Why |
|---|---|---|
| **Cross-restart resume** | checkpointer 默认 `FileCheckpointSaver`（`durableCheckpointer`） | 重启后 interrupt/checkpoint 不丢 |
| **One session, one topic** | `hasStarted()` 读 `graph.getState()` 是否已有 checkpoint（非进程内存） | 首条开题，之后皆 resume 同一项目；重启后仍准 |
| **Stage progress** | `emitStage(config, …)` → `onStage` | 多阶段流水线每步可见（CLI `▸`，ACP message chunk） |
| **Recursion guard** | `recursionLimit` + `withTimeout(model.invoke(...), ms)` | 回边/挂死步骤不拖垮整图 |

> **conversational 与 HITL（对照）**
>
> | | **conversational**（默认图） | **HITL durable** |
> |---|---|---|
> | `conversational` | `true`（不暴露 `hasStarted`） | 默认 `false`（暴露 `hasStarted`） |
> | 每轮入口 | 始终 `query`，历史靠 checkpointer 累积 | 首条开题，interrupt 后 `resume` |
> | 共同底座 | **都要** `FileCheckpointSaver` +（建议）`appConfig` 触发 compaction | 同左 |
>
> 真实业务常是混合：开放多轮用 conversational；某次任务进入人审图时切 HITL 物化，人审完再回开放对话——checkpoint 目录按 `threadId` / workspace 隔离，勿手搓第二套持久化。

> **Context compaction**（long-running flows）：多轮消息超阈值时 `compactHistory` 摘要，再经
> `compactionUpdate` 写回 channel。见 `src/libs/compaction.ts`。

---

**何时该读这里**：默认图是顺序 ReAct 式（够覆盖大多数编排）。一旦你要并行 fan-out、HITL、动态路由、subgraph、checkpoint resume，或 **cross-restart durable stateful flow** —— 回来看对应小节。

---

## 模式与默认图对照

| 模式 | 参考 |
|---|---|
| 标准 ReAct + conversational 多轮 | `src/app/graph.ts` + `default-flow.ts` |
| `Send` / `interrupt` / subgraph / checkpointer | 本文各节 + `libs/nodes` factory |
| **Durable stateful flow** | `createStatefulFlow`（`src/surfaces/stateful-flow.ts`） |
| **Context compaction** | `src/libs/compaction.ts` |
| 扩展思路（RAG / 平台能力等） | [examples.md](./examples.md)（仅文档） |

> 术语约定：「平台问答卡片」= **主平台的问答卡片**（模板统一技术服务用语；弃用产品口语问答卡片、dockpanel 等），定义见 [glossary.md](./glossary.md)。

> `interrupt` 的"采集回复 → resume"已由模板的 **`StatefulFlow`** 接入层（seam）在 acp/cli surface 接好——
> 不用自己写 host 端恢复逻辑。且续跑状态由 checkpointer 推断（`hasStarted`）——**一个会话一个主题**：
> 首条开题、之后都续跑同一项目，**进程/IDE 重启后仍能续跑**（见 `createStatefulFlow` 与上文第 6 节）。
