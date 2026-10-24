/**
 * think 节点 —— bindTools 的模型决定调工具（AIMessage.tool_calls）还是直接回答。
 *
 * 本节点**自管 model 解析 + bindTools**：工厂在创建时解析一次模型并绑定工具，返回的节点函数
 * 闭包持有 boundModel。
 * - **无凭证**：降级回显输入（保证 CLI 冒烟 / 单测可跑，见 tests/flow.test.ts）。
 * - **有凭证但解析或调用失败**：抛错，由 ACP/CLI surface 向用户暴露真实失败（不伪装成功）。
 */

import { AIMessage, SystemMessage, type BaseMessage } from "@langchain/core/messages";
import type { StructuredTool } from "@langchain/core/tools";
import {
  coerceMessagesToTextContent,
  isIllegalContentTypeError,
  resolveCoerceMode,
  shouldCoerceToTextOnly,
} from "../../libs/messages/coerce-text-content.js";
import { checkpointRepairUpdate } from "../../libs/messages/repair-checkpoint.js";
import {
  isInvalidToolResultsError,
  normalizeAiMessageToolCalls,
  sanitizeToolCalls,
} from "../../libs/messages/sanitize-tool-calls.js";
import { resolveModel, logger, type AppConfig } from "../../runtime/index.js";
import { hasModelCredentials } from "../../libs/compaction.js";
import {
  extractReasoningTextFromMessage,
  extractText,
} from "../../libs/nodes/llm.js";
import {
  invokeWithResilience,
  resolveLlmResilience,
} from "../../runtime/services/llm-resilience.js";
import type { FlowState } from "../state.js";

const log = logger.child("flow-think");

type BoundModel = {
  invoke: (m: BaseMessage[], options?: { signal?: AbortSignal }) => Promise<AIMessage>;
};

/** Model-like interface that has bindTools (BaseChatModel, ConfigurableModel, etc.) */
interface ModelWithTools {
  bindTools(tools: StructuredTool[]): BoundModel;
}

/**
 * 前置注入 SystemMessage（首条已是 system 则跳过）。
 *
 * Anthropic 协议：给 system 块打 ephemeral 缓存断点 → 位置式缓存覆盖「tools + system」整块
 * （最大的稳定前缀，含全部工具 schema），多轮第二轮起命中、显著降 TTFT（对标 nuwaxcode
 * packages/llm 的 caching-on-by-default）。OpenAI 兼容端点是隐式服务端缓存、无需标记，按原字符串注入。
 */
function withSystemPrompt(
  messages: BaseMessage[],
  systemPrompt: string,
  provider?: string
): BaseMessage[] {
  if (!systemPrompt) return messages;
  if (messages.length > 0 && messages[0]?._getType?.() === "system") return messages;
  return [buildSystemMessage(systemPrompt, provider), ...messages];
}

/** provider 感知地造 SystemMessage：anthropic 带 cache_control 缓存断点，其余裸字符串。 */
function buildSystemMessage(systemPrompt: string, provider?: string): SystemMessage {
  if (provider === "anthropic") {
    // cache_control 不在 core 的 system content 类型里（由 @langchain/anthropic 在 wire 层读取），故整体 cast。
    return new SystemMessage({
      content: [{ type: "text", text: systemPrompt, cache_control: { type: "ephemeral" } }],
    } as unknown as ConstructorParameters<typeof SystemMessage>[0]);
  }
  return new SystemMessage(systemPrompt);
}

export interface ThinkNodeDeps {
  /** AppConfig（resolveModel / 韧性参数用）；反射拓扑或无凭证时可空。 */
  config?: AppConfig;
  /** 绑给模型的工具集（FlowRuntime.allTools）。 */
  allTools: StructuredTool[];
  /** 系统提示词（注入 SystemMessage）。 */
  systemPrompt?: string;
}

