/**
 * LLM 节点 factory + LLM 内容原语。
 *
 * 汇聚：
 *  - 原语：extractText（content→纯文本）、parseJson（安全 JSON 解析）、streamLLMText（韧性流式调用）；
 *  - factory：createLlmNode（一次调→文本/结构化）、createLlmStreamNode（流式→emit token）。
 *
 * factory 泛型于 S，用 prompt(state)/write(result,state) 回调解耦具体 state 形状。
 * consume runtime 韧性（invokeWithResilience/withRetry/withTimeout/共享闸门）+ 本目录 emitTextToken。
 */

import { SystemMessage, type BaseMessage } from "@langchain/core/messages";
import type { LangGraphRunnableConfig } from "@langchain/langgraph";
import type { AppConfig } from "../../runtime/index.js";
import {
  invokeWithResilience,
  withRetry,
  withTimeout,
  getSharedLlmLimiter,
  resolveLlmResilience,
} from "../../runtime/services/llm-resilience.js";
import { emitTextToken } from "./emit.js";
import { foldStreamTextChunk } from "./stream-text-delta.js";

/** 从 LLM 返回的 content 抽纯文本（string 或 content block 数组）。 */
export function extractText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((b) =>
        b && typeof b === "object" && "text" in b
          ? String((b as { text: unknown }).text)
          : ""
      )
      .join("");
  }
  return "";
}

type MessageLike = {
  content?: unknown;
  additional_kwargs?: Record<string, unknown>;
};

/** 从 Anthropic content blocks 抽取原生 extended thinking 文本。 */
export function extractAnthropicThinkingText(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .map((block) => {
      if (!block || typeof block !== "object") return "";
      const value = block as { type?: unknown; thinking?: unknown };
      return value.type === "thinking" && typeof value.thinking === "string"
        ? value.thinking
        : "";
    })
    .join("");
}

/**
 * 从消息的 additional_kwargs 抽 reasoning 文本。
 * OpenAI 兼容 reasoning 模型常见字段：reasoning_content / reasoning。
 */
export function extractReasoningTextFromMessage(message: MessageLike | null | undefined): string {
  const kwargs = message?.additional_kwargs;
  if (!kwargs || typeof kwargs !== "object") return "";
  const value = kwargs.reasoning_content ?? kwargs.reasoning;
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return extractText(value);
  return "";
}

/**
 * 抽取流式思考文本：Anthropic 原生 thinking block 优先，兼容代理的 reasoning 字段兜底。
 * 仅用于 thought 通道，不用于终态可见正文兜底。
 */
export function extractThoughtTextFromMessage(message: MessageLike | null | undefined): string {
  const anthropicThinking = extractAnthropicThinkingText(message?.content);
  if (anthropicThinking) return anthropicThinking;
  return extractReasoningTextFromMessage(message);
}

/**
 * 抽取用户可见文本：优先 content；content 为空时兜底 reasoning_content。
 *
 * 仅用于**终态**（respond / 空回复自愈），不要用于 messages 流式路径。
 * 流式必须分流：content → text，reasoning_content → thought（见 mapStreamChunk）。
 * 部分 OpenAI 兼容 reasoning 模型偶发把用户可见回答只写进 reasoning_content。
 */
export function extractVisibleTextFromMessage(message: MessageLike | null | undefined): string {
  const content = extractText(message?.content);
  if (content.trim()) return content;
  return extractReasoningTextFromMessage(message);
}

