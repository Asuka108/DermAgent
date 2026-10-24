/**
 * 上下文压缩 —— 超阈值时裁剪 + LLM 摘要旧消息。
 *
 * 框架优先：裁剪用 core `trimMessages`；摘要用标准 LLM invoke（产出一条 SystemMessage）。
 * 消费 config.compaction（contextWindow / triggerThreshold / keepRecentTokens）。
 *
 * 三个分支：
 *  - 未超阈值 → 原样返回
 *  - 超阈值 + 有凭证 → 保留最近 keepRecentTokens + LLM 摘要旧部分成 SystemMessage
 *  - 超阈值 + 无凭证 → 仅 trimMessages 裁剪（不摘要，避免调模型）
 *
 * 典型接入：StatefulFlow.run 入口 load history → compactHistory → 作为初始 messages 传图。
 * 默认图 conversational 多轮时历史增长，可在 createStatefulFlow 路径自动触发压缩。
 */

import {
  trimMessages,
  SystemMessage,
  HumanMessage,
  RemoveMessage,
  type BaseMessage,
} from "@langchain/core/messages";
import type { RunnableConfig } from "@langchain/core/runnables";
import { resolveModel, logger, type AppConfig } from "../runtime/index.js";
import { invokeWithResilience, resolveLlmResilience } from "../runtime/services/llm-resilience.js";

const log = logger.child("compaction");

/** 粗估 token 数（char / 4）。用于触发判定与裁剪。 */
export function estimateTokens(messages: BaseMessage[], charPerToken = 4): number {
  const chars = messages.reduce((n, m) => {
    const c = m.content;
    return n + (typeof c === "string" ? c.length : JSON.stringify(c).length);
  }, 0);
  return Math.ceil(chars / charPerToken);
}

export function hasModelCredentials(config?: AppConfig): boolean {
  const vars = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"];
  if (config?.model.apiKeyEnv) vars.push(config.model.apiKeyEnv);
  if (config?.model.authTokenEnv) vars.push(config.model.authTokenEnv);
  return vars.some((v) => Boolean(process.env[v]));
}

function messagesToText(messages: BaseMessage[]): string {
  return messages
    .map((m) => {
      const t = typeof m.content === "string" ? m.content : JSON.stringify(m.content);
      return `${m._getType()}: ${t}`;
    })
    .join("\n");
}

/**
 * 把「压缩后的消息列表」转成给 MessagesAnnotation 的**替换更新**（durable stateful flow 历史落地的关键）。
 *
 * MessagesAnnotation 的 reducer 默认是「追加」；要真正用摘要替换旧历史，需先 RemoveMessage 删掉
 * 旧消息（按 id），再写回压缩结果（摘要 + 保留的近期消息）。把这步抽成纯函数便于单测，
 * 调用方拿到后 `graph.updateState(config, { messages })` 即可。
 *
 * compacted 未变短（没触发压缩 / 无 id 可删）时返回 []，调用方据此跳过 updateState。
 */
export function compactionUpdate(
  prior: BaseMessage[],
  compacted: BaseMessage[]
): BaseMessage[] {
  // 同引用 = compactHistory 未触发（未超阈值）；更长 = 防御性兜底
  if (compacted === prior || compacted.length > prior.length) return [];
  const removals = prior
    .filter((m) => m.id)
    .map((m) => new RemoveMessage({ id: m.id! }));
  if (removals.length === 0) return [];
  return [...removals, ...compacted];
}

/** 一张「可压缩」的图：能读状态、能写回状态（LangGraph `.compile()` 产物天然满足）。 */
export interface CompactableGraph {
  getState(config: RunnableConfig): Promise<{ values: unknown }>;
  updateState(config: RunnableConfig, values: Record<string, unknown>): Promise<unknown>;
}

