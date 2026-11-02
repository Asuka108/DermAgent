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
import { routeIntentRules, type IntentMeta, type IntentState } from "../intent/index.js";
import { readMemoryForTurn, type MemoryMeta, type MemoryPort } from "../memory/index.js";

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

/** 路由标注用的会话状态快照（只给标签/布尔，不给病例字段值）。 */
export function intentStateOf(state: FlowState): IntentState {
  const filled = state.case
    ? Object.entries(state.case)
        .filter(([, v]) => typeof v === "string" && v.trim())
        .map(([k]) => k)
    : [];
  return {
    phase: state.draft ? "doc_ready" : state.case ? "screening" : "case_intake",
    has_draft: Boolean(state.draft),
    filled_fields: filled,
  };
}

export interface IntakeNodeOptions {
  /** 完整路由（规则 → LLM）；缺省只跑规则层。合成案例不调用（确定性路径，省一次模型调用）。 */
  route?: (state: FlowState) => IntentMeta | Promise<IntentMeta>;
  /** 记忆端口（缺省不读记忆，行为与接入前一致）。 */
  memory?: MemoryPort;
  /** 当前病例句柄（多轮续跑时由调用方传入）。 */
  caseId?: string | ((state: FlowState) => string | undefined);
}

function intentStep(meta: IntentMeta): string {
  const tag = meta.intent ?? "undecided";
  return `intent:${meta.source}@L${meta.layer}:${tag}${meta.needs_confirm ? ":confirm" : ""}`;
}

function memoryStep(meta: MemoryMeta | null): string[] {
  if (!meta?.enabled) return [];
  const degraded = meta.degraded.profile || meta.degraded.episodic;
  return [
    `memory:${meta.source} facts=${meta.profile_facts} hits=${meta.episodic_hits}` +
      `${degraded ? ":degraded" : ""}`,
  ];
}

function resolveCaseId(
  option: IntakeNodeOptions["caseId"],
  state: FlowState
): string | undefined {
  return typeof option === "function" ? option(state) : option;
}

/** intake：命中合成案例 → 确定性写入病例；否则只留轨迹，转 intake_llm。附带意图路由/记忆标注。 */
export function createIntakeNode(options: IntakeNodeOptions = {}) {
  return async (state: FlowState): Promise<Partial<FlowState>> => {
    const memoryMeta = await readMemoryForTurn(
      options.memory,
      resolveCaseId(options.caseId, state),
      state.input
    );
    const demo = matchDemoCase(state.input);
    if (demo) {
      const meta = routeIntentRules(state.input, intentStateOf(state));
      return {
        case: demo.case,
        missing: computeMissing(demo.case),
        intentMeta: meta,
        memoryMeta,
        steps: [
          `intake:命中合成案例${demo.id}（${demo.label}）`,
          intentStep(meta),
          ...memoryStep(memoryMeta),
        ],
      };
    }
    const meta = options.route
      ? await options.route(state)
      : routeIntentRules(state.input, intentStateOf(state));
    return {
      intentMeta: meta,
      memoryMeta,
      steps: [
        "intake:非合成案例指令，转 LLM 结构化解析",
        intentStep(meta),
        ...memoryStep(memoryMeta),
      ],
    };
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
