/**
 * createToolExecNode —— 执行 last AIMessage 的 tool_calls + 三态 onToolCall 透出
 * （in_progress → completed/failed）。默认图 tools 节点的泛化版（包 prebuilt ToolNode）。
 *
 * 也提供 runTool：执行单个工具 fn 并三态透出（供自定义工具节点 / MCP 检索节点用，
 * 如 topologies 的 research/search 节点）。
 */

import { randomUUID } from "node:crypto";
import { ToolNode } from "@langchain/langgraph/prebuilt";
import type { LangGraphRunnableConfig } from "@langchain/langgraph";
import { AIMessage, ToolMessage, type BaseMessage } from "@langchain/core/messages";
import type { StructuredTool } from "@langchain/core/tools";
import type { FlowCallbacks, ToolCallEvent } from "../../core/flow-types.js";
import type { AppConfig } from "../../runtime/index.js";
import {
  coerceContentToText,
  messageContentNeedsTextCoerce,
  rebuildMessageWithTextContent,
  resolveCoerceMode,
  shouldCoerceToTextOnly,
} from "../messages/coerce-text-content.js";
import { normalizeToolResult } from "./tool-result-normalize.js";

/**
 * 执行一个工具 fn，并把过程经 onToolCall 三态透出。
 * MCP 有时返回 "Unknown tool: xxx" 但不抛错 —— 视为失败。
 * @returns { result, ok }
 */
export async function runTool(
  toolName: string,
  args: Record<string, unknown>,
  fn: () => string | Promise<string>,
  onToolCall?: (e: ToolCallEvent) => void | Promise<void>
): Promise<{ result: string; ok: boolean }> {
  const toolCallId = randomUUID();
  if (onToolCall) {
    await onToolCall({ toolCallId, toolName, args, status: "in_progress" });
  }
  try {
    const result = await fn();
    const unknownTool =
      typeof result === "string" && /^Unknown tool:/i.test(result.trim());
    if (unknownTool) {
      if (onToolCall) {
        await onToolCall({ toolCallId, toolName, args, status: "failed", error: result });
      }
      return { result, ok: false };
    }
    if (onToolCall) {
      await onToolCall({ toolCallId, toolName, args, status: "completed", result });
    }
    return { result, ok: true };
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (onToolCall) {
      await onToolCall({ toolCallId, toolName, args, status: "failed", error: message });
    }
    return { result: message, ok: false };
  }
}

export interface ToolExecNodeOptions<S extends { messages: BaseMessage[] }> {
  /** 可执行工具集（与 think 绑定同一组）。 */
  tools: StructuredTool[];
  /** AppConfig，用于判定模型是否支持 vision content blocks。 */
  config?: AppConfig;
  /** surface 回调（透出工具调用事件）。 */
  callbacks?: FlowCallbacks;
  /** 把 ToolMessage[] 映射成 state 更新（默认 { messages }）。 */
  write?: (toolMsgs: ToolMessage[], state: S) => Partial<S>;
}

/**
 * 创建工具执行节点：实例化一次 ToolNode，执行前后透出 onToolCall 三态。
 * 默认返回 { messages }；默认图传 write 附带 steps。
 */
