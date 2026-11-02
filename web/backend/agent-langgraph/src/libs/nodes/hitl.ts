/**
 * HITL 人审节点工厂 —— `createHumanApprovalNode` / `createPermissionApprovalNode`。
 *
 * ## 三种人审形态（选型）
 *
 * | 形态 | 工厂 | 机制 | 适用场景 |
 * | --- | --- | --- | --- |
 * | **对话式 interrupt** | `createHumanApprovalNode` | `interrupt()` → 跨 turn 收用户**文本** | 长文 review、需自由反馈、CLI/任意 ACP 客户端 |
 * | **同步弹窗** | `createPermissionApprovalNode` | `onApprovalRequest` → 同 turn 点选 | 秒级 yes/no（确认发布、确认删除） |
 * | **结构化表单（ask-question MCP）** | `createAskQuestionPresentationNode`（拓扑层，见下） | 图节点内 **direct invoke** MCP 工具 → 再 `interrupt` 收回复 | 平台问答卡片 / 多字段表单（通过+意见、选项等） |
 *
 * **ask-question MCP 不是 `createHumanApprovalNode` 的替代品**，而是其**前置展示层**：
 * MCP 负责渲染结构化 UI（`rawInput.ui` → ACP tool_call）；`interrupt` 仍负责 durable checkpoint / resume。
 * 二者必须拆成**相邻两节点**（如 `present_review` → `review`），在自建 HITL 图里接线。
 *
 * ## ask-question MCP：何时用、如何用
 *
 * **应用 ask-question 的场景**（图内 HITL，非 default ReAct 随意调用）：
 * - 内容审阅定稿：草稿 +「通过 / 按意见修改」+ 修改意见（human-in-loop）
 * - 需固定字段（radio / select / textarea）的多步确认，且 ACP 宿主支持平台问答卡片渲染
 * - 要在 ACP 侧展示 **tool_call 卡片**（`ask-question__nuwax_ask_question`），而非纯文本 interrupt
 *
 * **不要用 ask-question 的场景**：
 * - 秒级二元确认 → `createPermissionApprovalNode`
 * - 只需用户打一段话 → 单独 `createHumanApprovalNode`（纯文本 interrupt 即可）
 * - default ReAct 普通闲聊 / 澄清 —— **禁止**模型在 think 里调 `nuwax_ask_question` 代替对话；
 *   结构化表单仅用于**图编排好的 HITL 节点**或用户明确要求填表
 *
 * **配置与工具定位**：
 * - 包内 fallback：`config/mcp.default.json` → `ask-question` server（`nuwax-ask-question-mcp`）
 * - 平台同名下发 **session-wins** 覆盖内置（`runtime-context` `mergeServers` 后者优先）
 * - 工具名：`ask-question__nuwax_ask_question`（或无前缀 `nuwax_ask_question`）；用
 *   `findAskQuestionTool(runtime.allTools)` 从已 hydrate 的 MCP 工具集定位
 *
 * **接线模板**（自建 HITL 图）：
 * ```
 * START → compose → present_review(MCP 展示) → review(interrupt) → finalize → END
 * ```
 * - `present_review`：`createAskQuestionPresentationNode(askQuestionTool)`，经 `onToolCall` 透出
 *   in_progress/completed；MCP 返回 `pending`，**不**替代 checkpoint
 * - `review`：`createHumanApprovalNode` + `normalizeReviewFeedback` 归一化 JSON/表单文本 → `ok` 或修改意见
 * - resume 只重跑 `review`，**不会**重复发 MCP 卡片（`present_review` 已落 checkpoint）
 *
 * API 见 `docs/node-kit.md` § createHumanApprovalNode。
 */

import { Command, interrupt, type LangGraphRunnableConfig } from "@langchain/langgraph";
import type { FlowCallbacks } from "../../core/flow-types.js";

const APPROVAL_RE = /^(ok|okay|通过|可以|批准|approved?|confirm(ed)?|yes|好的?|lgtm)$/i;

