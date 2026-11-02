/**
 * ACP Server — 让工作流图成为请求路径（与具体图解耦）。
 *
 * 关键 seam：deepagents-acp 的 `onPrompt` 钩子在 agent 运行前触发，
 * 返回 `{ stopReason }` 即短路 agent。主入口经 materializeFlow 传入 StatefulFlow；
 * legacy 仍可能传入 legacy function executor。
 * 把回答经 `conn` 流式推给客户端，然后短路——deep agent 永不进入请求路径。
 *
 * per-session 配置（D）：`createExecutor` 工厂 + `configureSession` 钩子让 ACP `session/new`
 * 下发的 `cwd` / `mcpServers` / `model` 经 loadConfig（ACP 最高优先级）装配**每会话独立** runtime；
 * `onSessionClosed` 释放该会话资源（MCP stdio 子进程）。未传 createExecutor 时退回单 executor 模式。
 *
 * DeepAgentsServer 仍要求一个 agentConfig（其内部 createDeepAgent 用它），
 * 所以我们给一个极简、零自定义工具的 throwaway agent，它从不被调用。
 */

import {
  DeepAgentsServer,
  type DeepAgentConfig,
  type DeepAgentsServerHooks,
  type StopReason,
} from "../../libs/deepagents-acp/index.js";
import {
  logger,
  setLogSession,
  resolveModel,
  type AppConfig,
  type ACPSessionConfig,
} from "../../runtime/index.js";
import {
  loadSessionConfigFromEnv,
  mergeAcpSessionConfig,
  resolveAcpSessionConfig,
  extractRequestIdFromAcpParams,
} from "./session-config.js";
import {
  logConfigureSessionDiagnostics,
  logStartupAcpEnvDiagnostics,
} from "./session-diagnostics.js";
import {
  runInAcpPromptCycle,
  traceFlowCallbacks,
  traceFlowRun,
  logAcpPromptStart,
  logAcpPromptEnd,
  logPromptComplete,
} from "../../runtime/session-trace.js";
import type {
  FlowExecutor,
  StatefulFlow,
  ToolCallEvent,
  StageEvent,
  PlanEvent,
  FlowCallbacks,
  PermissionDecision,
} from "../../core/flow-types.js";
import { emitToolCall, type AcpToolConnection } from "./emit-tool-call.js";
import { AcpPlanCoordinator } from "./plan-coordinator.js";
import { randomUUID } from "node:crypto";
import { buildPermissionToolCall } from "../../libs/deepagents-acp/index.js";
import type { PermissionOption, ToolCallUpdate } from "@agentclientprotocol/sdk";

const log = logger.child("flow-acp");

/** 工具审批选项（对齐 Legacy deepagents-acp/server.ts requestToolPermission 的 4 选项）。 */
const PERMISSION_OPTIONS: PermissionOption[] = [
  { optionId: "allow-once", name: "Allow once", kind: "allow_once" },
  { optionId: "allow-always", name: "Always allow", kind: "allow_always" },
  { optionId: "reject-once", name: "Reject", kind: "reject_once" },
  { optionId: "reject-always", name: "Always reject", kind: "reject_always" },
];

/** signal 中止哨兵：审批 await 期间 client cancel → 返回它而非 reject（见 raceWithAbort）。 */
const ABORTED = Symbol("permission-aborted");

/** buildAcpCallbacks 的审批配置（AppConfig.permissions 子集）。 */
interface PermissionGateConfig {
  mode: string;
  interruptOn: string[];
}

/**
 * 跑 fn，但若 signal 已/将 abort 则提前以 ABORTED resolve（放弃等待 client 应答；
 * 底层 RPC 不主动取消，整图取消由 graph.stream({signal}) 收尾）。
 */
