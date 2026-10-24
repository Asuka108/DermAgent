/**
 * 会话调试 trace —— 与 app 业务节点解耦。
 *
 * ACP 路径：`runInAcpPromptCycle` 在 onPrompt 周期内设置 AsyncLocalStorage，
 * 周期内 tool/stage/LLM 日志自动带 sessionId + promptMs。
 * CLI 路径：surfaces/cli/run.ts 在调用 flow 前包 `traceFlowRun` / `traceFlowCallbacks`。
 * 业务图（stateful-flow / default-flow）不感知 trace。
 */

import { AsyncLocalStorage } from "node:async_hooks";
import type { FlowCallbacks, ToolCallEvent } from "../core/flow-types.js";
import {
  logger,
  truncateForLog,
  formatPayloadForLog,
  formatMessagesForLog,
  getEffectiveLogLevel,
} from "./logger.js";
import { beginPerfSession, endPerfSession, markStart, markEnd, recordPhase } from "./perf-trace.js";

const log = logger.child("session-trace");

/** 单次 ACP onPrompt 调用周期上下文（AsyncLocalStorage）。 */
export interface AcpPromptCycle {
  sessionId: string;
  startedAt: number;
  /**
   * `session/prompt` `_meta.requestId` 透传的业务请求 ID（一轮用户消息）。
   * 与 JSON-RPC `/$/cancel_request` 的 requestId、ask-question 表单 requestId 不是同一概念。
   */
  requestId?: string;
  /** query | resume */
  mode?: string;
  query?: string;
}

const acpPromptStore = new AsyncLocalStorage<AcpPromptCycle>();

/**
 * 单次 flow 执行的 perf 作用域（traceFlowRun 设，traceFlowCallbacks.onToken 读首 token 算 TTFT）。
 * 经 AsyncLocalStorage 传递，使任意拓扑/自实现 flow 的 onToken 都能拿到 run 起点而无需改 flow 签名。
 */
interface PerfRunScope {
  /** run 起点（performance.now() 时基，与 perf-trace 一致），算端到端 TTFT 用。 */
  perfStartedAt: number;
  /** 是否已记录首个 token（一轮只记一次 flow.firstToken）。 */
  firstTokenDone: boolean;
}
const perfRunStore = new AsyncLocalStorage<PerfRunScope>();

/** 当前 flow 执行的 perf 作用域（traceFlowRun 执行范围内有效；无则 undefined）。 */
function getPerfRun(): PerfRunScope | undefined {
  return perfRunStore.getStore();
}

/** 是否处于 ACP onPrompt 周期内（executor 层据此跳过重复 trace 包装）。 */
export function isInAcpPromptCycle(): boolean {
  return acpPromptStore.getStore() != null;
}

/** 当前 ACP 周期（无则 undefined）。 */
export function getAcpPromptCycle(): AcpPromptCycle | undefined {
  return acpPromptStore.getStore();
}

/** 供 session-trace / llm-resilience 附带的周期字段。 */
export function acpPromptLogFields(extra?: Record<string, unknown>): Record<string, unknown> {
  const cycle = getAcpPromptCycle();
  if (!cycle) return extra ?? {};
  return {
    sessionId: cycle.sessionId,
    ...(cycle.requestId ? { requestId: cycle.requestId } : {}),
    promptMs: Date.now() - cycle.startedAt,
    ...(cycle.mode ? { mode: cycle.mode } : {}),
    ...extra,
  };
}

/**
 * 在 ACP onPrompt 周期内执行 fn；周期内所有 trace 自动关联 sessionId。
 * 由 surfaces/acp/server.ts 在 onPrompt 入口调用。
 */
export function runInAcpPromptCycle<T>(
  cycle: AcpPromptCycle,
  fn: () => Promise<T>
): Promise<T> {
  return acpPromptStore.run(cycle, fn);
}

/** trace 上下文（CLI 等非 ACP 路径可显式传 threadId）。 */
export interface SessionTraceContext {
  sessionId?: string;
  threadId?: string;
}

function resolveSessionId(ctx?: SessionTraceContext): string | undefined {
  return getAcpPromptCycle()?.sessionId ?? ctx?.sessionId ?? ctx?.threadId;
}

/**
 * 工具调用断言只需要工具名、状态与结果长度，不应为此开启全局 debug
 * （debug 会关闭凭证/端点脱敏）。专用开关下以 info 输出安全摘要；正常运行仍保持 debug。
 */
