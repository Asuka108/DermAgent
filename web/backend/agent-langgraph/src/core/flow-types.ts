import type { BaseCheckpointSaver } from "@langchain/langgraph";
import type { RunnableConfig } from "@langchain/core/runnables";

/**
 * Flow 契约（core 层）—— graph 与 surface 之间的共享词汇，**纯类型、零依赖**。
 *
 * 这是分层架构的最底契约层（L1 core）：app（默认图）与 surfaces（ACP/CLI）都 import
 * 这里的类型，从而**互不直接依赖**。默认图与各 flow/topology 各自实现一个 FlowExecutor /
 * StatefulFlow 插进同一套 surface，避免重复 DeepAgentsServer / onPrompt / 流式 等 plumbing。
 *
 * 历史路径 `surfaces/flow-types.ts` 现为本文件的 re-export shim（对外消费者仍可用）。
 */

/** flow 执行结果：answer 为完整回答，footer 为可选脚注（如来源列表）。 */
export interface FlowResult {
  answer: string;
  footer?: string;
}

/** 一次工具调用事件（如 retrieve 调 MCP 检索源）。surface 据此向客户端展示「工具调用过程」。 */
export interface ToolCallEvent {
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
  status: "in_progress" | "completed" | "failed";
  /** completed 时的结果（文本或对象，会序列化展示） */
  result?: unknown;
  /** failed 时的错误信息 */
  error?: string;
}

/**
 * 工具审批请求事件 —— 副作用工具执行前向 surface 申请许可。
 * surface（ACP）据此调 `conn.requestPermission` 弹窗；节点据返回决定执行 / 合成拒绝。
 */
export interface PermissionRequestEvent {
  toolCallId: string;
  toolName: string;
  args: Record<string, unknown>;
}

/** 审批决定：放行 / 拒绝 / 取消（含 client 取消、signal 中止）。 */
export type PermissionDecision = "allow" | "reject" | "cancelled";

/**
 * 流程级审批请求 —— 图节点显式征询用户确认（如"确认发布?"），
 * 区别于工具级 {@link PermissionRequestEvent}（无 toolName/args，是图编排里的人审关卡）。
 * 与 onPermissionRequest 共走同步弹窗通道，复用 {@link PermissionDecision} 作返回。
 */
export interface ApprovalRequestEvent {
  /** 弹窗主文案（如"确认发布?"）。 */
  title: string;
  /** 可选详情（如待确认内容摘要）。 */
  detail?: string;
}

/** ACP Plan 单条条目（与 deepagents-acp PlanEntry 对齐）。 */
export interface PlanEntry {
  content: string;
  priority?: "high" | "medium" | "low";
  status: "pending" | "in_progress" | "completed" | "skipped";
}

/** 结构化 Plan 更新（研究大纲 / 任务清单）。 */
export interface PlanEvent {
  entries: PlanEntry[];
  /** 内部来源标识；ACP 输出前用于合并并行 subagent 的计划，不直接进入协议结构。 */
  source?: string;
  /** 触发该 subagent 的父级 task tool_call_id；用于隔离并行计划。 */
  toolCallId?: string;
}

/**
 * 阶段/进度事件 —— durable stateful flow 多阶段流水线（plan → research → draft → review）的可视化。
 * 与 ToolCallEvent 互补：tool 事件是「调了什么外部能力」，stage 事件是「现在走到流水线哪一步」。
 * surface 据此给客户端推进度（CLI 打印 / ACP 发 message chunk）。节点经
 * `config.configurable.onStage` 触发（与 onToolCall 同机制，可穿透 Send 并行实例）。
 */
export interface StageEvent {
  /** 阶段名（如 "调研" / "撰写初稿" / "质量评审"）。 */
  stage: string;
  /** 可选：当前第几步（1-based）。 */
  index?: number;
  /** 可选：总步数。 */
  total?: number;
  /** 可选：本步细节（如正在调研的章节标题）。 */
  detail?: string;
}

/**
 * 回调集合（流式 token + 工具调用 + 阶段 + Plan）——FlowExecutor 与 StatefulFlow 共用。
 * signal（可选）：任务取消信号。surface 从 ACP cancel controller 取，透传到
 * `graph.stream({signal})`；中止时 LangGraph 以 AbortError reject，surface 据此快速收尾。
 */
