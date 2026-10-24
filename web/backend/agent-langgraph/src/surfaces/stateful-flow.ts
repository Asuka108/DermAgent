/**
 * createStatefulFlow —— durable stateful flow 统一基座（multi-turn HITL + cross-restart resume）。
 *
 * 在此之前，每个有状态示例（deep-research / travel / pm / human-in-loop）都各自手写一份
 * 几乎相同的 run-loop：建 config → `stream(Command resume)` vs `stream(初始 state)` →
 * 扫 chunk 找 `__interrupt__` → 返回 interrupted/done。本 helper 把这段收成一处，示例只需给出
 * 三件「图相关」的事：buildGraph / toInput / toResult。
 *
 * Durable stateful flow features（相对手写 run-loop 的增量）：
 *  1. **Cross-restart resume**：checkpointer 缺省经 `durableCheckpointer` → `FileCheckpointSaver`，
 *     图状态/interrupt 落盘 → 进程/IDE 重启后仍可 resume。
 *  2. **One session, one topic**：`hasStarted` 读 `graph.getState()` 是否已有 checkpoint，
 *     而非进程内存 Set → 首条开题、之后皆 resume 同一项目；重启后仍准。
 *  3. **Recursion guard**：`recursionLimit` 防 reflection 回边跑飞。
 *  4. **Multi-mode stream**：`streamMode` + `mapStreamChunk` → onToken/onPlan/onStage/onToolCall。
 */

import {
  Command,
  INTERRUPT,
  MemorySaver,
  type BaseCheckpointSaver,
} from "@langchain/langgraph";
import type { RunnableConfig } from "@langchain/core/runnables";
import { type AppConfig } from "../runtime/index.js";
import type { MultiServerMCPClient } from "@langchain/mcp-adapters";
import type {
  StatefulFlow,
  FlowRunResult,
  FlowCallbacks,
} from "../core/flow-types.js";
import { applyCompaction } from "../libs/compaction.js";
import { applyCheckpointMessageRepair } from "../libs/messages/repair-checkpoint.js";
import { mapStreamChunk } from "./map-stream-chunk.js";
import { dispatchSurfaceEvent } from "./dispatch-surface-event.js";
import { foldStreamTextChunk } from "../libs/nodes/stream-text-delta.js";

/** LangGraph 多模式 stream 的 streamMode 列表。 */
const STREAM_MODES = ["messages", "tools", "custom", "updates"] as const;
type StreamRunnableConfig = RunnableConfig & { streamMode?: string[] };

/**
 * createStatefulFlow 需要的「图」最小结构 —— LangGraph `.compile()` 的产物天然满足。
 * 用结构化类型而非 CompiledStateGraph 泛型，避免示例间为对齐复杂泛型而打架。
 */
export interface RunnableGraph {
  stream(
    input: unknown,
    config?: RunnableConfig
  ): Promise<AsyncIterable<unknown>>;
  getState(
    config: RunnableConfig
  ): Promise<{
    values: unknown;
    next?: readonly string[];
    /** 当前 checkpoint 的定位 config —— 有 checkpoint_id ⇒ 该 thread 已开过题（见 hasStarted）。 */
    config?: { configurable?: { checkpoint_id?: string } };
  }>;
  /** 写回状态（自动压缩用 RemoveMessage 替换历史；LangGraph compile 产物天然支持）。 */
  updateState(config: RunnableConfig, values: Record<string, unknown>): Promise<unknown>;
}