export function isApproval(
  feedback: string,
  opts?: { regex?: RegExp }
): boolean {
  const re = opts?.regex ?? APPROVAL_RE;
  const fb = feedback.trim();
  return !fb || re.test(fb);
}

export interface HumanApprovalNodeOptions<S> {
  /** interrupt 载荷：字符串（包成 {question}）或完整 payload 对象。 */
  question: (state: S) => string | Record<string, unknown>;
  /** 自定义通过判定词。 */
  regex?: RegExp;
  /** 简单写回（默认 { feedback }）。与 route 二选一。 */
  write?: (feedback: string, approved: boolean, state: S) => Partial<S>;
  /** 路由变体：返回 Command（含 goto + update）。与 write 二选一。 */
  route?: (approved: boolean, feedback: string, state: S) => Command;
}

/**
 * 对话式 HITL：跨 turn `interrupt` 收用户文本（或宿主把表单提交格式化成文本/JSON）。
 *
 * 若上游有 ask-question MCP 展示节点，在 `write` 里用 `normalizeReviewFeedback`（human-in-loop 拓扑）
 * 把表单 JSON /「处理方式：通过」等格式统一成 `ok` 或修改意见，再交 `createApprovalFinalizeNode`。
 */
export function createHumanApprovalNode<S>(
  opts: HumanApprovalNodeOptions<S>
) {
  const { question, regex, write, route } = opts;
  return (state: S): Partial<S> | Command => {
    const q = question(state);
    const payload = typeof q === "string" ? { question: q } : q;
    const feedback = String(interrupt(payload) ?? "").trim();
    const approved = isApproval(feedback, regex ? { regex } : undefined);
    if (route) return route(approved, feedback, state);
    if (write) return write(feedback, approved, state);
    return { feedback } as unknown as Partial<S>;
  };
}

/**
 * createPermissionApprovalNode —— 弹窗式审批节点（同步门控，非 interrupt）。
 *
 * 与 createHumanApprovalNode（前置 interrupt，跨轮对话式）互补：本节点经
 * `configurable.onApprovalRequest`（ACP surface 注入）**同 turn 同步**弹
 * `session/request_permission` 征询确认，用户点选项即决（不结束 turn、不等下一轮消息）。
 * 适合秒级 yes/no（如"确认发布?"）。复用工具审批（A）的同步弹窗通道；
 * 未注入回调（CLI / 非 ACP）→ 默认放行（graceful，与工具审批一致）。
 *
 * 需要多字段表单（非二元选项）时，不要用本节点 —— 用 ask-question MCP + interrupt（见模块头注释）。
 *
 * @example
 * const confirm = createPermissionApprovalNode<S>({
 *   request: (s) => ({ title: "确认发布?", detail: s.draft }),
 *   approved: (s) => new Command({ goto: "publish" }),
 *   rejected: (s) => new Command({ goto: "revise" }),
 * });
 */
export interface PermissionApprovalNodeOptions<S> {
  /** 弹窗内容：字符串（作 title）或 { title, detail }。 */
  request: (state: S) => string | { title: string; detail?: string };
  /** 通过（allow）时的写回 / 路由。 */
  approved: (state: S) => Partial<S> | Command;
  /** 拒绝 / 取消（reject / cancelled）时的写回 / 路由。 */
  rejected: (state: S) => Partial<S> | Command;
}

export function createPermissionApprovalNode<S>(
  opts: PermissionApprovalNodeOptions<S>
) {
  return async (
    state: S,
    config?: LangGraphRunnableConfig
  ): Promise<Partial<S> | Command> => {
    const onApprovalRequest = config?.configurable
      ?.onApprovalRequest as FlowCallbacks["onApprovalRequest"];
    const req = opts.request(state);
    const event = typeof req === "string" ? { title: req } : req;
    // 未注入回调（CLI / 非 ACP surface）→ 默认放行（graceful，与工具审批 A 一致）。
    const decision = onApprovalRequest ? await onApprovalRequest(event) : "allow";
    return decision === "allow" ? opts.approved(state) : opts.rejected(state);
  };
}