export interface FlowCallbacks {
  /**
   * @param source subagent 名（有值时 ACP 用独立 messageId 分桶）
   * @param toolCallId 父图 AIMessage.tool_calls[].id（并行 task 时区分同名校 subagent 流）
   */
  onToken?: (token: string, source?: string, toolCallId?: string) => void | Promise<void>;
  /**
   * 模型思考增量（可选）—— ACP 映射为 `agent_thought_chunk`，与 onToken 的可见回复分离。
   * 不得把思考写入 onToken，否则会与正文拼成同一条用户可见消息。
   * source / toolCallId 与 onToken 同义，用于并行 subagent 独立分桶。
   */
  onThought?: (token: string, source?: string, toolCallId?: string) => void | Promise<void>;
  onToolCall?: (e: ToolCallEvent) => void | Promise<void>;
  /** Stage progress for durable stateful flows（可选）。 */
  onStage?: (e: StageEvent) => void | Promise<void>;
  /** 结构化 Plan 更新（ACP sessionUpdate: plan）。 */
  onPlan?: (e: PlanEvent) => void | Promise<void>;
  /**
   * 工具审批（可选）—— 副作用工具执行前征询许可（ACP `session/request_permission`）。
   * 节点对每个 tool_call 调用一次（要不要弹、按什么名单/模式判定都在实现内，对齐
   * Claude SDK 的 canUseTool）；返回 "reject"/"cancelled" 时节点合成拒绝 ToolMessage
   * 并跳过执行。未注入则全放行（CLI / 非 ACP surface 向后兼容）。
   */
  onPermissionRequest?: (e: PermissionRequestEvent) => Promise<PermissionDecision>;
  /**
   * 流程级审批（可选）—— 图节点显式征询确认（ACP `session/request_permission` 弹窗）。
   * 与 onPermissionRequest 同走同步弹窗通道，但语义是"图编排里的人审关卡"而非工具门控；
   * 由 createPermissionApprovalNode 调用。未注入则默认放行（CLI / 非 ACP 向后兼容）。
   */
  onApprovalRequest?: (e: ApprovalRequestEvent) => Promise<PermissionDecision>;
  /** 任务取消信号（可选）。被中止时底层图运行 reject，不再继续产出 token。 */
  signal?: AbortSignal;
}

/**
 * Flow 执行器（单次调用）：给定查询产出结果。
 * - onToken：流式推送回答增量（surface 据此决定是否再整段重发 answer）。
 * - onToolCall：推送工具调用事件（如检索），surface 据此向客户端发 ACP tool_call。
 */
export type FlowExecutor = (query: string, opts: FlowCallbacks) => Promise<FlowResult>;

/**
 * 有状态 flow —— 支持 human-in-the-loop（interrupt / resume）。
 *
 * 与 FlowExecutor（单次）互补：executor 跑完即结束；StatefulFlow 可在中途 `interrupt`
 * 暂停、把问题抛给用户，下一轮带 `resume` 恢复（节点从暂停点之后续跑）。
 * threadId 隔离多会话（ACP 用 sessionId），图状态由 checkpointer 持久化。
 *
 * 一次 run 的结果只有两种：
 *  - done：跑到底，answer 为最终回答；
 *  - interrupted：图在某节点 interrupt 暂停，question 是要问用户的话（surface 发给用户、等下一轮 resume）。
 */
export type FlowRunResult =
  | { status: "done"; answer: string; footer?: string }
  | { status: "interrupted"; question: string };

/**
 * 从 checkpointer 读出的 thread 历史（供 session/load 回放、诊断等）。
 * `messages` 通常为 LangChain `BaseMessage[]`；core 层用 `unknown[]` 避免绑死 langchain。
 */
export interface SessionThreadHistory {
  messages: unknown[];
  /** 磁盘/内存 checkpointer 中是否存在该 thread 的 checkpoint。 */
  hasCheckpoint: boolean;
}

export interface StatefulFlow {
  run(
    input: { query?: string; resume?: string },
    threadId: string,
    callbacks?: FlowCallbacks
  ): Promise<FlowRunResult>;
  /**
   * 该 thread 是否**已经开始过**（checkpointer 里已有该会话的 checkpoint）。可选。
   *
   * Durable stateful flow seam：`hasStarted` 判断下一条消息是 resume 还是新 query。
   * **一个会话 = 一个主题/项目**：首条消息开题（无 checkpoint → 新任务），之后每条都续跑同一项目
   * （有 checkpoint → resume），无论它停在 interrupt、错在某节点、还是已跑完——
   * 都不会被误当成「新主题」重头开始。
   *
   * 实现应**从 checkpointer 状态推断**（`graph.getState()` 是否有 checkpoint），而非进程内存——
   * 这样进程/IDE 重启后仍准（见 createStatefulFlow）。未实现时 surface 退回内存跟踪。
   */
  hasStarted?(threadId: string): Promise<boolean>;
  /**
   * 从持久化 checkpointer 读取可回放消息。可选；未实现时 ACP load 无法跨进程回放 UI 历史。
   * ACP 场景下 `threadId` 即 `sessionId`（见 surfaces/acp onPrompt）。
   */
  getThreadMessages?(threadId: string): Promise<SessionThreadHistory | void>;
  /**
   * 修复 checkpointer 中孤立的 tool_calls（cancel 补 ToolMessage；否则 sanitize）。
   * ACP cancel 与 run 入口共用；`threadId` 即 sessionId。
   */
  repairCheckpoint?(
    threadId: string,
    opts?: { cancelledToolCallIds?: string[]; cancelReason?: string }
  ): Promise<boolean>;
}