async function raceWithAbort<T>(
  fn: () => Promise<T>,
  signal?: AbortSignal,
): Promise<T | typeof ABORTED> {
  if (!signal) return fn();
  if (signal.aborted) return ABORTED;
  return new Promise<T | typeof ABORTED>((resolve, reject) => {
    const onAbort = () => resolve(ABORTED);
    signal.addEventListener("abort", onAbort, { once: true });
    fn().then(
      (v) => {
        signal.removeEventListener("abort", onAbort);
        resolve(v);
      },
      (e) => {
        signal.removeEventListener("abort", onAbort);
        reject(e);
      },
    );
  });
}

/** ACP 连接的最小接口：向客户端推 sessionUpdate。 */
type AcpConnection = AcpToolConnection;

/** ACP 流式回传统计（写入 prompt_end）。 */
interface AcpStreamStats {
  streamed: boolean;
  streamChars: number;
  tokenChunks: number;
}

function createAcpStreamStats(): AcpStreamStats {
  return { streamed: false, streamChars: 0, tokenChunks: 0 };
}

/**
 * 调 ACP `session/request_permission` 弹窗并归一结果（A 工具审批 / B 流程审批共用）。
 * 含 signal race（中止→cancelled）、outcome 解析、graceful 降级（client 不支持 / RPC 抛错→放行）。
 * **不在 agent 侧缓存 always**：规则记忆 / strict 校验 / 审计由 client 审批中枢（如 NuwaClaw
 * `permissionCoordinator`）统一管——agent 每次发请求，对齐 claude-code-acp。
 */
async function callAcpPermission(
  conn: AcpConnection,
  sessionId: string,
  toolCall: ToolCallUpdate,
  signal?: AbortSignal,
): Promise<PermissionDecision> {
  if (typeof conn.requestPermission !== "function") return "allow";
  try {
    const result = await raceWithAbort(
      () => conn.requestPermission!({ sessionId, toolCall, options: PERMISSION_OPTIONS }),
      signal,
    );
    if (result === ABORTED) return "cancelled";
    const outcome = result?.outcome;
    if (!outcome || outcome.outcome === "cancelled") return "cancelled";
    // allow_once/allow_always → allow；reject_once/reject_always → reject（always 的"记忆"由 client 管）。
    return outcome.optionId === "reject-once" || outcome.optionId === "reject-always"
      ? "reject"
      : "allow";
  } catch (err) {
    // graceful：client 不实现 / RPC 出错 → 放行，避免卡死不支持审批的工作流。
    log.warn("requestPermission failed; allowing (degraded)", {
      sessionId,
      error: String(err),
    });
    return "allow";
  }
}

/**
 * 工具审批 handler（ACP）—— 注入 FlowCallbacks.onPermissionRequest。
 * 判定顺序对齐 Claude SDK canUseTool：mode=yolo / client 不支持 / 非 interruptOn → 放行；
 * 否则**每次**经 conn.requestPermission 弹窗（always 记忆 / 规则交 client 审批中枢，agent 不缓存）。
 * signal 中止 → "cancelled"；RPC 抛错 → graceful 放行。export 供单测。
 */
export function createAcpPermissionHandler(
  conn: AcpConnection,
  sessionId: string,
  permissions: PermissionGateConfig,
  workspaceRoot: string,
  signal?: AbortSignal,
): NonNullable<FlowCallbacks["onPermissionRequest"]> {
  return async (e) => {
    const connSupported = typeof conn.requestPermission === "function";
    const inList = permissions.interruptOn.includes(e.toolName);
    if (permissions.mode === "yolo") {
      log.info("permission 门控放行", { sessionId, toolName: e.toolName, reason: "mode=yolo" });
      return "allow";
    }
    if (!connSupported) {
      log.info("permission 门控放行", { sessionId, toolName: e.toolName, reason: "conn.requestPermission 未实现（graceful 降级）" });
      return "allow";
    }
    if (!inList) {
      log.info("permission 门控放行", { sessionId, toolName: e.toolName, reason: `不在 interruptOn [${permissions.interruptOn.join(",")}]` });
      return "allow";
    }
    log.info("permission 门控 → 发 request_permission", { sessionId, toolName: e.toolName, mode: permissions.mode });
    return callAcpPermission(
      conn,
      sessionId,
      buildPermissionToolCall(e.toolCallId, e.toolName, e.args, workspaceRoot),
      signal,
    );
  };
}

