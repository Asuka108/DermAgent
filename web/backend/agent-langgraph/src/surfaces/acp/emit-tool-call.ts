/**
 * emitToolCall —— Flow ToolCallEvent → ACP session/update（tool_call / tool_call_update）。
 *
 * 展示逻辑：libs/deepagents-acp/acp-tool-presentation.ts（与 Legacy 共用）
 * ACP 调试与 HITL：见 docs/troubleshooting.md
 * 参考实现：https://github.com/nuwax-ai/claude-code-acp-ts
 *
 * ACP 宿主 acpUpdateMapper 只读 rawInput / rawOutput。ask-question 依赖 rawInput.ui（平台问答卡片）。
 * 部分宿主合成 ASK_QUESTION 事件要求 tool 进度 title 含 `nuwax_ask_question`；
 * completed 也须带 title（in_progress 被 delay 时可能仅收到 completed）。
 */

import type {
  RequestPermissionRequest,
  RequestPermissionResponse,
} from "@agentclientprotocol/sdk";
import {
  toolInfoFromToolEvent,
  toolUpdateFromToolResult,
} from "../../libs/deepagents-acp/index.js";
import { extractMcpStructuredRawInput } from "../../libs/nodes/tool-result-normalize.js";
import { logger } from "../../runtime/index.js";
import type { ToolCallEvent } from "../../core/flow-types.js";

const log = logger.child("flow-acp");

/** ACP 连接最小接口（tool 相关字段）。 */
export interface AcpToolConnection {
  sessionUpdate(params: {
    sessionId: string;
    update: Record<string, unknown>;
  }): Promise<void>;
  /**
   * 工具审批 RPC（ACP `session/request_permission`）。**可选** —— client 不实现时为
   * undefined，审批层降级放行（见 surfaces/acp/server.ts buildAcpCallbacks）。
   * 返回 outcome：`{outcome:"selected", optionId}` 选中某项 / `{outcome:"cancelled"}` 取消。
   */
  requestPermission?(
    params: RequestPermissionRequest,
  ): Promise<RequestPermissionResponse>;
}

/** emitToolCall 可选参数。 */
export interface EmitToolCallOptions {
  /** session cwd，用于 locations / diff 绝对路径 */
  workspaceRoot?: string;
  /** in_progress 时缓存的 args，completed 时回填 rawInput */
  inflightTools?: Map<string, ToolCallEvent>;
  /**
   * 本 prompt 回合已发过 `tool_call` 首包的 id（C-dedupe）。
   * createToolExecNode 与 LangGraph `tools` stream 可能对同一 id 各触发一次 in_progress；
   * 二次改为 `tool_call_update` 精炼 rawInput，避免重复 tool_call。
   */
  emittedToolCallIds?: Set<string>;
  /**
   * 本 prompt 回合已发过 terminal（completed/failed）update 的 id（双轨去重）。
   * 节点直出 completed（带完整 rawInput + result）先到；stream `on_tool_end` 的冗余 completed
   * 后到且缺 rawInput（dispatch tool_update 不带 input）→ 据此跳过，避免无 rawInput 的第二个
   * completed 覆盖首个（ask-question 平台问答卡片依赖 rawInput.ui）。
   */
  completedToolCallIds?: Set<string>;
}

const EMPTY_RESULT_PLACEHOLDER = "(工具已执行，但未返回内容)";
const FAILED_NO_DETAIL = "(工具执行失败，无错误详情)";

/**
 * 把 FlowExecutor 的 ToolCallEvent 翻译成 ACP tool_call / tool_call_update 推给客户端。
 */
