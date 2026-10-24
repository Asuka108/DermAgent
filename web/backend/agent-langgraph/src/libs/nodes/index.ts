/**
 * src/libs/nodes —— 可复用 LangGraph 节点 factory 目录（「现成节点」菜单）。
 *
 * 与 src/app/nodes 平级区分：app/nodes 放**默认图专属节点定义**（think/respond 等），
 * 本目录放**跨图复用的 factory + 支撑原语**，默认图与示例共享、可选不用。
 *
 * 主导出（factory）：
 *  - llm：createLlmNode（一次调→文本/结构化）、createLlmStreamNode（流式）、createLlmRouterNode（LLM 裁决→Command goto）
 *  - tools：createToolExecNode（ToolNode + 三态透出）、createPlatformToolActionNode（主动工具调用）、createMcpRetrievalNode（主动 MCP 检索）
 *  - hitl：createHumanApprovalNode（前置 interrupt）、createApprovalFinalizeNode（后置定稿）
 *  - prepare：createPrepareNode（input → HumanMessage）
 *
 * 支撑原语（factory 内部用，过渡期示例也可 import）：emit* / runTool / extractText /
 * parseJson / streamLLMText / isApproval。韧性原语在 runtime/services/llm-resilience，
 * checkpointer 选择（durableCheckpointer）在 runtime/services/file-checkpoint-saver。
 */

export { emitStage, emitPlan, emitTextToken, STREAM_TEXT_NODES } from "./emit.js";
export { createPrepareNode, type PrepareNodeOptions } from "./prepare.js";
export {
  createHumanApprovalNode,
  createPermissionApprovalNode,
  isApproval,
  type HumanApprovalNodeOptions,
  type PermissionApprovalNodeOptions,
} from "./hitl.js";
export {
  createApprovalFinalizeNode,
  type ApprovalFinalizeNodeOptions,
} from "./approval-finalize.js";
export { createLlmRouterNode, type LlmRouterNodeOptions } from "./llm-router.js";
export { createMcpRetrievalNode, type McpRetrievalNodeOptions } from "./mcp-retrieval.js";
export { createToolExecNode, runTool, type ToolExecNodeOptions } from "./tools.js";
export {
  createPlatformToolActionNode,
  type PlatformToolActionNodeOptions,
  type PlatformToolActionResult,
} from "./platform-tool.js";
export {
  normalizeToolMessageContent,
  normalizeToolResult,
  extractToolEndOutput,
  extractMcpStructuredRawInput,
  type NormalizedToolResult,
} from "./tool-result-normalize.js";
export {
  createLlmNode,
  createLlmStreamNode,
  extractText,
  extractAnthropicThinkingText,
  extractReasoningTextFromMessage,
  extractThoughtTextFromMessage,
  extractVisibleTextFromMessage,
  parseJson,
  streamLLMText,
  type LlmNodeOptions,
  type LlmStreamNodeOptions,
  type LLMLike,
  type ChatModelLike,
  type StreamLlmTextOptions,
} from "./llm.js";
export { foldStreamTextChunk, type StreamTextFoldResult } from "./stream-text-delta.js";
export { createFanout, type FanoutOptions } from "./fanout.js";
export { createSubgraphNode, type SubgraphNodeOptions } from "./subgraph.js";
export { requireModel } from "./model-resolver.js";

/**
 * 路由(router)模式 —— 三种路径:
 *  - HITL 门禁路由(人审 → 通过/打回):`createHumanApprovalNode({ route })`(返回 Command)。
 *  - 规则条件边(`toolsCondition` / `routeAfterGrade` 等):普通 `(state) => nodeName` 纯函数 + `addConditionalEdges`。
 *  - LLM 裁决路由(reflection/evaluator):优先 `createLlmRouterNode`(节点内 Command goto);
 *    或 `createLlmNode({ parse })` + 外部 `routeAfterXxx` 纯条件边(两种方式可复用同一 routeAfter 函数)。
 */