/**
 * 流程级审批 handler（ACP）—— 注入 FlowCallbacks.onApprovalRequest（createPermissionApprovalNode 用）。
 * 与工具审批（A）共用同步弹窗通道，但**无 interruptOn 名单 / 缓存**：图节点显式放的关卡总是弹
 * （除 mode=yolo）。title 经 buildPermissionToolCall 包成 toolCall 供 client 弹窗展示。
 */
export function createAcpApprovalHandler(
  conn: AcpConnection,
  sessionId: string,
  permissions: PermissionGateConfig,
  workspaceRoot: string,
  signal?: AbortSignal,
): NonNullable<FlowCallbacks["onApprovalRequest"]> {
  return async (e) => {
    if (permissions.mode === "yolo") return "allow";
    const toolCall = buildPermissionToolCall(
      randomUUID(),
      e.title,
      e.detail ? { detail: e.detail } : {},
      workspaceRoot,
    );
    return callAcpPermission(conn, sessionId, toolCall, signal);
  };
}

/**
 * 组装 ACP onPrompt 的 callbacks：流式推送 + tool trace。
 * 所有调试日志在此层注入，flow 图本身无感知。
 */
function buildAcpCallbacks(
  conn: AcpConnection,
  sessionId: string,
  stats: AcpStreamStats,
  inflightTools: Map<string, ToolCallEvent>,
  emittedToolCallIds: Set<string>,
  completedToolCallIds: Set<string>,
  workspaceRoot: string,
  permissions: PermissionGateConfig,
  signal?: AbortSignal
): ReturnType<typeof traceFlowCallbacks> {
  const planCoordinator = new AcpPlanCoordinator();
  // 并行 subagent 可能同时更新 plan；串行发送快照，避免较旧网络请求后完成并覆盖新状态。
  let planSendQueue: Promise<void> = Promise.resolve();

  return traceFlowCallbacks(
    {
      onToken: (token, source, toolCallId) => {
        stats.streamed = true;
        stats.streamChars += token.length;
        stats.tokenChunks += 1;
        // source（subagent name）+ toolCallId → 独立 messageId；并行同名校 subagent 不再撞桶。
        const safe = (s: string) => s.replace(/[:\s]/g, "_");
        const messageId = source
          ? `subagent:${safe(source)}:${safe(toolCallId ?? "unknown")}`
          : undefined;
        return streamText(conn, sessionId, token, "agent", messageId);
      },
      // 思考通道：走 agent_thought_chunk。故意不置 stats.streamed，
      // 以便 content 为空、仅有 reasoning 时，回合结束仍可用 res.answer 兜底推送正文。
      onThought: (token, source, toolCallId) => {
        const safe = (s: string) => s.replace(/[:\s]/g, "_");
        const messageId = source
          ? `subagent-thought:${safe(source)}:${safe(toolCallId ?? "unknown")}`
          : undefined;
        return streamText(conn, sessionId, token, "thought", messageId);
      },
      onToolCall: async (e) => {
        await emitToolCall(conn, sessionId, e, {
          inflightTools,
          workspaceRoot,
          emittedToolCallIds,
          completedToolCallIds,
        });
        if (e.status === "in_progress") {
          inflightTools.set(e.toolCallId, e);
        } else {
          inflightTools.delete(e.toolCallId);
        }
      },
      onStage: (e) => streamText(conn, sessionId, formatStage(e), "thought"),
      onPlan: (e) => {
        planCoordinator.update(e);
        // 发送时再 snapshot，避免并行更新入队后、emit 前状态已变却发出过期快照。
        const queued = planSendQueue.then(() =>
          emitPlan(conn, sessionId, planCoordinator.snapshot())
        );
        // 后续更新不应被一次发送失败永久阻断；当前调用仍收到原始 reject。
        planSendQueue = queued.catch(() => undefined);
        return queued;
      },
      onPermissionRequest: createAcpPermissionHandler(
        conn,
        sessionId,
        permissions,
        workspaceRoot,
        signal,
      ),
      onApprovalRequest: createAcpApprovalHandler(
        conn,
        sessionId,
        permissions,
        workspaceRoot,
        signal,
      ),
      ...(signal ? { signal } : {}),
    },
    { sessionId, threadId: sessionId }
  );
}