export async function emitToolCall(
  conn: AcpToolConnection,
  sessionId: string,
  e: ToolCallEvent,
  options?: EmitToolCallOptions
): Promise<void> {
  const { workspaceRoot, inflightTools, emittedToolCallIds, completedToolCallIds } =
    options ?? {};

  if (e.status === "in_progress") {
    const info = toolInfoFromToolEvent(e.toolName, e.args, workspaceRoot);
    const payload = {
      toolCallId: e.toolCallId,
      title: info.title,
      kind: info.kind,
      rawInput: e.args,
      ...(info.locations?.length ? { locations: info.locations } : {}),
      ...(info.content?.length ? { content: info.content } : {}),
    };

    // 双轨 onToolCall（节点直出 + tools stream）二次 in_progress → 精炼 update，对齐 alreadyCached
    if (emittedToolCallIds?.has(e.toolCallId)) {
      await conn.sessionUpdate({
        sessionId,
        update: {
          sessionUpdate: "tool_call_update",
          status: "in_progress",
          ...payload,
        },
      });
      return;
    }

    emittedToolCallIds?.add(e.toolCallId);
    await conn.sessionUpdate({
      sessionId,
      update: {
        sessionUpdate: "tool_call",
        status: "in_progress",
        ...payload,
      },
    });
    return;
  }

  emittedToolCallIds?.delete(e.toolCallId);

  // 双轨 terminal 去重：节点直出已发过 completed/failed（带完整 rawInput + result），
  // stream on_tool_end 的冗余 terminal 后到且缺 rawInput（dispatch tool_update 不带 input）→ 跳过，
  // 避免无 rawInput 的第二个 completed 覆盖首个（dataflow-nuwaclaw.md 双轨去重）。
  if (completedToolCallIds?.has(e.toolCallId)) {
    return;
  }
  completedToolCallIds?.add(e.toolCallId);

  const cached = inflightTools?.get(e.toolCallId);
  // MCP→ACP：CallToolResult.structuredContent（及其中 .input）优先于 LLM 原始 args。
  const mcpStructuredRawInput =
    e.status === "completed" ? extractMcpStructuredRawInput(e.result) : undefined;
  const rawInput =
    mcpStructuredRawInput ??
    (cached?.args && Object.keys(cached.args).length > 0 ? cached.args : undefined);

  // terminal update 也要带 title/kind：部分 ACP 宿主用 title.contains("nuwax_ask_question") 合成 ASK_QUESTION；
  // 部分宿主可能只转发 completed（in_progress 被 permissionGatedToolUpdate delay）。
  const presentationToolName = cached?.toolName ?? e.toolName;
  const presentationArgs: Record<string, unknown> =
    rawInput != null &&
    typeof rawInput === "object" &&
    !Array.isArray(rawInput)
      ? (rawInput as Record<string, unknown>)
      : (cached?.args ?? e.args ?? {});
  const terminalPresentation = toolInfoFromToolEvent(
    presentationToolName,
    presentationArgs,
    workspaceRoot
  );

  const update: Record<string, unknown> = {
    sessionUpdate: "tool_call_update",
    toolCallId: e.toolCallId,
    status: e.status,
    title: terminalPresentation.title,
    kind: terminalPresentation.kind,
  };

  if (rawInput) {
    update.rawInput = rawInput;
  }

  if (e.status === "completed") {
    if (e.result != null) {
      const presentation = toolUpdateFromToolResult(e.toolName, e.result, {
        workspaceRoot,
      });
      update.rawOutput = presentation.rawOutput;
      if (presentation.content?.length) {
        update.content = presentation.content;
      } else if (presentation.displayText) {
        update.content = [
          {
            type: "content",
            content: { type: "text", text: presentation.displayText },
          },
        ];
      }
    } else {
      log.warn("tool_call_update completed 但 result 为空", {
        sessionId,
        toolCallId: e.toolCallId,
        toolName: e.toolName,
        hasRawInput: !!rawInput,
      });
      update.content = [
        {
          type: "content",
          content: { type: "text", text: EMPTY_RESULT_PLACEHOLDER },
        },
      ];
    }
  } else if (e.status === "failed") {
    const errText = e.error?.trim() ? e.error : FAILED_NO_DETAIL;
    update.content = [
      { type: "content", content: { type: "text", text: errText } },
    ];
  }

  await conn.sessionUpdate({ sessionId, update });
}
