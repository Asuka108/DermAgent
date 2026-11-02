/**
 * LLM 调用韧性 —— 模板级共用（默认 ReAct 图、compaction、durable stateful flow 示例）。
 *
 * 从 deep-research 实战反哺的能力：
 *  - 超时护栏（timeout guard）：慢模型（如 mimo-v2.5-pro）+ Send 并行时单次 invoke 易 >60s
 *  - 指数退避重试：限流 429 / 网络抖动不应直接掐死整条流水线
 *  - 并发闸门：并行扇出时避免 N 路同时打满上游 API（搜索侧 rateLimited，LLM 侧需单独限流）
 *
 * 超时/并发可通过 env 或 config.model.settings 覆盖：
 *   LLM_TIMEOUT_MS / model.settings.invokeTimeoutMs
 *   LLM_LONG_TIMEOUT_MS / model.settings.invokeLongTimeoutMs
 *   LLM_MAX_CONCURRENT / model.settings.maxConcurrentInvokes
 */

import type { BaseMessage } from "@langchain/core/messages";
import { logger, type AppConfig } from "../index.js";
import { acpPromptLogFields } from "../session-trace.js";
import { markStart, markEnd } from "../perf-trace.js";

const log = logger.child("llm-resilience");

/** 短调用默认超时（plan / review / think / 章节摘要）。实测 60s 对慢模型偏紧。 */
export const LLM_TIMEOUT_SHORT_MS = 120_000;
/** 长调用默认超时（draft / respond / compaction 摘要）。 */
export const LLM_TIMEOUT_LONG_MS = 180_000;
/** Send 并行或高负载时 LLM 默认最大并发。 */
export const LLM_DEFAULT_MAX_CONCURRENT = 2;

export interface LlmResilienceSettings {
  shortTimeoutMs: number;
  longTimeoutMs: number;
  maxConcurrent: number;
}

function parsePositiveInt(raw: string | undefined): number | undefined {
  if (!raw?.trim()) return undefined;
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : undefined;
}

/**
 * 合并 env + AppConfig 得到 LLM 韧性参数（env 经 config loader 写入 settings 后与此一致）。
 */
export function resolveLlmResilience(config?: AppConfig): LlmResilienceSettings {
  const settings = config?.model?.settings as
    | {
        invokeTimeoutMs?: number;
        invokeLongTimeoutMs?: number;
        maxConcurrentInvokes?: number;
      }
    | undefined;

  return {
    shortTimeoutMs:
      settings?.invokeTimeoutMs ??
      parsePositiveInt(process.env.LLM_TIMEOUT_MS) ??
      LLM_TIMEOUT_SHORT_MS,
    longTimeoutMs:
      settings?.invokeLongTimeoutMs ??
      parsePositiveInt(process.env.LLM_LONG_TIMEOUT_MS) ??
      LLM_TIMEOUT_LONG_MS,
    maxConcurrent:
      settings?.maxConcurrentInvokes ??
      parsePositiveInt(process.env.LLM_MAX_CONCURRENT) ??
      LLM_DEFAULT_MAX_CONCURRENT,
  };
}

/**
 * 单步 Promise 超时护栏。超时 reject，由调用方或 invokeWithResilience 决定重试/降级。
 *
 * 传入 `signal` 时，abort 立即以 AbortError reject（不等超时）——用于 ACP cancel
 * 即时打断正在跑的 LLM 调用，而非等到 between-node 边界或 timeout 兜底。
 */
