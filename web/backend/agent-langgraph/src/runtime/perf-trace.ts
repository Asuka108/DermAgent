/**
 * perf-trace —— 加载/运行阶段耗时追踪（性能分析用）。
 *
 * 面向「启动加载耗时」定位：runtime 装配（MCP 加载、skills/subagents 发现、工具构建、
 * 系统提示词组装、checkpointer 建立、图构建）各阶段分别计时，统一以
 * `perf phase=<name> ms=<n>` 行输出，便于外部日志分析按阶段聚合耗时。
 *
 * 开关：进程级**全局**环境变量 `PERF_TRACE`（非按 session/agent 分）。**默认开启**（随时可排查
 * 启动性能）；显式设 `PERF_TRACE=0|false|off` 全局关闭。关闭时 markStart/markEnd/timePhase 仍可调用
 * （无 I/O 副作用；仅极小计时/记账开销），只是不落日志。
 *
 * 精度：使用 `performance.now()`（亚毫秒），四舍五入到整数毫秒记录。
 *
 * 并发：session 级汇总用模块级 activeSession，契合 ACP「一个 connection 串行 prompt / 串行装配」。
 * 真并发多 session 装配时汇总可能交叉（与 logger currentWriter 同一权衡）；单阶段 `perf phase=` 行不受影响。
 */

import { logger } from "./logger.js";

const perfLog = logger.child("perf");

/** PERF_TRACE 是否启用（全局 env 开关，默认开；仅 PERF_TRACE=0|false|off 时关闭）。 */
export function isPerfTraceEnabled(): boolean {
  const v = process.env.PERF_TRACE?.trim().toLowerCase();
  return v !== "0" && v !== "false" && v !== "off";
}

/** 单阶段计时句柄（markStart 产出，markEnd 消费）。 */
export interface PerfMark {
  name: string;
  startedAt: number;
}

interface PerfSession {
  label: string;
  startedAt: number;
  phases: Array<{ name: string; ms: number }>;
}

/** 当前活跃汇总会话（beginPerfSession 设，endPerfSession 清）。 */
let activeSession: PerfSession | null = null;

/** 开始一个阶段计时。返回句柄传给 markEnd。 */
export function markStart(name: string): PerfMark {
  return { name, startedAt: performance.now() };
}

/**
 * 结束阶段计时：记录耗时（毫秒），启用时打 `perf phase=<name> ms=<n>` 行并计入当前汇总。
 * @returns 本阶段耗时（毫秒，整数）。
 */
export function markEnd(mark: PerfMark, extra?: Record<string, unknown>): number {
  const ms = Math.round(performance.now() - mark.startedAt);
  if (activeSession) {
    activeSession.phases.push({ name: mark.name, ms });
  }
  if (isPerfTraceEnabled()) {
    perfLog.info("perf", { phase: mark.name, ms, ...extra });
  }
  return ms;
}

/**
 * 直接记录一段已测算好的耗时（不依赖 markStart/markEnd 句柄）。
 *
 * 用于事件触发型计时——例如首个流式 token 到达时才算 TTFT，markStart/markEnd 无法回溯包裹。
 * 启用时打 `perf phase=<name> ms=<n>` 行并计入当前汇总会话。
 */
export function recordPhase(name: string, ms: number, extra?: Record<string, unknown>): void {
  if (activeSession) {
    activeSession.phases.push({ name, ms });
  }
  if (isPerfTraceEnabled()) {
    perfLog.info("perf", { phase: name, ms, ...extra });
  }
}

/** 包裹一段（异步或同步）逻辑并计时，异常也会记录耗时。 */
export async function timePhase<T>(
  name: string,
  fn: () => Promise<T> | T,
  extra?: Record<string, unknown>
): Promise<T> {
  const mark = markStart(name);
  try {
    return await fn();
  } finally {
    markEnd(mark, extra);
  }
}

/** 开始一次汇总会话（如整个 runtime 装配）。返回的 session 传给 endPerfSession。 */
export function beginPerfSession(label: string): PerfSession {
  const session: PerfSession = { label, startedAt: performance.now(), phases: [] };
  activeSession = session;
  return session;
}

/**
 * 结束汇总会话：启用时打一行 `perf-summary` —— total 总耗时 + 各阶段耗时（按 ms 降序），
 * 便于一眼定位瓶颈。仅清理传入的 session（避免嵌套/交叉误清）。
 */
export function endPerfSession(session: PerfSession, extra?: Record<string, unknown>): void {
  const totalMs = Math.round(performance.now() - session.startedAt);
  if (activeSession === session) {
    activeSession = null;
  }
  if (!isPerfTraceEnabled()) return;
  const sorted = [...session.phases].sort((a, b) => b.ms - a.ms);
  const breakdown = sorted.map((p) => `${p.name}=${p.ms}`).join(",");
  perfLog.info("perf-summary", {
    label: session.label,
    totalMs,
    phaseCount: session.phases.length,
    phases: breakdown,
    ...extra,
  });
}
