/**
 * dispatchSurfaceEvent —— 把归一后的 SurfaceStreamEvent 分发给 FlowCallbacks。
 *
 * stateful-flow 多模式 stream 与 ACP surface 共用，避免两处重复映射逻辑。
 */

import type { FlowCallbacks } from "../core/flow-types.js";
import type { SurfaceStreamEvent } from "./stream-events.js";
import { STREAM_TEXT_NODES } from "../libs/nodes/index.js";

/**
 * 将单个 surface 事件分发给 callbacks。
 * @param metadata LangGraph messages mode 附带的 metadata（含 langgraph_node）。
 */
export async function dispatchSurfaceEvent(
  event: SurfaceStreamEvent,
  callbacks: FlowCallbacks | undefined,
  metadata?: { langgraph_node?: string }
): Promise<void> {
  if (!callbacks) return;

  switch (event.type) {
    case "text": {
      const node = metadata?.langgraph_node;
      // custom writer 的 text 无 metadata，直接透出；messages mode 按节点白名单过滤，
      // 避免中间决策节点（RAG rewrite/grade/route 等）的 token 泄漏给用户。
      if (node && !STREAM_TEXT_NODES.has(node)) return;
      await callbacks.onToken?.(event.text);
      break;
    }
    case "thought": {
      const node = metadata?.langgraph_node;
      // 与 text 相同节点白名单：只透出 think/respond 等回答相关节点的 reasoning。
      if (node && !STREAM_TEXT_NODES.has(node)) return;
      await callbacks.onThought?.(event.text);
      break;
    }
    case "plan":
      await callbacks.onPlan?.({ entries: event.entries });
      break;
    case "stage":
      await callbacks.onStage?.({
        stage: event.stage,
        index: event.index,
        total: event.total,
        detail: event.detail,
      });
      break;
    case "tool_start":
      await callbacks.onToolCall?.({
        toolCallId: event.id,
        toolName: event.name,
        args:
          event.input && typeof event.input === "object" && !Array.isArray(event.input)
            ? (event.input as Record<string, unknown>)
            : { input: event.input },
        status: "in_progress",
      });
      break;
    case "tool_update":
      // stream 解析失败时 output 为空；跳过以免发出无内容的 completed 覆盖节点直出结果
      if (event.status === "completed" && event.output === undefined) {
        break;
      }
      await callbacks.onToolCall?.({
        toolCallId: event.id,
        toolName: event.name ?? event.id,
        args: {},
        status: event.status,
        ...(event.status === "completed"
          ? { result: event.output }
          : { error: event.error ?? String(event.output ?? "failed") }),
      });
      break;
    case "interrupt":
      // interrupt 由 stateful-flow run-loop 统一处理，此处不派发。
      break;
    default:
      break;
  }
}
