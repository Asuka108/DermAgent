/**
 * Model Resolution
 *
 * Builds the chat model instance deepagents expects from the app config.
 * Instances are cached so repeated calls during a single agent lifecycle do not
 * re-instantiate.
 *
 * Note: conversation summarization is handled by deepagents' built-in
 * createSummarizationMiddleware (injected by createDeepAgent), so this module no
 * longer builds a separate summarizer model.
 */

import { type CreateDeepAgentParams } from "deepagents";
import { ChatAnthropic } from "@langchain/anthropic";
import { ChatOpenAI } from "@langchain/openai";
import { type AppConfig } from "../config/config-loader.js";

/** Build the model string deepagents expects: "provider:model-name" */
export function resolveModelString(config: AppConfig): string {
  return `${config.model.provider}:${config.model.name}`;
}

// Cache the model instance to avoid redundant instantiation on repeated calls.
let cachedModel: { key: string; instance: CreateDeepAgentParams["model"] } | null = null;

/**
 * provider-aware 解析 apiKey：openai 优先 OPENAI_API_KEY，其余（anthropic 系）优先 authToken。
 * 空则返回 ""（不抛错）——requireModel 据此判定缺凭证；cacheKey 含它使 env 后置填充时重建实例。
 */
export function resolveApiKey(config: AppConfig): string {
  if (config.model.provider === "openai") {
    return (
      process.env.OPENAI_API_KEY ||
      process.env[config.model.apiKeyEnv] ||
      process.env[config.model.authTokenEnv] ||
      ""
    );
  }
  return (
    process.env[config.model.authTokenEnv] ||
    process.env[config.model.apiKeyEnv] ||
    process.env.ANTHROPIC_AUTH_TOKEN ||
    process.env.ANTHROPIC_API_KEY ||
    ""
  );
}

/**
 * LangChain `ChatAnthropic` → Anthropic SDK 会在 baseURL 后自行拼接 `/v1/messages`。
 *
 * `ANTHROPIC_BASE_URL` 通常止于网关前缀（如 `https://.../api/proxy/model`），
 * SDK 最终请求 `.../model/v1/messages`。
 *
 * 若此处再补 `/v1` 会变成 `.../v1/v1/messages` → 404（实测 test-llm-proxy + glm-5.1）。
 * 用户若已配 `.../model/v1`，去掉尾部 `/vN` 避免双版本段。
 */
function normalizeAnthropicBaseUrl(baseUrl?: string): string | undefined {
  if (!baseUrl) return undefined;
  return baseUrl.replace(/\/+$/, "").replace(/\/v\d+$/, "");
}

/**
 * Anthropic 协议默认开启思考。
 * Opus 4.7 只接受 adaptive；其他官方 Claude 与三方兼容端点使用兼容面更广的固定预算形态。
 */
function resolveAnthropicThinking(model: string, budgetTokens: number):
  | { type: "adaptive" }
  | { type: "enabled"; budget_tokens: number } {
  if (model.startsWith("claude-opus-4-7")) return { type: "adaptive" };
  return { type: "enabled", budget_tokens: budgetTokens };
}

/** Build the model instance/string accepted by deepagents. */
export function resolveModel(config: AppConfig): CreateDeepAgentParams["model"] {
  const cacheKey = `${config.model.provider}:${config.model.name}|${config.model.baseUrl ?? ""}|${config.model.settings.temperature}|${config.model.settings.maxTokens ?? ""}|${config.model.settings.thinkingBudgetTokens}|${resolveApiKey(config)}`;
  if (cachedModel && cachedModel.key === cacheKey) {
    return cachedModel.instance;
  }

  const apiKey = resolveApiKey(config);

  let instance: CreateDeepAgentParams["model"];

  if (config.model.provider === "openai") {
    instance = new ChatOpenAI({
      model: config.model.name,
      apiKey,
      configuration: {
        baseURL: config.model.baseUrl,
      },
      temperature: config.model.settings.temperature,
      maxTokens: config.model.settings.maxTokens,
      // 开启 streaming 后 invoke 仍返回聚合 AIMessage（LangChain 内部聚合 stream），但底层以流式发出，
      // 使 graph.stream(streamMode:"messages") 能逐 token 透出最终回答（对齐 anthropic 分支）。
      streaming: true,
    }) as unknown as CreateDeepAgentParams["model"];
  } else {
    const thinking = resolveAnthropicThinking(
      config.model.name,
      config.model.settings.thinkingBudgetTokens
    );
    instance = new ChatAnthropic({
      model: config.model.name,
      apiKey,
      anthropicApiUrl: normalizeAnthropicBaseUrl(config.model.baseUrl),
      // Anthropic extended thinking 与自定义 temperature 互斥；开启后交由 SDK 省略采样参数。
      temperature: undefined,
      maxTokens: config.model.settings.maxTokens,
      thinking,
      // Anthropic SDK 对非流式请求有 10 分钟硬上限（long-running/慢模型如经 anthropic 协议代理的 glm 会触发
      // "Streaming is required for operations that may take longer than 10 minutes"）。
      // 开启 streaming 后 invoke 仍返回聚合 AIMessage（LangChain 内部聚合 stream），但底层以流式发出，绕过该限制。
      streaming: true,
    });
  }

  cachedModel = { key: cacheKey, instance };
  return instance;
}