export async function withTimeout<T>(
  p: Promise<T>,
  ms: number,
  label = "操作",
  signal?: AbortSignal
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let onAbort: (() => void) | undefined;
  const abortPromise = new Promise<never>((_, reject) => {
    if (!signal) return;
    const fail = () => {
      const err = new Error(`${label}已取消`);
      err.name = "AbortError";
      reject(err);
    };
    if (signal.aborted) {
      fail();
    } else {
      onAbort = fail;
      signal.addEventListener("abort", fail, { once: true });
    }
  });
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label}超时（${ms}ms）`)), ms);
  });
  try {
    return await Promise.race([p, timeout, abortPromise]);
  } finally {
    if (timer) clearTimeout(timer);
    if (onAbort && signal) signal.removeEventListener("abort", onAbort);
  }
}

/**
 * 指数退避重试。重试用尽仍失败才抛，交调用方降级。
 *
 * 传入 `signal` 时：abort 或捕获到 AbortError 立即抛出、不再重试——用户已取消，
 * 退避后重试只会拖长取消响应。
 */
export async function withRetry<T>(
  fn: () => Promise<T>,
  opts: {
    attempts?: number;
    baseDelayMs?: number;
    label?: string;
    signal?: AbortSignal;
  } = {}
): Promise<T> {
  const attempts = opts.attempts ?? 3;
  const base = opts.baseDelayMs ?? 800;
  let lastErr: unknown;
  for (let i = 0; i < attempts; i++) {
    if (opts.signal?.aborted) {
      const err = new Error(`${opts.label ?? "LLM"}已取消`);
      err.name = "AbortError";
      throw err;
    }
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      // 取消不重试：用户已取消，立即抛出由调用方降级/收尾
      if ((err as Error)?.name === "AbortError" || opts.signal?.aborted) {
        throw err;
      }
      if (i < attempts - 1) {
        log.warn(`${opts.label ?? "LLM"}失败，重试 ${i + 1}/${attempts - 1}`, {
          error: String(err),
        });
        await new Promise((r) => setTimeout(r, base * 2 ** i));
      }
    }
  }
  throw lastErr;
}

/**
 * 信号量式并发限制器——超出 maxConcurrent 的调用 FIFO 排队。
 * fn 抛错也会 release 槽位。
 */
export function createConcurrencyLimiter(maxConcurrent: number) {
  if (maxConcurrent < 1) {
    throw new Error("createConcurrencyLimiter: maxConcurrent 至少为 1");
  }
  let active = 0;
  const waiters: Array<() => void> = [];

  const acquire = (): Promise<void> =>
    new Promise((resolve) => {
      if (active < maxConcurrent) {
        active++;
        resolve();
      } else {
        waiters.push(() => {
          active++;
          resolve();
        });
      }
    });

  const release = (): void => {
    active--;
    const next = waiters.shift();
    if (next) next();
  };

  return async function limited<T>(fn: () => Promise<T>): Promise<T> {
    await acquire();
    try {
      return await fn();
    } finally {
      release();
    }
  };
}

/** 按 config/env 惰性创建的全局 LLM 并发闸门（默认图 + compaction + 示例可共用）。 */
let sharedLimiter: {
  max: number;
  limit: ReturnType<typeof createConcurrencyLimiter>;
} | null = null;

export function getSharedLlmLimiter(config?: AppConfig): ReturnType<typeof createConcurrencyLimiter> {
  const { maxConcurrent } = resolveLlmResilience(config);
  if (!sharedLimiter || sharedLimiter.max !== maxConcurrent) {
    sharedLimiter = {
      max: maxConcurrent,
      limit: createConcurrencyLimiter(maxConcurrent),
    };
  }
  return sharedLimiter.limit;
}

export interface InvokeWithResilienceOptions {
  /** 单次 invoke 超时（毫秒）。 */
  timeoutMs?: number;
  /** 超时错误文案前缀。 */
  label?: string;
  /** 重试次数（默认 3）。 */
  attempts?: number;
  /** 重试日志前缀。 */
  retryLabel?: string;
  /** 重试基础退避（毫秒）。 */
  baseDelayMs?: number;
  /**
   * 是否经共享并发闸门排队。
   * 默认图 think 为串行可 false；Send 扇出并行示例应 true。
   */
  useSharedLimiter?: boolean;
  /** useSharedLimiter 时用于解析并发上限的 config。 */
  config?: AppConfig;
  /**
   * 取消信号（ACP cancel）。传入后透传到 model.invoke + 超时 + 重试：
   * abort 时正在跑的 LLM 调用立即以 AbortError reject，且不重试。
   */
  signal?: AbortSignal;
}

type InvokeModel = {
  invoke: (
    messages: BaseMessage[],
    options?: { signal?: AbortSignal }
  ) => Promise<{ content: unknown }>;
};

/**
 * 从 invoke 结果（AIMessage）抽取 token 用量 + 缓存命中，用于诊断 TTFT/前缀缓存。
 *
 * 优先 LangChain 标准化 `usage_metadata`（`input_token_details.cache_read` = 命中的缓存 token）；
 * 再兜底刮取 provider 原生 usage 里的非标准字段（deepseek `prompt_cache_hit_tokens` /
 * openai 兼容 `prompt_tokens_details.cached_tokens`），因为某些兼容端点（如 mimo）未必映射到标准位。
 * 全部 optional：缺字段就不记（fallback 路径 / 端点不报 usage 时返回空对象）。
 */
function extractUsageFields(result: unknown): Record<string, number> {
  const fields: Record<string, number> = {};
  if (!result || typeof result !== "object") return fields;
  const r = result as {
    usage_metadata?: {
      input_tokens?: number;
      output_tokens?: number;
      input_token_details?: { cache_read?: number; cache_creation?: number };
    };
    response_metadata?: {
      usage?: Record<string, unknown>;
      tokenUsage?: Record<string, unknown>;
    };
  };
  const um = r.usage_metadata;
  if (um) {
    if (typeof um.input_tokens === "number") fields.inputTokens = um.input_tokens;
    if (typeof um.output_tokens === "number") fields.outputTokens = um.output_tokens;
    const det = um.input_token_details;
    if (det) {
      if (typeof det.cache_read === "number") fields.cachedTokens = det.cache_read;
      if (typeof det.cache_creation === "number") fields.cacheCreationTokens = det.cache_creation;
    }
  }
  // provider 原生 usage（标准位取不到缓存时的兜底）；命中数统一归一到 cachedTokens，
  // 使 grep cachedTokens 覆盖所有 provider（mimo/deepseek/openai 兼容），raw_* 仅留作对账。
  const raw = r.response_metadata?.usage ?? r.response_metadata?.tokenUsage;
  if (raw && typeof raw === "object") {
    const trySetCached = (v: unknown) => {
      if (typeof v === "number" && fields.cachedTokens === undefined) fields.cachedTokens = v;
    };
    trySetCached((raw as Record<string, unknown>).prompt_cache_hit_tokens);
    trySetCached((raw as Record<string, unknown>).cached_tokens);
    const miss = (raw as Record<string, unknown>).prompt_cache_miss_tokens;
    if (typeof miss === "number") fields.raw_prompt_cache_miss_tokens = miss;
    const ptd = (raw as { prompt_tokens_details?: { cached_tokens?: number } }).prompt_tokens_details;
    if (ptd) trySetCached(ptd.cached_tokens);
  }
  return fields;
}

/**
 * 标准 LLM 调用链：可选并发闸门 → 超时 → 重试。
 * 默认图、compaction、durable stateful flow 示例节点统一经此入口，避免各处复制护栏逻辑。
 */
export function invokeWithResilience<M extends InvokeModel>(
  model: M,
  messages: BaseMessage[],
  options: InvokeWithResilienceOptions = {}
): Promise<Awaited<ReturnType<M["invoke"]>>> {
  const resilience = resolveLlmResilience(options.config);
  const timeoutMs = options.timeoutMs ?? resilience.shortTimeoutMs;
  const label = options.label ?? "LLM 调模型";
  const startedAt = Date.now();
  // perf 计时（与 startedAt 同口径，含并发闸门排队）：每次模型调用一行 perf + 计入当前 prompt 汇总。
  const perfMark = markStart("llm.invoke");
  log.debug("LLM invoke start", acpPromptLogFields({
    label,
    messageCount: messages.length,
    timeoutMs,
    useSharedLimiter: Boolean(options.useSharedLimiter),
  }));
  const run = () =>
    withRetry(
      () =>
        withTimeout(
          model.invoke(messages, options.signal ? { signal: options.signal } : undefined),
          timeoutMs,
          label,
          options.signal
        ),
      {
        attempts: options.attempts,
        baseDelayMs: options.baseDelayMs,
        label: options.retryLabel ?? label,
        signal: options.signal,
      }
    )
      .then((result) => {
        const durationMs = Date.now() - startedAt;
        log.debug("LLM invoke done", acpPromptLogFields({ label, durationMs }));
        // TTFT/缓存诊断（info 级，不依赖 LOG_LEVEL=debug）：记 token 用量 + 缓存命中。
        // 看 cachedTokens/raw_prompt_cache_hit_tokens 是否 >0 → 端点(mimo)到底缓不缓存；
        // 看 inputTokens 大小 → 44 工具 schema 在 prefill 里占多少。无该行=端点未报 usage。
        const usage = extractUsageFields(result);
        if (Object.keys(usage).length > 0) {
          log.info("LLM usage", acpPromptLogFields({ label, durationMs, ...usage }));
        }
        markEnd(perfMark, { label, ...usage });
        return result;
      })
      .catch((err) => {
        markEnd(perfMark, { label, failed: true });
        log.warn("LLM invoke failed", acpPromptLogFields({
          label,
          durationMs: Date.now() - startedAt,
          error: String(err),
        }));
        throw err;
      }) as Promise<Awaited<ReturnType<M["invoke"]>>>;

  if (options.useSharedLimiter) {
    return getSharedLlmLimiter(options.config)(run);
  }
  return run();
}
