/**
 * Checkpoint / 消息流中的孤立 tool_calls / tool_use 清洗。
 *
 * LangGraph checkpoint 反序列化后消息常为 plain object（`type: "ai"`），
 * 不能用 instanceof AIMessage。think 调 LLM 前与 checkpoint 写回前共用本模块。
 *
 * 线上坑（DeepSeek Anthropic 协议）：模型可能把 `tool_use` 只写在 `content[]` 里，
 * 而 `AIMessage.tool_calls` 为空 → toolsCondition 走 respond/END，工具未执行，
 * 下一轮 LLM 报 `tool_use without tool_result` / INVALID_TOOL_RESULTS。
 * 本模块同时扫描顶层 tool_calls、additional_kwargs、以及 content 里的 tool_use 块。
 */

import { AIMessage, type BaseMessage } from "@langchain/core/messages";
import { logger } from "../../runtime/logger.js";

const log = logger.child("checkpoint-messages");

/** content[] 里解析出的 Anthropic 风格 tool_use 块。 */
export interface ContentToolUse {
  id: string;
  name: string;
  args: Record<string, unknown>;
}

/** 识别消息类型：类实例用 _getType()，反序列化对象用 type 字段。 */
export function msgType(msg: BaseMessage): string {
  const raw = msg as unknown as Record<string, unknown>;
  if (typeof raw._getType === "function") {
    return (raw._getType as () => string)();
  }
  return typeof raw.type === "string" ? raw.type : "";
}

export function msgToolCalls(
  msg: BaseMessage
): Array<{ id?: string; name: string; args: Record<string, unknown> }> {
  const raw = msg as unknown as Record<string, unknown>;
  const top = raw.tool_calls;
  if (Array.isArray(top) && top.length > 0) {
    return top as Array<{ id?: string; name: string; args: Record<string, unknown> }>;
  }
  // checkpoint 反序列化后可能只剩 additional_kwargs.tool_calls
  const kwargs = raw.additional_kwargs as Record<string, unknown> | undefined;
  const nested = kwargs?.tool_calls;
  return Array.isArray(nested)
    ? (nested as Array<{ id?: string; name: string; args: Record<string, unknown> }>)
    : [];
}

/**
 * 从 AIMessage.content 数组提取 Anthropic `tool_use` 块。
 * 仅当 content 为数组且块带 type/id/name 时生效；字符串 content 返回 []。
 */
export function msgContentToolUses(msg: BaseMessage): ContentToolUse[] {
  const raw = msg as unknown as Record<string, unknown>;
  const content = raw.content;
  if (!Array.isArray(content)) return [];
  const out: ContentToolUse[] = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    const b = block as Record<string, unknown>;
    if (b.type !== "tool_use") continue;
    const id = typeof b.id === "string" ? b.id : "";
    if (!id) continue;
    const name = typeof b.name === "string" && b.name.length > 0 ? b.name : "unknown";
    const input = b.input;
    const args =
      input && typeof input === "object" && !Array.isArray(input)
        ? (input as Record<string, unknown>)
        : {};
    out.push({ id, name, args });
  }
  return out;
}

/** 合并顶层 tool_calls 与 content tool_use 的全部 call id。 */
export function collectAiToolCallIds(msg: BaseMessage): Set<string> {
  const ids = new Set<string>();
  for (const c of msgToolCalls(msg)) {
    if (c.id) ids.add(c.id);
  }
  for (const u of msgContentToolUses(msg)) {
    ids.add(u.id);
  }
  return ids;
}

export function msgToolCallId(msg: BaseMessage): string {
  const raw = (msg as unknown as Record<string, unknown>).tool_call_id;
  return typeof raw === "string" ? raw : "";
}

export function messageId(msg: BaseMessage): string | undefined {
  const raw = (msg as unknown as Record<string, unknown>).id;
  return typeof raw === "string" ? raw : undefined;
}

/**
 * 收集缺少「紧随其后」ToolMessage 的 tool_call / tool_use id。
 *
 * Anthropic 要求：assistant 的每个 tool_use 后必须**立刻**跟对应 tool_result。
 * 因此这里用邻接扫描，而不是「全历史某处存在同 id 即可」——后者仍会 400。
 */
export function findOrphanedToolCallIds(messages: BaseMessage[]): Set<string> {
  const orphaned = new Set<string>();
  for (let i = 0; i < messages.length; i++) {
    const msg = messages[i]!;
    if (msgType(msg) !== "ai") continue;
    const callIds = collectAiToolCallIds(msg);
    if (callIds.size === 0) continue;

    // 仅统计紧跟在本 AIMessage 之后的连续 ToolMessage
    const fulfilled = new Set<string>();
    for (let j = i + 1; j < messages.length; j++) {
      const next = messages[j]!;
      if (msgType(next) !== "tool") break;
      const tid = msgToolCallId(next);
      if (tid) fulfilled.add(tid);
    }
    for (const id of callIds) {
      if (!fulfilled.has(id)) orphaned.add(id);
    }
  }
  return orphaned;
}

/**
 * 识别 LLM 返回的 INVALID_TOOL_RESULTS / tool_use 缺 tool_result 类 400。
 * 供 think 自愈分支与 content.type 非法分支并列使用。
 */