async function failInflightToolsOnCancel(
  conn: AcpConnection,
  sessionId: string,
  inflightTools: Map<string, ToolCallEvent>
): Promise<void> {
  for (const e of inflightTools.values()) {
    try {
      await conn.sessionUpdate({
        sessionId,
        update: {
          sessionUpdate: "tool_call_update",
          toolCallId: e.toolCallId,
          status: "failed",
          ...(e.args && Object.keys(e.args).length > 0 ? { rawInput: e.args } : {}),
          content: [
            {
              type: "content",
              content: { type: "text", text: "已取消（客户端 session/cancel）" },
            },
          ],
        },
      });
    } catch (tuErr) {
      log.warn("cancel: tool_call_update 发送失败", {
        sessionId,
        toolCallId: e.toolCallId,
        error: String(tuErr),
      });
    }
  }
}

async function streamText(
  conn: AcpConnection,
  sessionId: string,
  text: string,
  kind: "agent" | "thought" = "agent",
  messageId?: string
): Promise<void> {
  if (!text) return;
  await conn.sessionUpdate({
    sessionId,
    update: {
      sessionUpdate: kind === "thought" ? "agent_thought_chunk" : "agent_message_chunk",
      // messageId（ACP spec）：同 messageId 的 chunks 属同一消息；subagent 用
      // subagent:<name>:<toolCallId>，客户端据此分组区分主 agent 与各并行 task 流。
      ...(messageId ? { messageId } : {}),
      content: { type: "text", text },
    },
  });
}

/** 结构化 Plan → ACP sessionUpdate: plan。 */
async function emitPlan(
  conn: AcpConnection,
  sessionId: string,
  e: PlanEvent
): Promise<void> {
  if (!e.entries.length) return;
  await conn.sessionUpdate({
    sessionId,
    update: {
      sessionUpdate: "plan",
      entries: e.entries,
    },
  });
}

/** durable stateful flow 阶段事件 → thought chunk（不污染主回答区）。 */
function formatStage(e: StageEvent): string {
  const pos = e.index && e.total ? ` [${e.index}/${e.total}]` : "";
  const detail = e.detail ? ` · ${e.detail}` : "";
  return `\n▸${pos} ${e.stage}${detail}\n`;
}

/** per-session 装配产物：executor + 资源释放钩子。 */
export interface SessionExecutor {
  executor: FlowExecutor | StatefulFlow;
  /** 释放该 session 的运行时资源（如 MCP stdio 子进程）。session 关闭时调用。 */
  dispose?: () => Promise<void>;
}

export interface FlowAcpOptions {
  /**
   * 单 executor 模式：StatefulFlow（主路径）；legacy function executor 仅 legacy function 兼容。
   * 与 createExecutor 二选一；createExecutor 优先。
   */
  executor?: FlowExecutor | StatefulFlow;
  /**
   * per-session 工厂模式：按 ACP session 的 cwd / mcpServers / model 装配**每会话独立** runtime
   * （ACP 最高优先级，见 loadConfig 第 6 层）。在 configureSession 阶段调用，onSessionClosed 释放。
   */
  createExecutor?: (args: {
    sessionConfig: ACPSessionConfig;
    workspaceRoot: string;
    /** ACP sessionId（MCP 工具 schema 缓存主键）。 */
    sessionId: string;
    /** ACP 装配 phase：`new`（清缓存不读）/ `load`（读缓存）。 */
    sessionPhase: string;
  }) => Promise<SessionExecutor>;
  /** throwaway agent 的身份/模型（agent 永不运行） */
  appConfig: AppConfig;
  debug?: boolean;
}

