/**
 * 肤智（DermAgent） bespoke 节点（为何不用 factory——逐节点注释）：
 *
 *  - intake：合成案例确定性解析（无 LLM 依赖），free-form 走 intake_llm（createLlmNode）。
 *  - ask_more：interrupt 原语收「补充资料」并合并回病例——语义是追问而非审批门
 *    （createHumanApprovalNode 的通过判定/路由语义不适用），故手写。
 *  - case_closed：医生拒绝后的确定性收尾（不调 LLM）。
 *
 * 节点名均与 state channel 不同名（R-G007）；节点只返回 Partial，不 mutate（R-G008）。
 */

import { interrupt } from "@langchain/langgraph";
import { hasModelCredentials } from "../../libs/compaction.js";
import type { ChatModelLike, LLMLike } from "../../libs/nodes/index.js";
import { resolveModel, logger, type AppConfig } from "../../runtime/index.js";
import type { FlowState } from "../state.js";
import { matchDemoCase } from "../knowledge/demo-cases.js";
import { computeMissing, mergeCaseReply, renderAskQuestion } from "../logic.js";

const log = logger.child("derm-nodes");

/** 宽容版模型解析：无凭证/解析失败 → null（节点走 fallback，图仍可跑）。 */
export function tryResolveLlm(config?: AppConfig): LLMLike | null {
  try {
    if (!config || !hasModelCredentials(config)) return null;
    const resolved = resolveModel(config);
    if (!resolved || typeof resolved === "string") return null;
    return resolved as unknown as LLMLike;
  } catch (err) {
    log.warn("模型解析失败，走 fallback", { err: String(err) });
    return null;
  }
}

/** 结构化节点的 ChatModelLike 视图（createLlmNode 的 model 选项）。 */
export function tryResolveModel(config?: AppConfig): ChatModelLike | null {
  return tryResolveLlm(config);
}

/** intake：命中合成案例 → 确定性写入病例；否则只留轨迹，转 intake_llm。 */
export function createIntakeNode() {
  return (state: FlowState): Partial<FlowState> => {
    const demo = matchDemoCase(state.input);
    if (demo) {
      return {
        case: demo.case,
        missing: computeMissing(demo.case),
        steps: [`intake:命中合成案例${demo.id}（${demo.label}）`],
      };
    }
    return { steps: ["intake:非合成案例指令，转 LLM 结构化解析"] };
  };
}

/** ask_more：interrupt 收补充资料 → 合并回病例 → 回 screen 复检。 */
export function createAskMoreNode() {
  return (state: FlowState): Partial<FlowState> => {
    const question = renderAskQuestion(state.missing, state.intakeNote || undefined);
    const reply = String(interrupt({ question }) ?? "").trim();
    if (!state.case) {
      return { steps: ["ask_more:病例为空（异常），跳过合并"] };
    }
    const merged = mergeCaseReply(state.case, reply);
    const nextRound = state.askCount + 1;
    return {
      case: merged,
      missing: computeMissing(merged),
      askCount: nextRound,
      steps: [`ask_more:收到补充（第${nextRound}轮）`],
    };
  };
}

/** case_closed：医生拒绝 → 不生成报告，确定性收尾。 */
export function createCaseClosedNode() {
  return (state: FlowState): Partial<FlowState> => ({
    output: [
      "❌ **医生已拒绝本病例的待审核草稿，按流程不生成报告。**",
      state.feedback ? `拒绝意见：${state.feedback}` : "",
      "",
      "如需重新评估，请修改病例资料后重新发起问诊。",
    ]
      .filter((l) => l !== "")
      .join("\n"),
    report: "",
    steps: ["case_closed:医生拒绝，不生成报告"],
  });
}
