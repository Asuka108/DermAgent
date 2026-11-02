/**
 * Checkpoint 消息修复 —— cancel 补 ToolMessage + run 入口清洗 + RemoveMessage 写回。
 *
 * MessagesAnnotation reducer 默认追加；持久化修复须 compaction 同款 RemoveMessage 替换。
 */

import { RemoveMessage, ToolMessage, type BaseMessage } from "@langchain/core/messages";
import type { RunnableConfig } from "@langchain/core/runnables";
import { logger } from "../../runtime/logger.js";
import { coerceMessagesToTextContent, resolveCoerceMode, shouldCoerceToTextOnly } from "./coerce-text-content.js";
import {
  findOrphanedToolCallIds,
  messageId,
  sanitizeToolCalls,
} from "./sanitize-tool-calls.js";

const log = logger.child("checkpoint-repair");

export interface CheckpointRepairOptions {
  /** ACP cancel 时 in-flight 的 tool_call_id；为这些 id 补 synthetic ToolMessage。 */
  cancelledToolCallIds?: Iterable<string>;
  cancelReason?: string;
  /**
   * 是否将多模态 content（image_url 等）压成纯文本后写回。
   * 默认 true；传 false 或开启 supportsVision 时跳过（见 shouldCoerceToTextOnly）。
   */
  coerceTextContent?: boolean;
  /** 可选：用于判定 supportsVision / provider coerce 强度。 */
  appConfig?: { model?: { provider?: string; settings?: Record<string, unknown> } };
}

/** 为指定孤立 tool_call 追加取消 ToolMessage，再 sanitize 剩余孤立项。 */
export function completeOrphanedToolCalls(
  messages: BaseMessage[],
  toolCallIds: Iterable<string>,
  content = "已取消（客户端 session/cancel）"
): BaseMessage[] {
  const targetIds = new Set(toolCallIds);
  const orphaned = findOrphanedToolCallIds(messages);
  const toComplete = [...orphaned].filter((id) => targetIds.has(id));
  if (!toComplete.length) return messages;
  const additions = toComplete.map(
    (id) => new ToolMessage({ content, tool_call_id: id })
  );
  return sanitizeToolCalls([...messages, ...additions]);
}

/** 内存侧修复：cancel 补全 + 全量 sanitize + 多模态 content 压文本。 */
export function repairCheckpointMessages(
  prior: BaseMessage[],
  opts: CheckpointRepairOptions = {}
): BaseMessage[] {
  const cancelled = opts.cancelledToolCallIds ? [...opts.cancelledToolCallIds] : [];
  let next =
    cancelled.length > 0
      ? completeOrphanedToolCalls(prior, cancelled, opts.cancelReason)
      : prior;
  next = sanitizeToolCalls(next);
  // 默认剥图：修复已被 image_url 毒死的历史会话（智谱等 text-only 端点）
  const doCoerce =
    opts.coerceTextContent !== false && shouldCoerceToTextOnly(opts.appConfig);
  if (doCoerce) {
    next = coerceMessagesToTextContent(next, {
      mode: resolveCoerceMode(opts.appConfig),
    });
  }
  return next;
}

/**
 * 转成 MessagesAnnotation 可写的替换更新（RemoveMessage + 全量 repaired）。
 * 消息无 id 时无法安全写回，返回 []（仍依赖 think 入口清洗）。
 */
export function checkpointRepairUpdate(
  prior: BaseMessage[],
  repaired: BaseMessage[]
): BaseMessage[] {
  if (repaired === prior) return [];
  const removals = prior
    .filter((m) => messageId(m))
    .map((m) => new RemoveMessage({ id: messageId(m)! }));
  if (!removals.length) return [];
  return [...removals, ...repaired];
}

export interface CheckpointRepairableGraph {
  getState(config: RunnableConfig): Promise<{ values: unknown }>;
  updateState(config: RunnableConfig, values: Record<string, unknown>): Promise<unknown>;
}

/** 读 checkpoint → 修复 messages → 写回。修复成功返回 true。 */
export async function applyCheckpointMessageRepair(
  graph: CheckpointRepairableGraph,
  config: RunnableConfig,
  opts: CheckpointRepairOptions = {}
): Promise<boolean> {
  const values = (await graph.getState(config)).values as { messages?: BaseMessage[] } | undefined;
  const prior = values?.messages ?? [];
  if (!prior.length) return false;

  const repaired = repairCheckpointMessages(prior, opts);
  const update = checkpointRepairUpdate(prior, repaired);
  if (!update.length) return false;

  await graph.updateState(config, { messages: update });
  log.info("checkpoint messages repaired", {
    threadId: config.configurable?.thread_id,
    priorCount: prior.length,
    repairedCount: repaired.length,
    cancelledToolCalls: opts.cancelledToolCallIds ? [...opts.cancelledToolCallIds] : [],
  });
  return true;
}