export function isInvalidToolResultsError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err);
  if (/INVALID_TOOL_RESULTS/i.test(msg)) return true;
  if (/tool_use[\s\S]*tool_result/i.test(msg) && /immediately after|without/i.test(msg)) {
    return true;
  }
  if (/tool_calls?.*must have.*[Tt]oolMessage/i.test(msg)) return true;
  return false;
}

/**
 * 把 content[] 里的 tool_use 同步进 AIMessage.tool_calls，
 * 让 toolsCondition / ToolNode 能看见，避免「有 tool_use 却当纯文本 END」。
 * 已齐全时返回原引用。
 */
export function normalizeAiMessageToolCalls(msg: AIMessage): AIMessage {
  const contentUses = msgContentToolUses(msg);
  if (contentUses.length === 0) return msg;

  const existing = msgToolCalls(msg);
  const existingIds = new Set(existing.map((c) => c.id).filter(Boolean) as string[]);
  const missing = contentUses.filter((u) => !existingIds.has(u.id));
  if (missing.length === 0) return msg;

  const raw = msg as unknown as Record<string, unknown>;
  const merged = [
    ...existing,
    ...missing.map((u) => ({
      id: u.id,
      name: u.name,
      args: u.args,
      type: "tool_call" as const,
    })),
  ];

  log.info("content tool_use 已同步到 tool_calls", {
    added: missing.map((m) => m.id),
    total: merged.length,
  });

  return new AIMessage({
    content: (raw.content as AIMessage["content"]) ?? "",
    additional_kwargs: raw.additional_kwargs as AIMessage["additional_kwargs"],
    tool_calls: merged as AIMessage["tool_calls"],
    id: typeof raw.id === "string" ? raw.id : undefined,
    name: typeof raw.name === "string" ? raw.name : undefined,
    response_metadata: raw.response_metadata as AIMessage["response_metadata"],
  });
}

/**
 * 从 AIMessage 剥离孤立 tool_calls，并剥掉 content[] 中对应的 tool_use 块。
 * 无变更时返回原数组引用（便于调用方跳过写回）。
 *
 * 同时清除 additional_kwargs.tool_calls：OpenAI converter 会在顶层 tool_calls
 * 为空时回退序列化 kwargs，否则仍会触发 INVALID_TOOL_RESULTS。
 */
export function sanitizeToolCalls(messages: BaseMessage[]): BaseMessage[] {
  const orphaned = findOrphanedToolCallIds(messages);
  if (orphaned.size === 0) return messages;
  log.warn("发现孤立 tool_calls/tool_use，已移除", {
    orphanedCount: orphaned.size,
    ids: [...orphaned],
  });
  return messages.map((msg) => {
    if (msgType(msg) !== "ai") return msg;
    return stripOrphanedFromAiMessage(msg, orphaned) ?? msg;
  });
}

/**
 * 对单条 AI 消息剥离 orphaned ids。
 * 无变更返回 null（调用方保留原引用）。
 */
function stripOrphanedFromAiMessage(
  msg: BaseMessage,
  orphaned: Set<string>
): AIMessage | null {
  const raw = msg as unknown as Record<string, unknown>;
  const calls = msgToolCalls(msg);
  const valid = calls.filter((c) => !(c.id && orphaned.has(c.id)));
  const contentUses = msgContentToolUses(msg);
  const orphanInContent = contentUses.some((u) => orphaned.has(u.id));
  const orphanInCalls = valid.length !== calls.length;

  // 仅 additional_kwargs 有孤立 call、顶层已空时也要清 kwargs
  const kwargs = raw.additional_kwargs as Record<string, unknown> | undefined;
  const kwargsCalls =
    kwargs && Array.isArray(kwargs.tool_calls) ? kwargs.tool_calls : null;
  const needsKwargsClear =
    kwargsCalls != null &&
    kwargsCalls.some((c) => {
      if (!c || typeof c !== "object") return false;
      const id = (c as { id?: string }).id;
      return typeof id === "string" && orphaned.has(id);
    });

  if (!orphanInContent && !orphanInCalls && !needsKwargsClear) {
    return null;
  }

  // 保留非 tool_use 的 content 块（text / thinking 等）；勿把数组强转成 ""
  let content: AIMessage["content"] = (raw.content as AIMessage["content"]) ?? "";
  if (Array.isArray(raw.content) && orphanInContent) {
    const filtered = (raw.content as unknown[]).filter((block) => {
      if (!block || typeof block !== "object") return true;
      const b = block as Record<string, unknown>;
      if (b.type !== "tool_use") return true;
      const id = typeof b.id === "string" ? b.id : "";
      return !id || !orphaned.has(id);
    });
    content = filtered.length > 0 ? (filtered as AIMessage["content"]) : "";
  }

  const additionalKwargs = kwargs ? { ...kwargs } : undefined;
  if (additionalKwargs) {
    delete additionalKwargs.tool_calls;
  }

  return new AIMessage({
    content,
    additional_kwargs:
      additionalKwargs && Object.keys(additionalKwargs).length > 0
        ? additionalKwargs
        : undefined,
    // 显式 []：避免 undefined 时 AIMessage 从 additional_kwargs 再解析
    tool_calls: valid as AIMessage["tool_calls"],
    id: typeof raw.id === "string" ? raw.id : undefined,
    name: typeof raw.name === "string" ? raw.name : undefined,
  });
}