/**
 * 对一张带 checkpointer 的图，就地压缩其 `state.messages`（超阈值时摘要+RemoveMessage 替换）。
 *
 * 把「读 checkpoint 历史 → compactHistory → compactionUpdate → updateState」收成一处，
 * 供 createStatefulFlow（自动）与自定义有状态 flow 共用，避免各处重写。
 * 状态无 `messages`（如 topic/plan 型 flow）或未超阈值时为 no-op，返回 false。
 */
export async function applyCompaction(
  graph: CompactableGraph,
  config: RunnableConfig,
  appConfig: AppConfig
): Promise<boolean> {
  const values = (await graph.getState(config)).values as { messages?: BaseMessage[] } | undefined;
  const prior = values?.messages ?? [];
  if (!prior.length) return false;
  const compacted = await compactHistory(prior, appConfig);
  const update = compactionUpdate(prior, compacted);
  if (!update.length) return false;
  await graph.updateState(config, { messages: update });
  return true;
}

export async function compactHistory(
  messages: BaseMessage[],
  config: AppConfig
): Promise<BaseMessage[]> {
  const cc = config.compaction;
  if (!cc?.enabled || messages.length < 2) return messages;

  const total = estimateTokens(messages);
  const threshold = Math.floor(cc.contextWindow * cc.triggerThreshold);
  if (total <= threshold) return messages;

  log.info("compacting history", { beforeTokens: total, threshold });

  // 有凭证时优先用 model 作 tokenCounter（精确裁剪）；无凭证或 model 不支持 token counting 回退 estimateTokens。
  const rawModel = hasModelCredentials(config) ? resolveModel(config) : null;
  const countModel = rawModel && typeof rawModel !== "string" ? rawModel : null;
  let recent: BaseMessage[];
  try {
    recent = (await trimMessages(messages, {
      maxTokens: cc.keepRecentTokens,
      strategy: "last",
      tokenCounter: countModel ?? ((msgs: BaseMessage[]) => estimateTokens(msgs)),
      includeSystem: true,
    })) as BaseMessage[];
  } catch {
    log.warn("model tokenCounter 不可用 → 回退 estimateTokens 裁剪");
    recent = (await trimMessages(messages, {
      maxTokens: cc.keepRecentTokens,
      strategy: "last",
      tokenCounter: (msgs: BaseMessage[]) => estimateTokens(msgs),
      includeSystem: true,
    })) as BaseMessage[];
  }

  const oldCount = messages.length - recent.length;
  if (oldCount <= 0) return recent;
  // oldMessages 排除 system（trimMessages includeSystem:true 已把 system 留在 recent 头，
  // 否则摘要它会在返回值里产出第二条 SystemMessage）
  const oldMessages = messages.slice(0, oldCount).filter((m) => m._getType() !== "system");

  if (!hasModelCredentials(config)) {
    log.warn("超阈值但无凭证 → 仅裁剪不摘要", { oldCount });
    return recent;
  }

  try {
    const raw = resolveModel(config);
    const model = raw && typeof raw !== "string" ? raw : null;
    if (!model) return recent;
    const { longTimeoutMs } = resolveLlmResilience(config);
    const res = await invokeWithResilience(
      model as unknown as { invoke: (m: BaseMessage[]) => Promise<{ content: unknown }> },
      [
        new SystemMessage("把以下对话历史压缩成简洁摘要，保留关键事实、决定与未决问题。只输出摘要正文。"),
        new HumanMessage(messagesToText(oldMessages)),
      ],
      {
        timeoutMs: longTimeoutMs,
        label: "compaction 摘要",
        retryLabel: "compaction LLM",
        config,
      }
    );
    const summary = typeof res.content === "string" ? res.content : JSON.stringify(res.content);
    log.info("history summarized", { afterTokens: estimateTokens(recent) + estimateTokens([new SystemMessage(summary)]) });
    return [new SystemMessage(`[会话历史摘要]\n${summary}`), ...recent];
  } catch (err) {
    log.warn("摘要失败 → 回退裁剪", { error: String(err) });
    return recent;
  }
}