/** 从 LLM 文本抽第一段 JSON（容忍 ```json 围栏与前后说明文字）。 */
export function parseJson<T>(text: string): T {
  const cleaned = text.replace(/```(?:json)?/gi, "").trim();
  const start = cleaned.search(/[[{]/);
  if (start === -1) throw new Error(`LLM 未返回 JSON：${text.slice(0, 200)}`);
  const close = cleaned[start] === "[" ? "]" : "}";
  const end = cleaned.lastIndexOf(close);
  if (end <= start) throw new Error(`LLM JSON 不完整：${text.slice(0, 200)}`);
  return JSON.parse(cleaned.slice(start, end + 1)) as T;
}

/** 仅在有 systemPrompt 且 messages 首条非 system 时前置注入。 */
function withSystemPrompt(messages: BaseMessage[], systemPrompt: string): BaseMessage[] {
  if (!systemPrompt) return messages;
  if (messages.length > 0 && messages[0]?._getType?.() === "system") return messages;
  return [new SystemMessage(systemPrompt), ...messages];
}

export type LLMLike = {
  invoke: (
    messages: BaseMessage[],
    options?: { signal?: AbortSignal }
  ) => Promise<{ content: unknown }>;
  stream?: (
    messages: BaseMessage[],
    options?: { signal?: AbortSignal }
  ) =>
    | Promise<AsyncIterable<{ content?: unknown }>>
    | AsyncIterable<{ content?: unknown }>;
};

export interface StreamLlmTextOptions {
  label?: string;
  retryLabel?: string;
}

/**
 * 流式调模型：仅用于用户可见的大段输出（draft/respond）。
 * 有 visible token sink 且模型支持 stream → 逐 chunk emitTextToken；否则退回一次性 invoke。
 */
export async function streamLLMText(
  m: LLMLike,
  messages: BaseMessage[],
  appConfig: AppConfig | undefined,
  config: LangGraphRunnableConfig | undefined,
  timeoutMs: number,
  opts?: StreamLlmTextOptions
): Promise<{ text: string; streamed: boolean }> {
  const label = opts?.label ?? "流式调模型";
  const retryLabel = opts?.retryLabel ?? "streaming LLM";
  const hasVisibleTokenSink = Boolean(
    (config?.configurable as { onToken?: unknown } | undefined)?.onToken
  );

  if (!m.stream || !hasVisibleTokenSink) {
    const res = await invokeWithResilience(m, messages, {
      timeoutMs,
      label,
      retryLabel,
      useSharedLimiter: true,
      config: appConfig,
      signal: config?.signal,
    });
    return { text: extractText(res.content), streamed: false };
  }

  const signal = config?.signal;
  const run = async () => {
    let full = "";
    const stream = await Promise.resolve(
      m.stream!(messages, signal ? { signal } : undefined)
    );
    for await (const chunk of stream) {
      const text = extractText(chunk.content);
      if (!text) continue;
      const folded = foldStreamTextChunk(full, text);
      if (!folded.delta) continue;
      full = folded.full;
      await emitTextToken(config, folded.delta);
    }
    return full;
  };

  const text = await getSharedLlmLimiter(appConfig)(() =>
    withRetry(
      () => withTimeout(run(), timeoutMs, label, signal),
      { label: retryLabel, signal }
    )
  );
  return { text, streamed: true };
}

export type ChatModelLike = { invoke: (messages: BaseMessage[]) => Promise<{ content: unknown }> };

/** createLlmNode 选项。 */
export interface LlmNodeOptions<S> {
  /** 模型实例，或按 state 解析（返回 falsy 触发 fallback）。 */
  model: ChatModelLike | ((state: S) => ChatModelLike | null | undefined);
  /** 由 state 构造消息。 */
  prompt: (state: S) => BaseMessage[];
  /** 把结果写回 state。parse 提供时 parsed = parse(content)。可选 config 供 write 内发副作用（emitPlan/emitStage）。 */
  write: (r: { content: string; parsed?: unknown }, state: S, config?: LangGraphRunnableConfig) => Partial<S>;
  /** 结构化输出：把 content 文本 parse 成 T。 */
  parse?: (text: string) => unknown;
  /** 可选系统提示词（首条非 system 时前置）。 */
  systemPrompt?: string;
  /** 韧性 config + label。 */
  config?: AppConfig;
  label?: string;
  retryLabel?: string;
  timeoutMs?: number;
  /** 重试次数（默认走 invokeWithResilience 的 3；评估等节点可传 1 不重试）。 */
  attempts?: number;
  /** 无模型 / 调用失败时的降级返回（无则抛错）。 */
  fallback?: (state: S, reason: "no-model" | "error", err?: unknown) => Partial<S>;
}

/**
 * 一次调 LLM → 写回（吃纯文本 / 结构化输出两类）。
 * 不含 bindTools（ReAct 工具决策节点保持各自实现，见默认图 think）。
 */
export function createLlmNode<S>(opts: LlmNodeOptions<S>) {
  const {
    model: modelOpt,
    prompt,
    write,
    parse,
    systemPrompt,
    config,
    label = "LLM 调模型",
    retryLabel,
    timeoutMs,
    attempts,
    fallback,
  } = opts;

  return async (state: S, lgConfig?: LangGraphRunnableConfig): Promise<Partial<S>> => {
    const model = typeof modelOpt === "function" ? modelOpt(state) : modelOpt;
    if (!model) {
      if (fallback) return fallback(state, "no-model");
      throw new Error(`${label}: 无模型`);
    }
    const messages = withSystemPrompt(prompt(state), systemPrompt ?? "");
    try {
      const { shortTimeoutMs } = resolveLlmResilience(config);
      const ai = (await invokeWithResilience(model, messages, {
        timeoutMs: timeoutMs ?? shortTimeoutMs,
        label,
        retryLabel: retryLabel ?? label,
        useSharedLimiter: true,
        attempts,
        config,
        signal: lgConfig?.signal,
      })) as { content: unknown };
      const content = extractText(ai.content);
      const parsed = parse ? parse(content) : undefined;
      return write(parse ? { content, parsed } : { content }, state, lgConfig);
    } catch (err) {
      if (fallback) return fallback(state, "error", err);
      throw err;
    }
  };
}

/** createLlmStreamNode 选项。 */
export interface LlmStreamNodeOptions<S> {
  model: LLMLike | ((state: S) => LLMLike);
  prompt: (state: S) => BaseMessage[];
  write: (r: { text: string; streamed: boolean }, state: S, config?: LangGraphRunnableConfig) => Partial<S>;
  config?: AppConfig;
  label?: string;
  retryLabel?: string;
  /** 流式需显式（长）超时。 */
  timeoutMs: number;
  /** 流式失败时的降级返回（无则抛错）。 */
  fallback?: (state: S) => Partial<S>;
}

/** 流式调 LLM → 写回文本（draft / respond 模式）。 */
export function createLlmStreamNode<S>(opts: LlmStreamNodeOptions<S>) {
  const { model: modelOpt, prompt, write, config, label, retryLabel, timeoutMs, fallback } = opts;
  return async (state: S, lgConfig?: LangGraphRunnableConfig): Promise<Partial<S>> => {
    const model = typeof modelOpt === "function" ? modelOpt(state) : modelOpt;
    try {
      const { text, streamed } = await streamLLMText(model, prompt(state), config, lgConfig, timeoutMs, {
        label,
        retryLabel,
      });
      return write({ text: text.trim(), streamed }, state, lgConfig);
    } catch (err) {
      if (fallback) return fallback(state);
      throw err;
    }
  };
}
