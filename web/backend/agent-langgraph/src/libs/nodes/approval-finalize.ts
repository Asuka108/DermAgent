/**
 * createApprovalFinalizeNode —— 收口「isApproval 短路定稿」bespoke 模式（×3：human-in-loop/travel/pm finalize）。
 *
 * 模式：`if (isApproval(feedback)) 确定性输出; else LLM 按意见修订`。
 * 与 createHumanApprovalNode 互补：后者**前置** interrupt 收 feedback；本 factory **后置**——
 * feedback 已在 state，按是否通过短路（不调 LLM）或调 LLM 修订。一个完整 HITL 流常是 approval → … → finalize。
 *
 * @example
 * const finalize = createApprovalFinalizeNode<MyState>({
 *   approvedOutput: (s) => ({ output: `✅ 已通过：\n${s.draft}` }),
 *   rejectedLlm: {
 *     model: () => requireModel(appConfig, "review"),
 *     prompt: (s) => [new SystemMessage("按意见改写"), new HumanMessage(`原稿:${s.draft}\n意见:${s.feedback}`)],
 *     write: (r) => ({ output: `✏️ ${r.text}` }),
 *     config: appConfig, timeoutMs: resolveLlmResilience(appConfig).longTimeoutMs,
 *   },
 * });
 */
import type { LangGraphRunnableConfig } from "@langchain/langgraph";
import { createLlmStreamNode, type LlmStreamNodeOptions } from "./llm.js";
import { isApproval } from "./hitl.js";

export interface ApprovalFinalizeNodeOptions<S> {
  /** feedback 来源 channel 名（默认 "feedback"）。 */
  feedbackField?: string;
  /** 通过判定（默认 isApproval；可传自定义 regex/fn）。 */
  isApproved?: (feedback: string) => boolean;
  /** 通过时的确定性输出（不调 LLM，如甘特排期 / 定稿标记）。 */
  approvedOutput: (state: S) => Partial<S>;
  /** 未通过时的 LLM 修订：复用 createLlmStreamNode 选项（prompt 可读 s.feedback，write 产出修订输出，支持流式）。 */
  rejectedLlm: LlmStreamNodeOptions<S>;
}

/**
 * 造一个「按 feedback 短路定稿 / 否则 LLM 修订」的节点。
 * 返回普通 Partial<S>（非 Command）——定稿节点固定写 output 后到 END，不路由。
 */
export function createApprovalFinalizeNode<S>(
  opts: ApprovalFinalizeNodeOptions<S>
): (state: S) => Promise<Partial<S>> {
  const field = opts.feedbackField ?? "feedback";
  const isApproved = opts.isApproved ?? isApproval;
  const rejected = createLlmStreamNode<S>(opts.rejectedLlm);

  return async (state: S, config?: LangGraphRunnableConfig): Promise<Partial<S>> => {
    const raw = (state as Record<string, unknown>)[field];
    const fb = String(raw ?? "").trim();
    if (isApproved(fb)) return opts.approvedOutput(state);
    return rejected(state, config);
  };
}