function logToolInvokeSummary(
  message: "tool invoke start" | "tool invoke done" | "tool invoke failed",
  context: Record<string, unknown>
): void {
  if (process.env.SMOKE_TOOL_TRACE === "1") {
    log.info(message, context);
  } else {
    log.debug(message, context);
  }
}

function traceToolCallEvent(e: ToolCallEvent, ctx?: SessionTraceContext): void {
  const base = acpPromptLogFields({
    toolName: e.toolName,
    toolCallId: e.toolCallId,
    sessionId: resolveSessionId(ctx),
  });
  if (e.status === "in_progress") {
    logToolInvokeSummary("tool invoke start", base);
    log.block("debug", `tool ${e.toolName} args`, formatPayloadForLog(e.args));
    return;
  }
  if (e.status === "completed") {
    const text = typeof e.result === "string" ? e.result : JSON.stringify(e.result ?? "");
    logToolInvokeSummary("tool invoke done", { ...base, resultChars: text.length });
    log.block("debug", `tool ${e.toolName} result`, formatPayloadForLog(text));
    return;
  }
  // smoke 摘要不带底层错误正文；其中可能包含 URL、响应体或凭证。
  if (process.env.SMOKE_TOOL_TRACE === "1") {
    logToolInvokeSummary("tool invoke failed", base);
  } else {
    logToolInvokeSummary("tool invoke failed", { ...base, error: e.error });
  }
  if (e.error) {
    log.block("debug", `tool ${e.toolName} error`, formatPayloadForLog(e.error));
  }
}

/**
 * 包装 FlowCallbacks：在原有回调前后写 tool/stage trace。
 * ACP 周期内由 server 包一层；CLI 由 stateful-flow 包一层。
 */
export function traceFlowCallbacks(
  callbacks: FlowCallbacks = {},
  ctx?: SessionTraceContext
): FlowCallbacks {
  return {
    ...callbacks,
    onToken: async (token, source, toolCallId) => {
      // 端到端 TTFT：首个流式 token 到达时刻 − run 起点（perfStartedAt）。一轮只记一次。
      // 任意拓扑/自实现 flow 经 traceFlowCallbacks 包装即自动获得，无需 flow 自身插点。
      const runScope = getPerfRun();
      if (runScope && !runScope.firstTokenDone) {
        runScope.firstTokenDone = true;
        recordPhase(
          "flow.firstToken",
          Math.round(performance.now() - runScope.perfStartedAt),
          source ? { source } : undefined,
        );
      }
      return callbacks.onToken?.(token, source, toolCallId);
    },
    onToolCall: async (e) => {
      traceToolCallEvent(e, ctx);
      await callbacks.onToolCall?.(e);
    },
    onStage: callbacks.onStage
      ? async (e) => {
          log.debug(
            "stage",
            acpPromptLogFields({
              stage: e.stage,
              index: e.index,
              total: e.total,
              sessionId: resolveSessionId(ctx),
              threadId: ctx?.threadId,
            })
          );
          return callbacks.onStage!(e);
        }
      : undefined,
    onPlan: callbacks.onPlan,
    signal: callbacks.signal,
  };
}

export interface TraceFlowRunMeta {
  input?: string;
  sessionId?: string;
  threadId?: string;
  mode?: string;
  [key: string]: unknown;
}

function compactMeta(meta: TraceFlowRunMeta): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(meta)) {
    if (v !== undefined) out[k] = v;
  }
  return out;
}

/**
 * 包裹一次 flow 执行（由 surface 层调用：ACP server / CLI）。
 */