/** 创建 think 节点：解析一次模型 + bindTools，返回的节点函数闭包持有 boundModel。 */
export function createThinkNode(deps: ThinkNodeDeps) {
  const { config, allTools, systemPrompt } = deps;
  const hasCreds = hasModelCredentials(config);

  let boundModel: BoundModel | null = null;
  /** resolveModel/bindTools 失败时保留原因，有凭证时节点内抛出让 surface 处理 */
  let resolveError: unknown = null;
  if (config && hasCreds) {
    try {
      const raw = resolveModel(config);
      if (raw && typeof raw !== "string") {
        // raw is a model instance (BaseChatModel, ConfigurableModel, etc.) that has bindTools
        const model = raw as unknown as ModelWithTools;
        boundModel = model.bindTools(allTools);
      }
    } catch (err) {
      resolveError = err;
      log.warn("resolveModel/bindTools failed", { error: String(err) });
    }
  }

  return async (
    state: FlowState,
    runtimeConfig?: { signal?: AbortSignal }
  ): Promise<Partial<FlowState>> => {
    if (!hasCreds) {
      // 无凭证 fallback：直接回显输入为回答（不调工具，保证图始终可跑）
      return {
        messages: [new AIMessage({ content: `(无模型凭证，回显输入)\n${state.input}` })],
        steps: ["think#fallback: no model"],
      };
    }
    if (!boundModel) {
      const detail = resolveError ? String(resolveError) : "model instance unavailable";
      throw new Error(`think: 模型解析失败（已配置凭证）: ${detail}`);
    }
    const { shortTimeoutMs } = resolveLlmResilience(config);
    // 孤立 tool_calls 清洗 +（默认）多模态 / 非字符串 content 压纯文本
    const sanitized = sanitizeToolCalls(state.messages);
    let forModel = sanitized;
    /** sanitize / coerce 相对 state.messages 有变更时需写回 checkpoint */
    let historyDirty = sanitized !== state.messages;
    if (shouldCoerceToTextOnly(config)) {
      const coerced = coerceMessagesToTextContent(sanitized, {
        mode: resolveCoerceMode(config),
      });
      if (coerced !== sanitized) {
        forModel = coerced;
        historyDirty = true;
      }
    }
    const promptForModel = withSystemPrompt(
      forModel,
      systemPrompt ?? "",
      config?.model.provider
    );
    log.debug("think 注入 systemPrompt", {
      systemPromptChars: systemPrompt?.trim().length ?? 0,
      messageCount: promptForModel.length,
      firstMessageType: promptForModel[0]?._getType?.() ?? "unknown",
    });
    const invokeOpts = {
      timeoutMs: shortTimeoutMs,
      label: "think 调模型",
      retryLabel: "think LLM",
      config,
      signal: runtimeConfig?.signal,
    };
    // 有凭证时调用失败直接抛错，避免回显输入伪装成功（ACP stopReason=end_turn 误导用户）
    let ai: AIMessage;
    try {
      ai = (await invokeWithResilience(boundModel, promptForModel, invokeOpts)) as AIMessage;
    } catch (err) {
      // 自愈 A：content.type 非法 → aggressive 压扁后单次重试，并写回历史。
      if (isIllegalContentTypeError(err)) {
        log.warn("think LLM content.type 非法，强制 aggressive text coerce 后重试一次", {
          error: String(err),
          firstPassCoerced: shouldCoerceToTextOnly(config),
        });
        forModel = coerceMessagesToTextContent(sanitized, {
          mode: "all-non-string",
        });
        historyDirty = forModel !== sanitized || historyDirty;
        const retryPrompt = withSystemPrompt(
          forModel,
          systemPrompt ?? "",
          config?.model.provider
        );
        ai = (await invokeWithResilience(boundModel, retryPrompt, {
          ...invokeOpts,
          attempts: 1,
        })) as AIMessage;
      } else if (isInvalidToolResultsError(err)) {
        // 自愈 B：孤立 tool_use / INVALID_TOOL_RESULTS → 再 sanitize 一次后重试。
        // 覆盖「content[] 残留 tool_use 而 tool_calls 为空」等入口 sanitize 曾漏掉的格式。
        log.warn("think LLM INVALID_TOOL_RESULTS，强制 sanitize tool_use 后重试一次", {
          error: String(err),
        });
        const repaired = sanitizeToolCalls(forModel);
        forModel = repaired;
        historyDirty = repaired !== state.messages || historyDirty;
        const retryPrompt = withSystemPrompt(
          forModel,
          systemPrompt ?? "",
          config?.model.provider
        );
        ai = (await invokeWithResilience(boundModel, retryPrompt, {
          ...invokeOpts,
          attempts: 1,
        })) as AIMessage;
      } else {
        throw err;
      }
    }

    // 出口规范化：content 里的 tool_use → tool_calls，避免 toolsCondition 误走 respond/END
    const normalized = normalizeAiMessageToolCalls(ai);
    const toolCallsSynced = normalized !== ai;
    ai = normalized;

    // 部分 OpenAI 兼容 reasoning 模型偶发把用户可见回答写进 reasoning_content，
    // 同时 content=""、无 tool_calls。若不提升，respond / 流式路径会输出空回复。
    let reasoningContentPromoted = false;
    const hasToolCalls = (ai.tool_calls?.length ?? 0) > 0;
    const reasoningFallback = extractReasoningTextFromMessage(ai);
    if (!extractText(ai.content).trim() && !hasToolCalls && reasoningFallback.trim()) {
      ai = new AIMessage({
        content: reasoningFallback,
        tool_calls: ai.tool_calls,
        additional_kwargs: ai.additional_kwargs,
        response_metadata: ai.response_metadata,
        id: ai.id,
        usage_metadata: ai.usage_metadata,
        name: ai.name,
      });
      reasoningContentPromoted = true;
      log.info("think promoted reasoning_content to content", {
        reasoningChars: reasoningFallback.length,
      });
    }

    // 清洗后的历史写回 checkpoint，避免同轮后续 think / 下轮 run 再踩毒 content / 孤立 tool_calls
    const outMessages: BaseMessage[] = [];
    if (historyDirty) {
      const writeback = checkpointRepairUpdate(state.messages, forModel);
      if (writeback.length > 0) {
        outMessages.push(...writeback);
        log.info("think 已将修复后的历史写回 state", {
          priorCount: state.messages.length,
          repairedCount: forModel.length,
          sanitized: sanitized !== state.messages,
        });
      } else {
        log.warn("think 修复后无法写回（消息缺 id），仅依赖当次 LLM 入参", {
          priorCount: state.messages.length,
        });
      }
    }
    outMessages.push(ai);

    return {
      messages: outMessages,
      steps: [
        `think: ${(ai.tool_calls ?? []).length} tool_calls`,
        ...(historyDirty ? ["think#history-writeback"] : []),
        ...(toolCallsSynced ? ["think#tool_use→tool_calls"] : []),
        ...(reasoningContentPromoted ? ["think#reasoning_content→content"] : []),
      ],
    };
  };
}