export function createToolExecNode<S extends { messages: BaseMessage[] }>(
  opts: ToolExecNodeOptions<S>
) {
  const { tools, config: appConfig, callbacks, write } = opts;
  const toolNode = new ToolNode(tools);

  return async (state: S, config?: LangGraphRunnableConfig): Promise<Partial<S>> => {
    const onToolCall =
      (config?.configurable?.onToolCall as FlowCallbacks["onToolCall"]) ??
      callbacks?.onToolCall;
    const onPermissionRequest =
      (config?.configurable?.onPermissionRequest as FlowCallbacks["onPermissionRequest"]) ??
      callbacks?.onPermissionRequest;
    const last = state.messages[state.messages.length - 1] as AIMessage;
    const calls = (last?.tool_calls ?? []) as Array<{
      id?: string;
      name: string;
      args: Record<string, unknown>;
    }>;
    const argsById = new Map<string, Record<string, unknown>>();
    for (const c of calls) {
      if (onToolCall && c.id) {
        argsById.set(c.id, c.args);
        await onToolCall({
          toolCallId: c.id,
          toolName: c.name,
          args: c.args,
          status: "in_progress",
        });
      }
    }
    // 审批门控（A2）：对每个 tool_call 征询许可；被拒/取消的 call 预合成 error ToolMessage
    // 注入 ToolNode 输入 —— ToolNode 会跳过输入里已存在 ToolMessage 的 tool_call_id
    // (@langchain/langgraph prebuilt tool_node 的 toolMessageIds 去重)，从而不执行该 call。
    // 是否需要审批（名单 / 模式）全在 onPermissionRequest 实现内判定（对齐 Claude SDK canUseTool）。
    const rejectedToolMsgs: ToolMessage[] = [];
    const deniedIds = new Set<string>(); // reject：节点补发 failed terminal（客户端 in_progress→failed，不卡转圈）
    const cancelledIds = new Set<string>(); // cancelled：跳过 terminal，交 onPrompt failInflightToolsOnCancel 收尾（避免双发）
    if (onPermissionRequest) {
      for (const c of calls) {
        if (!c.id) continue;
        const decision = await onPermissionRequest({
          toolCallId: c.id,
          toolName: c.name,
          args: c.args,
        });
        if (decision === "reject" || decision === "cancelled") {
          const cancelled = decision === "cancelled";
          rejectedToolMsgs.push(
            new ToolMessage({
              tool_call_id: c.id,
              name: c.name,
              content: cancelled
                ? `Permission request cancelled: ${c.name}`
                : `Permission denied: ${c.name}`,
              status: "error",
            }),
          );
          (cancelled ? cancelledIds : deniedIds).add(c.id);
        }
      }
    }

    const result = (await toolNode.invoke(
      {
        messages:
          rejectedToolMsgs.length > 0
            ? [...state.messages, ...rejectedToolMsgs]
            : state.messages,
      },
      {
        ...config,
        configurable: {
          ...config?.configurable,
          // write_todos 从 ToolRuntime.configurable 发 Plan；兼容 graph 构造期 callbacks
          //（executeFlow）与运行期 configurable callbacks（StatefulFlow / ACP）两条注入路径。
          onPlan: config?.configurable?.onPlan ?? callbacks?.onPlan,
          // 单 call 时透传 id，供 task 工具 onToken 构造 ACP messageId；多 call 并行由 LangGraph 分叉。
          langgraph_tool_call_id: calls.length === 1 ? calls[0]?.id : undefined,
        },
      }
    )) as {
      messages?: ToolMessage[];
    };
    const executed = result?.messages ?? [];

    // 合并执行结果 + 预合成拒绝消息，按原 calls 顺序重排（避免 LLM 见到乱序 tool_call_id）。
    let toolMsgs: ToolMessage[];
    if (rejectedToolMsgs.length === 0) {
      toolMsgs = executed;
    } else {
      const byId = new Map<string, ToolMessage>();
      for (const tm of executed) byId.set(tm.tool_call_id, tm);
      for (const tm of rejectedToolMsgs) byId.set(tm.tool_call_id, tm);
      toolMsgs = [];
      for (const c of calls) {
        if (!c.id) continue;
        const tm = byId.get(c.id);
        if (tm) {
          toolMsgs.push(tm);
          byId.delete(c.id);
        }
      }
      // 兜底：无 id 等未被 calls 覆盖的执行结果按原顺序补上。
      for (const tm of executed) {
        if (byId.has(tm.tool_call_id)) {
          toolMsgs.push(tm);
          byId.delete(tm.tool_call_id);
        }
      }
    }

    // 写路径：text-only 模型默认压成纯文本，避免毒化 checkpoint。
    // anthropic → 仅剥图像；openai 兼容（含智谱）→ 压扁任意非字符串 content。
    // vision 显式开启时保留原始 content blocks。
    if (shouldCoerceToTextOnly(appConfig)) {
      const mode = resolveCoerceMode(appConfig);
      toolMsgs = toolMsgs.map((tm) => {
        if (!messageContentNeedsTextCoerce(tm.content, mode)) return tm;
        return rebuildMessageWithTextContent(
          tm,
          coerceContentToText(tm.content, mode)
        ) as ToolMessage;
      });
    }

    for (const tm of toolMsgs) {
      // cancelled 的不发 terminal —— 交 onPrompt failInflightToolsOnCancel 统一收尾（避免双发）。
      // reject(deniedIds) 的照常走 failed terminal（in_progress→failed，修复被拒工具客户端卡转圈）。
      if (cancelledIds.has(tm.tool_call_id)) continue;
      if (onToolCall) {
        const failed = tm.status === "error";
        const normalized = normalizeToolResult(tm.content);
        const args = argsById.get(tm.tool_call_id) ?? {};
        // 保留 MCP structuredContent，供 emitToolCall 提取 ask-question 规范化 rawInput
        const result =
          normalized.rawOutput !== undefined
            ? {
                type: "text",
                text: normalized.text,
                structuredContent: normalized.rawOutput,
              }
            : normalized.text;
        await onToolCall({
          toolCallId: tm.tool_call_id,
          toolName: tm.name ?? "",
          args,
          status: failed ? "failed" : "completed",
          ...(failed ? { error: normalized.text } : { result }),
        });
      }
    }
    if (write) return write(toolMsgs, state);
    return { messages: toolMsgs } as unknown as Partial<S>;
  };
}