export interface StatefulFlowOptions<S = Record<string, unknown>> {
  /**
   * 用传入的 checkpointer 编译图并返回。示例传 `(cp) => createXxxGraph(appConfig, cp)`。
   * 只在 createStatefulFlow 内调用一次（per-run 的回调走 configurable，不必每轮重编）。
   */
  buildGraph: (checkpointer: BaseCheckpointSaver) => RunnableGraph;
  /** 新任务：把用户 query 映射成图初始 state（如 `(q) => ({ topic: q })`）。 */
  toInput: (query: string) => Record<string, unknown>;
  /** 终态：从图最终 values 取回答（+可选脚注）。 */
  toResult: (values: S) => { answer: string; footer?: string };
  /**
   * 持久化后端。缺省 MemorySaver（仅供单测/无 config 时）；
   * 生产/示例应传 FileCheckpointSaver（createFileCheckpointer(appConfig)）→ 跨重启续跑。
   */
  checkpointer?: BaseCheckpointSaver;
  /** 注入给所有节点的额外 configurable（如 `{ appConfig }`，供 Send 并行实例读取）。 */
  configurable?: Record<string, unknown>;
  /** 递归上限（防 reflection 回边死循环）。默认 50。 */
  recursionLimit?: number;
  /**
   * 传入则在「新 query（非 resume）」入口自动压缩 checkpoint 中累积的 `state.messages`
   * （超阈值摘要 + RemoveMessage 替换，见 libs/compaction）。状态无 messages 或未超阈值时 no-op。
   */
  appConfig?: AppConfig;
  /**
   * 对话型 flow（`conversational: true`，非 HITL durable stateful flow）。
   * true 时返回的 StatefulFlow **不暴露 hasStarted** → surface（ACP server / CLI run）每轮都走
   * query 分支：配合稳定 threadId（ACP=sessionId）+ checkpointer，历史经 MessagesAnnotation
   * reducer 自动累积 → 多轮记忆。压缩仍在新 query 入口触发（见 appConfig）。
   * （HITL 型保持 false：暴露 hasStarted，首条开题、之后 resume 续跑同一任务。）
   */
  conversational?: boolean;
  /**
   * runtime 注入的持久 MCP client。放进 configurable.mcpClient，供图内检索节点
   * （retrieve / mcp-retrieval）经 getClient(server) 复用持久连接，避免各自 spawn 临时 client。
   */
  mcpClient?: MultiServerMCPClient;
}

/** 从 stream 各 chunk 里取最后一个 interrupt 的 value（沿用各示例既有约定）。 */
function pickInterruptValue(chunk: Record<string, unknown>): unknown {
  const intr = chunk[INTERRUPT] as Array<{ value?: unknown }> | undefined;
  return intr && intr.length ? intr[intr.length - 1]?.value : undefined;
}

/** 判断 stream chunk 是否为多模式 `[mode, payload]` 元组。 */
function isModeChunk(chunk: unknown): chunk is [string, unknown] {
  return Array.isArray(chunk) && typeof chunk[0] === "string" && chunk.length === 2;
}

/**
 * 消费 LangGraph stream，归一事件并分发给 callbacks；返回最后一个 interrupt question（若有）。
 */
async function consumeStream(
  stream: AsyncIterable<unknown>,
  callbacks?: FlowCallbacks
): Promise<string | undefined> {
  let interruptQuestion: string | undefined;
  let multiMode = false;
  /** messages 模式文本折叠态（防御 provider 返回累积全文导致叠词）。 */
  let messagesTextFull = "";
  /** messages 模式思考折叠态（reasoning_content 与 content 分开 fold）。 */
  let messagesThoughtFull = "";

  for await (const raw of stream) {
    if (isModeChunk(raw)) {
      multiMode = true;
      const [mode, payload] = raw;
      const events = mapStreamChunk(mode, payload);
      for (const ev of events) {
        if (ev.type === "interrupt") {
          interruptQuestion = ev.question;
          continue;
        }
        const meta =
          mode === "messages" && Array.isArray(payload)
            ? (payload[1] as { langgraph_node?: string } | undefined)
            : undefined;
        // custom writer 的 text 已由 emitTextToken 发 delta；仅 messages 模式需折叠。
        if (ev.type === "text" && mode === "messages") {
          const folded = foldStreamTextChunk(messagesTextFull, ev.text);
          messagesTextFull = folded.full;
          if (!folded.delta) continue;
          await dispatchSurfaceEvent({ type: "text", text: folded.delta }, callbacks, meta);
          continue;
        }
        // reasoning_content 同样可能是累积全文，与 content 分开 fold 后走 thought 通道。
        if (ev.type === "thought" && mode === "messages") {
          const folded = foldStreamTextChunk(messagesThoughtFull, ev.text);
          messagesThoughtFull = folded.full;
          if (!folded.delta) continue;
          await dispatchSurfaceEvent({ type: "thought", text: folded.delta }, callbacks, meta);
          continue;
        }
        await dispatchSurfaceEvent(ev, callbacks, meta);
      }
      continue;
    }

    // 兜底：旧式单模式 updates chunk（无 streamMode 时）
    if (!multiMode) {
      const v = pickInterruptValue(raw as Record<string, unknown>);
      if (v !== undefined) {
        interruptQuestion =
          (v as { question?: string })?.question ?? String(v);
      }
    }
  }

  return interruptQuestion;
}