export async function traceFlowRun<T extends {
  output?: string;
  answer?: string;
  question?: string;
  steps?: string[];
  messages?: Array<{ _getType?: () => string; content?: unknown; tool_calls?: unknown }>;
  status?: string;
  footer?: string;
}>(
  label: string,
  meta: TraceFlowRunMeta,
  fn: () => Promise<T>
): Promise<T> {
  const startedAt = Date.now();
  const inputText = meta.input ?? "";
  const cycleFields = acpPromptLogFields();
  log.info(`${label} start`, {
    ...compactMeta(meta),
    ...cycleFields,
    input: inputText ? truncateForLog(inputText, 200) : undefined,
    inputChars: inputText.length,
  });
  // 每轮 prompt 一份 perf 汇总（label 即 trace label，如 flow.run）：聚合 flow.run / llm.invoke / flow.firstToken。
  // scope 经 AsyncLocalStorage 下发，onToken 据此算端到端 TTFT（任意拓扑/自实现 flow 通用）。
  const perfSession = beginPerfSession(label);
  const runMark = markStart(label);
  const scope: PerfRunScope = { perfStartedAt: runMark.startedAt, firstTokenDone: false };
  try {
    const result = await perfRunStore.run(scope, fn);
    const answer = result.output ?? result.answer ?? "";
    const doneMeta: Record<string, unknown> = {
      ...compactMeta(meta),
      ...acpPromptLogFields(),
      durationMs: Date.now() - startedAt,
      flowStatus: result.status,
      outputChars: answer.length,
      footerChars: result.footer?.length ?? 0,
    };
    if (result.steps?.length) doneMeta.steps = result.steps;
    if (result.status === "interrupted" && result.question) {
      doneMeta.questionChars = result.question.length;
    }
    log.info(`${label} done`, doneMeta);
    if (result.messages?.length) {
      log.block("debug", `${label} messages`, formatMessagesForLog(result.messages));
    } else if (answer) {
      log.debug(`${label} output preview`, {
        outputChars: answer.length,
        preview: truncateForLog(answer, 200),
      });
    }
    return result;
  } catch (err) {
    log.error(`${label} failed`, {
      ...compactMeta(meta),
      ...acpPromptLogFields(),
      durationMs: Date.now() - startedAt,
      error: String(err),
    });
    throw err;
  } finally {
    markEnd(runMark);
    endPerfSession(perfSession, {
      sessionId: resolveSessionId({ sessionId: meta.sessionId, threadId: meta.threadId }),
    });
  }
}

/** ACP session/prompt 周期起止（仅 surfaces/acp/server.ts 调用）。 */

/** 协议收到 session/prompt，onPrompt 入口。 */
export function logAcpPromptStart(meta: {
  sessionId: string;
  query: string;
  requestId?: string;
  mode?: string;
  resuming?: boolean;
  isStateful?: boolean;
}): void {
  const logQuery =
    getEffectiveLogLevel() === "debug" ? meta.query : truncateForLog(meta.query, 200);
  log.info("prompt_start", {
    sessionId: meta.sessionId,
    ...(meta.requestId ? { requestId: meta.requestId } : {}),
    ...(meta.mode ? { mode: meta.mode } : {}),
    ...(meta.resuming != null ? { resuming: meta.resuming } : {}),
    ...(meta.isStateful != null ? { isStateful: meta.isStateful } : {}),
    query: logQuery,
    queryChars: meta.query.length,
    promptMs: 0,
  });
}

/** onPrompt 即将 return { stopReason } 给 deepagents-acp。 */
export function logAcpPromptEnd(meta: {
  sessionId: string;
  startedAt: number;
  requestId?: string;
  /** ACP 协议返回给客户端的 stopReason（end_turn / cancelled / …）。 */
  stopReason: string;
  /** flow 内部状态（interrupted / done），与 stopReason 不同：HITL interrupt 仍返回 end_turn。 */
  flowStatus?: string;
  answerChars?: number;
  questionChars?: number;
  streamed?: boolean;
  streamChars?: number;
  tokenChunks?: number;
  error?: string;
}): void {
  const fields: Record<string, unknown> = {
    sessionId: meta.sessionId,
    ...(meta.requestId ? { requestId: meta.requestId } : {}),
    promptMs: Date.now() - meta.startedAt,
    stopReason: meta.stopReason,
  };
  if (meta.flowStatus) fields.flowStatus = meta.flowStatus;
  if (meta.answerChars != null) fields.answerChars = meta.answerChars;
  if (meta.questionChars != null) fields.questionChars = meta.questionChars;
  if (meta.streamed != null) fields.streamed = meta.streamed;
  if (meta.streamChars != null) fields.streamChars = meta.streamChars;
  if (meta.tokenChunks != null) fields.tokenChunks = meta.tokenChunks;
  if (meta.error) fields.error = meta.error;
  const level = meta.error ? "error" : "info";
  log[level](`prompt_end ${meta.stopReason}`, fields);
}

/** deepagents-acp 在 turn 收尾后回调（协议层确认）。 */
export function logPromptComplete(meta: {
  sessionId: string;
  stopReason: string;
  promptMs: number;
  requestId?: string;
}): void {
  log.info(`prompt_complete ${meta.stopReason}`, {
    sessionId: meta.sessionId,
    ...(meta.requestId ? { requestId: meta.requestId } : {}),
    stopReason: meta.stopReason,
    promptMs: meta.promptMs,
  });
}
