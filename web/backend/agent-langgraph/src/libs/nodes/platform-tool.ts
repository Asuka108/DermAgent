/**
 * createPlatformToolActionNode —— 主动调用已注入的工具集合。
 *
 * 与 createToolExecNode 不同：本节点不依赖上一条 AIMessage.tool_calls，而是由图节点
 * 自己从 state 构造参数并调用选中的 StructuredTool。平台工具的 URL / 鉴权 / schema
 * 在开发期静态沉淀到 spec/生成代码；运行期这里只消费 FlowRuntime.allTools。
 */

import { randomUUID } from "node:crypto";
import type { LangGraphRunnableConfig } from "@langchain/langgraph";
import type { StructuredTool } from "@langchain/core/tools";
import type { FlowCallbacks, ToolCallEvent } from "../../core/flow-types.js";

export interface PlatformToolActionResult {
  toolName: string;
  args: unknown;
  raw: unknown;
}

export interface PlatformToolActionNodeOptions<S> {
  /** 已按绑定目标过滤后的可用工具集合。 */
  tools: StructuredTool[];
  /** 指定工具名；未指定时使用 tools[0]。 */
  toolName?: string;
  /** 从 state 构造工具入参。 */
  args: (state: S) => unknown;
  /** 把工具原始返回写回 state。 */
  write: (result: PlatformToolActionResult, state: S) => Partial<S>;
  /** surface 回调（透出工具调用事件）。 */
  callbacks?: FlowCallbacks;
  label?: string;
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function toolResultForEvent(raw: unknown): ToolCallEvent["result"] {
  if (typeof raw === "string") return raw;
  let text: string;
  try {
    text = JSON.stringify(raw);
  } catch {
    text = String(raw);
  }
  return {
    type: "text",
    text,
    structuredContent: raw,
  };
}

export function createPlatformToolActionNode<S>(opts: PlatformToolActionNodeOptions<S>) {
  const { tools, toolName, args: buildArgs, write, callbacks, label = "platform-tool" } = opts;

  return async (state: S, config?: LangGraphRunnableConfig): Promise<Partial<S>> => {
    const onToolCall =
      (config?.configurable?.onToolCall as FlowCallbacks["onToolCall"]) ??
      callbacks?.onToolCall;
    const tool = toolName ? tools.find((t) => t.name === toolName) : tools[0];
    if (!tool) {
      throw new Error(`${label}: 未找到可用工具${toolName ? ` ${toolName}` : ""}`);
    }

    const builtArgs = buildArgs(state);
    const toolCallId = randomUUID();
    if (onToolCall) {
      await onToolCall({
        toolCallId,
        toolName: tool.name,
        args: builtArgs as Record<string, unknown>,
        status: "in_progress",
      });
    }

    try {
      // { callbacks: [] } 抑制 LangChain Runnable._callWithConfig 内部的 handleToolStart/End，
      // 否则会与本节点显式的 onToolCall 重复（出现第二个 unknown ToolCall 事件）。
      const raw = await tool.invoke(builtArgs, { callbacks: [] });
      if (onToolCall) {
        await onToolCall({
          toolCallId,
          toolName: tool.name,
          args: builtArgs as Record<string, unknown>,
          status: "completed",
          result: toolResultForEvent(raw),
        });
      }
      return write({ toolName: tool.name, args: builtArgs, raw }, state);
    } catch (err) {
      const message = errorMessage(err);
      if (onToolCall) {
        await onToolCall({
          toolCallId,
          toolName: tool.name,
          args: builtArgs as Record<string, unknown>,
          status: "failed",
          error: message,
        });
      }
      throw err;
    }
  };
}