/**
 * 把图包成模板 StatefulFlow —— run-loop + 持久化 resume 一处实现，全示例共享。
 */
export function createStatefulFlow<S = Record<string, unknown>>(
  options: StatefulFlowOptions<S>
): StatefulFlow {
  const checkpointer = options.checkpointer ?? new MemorySaver();
  const recursionLimit = options.recursionLimit ?? 50;
  const graph = options.buildGraph(checkpointer);

  const baseConfig = (threadId: string, callbacks?: FlowCallbacks): RunnableConfig => ({
    configurable: {
      ...options.configurable,
      thread_id: threadId,
      mcpClient: options.mcpClient,
      // 节点仍可直接读 callbacks（与 writer 双轨兼容过渡期）
      onToolCall: callbacks?.onToolCall,
      onStage: callbacks?.onStage,
      onPlan: callbacks?.onPlan,
      onToken: callbacks?.onToken,
      onThought: callbacks?.onThought,
      onPermissionRequest: callbacks?.onPermissionRequest,
      onApprovalRequest: callbacks?.onApprovalRequest,
    },
    recursionLimit,
  });

  const flow: StatefulFlow = {
    async run(input, threadId, callbacks): Promise<FlowRunResult> {
      const config = baseConfig(threadId, callbacks ?? {});

      // 每次 run 入口修复历史 checkpoint：孤立 tool_calls + 多模态 content 压文本（写回磁盘）。
      await applyCheckpointMessageRepair(graph, config, {
        appConfig: options.appConfig,
      });

      if (options.appConfig && input.resume === undefined) {
        await applyCompaction(graph, config, options.appConfig);
      }

      const streamInput =
        input.resume !== undefined
          ? new Command({ resume: input.resume })
          : options.toInput(input.query ?? "");

      const streamConfig: StreamRunnableConfig = {
        ...config,
        streamMode: [...STREAM_MODES],
        ...(callbacks?.signal ? { signal: callbacks.signal } : {}),
      };
      const stream = await graph.stream(streamInput, streamConfig);

      const interruptQuestion = await consumeStream(stream, callbacks ?? {});

      if (interruptQuestion !== undefined) {
        return { status: "interrupted", question: interruptQuestion };
      }

      const snapshot = await graph.getState(config);
      const { answer, footer } = options.toResult(snapshot.values as S);
      return { status: "done", answer, footer };
    },
  };

  flow.repairCheckpoint = async (threadId, opts) =>
    applyCheckpointMessageRepair(graph, baseConfig(threadId), {
      cancelledToolCallIds: opts?.cancelledToolCallIds,
      cancelReason: opts?.cancelReason,
      // 与 run 入口一致：按 supportsVision / provider 决定是否剥图与压扁强度
      appConfig: options.appConfig,
    });

  // 对话型不暴露 hasStarted → surface 每轮走 query（带历史累积，多轮对话）；
  // HITL 型暴露 → surface 据 checkpoint 判断 resume 续跑同一任务。
  if (!options.conversational) {
    /**
     * 持久化推断：该 thread 是否已有 checkpoint（开过题）。
     * 有 checkpoint ⇒ 同一会话的续跑（interrupt / 出错 / 已完成皆然）；无 ⇒ 全新会话、首条开题。
     */
    flow.hasStarted = async (threadId): Promise<boolean> => {
      const snapshot = await graph.getState({
        configurable: { thread_id: threadId },
      });
      return (
        Boolean(snapshot.config?.configurable?.checkpoint_id) ||
        (snapshot.next?.length ?? 0) > 0
      );
    };
  }

  return flow;
}