/**
 * 图拓扑反射的结构化产物（getFlowTopology / 各拓扑 getXxxTopology 返回）。
 *
 * 放 core（纯契约）：app/topology 产出/消费它；app/topology.ts re-export 以维持公开 npm 子路径 `/topology`。
 */
export interface FlowTopologyNode {
  id: string;
  label: string;
}

export interface FlowTopologyEdge {
  source: string;
  target: string;
  /** 条件边(addConditionalEdges)为 true，普通边为 false。 */
  conditional: boolean;
  /** 条件分支标签(如路由目标名)；普通边无标签。 */
  label?: string;
}

export interface FlowTopology {
  nodes: FlowTopologyNode[];
  edges: FlowTopologyEdge[];
  /** 同一拓扑的 Mermaid 源，可直接渲染 / 贴进文档。 */
  mermaid: string;
}

/** 面向用户的交互形态：CLI / 注册表用它决定是否需要写图。 */
export type FlowInteractionKind = "chat" | "pipeline" | "approval";

/** flow 的实现来源：默认底座、预设拓扑，或 custom 节点级编排。 */
export type FlowImplementationKind = "default" | "preset" | "custom";

/** flow 注册表的机器可读画像，供 CLI 选型。 */
export interface FlowProfile {
  interaction: FlowInteractionKind;
  implementation: FlowImplementationKind;
  userLabel: string;
  summary: string;
  defaultForAmbiguous?: boolean;
  requiresGraphReason?: boolean;
}

/**
 * LangGraph 编译图的最小结构（与 src/surfaces/stateful-flow.ts 的 RunnableGraph 逐字对齐）。
 * 放 core 供 app recipe 与 surfaces createStatefulFlow 共享，避免 app → surfaces 分层违规。
 */
export interface TopologyRunnableGraph {
  stream(input: unknown, config?: RunnableConfig): Promise<AsyncIterable<unknown>>;
  getState(config: RunnableConfig): Promise<{
    values: unknown;
    next?: readonly string[];
    /** 当前 checkpoint 定位 config —— 有 checkpoint_id ⇒ 该 thread 已开过题。 */
    config?: { configurable?: { checkpoint_id?: string } };
  }>;
  /** 写回状态（自动压缩用）。 */
  updateState(config: RunnableConfig, values: Record<string, unknown>): Promise<unknown>;
}

/**
 * stateful 图的构造配方：createStatefulFlow 的「图相关」入参子集。
 * checkpointer + appConfig 由组合根 materialize 时注入（来自 FlowRuntime）。
 */
export interface StatefulTopologyRecipe<S = Record<string, unknown>> {
  /** 用注入的 checkpointer 编译图并返回。 */
  buildGraph: (checkpointer: BaseCheckpointSaver) => TopologyRunnableGraph;
  /** 新任务：把用户 query 映射成图初始 state。 */
  toInput: (query: string) => Record<string, unknown>;
  /** 终态：从图最终 values 取回答（+可选脚注）。 */
  toResult: (values: S) => { answer: string; footer?: string };
  /** 注入给所有节点的额外 configurable（如 Send 并行实例读取）。 */
  configurable?: Record<string, unknown>;
  /** 递归上限（防 reflection 回边死循环）。 */
  recursionLimit?: number;
  /**
   * 平台工具引用（schema source of truth）。
   * 来源：平台已登记工具的真实配置 → 固化为 `FlowDef.platformToolRefs` /
   * recipe 透传；**不是**旧 scaffold `spec.tools` / `*.flow.json`。
   */
  platformToolRefs?: Array<{
    targetType: "Plugin" | "Workflow" | "Knowledge";
    targetId: number;
    name?: string;
    description?: string;
    schema?: unknown;
    inputSchema?: unknown;
    outputSchema?: unknown;
    mode?: string;
    method?: string;
    url?: string;
    auth?: unknown;
    names?: string[];
    toolNames?: string[];
  }>;
}