export {
  acpMcpToRecord,
  sessionConfigFromParams,
  resolveAcpSessionConfig,
  extractSystemPromptFromParams,
  extractRequestIdFromAcpParams,
  coalesceSystemPromptValue,
  loadSessionConfigFromEnv,
  mergeAcpSessionConfig,
} from "./session-config.js";

/**
 * 构造 flow surface 的三个 ACP hook（configureSession / onPrompt / onSessionClosed）。
 *
 * 从 bootstrapFlowAcp 抽出，便于在测试中直接调用 hook、注入假 executor / 假 conn，
 * 无需启动真实 stdio server。bootstrapFlowAcp 调它再把 hooks 交给 DeepAgentsServer——行为不变。
 *
 * onPrompt 把 ACP cancel controller 的 signal 经 callbacks.signal 透传进 flow，
 * flow 再交给 `graph.stream({signal})`：client 取消时图运行快速 reject，onPrompt 的 catch 收尾。
 */
export function createFlowHooks(options: FlowAcpOptions): DeepAgentsServerHooks {
  // per-session executor 缓存（createExecutor 模式）；单 executor 模式下恒空。
  const sessions = new Map<string, SessionExecutor>();
  // configureSession 下发的 workspace/cwd 也缓存下来；若 hook 未能立即建好 executor,
  // 后续懒建也必须沿用同一 session 配置，不能退回进程 cwd。
  const sessionConfigs = new Map<
    string,
    { sessionConfig: ACPSessionConfig; workspaceRoot: string; phase: string }
  >();
  const sessionFailures = new Map<string, string>();
  // 一个会话 = 一个主题：老式 flow（未实现 hasStarted）的「等回复」内存兜底，按 sessionId 跟踪。
  // 实现了 hasStarted 的 flow 优先从 checkpointer 推断续跑（跨进程/IDE 重启仍准）。
  const fallbackResume = new Set<string>();
  /** 记录每轮 onPrompt 起始时间，供 onPromptComplete 写 prompt_complete。 */
  // 一轮 prompt 的起点 + NuwaClaw requestId（onPromptComplete 收尾时仍可用）。
  const promptTurnMeta = new Map<
    string,
    { startedAt: number; requestId?: string }
  >();

  /** 取该 session 的 executor：优先 per-session 缓存，回退单 executor；createExecutor 模式下缺失则懒建。 */
  async function resolveExecutor(
    sessionId: string
  ): Promise<FlowExecutor | StatefulFlow | undefined> {
    const cached = sessions.get(sessionId);
    if (cached) return cached.executor;
    if (options.createExecutor) {
      const failure = sessionFailures.get(sessionId);
      if (failure) {
        throw new Error(`ACP session runtime 初始化失败: ${failure}`);
      }
      const configured = sessionConfigs.get(sessionId);
      // configureSession 未触发时的最后兜底：合并 env + 进程 cwd。
      const fallbackConfig = mergeAcpSessionConfig(loadSessionConfigFromEnv(), {
        cwd: process.cwd(),
      });
      const built = await options.createExecutor({
        sessionConfig: configured?.sessionConfig ?? fallbackConfig,
        workspaceRoot: configured?.workspaceRoot ?? process.cwd(),
        sessionId,
        // 兜底懒建（configureSession 未触发）无缓存可写，按 load 尽力命中即可。
        sessionPhase: configured?.phase ?? "load",
      });
      sessions.set(sessionId, built);
      return built.executor;
    }
    return options.executor;
  }

  /**
   * dispose 旧 executor 并按 sessionConfigs 重建（configureSession / model 热切换共用）。
   * phaseOverride：model 热切换等场景传 "load"，复用 MCP 工具缓存、避免误清；
   * 省略则用 configureSession 记录的 phase（new/load）。
   */
  async function rebuildSessionExecutor(
    sessionId: string,
    phaseOverride?: string
  ): Promise<void> {
    if (!options.createExecutor) return;
    const configured = sessionConfigs.get(sessionId);
    if (!configured) {
      log.warn("rebuildSessionExecutor: no session config cached", { sessionId });
      return;
    }
    try {
      const built = await options.createExecutor({
        sessionConfig: configured.sessionConfig,
        workspaceRoot: configured.workspaceRoot,
        sessionId,
        sessionPhase: phaseOverride ?? configured.phase,
      });
      try {
        await sessions.get(sessionId)?.dispose?.();
      } catch (dispErr) {
        log.warn("rebuildSessionExecutor: 旧 executor dispose 失败", {
          sessionId,
          error: String(dispErr),
        });
      }
      sessions.set(sessionId, built);
      sessionFailures.delete(sessionId);
      log.info("per-session runtime hot-reloaded", {
        sessionId,
        cwd: configured.workspaceRoot,
        model: configured.sessionConfig.model,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      sessionFailures.set(sessionId, message);
      sessions.delete(sessionId);
      log.error("rebuildSessionExecutor failed", { sessionId, error: message });
      throw err;
    }
  }

  return {
    // session/new | session/load：按 ACP cwd/mcpServers/model 装配 per-session runtime。
    async configureSession(ctx) {
      // 无论单/多 executor，都先绑定 per-session 日志（幂等）。
      setLogSession(ctx.sessionId);
      if (!options.createExecutor) return undefined;
      const { sessionConfig, workspaceRoot, fromParams } = resolveAcpSessionConfig(ctx.params);
      logConfigureSessionDiagnostics({
        sessionId: ctx.sessionId,
        phase: ctx.phase,
        params: ctx.params,
        fromParams,
        merged: sessionConfig,
        workspaceRoot,
      });
      sessionConfigs.set(ctx.sessionId, { sessionConfig, workspaceRoot, phase: ctx.phase });
      try {
        await rebuildSessionExecutor(ctx.sessionId);
        log.info("configureSession → per-session runtime", {
          sessionId: ctx.sessionId,
          phase: ctx.phase,
          cwd: workspaceRoot,
          mcpServers: sessionConfig.mcpServers ? Object.keys(sessionConfig.mcpServers) : [],
          systemPromptChars: sessionConfig.systemPrompt?.trim().length ?? 0,
          model: sessionConfig.model,
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        sessionFailures.set(ctx.sessionId, message);
        log.error("configureSession failed; session runtime disabled until reconfigured", {
          sessionId: ctx.sessionId,
          error: message,
        });
      }
      // 让 deepagents-acp 内置 backend 也用同一 workspace 根。
      return { workspaceRoot };
    },

    async onPrompt(ctx) {
      const text = ctx.promptText?.trim();
      if (!text) {
        return undefined; // 空输入：交回服务器默认处理
      }
      const conn = ctx.conn as AcpConnection;
      const sessionId = ctx.sessionId;
      // configureSession 未触发时（部分 host）仍保证 per-session log writer 就位。
      setLogSession(sessionId);
      const promptStartedAt = Date.now();
      // NuwaClaw 经 session/prompt `_meta.requestId` 透传；CLI / 无 meta 宿主则为 undefined。
      const requestId = extractRequestIdFromAcpParams(ctx.params);
      promptTurnMeta.set(sessionId, { startedAt: promptStartedAt, requestId });
      // ① 协议层：收到 session/prompt
      logAcpPromptStart({ sessionId, query: text, requestId });

      const executor = await resolveExecutor(sessionId);
      if (!executor) {
        log.warn("onPrompt: no executor available for session", { sessionId, requestId });
        promptTurnMeta.delete(sessionId);
        return undefined;
      }
      const isStateful = typeof executor !== "function";
      const durableResume =
        isStateful && typeof (executor as StatefulFlow).hasStarted === "function";

      return runInAcpPromptCycle(
        { sessionId, startedAt: promptStartedAt, query: text, requestId },
        async () => {
          const stats = createAcpStreamStats();
          const inflightTools = new Map<string, ToolCallEvent>();
          const emittedToolCallIds = new Set<string>();
          const completedToolCallIds = new Set<string>();
          const workspaceRoot =
            sessionConfigs.get(sessionId)?.workspaceRoot ?? process.cwd();
          const callbacks = buildAcpCallbacks(
            conn,
            sessionId,
            stats,
            inflightTools,
            emittedToolCallIds,
            completedToolCallIds,
            workspaceRoot,
            {
              // 防御：手构 appConfig（测试 / 非 loadConfig 路径）可能无 permissions；
              // 缺失 → 空名单 = 不审批（放行），不破坏未配审批的调用方。
              mode: options.appConfig.permissions?.mode ?? "ask",
              interruptOn: options.appConfig.permissions?.interruptOn ?? [],
            },
            ctx.signal
          );

          const endTurn = (meta: Omit<Parameters<typeof logAcpPromptEnd>[0], "sessionId" | "startedAt" | "requestId">) => {
            logAcpPromptEnd({
              sessionId,
              startedAt: promptStartedAt,
              requestId,
              streamed: stats.streamed,
              streamChars: stats.streamChars,
              tokenChunks: stats.tokenChunks,
              ...meta,
            });
          };

          try {
            if (isStateful) {
              const flow = executor as StatefulFlow;
              const resuming = durableResume
                ? await flow.hasStarted!(sessionId)
                : fallbackResume.has(sessionId);
              const mode = resuming ? "resume" : "query";
              // ② flow 执行（tool/stage/LLM trace 经 callbacks 注入）
              const res = await traceFlowRun(
                "flow.run",
                {
                  sessionId,
                  threadId: sessionId,
                  mode,
                  input: text,
                  resuming,
                  isStateful: true,
                },
                () =>
                  flow.run(
                    resuming ? { resume: text } : { query: text },
                    sessionId,
                    callbacks
                  )
              );
              if (res.status === "interrupted") {
                if (!durableResume) fallbackResume.add(sessionId);
                endTurn({
                  stopReason: "end_turn",
                  flowStatus: "interrupted",
                  questionChars: res.question?.length ?? 0,
                });
                if (res.question) {
                  await streamText(conn, sessionId, res.question);
                }
                return { stopReason: "end_turn" as StopReason };
              }
              if (!durableResume) fallbackResume.delete(sessionId);
              if (!stats.streamed && res.answer) await streamText(conn, sessionId, res.answer);
              if (res.footer) await streamText(conn, sessionId, res.footer);
              endTurn({
                stopReason: "end_turn",
                flowStatus: res.status,
                answerChars: res.answer?.length ?? 0,
              });
              return { stopReason: "end_turn" as StopReason };
            }

            const result = await traceFlowRun(
              "flow.run",
              {
                sessionId,
                mode: "query",
                input: text,
                isStateful: false,
              },
              () => (executor as FlowExecutor)(text, callbacks)
            );
            if (!stats.streamed && result.answer) await streamText(conn, sessionId, result.answer);
            if (result.footer) await streamText(conn, sessionId, result.footer);
            endTurn({
              stopReason: "end_turn",
              flowStatus: "done",
              answerChars: result.answer?.length ?? 0,
            });
            return { stopReason: "end_turn" as StopReason };
          } catch (err) {
            if ((err as Error)?.name === "AbortError" || ctx.signal?.aborted) {
              endTurn({ stopReason: "cancelled" });
              await failInflightToolsOnCancel(conn, sessionId, inflightTools);
              if (isStateful && typeof (executor as StatefulFlow).repairCheckpoint === "function") {
                await (executor as StatefulFlow).repairCheckpoint!(sessionId, {
                  cancelledToolCallIds: [...inflightTools.keys()],
                  cancelReason: "已取消（客户端 session/cancel）",
                });
              }
              return { stopReason: "cancelled" as StopReason };
            }
            endTurn({ stopReason: "end_turn", error: String(err) });
            if (!durableResume) fallbackResume.delete(sessionId);
            await streamText(conn, sessionId, "抱歉，处理您的问题时出现错误。");
            return { stopReason: "end_turn" as StopReason };
          }
        }
      );
    },

    /** ③ 协议层：deepagents-acp 确认 turn 已结束（含 onPrompt 短路路径）。 */
    async onPromptComplete(ctx) {
      const turn = promptTurnMeta.get(ctx.sessionId);
      logPromptComplete({
        sessionId: ctx.sessionId,
        stopReason: ctx.stopReason,
        promptMs: turn != null ? Date.now() - turn.startedAt : 0,
        requestId: turn?.requestId,
      });
      promptTurnMeta.delete(ctx.sessionId);
    },

    /** session/set_config_option(model)：更新 sessionConfig 并热重建 executor（think 节点闭包绑定新 model）。 */
    async onSessionConfigOption(ctx) {
      if (!options.createExecutor || ctx.configId !== "model") return;
      const model = ctx.value.trim();
      if (!model) return;

      const configured = sessionConfigs.get(ctx.sessionId);
      if (!configured) {
        log.warn("onSessionConfigOption: no session config cached", {
          sessionId: ctx.sessionId,
        });
        return;
      }

      if (configured.sessionConfig.model === model && sessions.has(ctx.sessionId)) {
        return;
      }

      configured.sessionConfig = {
        ...configured.sessionConfig,
        model,
      };
      sessionConfigs.set(ctx.sessionId, configured);
      // model 热切换：MCP 配置未变，按 load 复用工具 schema 缓存（不清、可命中）。
      await rebuildSessionExecutor(ctx.sessionId, "load");
    },

    // session 关闭：释放 per-session 资源（MCP stdio 子进程等）+ 清兜底状态。
    async onSessionClosed(ctx) {
      const entry = sessions.get(ctx.sessionId);
      if (entry) {
        try {
          await entry.dispose?.();
        } catch {
          /* best-effort teardown */
        }
        sessions.delete(ctx.sessionId);
      }
      sessionConfigs.delete(ctx.sessionId);
      sessionFailures.delete(ctx.sessionId);
      fallbackResume.delete(ctx.sessionId);
      log.info("session closed", { sessionId: ctx.sessionId });
    },

    // 跨进程 session/load 历史回放（待实现）：
    // getSessionHistory → resolveExecutor(sessionId) → flow.getThreadMessages?.(sessionId)
    // → 返回 messages；见 core/flow-types SessionThreadHistory 与 deepagents-acp getSessionHistory hook。
  };
}

/** 启动 Flow ACP 服务（stdio）。主路径 StatefulFlow；per-session 工厂见 createExecutor。 */
export async function bootstrapFlowAcp(options: FlowAcpOptions): Promise<void> {
  if (options.debug) {
    process.env.LOG_LEVEL = "debug";
  }
  const { appConfig } = options;

  log.info("Flow ACP bootstrapping", {
    agent: appConfig.agent.name,
    model: `${appConfig.model.provider}:${appConfig.model.name}`,
    mode: options.createExecutor ? "per-session" : "single-executor",
  });
  logStartupAcpEnvDiagnostics();

  // 极简 throwaway agent —— onPrompt 会短路，它永不进入请求路径。
  const agentConfig = {
    name: appConfig.agent.name,
    description: appConfig.agent.description,
    model: resolveModel(appConfig),
    tools: [],
  } as unknown as DeepAgentConfig;

  const hooks = createFlowHooks(options);

  const server = new DeepAgentsServer({
    agents: agentConfig,
    serverName: appConfig.agent.name,
    serverVersion: appConfig.agent.version || "1.0.0",
    debug: process.env.LOG_LEVEL === "debug",
    hooks,
  });

  log.info("Starting Flow ACP server (workflow short-circuits agent via onPrompt)");
  await server.start();
}
