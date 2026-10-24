/**
 * 肤智（DermAgent） Flow Graph —— 皮肤科智能问诊与循证辅助（HITL 固定管道）。
 *
 *   START → intake ─(合成案例?)-→ screen ─(缺必填?)-→ ask_more(interrupt) → screen
 *                └→ intake_llm ────┘        └→ retrieve → analyze → doctor_review(interrupt)
 *                                              doctor_review ─(confirmed)-→ finalize_report(流式) → END
 *                                                              ─(modified)─→ revise → doctor_review
 *                                                              ─(rejected)─→ case_closed → END
 *
 * 图选型理由（default ReAct 为何不够，见 docs/examples.md § 先判定）：
 *  必须固定阶段顺序（问诊 → 校验/红旗 → 检索 → 草稿 → 人审 → 报告）+ multi-turn HITL
 *  （interrupt/resume：医生确认/修改/拒绝），均命中「改图」行。
 *
 * 本文件只做「建节点 + 连边」（图是契约）：bespoke 节点在 `./nodes/derm.ts`，
 * 决策逻辑抽纯函数在 `./logic.ts`（可单测）。节点优先 factory：intake_llm / analyze /
 * revise 用 createLlmNode（结构化 JSON，write 读 r.parsed，R-G001），finalize_report 用
 * createLlmStreamNode（用户可见大段输出，write 读 r.text，R-G009），doctor_review 用
 * createHumanApprovalNode。无模型凭证时各 LLM 节点走 fallback（规则版合成演示稿），图始终可跑。
 *
 * CreateFlowGraphConfig 是 FlowRuntime 的结构子集 —— getFlowTopology 反射拓扑时传最小子集
 * （空 tools + MemorySaver），不加载 MCP、不需要凭证、不 invoke 节点。
 */

import { randomUUID } from "node:crypto";
import {
  StateGraph,
  START,
  END,
  MemorySaver,
  type BaseCheckpointSaver,
} from "@langchain/langgraph";
import { HumanMessage, SystemMessage } from "@langchain/core/messages";
import type { StructuredTool } from "@langchain/core/tools";
import { logger, type AppConfig } from "../runtime/index.js";
import { parseJson } from "../libs/nodes/index.js";
import {
  createLlmNode,
  createLlmStreamNode,
  createHumanApprovalNode,
  type LLMLike,
} from "../libs/nodes/index.js";
import { resolveLlmResilience } from "../runtime/services/llm-resilience.js";
import { FlowStateAnnotation, type CaseRecord, type ConsultDraft, type FlowState } from "./state.js";
import type { FlowCallbacks } from "../core/flow-types.js";
import {
  createIntakeNode,
  createAskMoreNode,
  createCaseClosedNode,
  tryResolveLlm,
  tryResolveModel,
} from "./nodes/derm.js";
import { createAnalyzeNode } from "./nodes/analyze.js";
import {
  buildCaseFromFreeText,
  buildFallbackDraft,
  buildFallbackRevisedDraft,
  buildFallbackReport,
  classifyReview,
  computeMissing,
  CONFIRM_RE,
  detectRedFlags,
  retrieveCitations,
  renderReviewQuestion,
  routeAfterIntake,
  routeAfterReview,
  routeAfterScreen,
  sanitizeDraftCitations,
} from "./logic.js";

const log = logger.child("derm-graph");

export interface CreateFlowGraphConfig {
  /** 保留接口兼容（task 子智能体 / ACP runtime 注入）；本管道无 ReAct 工具回路，不 bindTools。 */
  allTools?: StructuredTool[];
  /** checkpointer（FlowRuntime.checkpointer）；反射拓扑时默认 MemorySaver。 */
  checkpointer?: BaseCheckpointSaver;
  /** AppConfig（模型解析用）；反射拓扑时可空。 */
  config?: AppConfig;
  /** 系统提示词（平台 systemPrompt / prompts/derm-flow.md），注入各 LLM 节点。 */
  systemPrompt?: string;
  callbacks?: FlowCallbacks;
}

/** 节点级任务契约（单步 JSON/报告），智能体身份由 systemPrompt 注入。 */
const INTAKE_SYSTEM = [
  "你是皮肤科结构化问诊信息抽取引擎。从用户输入中提取九要素：",
  "body_region(身体区域)、chief_complaint(主诉)、symptoms(症状)、duration(病程)、trend(变化趋势)、",
  "triggers(诱因)、past_history(既往史)、allergies(过敏史)、current_medications(当前用药)，",
  "以及 photo_note(脱敏/合成照片说明，若有)。",
  '只输出 JSON：{"case": {…上述字段…}, "note": "若输入不是病例描述（如问候语），在此友好说明本智能体用途与所需信息，否则留空"}。',
  "提取不到的字段一律留空字符串，禁止臆测编造。",
].join("");

function withAgentPrompt(system: string, agentPrompt?: string): string {
  return agentPrompt ? `${system}\n\n# 智能体身份与约束\n${agentPrompt}` : system;
}

export function createFlowGraph(config: CreateFlowGraphConfig = {}) {
  const { checkpointer = new MemorySaver() } = config;
  const appConfig = config.config;
  const { longTimeoutMs } = resolveLlmResilience(appConfig);
  const agentPrompt = config.systemPrompt;

  // —— 节点 ——

  const intake = createIntakeNode();

  const intakeLlm = createLlmNode<FlowState>({
    model: () => tryResolveModel(appConfig),
    config: appConfig,
    timeoutMs: longTimeoutMs,
    label: "intake_llm",
    prompt: (s) => [
      new SystemMessage(withAgentPrompt(INTAKE_SYSTEM, agentPrompt)),
      new HumanMessage(`用户输入：${s.input}`),
    ],
    parse: (t) => parseJson<{ case?: Partial<CaseRecord>; note?: string }>(t),
    write: (r) => {
      const base = buildCaseFromFreeText("");
      const out = (r.parsed ?? {}) as { case?: Partial<CaseRecord>; note?: string };
      const p = out.case ?? {};
      const merged: CaseRecord = { ...base, ...Object.fromEntries(Object.entries(p).filter(([, v]) => v != null)) } as CaseRecord;
      return {
        case: merged,
        missing: computeMissing(merged),
        intakeNote: out.note?.trim() ?? "",
        steps: ["intake_llm:LLM 结构化解析完成"],
      };
    },
    fallback: (s) => {
      const c = buildCaseFromFreeText(s.input);
      return {
        case: c,
        missing: computeMissing(c),
        steps: ["intake_llm:无模型凭证/解析失败 → 规则抽取"],
      };
    },
  });

  const screen = (s: FlowState): Partial<FlowState> => {
    const redFlags = s.case ? detectRedFlags(s.case) : [];
    return {
      missing: computeMissing(s.case),
      redFlags,
      citations: [],
      steps: [`screen:必填缺失 ${s.missing.length} 项；红旗 ${redFlags.length} 项`],
    };
  };

  const askMore = createAskMoreNode();

  const retrieve = (s: FlowState): Partial<FlowState> => {
    const citations = retrieveCitations(s.case);
    return {
      citations,
      steps: [`retrieve:知识库检索命中 ${citations.length} 条引用`],
    };
  };

  // analyze 为 bespoke 节点（两段式：生成 → 非法 JSON 受限修复一次，见 nodes/analyze.ts）
  const analyze = createAnalyzeNode(appConfig, agentPrompt);

  const doctorReview = createHumanApprovalNode<FlowState>({
    question: (s) => renderReviewQuestion(s.draft, s.redFlags),
    regex: CONFIRM_RE,
    write: (fb, approved) => ({ feedback: fb, decision: classifyReview(fb, approved) }),
  });

  const revise = createLlmNode<FlowState>({
    model: () => tryResolveModel(appConfig),
    config: appConfig,
    timeoutMs: longTimeoutMs,
    label: "revise",
    prompt: (s) => [
      new SystemMessage(
        withAgentPrompt(
          [
            "你是皮肤科循证分析引擎的修订器：按医生意见修订待审核草稿。",
            "只输出与原草稿相同 schema 的 JSON（可修订：caseSummary.symptomSummary、suspectedConditions、symptomAssessment、medicationChecks、severity.basis、recommendedNextSteps、uncertainties）。",
            "硬约束：引用只能使用原草稿中已存在的 citationId（不得新增来源）；status=unknown/not_asked 不得写成 absent；禁止自动处方、剂量与疗程；简体中文。",
          ].join("\n"),
          agentPrompt
        )
      ),
      new HumanMessage(
        [
          `原草稿：${JSON.stringify(s.draft, null, 2)}`,
          `医生意见：${s.feedback || "（无具体意见，请综合完善）"}`,
        ].join("\n\n")
      ),
    ],
    parse: (t) => parseJson<Partial<ConsultDraft>>(t),
    write: (r, s) => ({
      draft: sanitizeDraftCitations(
        { ...(s.draft ?? {}), ...(r.parsed ?? {}), generated_by: "llm" },
        s.citations,
        s.redFlags,
        s.missing,
        { modelVersion: appConfig?.model?.name ?? "unknown" }
      ),
      decision: "pending",
      steps: ["revise:按医生意见修订草稿（LLM）"],
    }),
    fallback: (s) => ({
      draft: buildFallbackRevisedDraft(
        s.draft ?? buildFallbackDraft(s.case, s.citations, s.redFlags, s.missing),
        s.feedback
      ),
      decision: "pending",
      steps: ["revise:无模型凭证/调用失败 → 记录医生意见，草稿待人工落实"],
    }),
  });

  const finalizeReport = createLlmStreamNode<FlowState>({
    // 无凭证时 tryResolveLlm 返回 null → streamLLMText 内抛错 → 走 fallback（规则渲染报告）
    model: () => tryResolveLlm(appConfig) as LLMLike,
    config: appConfig,
    timeoutMs: longTimeoutMs,
    label: "finalize_report",
    prompt: (s) => [
      new SystemMessage(
        withAgentPrompt(
          [
            "你是肤智（DermAgent） 报告生成器：基于医生已确认的草稿生成正式报告（简体中文，Markdown 分节）。",
            "首行元信息：Schema 版本｜模型版本｜知识库快照｜生成时间。",
            "结构：一、病例摘要（含资料完备度）；二、鉴别诊断（仅供医生核对，标注置信度）；三、症状依据（unknown/未询问必须如实标注，不得写成「没有」）；",
            "四、用药核对（禁止自动处方、剂量与疗程）；五、严重程度（枚举值）；六、红旗与升级（含建议动作）；七、缺失信息；",
            "八、建议下一步（由医生决定）；九、不确定性；十、引用（citation_id｜文档名｜版本｜资料类型｜章节｜原文片段）。",
            "末尾必须原样给出草稿中的免责声明。不得新增知识库之外的结论。",
          ].join("\n"),
          agentPrompt
        )
      ),
      new HumanMessage(
        [
          `医生决定：确认（${s.feedback || "无附加意见"}）`,
          `已确认草稿：${JSON.stringify(s.draft, null, 2)}`,
        ].join("\n\n")
      ),
    ],
    write: (r) => ({
      report: r.text,
      output: r.text,
      steps: ["finalize_report:生成正式报告（流式）"],
    }),
    fallback: (s) => {
      const rep = buildFallbackReport(s.draft);
      return { report: rep, output: rep, steps: ["finalize_report:无模型凭证/失败 → 规则渲染正式报告"] };
    },
  });

  const caseClosed = createCaseClosedNode();

  // —— 连线（节点名 ≠ channel 名，R-G007；条件边返回值 ∈ targets，R-G004）——
  const graph = new StateGraph(FlowStateAnnotation)
    .addNode("intake", intake)
    .addNode("intake_llm", intakeLlm)
    .addNode("screen", screen)
    .addNode("ask_more", askMore)
    .addNode("retrieve", retrieve)
    .addNode("analyze", analyze)
    .addNode("doctor_review", doctorReview)
    .addNode("revise", revise)
    .addNode("finalize_report", finalizeReport)
    .addNode("case_closed", caseClosed)
    .addEdge(START, "intake")
    .addConditionalEdges("intake", routeAfterIntake, ["screen", "intake_llm"])
    .addEdge("intake_llm", "screen")
    .addConditionalEdges("screen", routeAfterScreen, ["ask_more", "retrieve"])
    .addEdge("ask_more", "screen")
    .addEdge("retrieve", "analyze")
    .addEdge("analyze", "doctor_review")
    .addConditionalEdges("doctor_review", routeAfterReview, ["revise", "finalize_report", "case_closed"])
    .addEdge("revise", "doctor_review")
    .addEdge("finalize_report", END)
    .addEdge("case_closed", END)
    .compile({ checkpointer });

  log.info(
    "肤智（DermAgent） graph compiled: intake → screen →(ask_more)→ retrieve → analyze → doctor_review →(revise|finalize_report|case_closed) → END",
    { systemPrompt: Boolean(agentPrompt) }
  );
  return graph;
}

/**
 * 单次 invoke（首条输入跑到 interrupt 为止；多轮续跑需 surface + checkpointer + Command resume）。
 *
 * @internal 仅测试 / 便利调用使用；生产路径（CLI / ACP）走 surface + createStatefulFlow。
 */
export async function executeFlow(
  input: string,
  deps: {
    allTools?: StructuredTool[];
    checkpointer: BaseCheckpointSaver;
    config?: AppConfig;
    systemPrompt?: string;
  },
  callbacks: FlowCallbacks = {}
): Promise<{
  output: string;
  steps: string[];
  messages: FlowState["messages"];
  draft: ConsultDraft | null;
  decision: FlowState["decision"];
}> {
  const graph = createFlowGraph({ ...deps, callbacks });
  const threadId = randomUUID();
  const result = (await graph.invoke(
    { input, messages: [] } as unknown as FlowState,
    {
      configurable: { thread_id: threadId },
      ...(callbacks?.signal ? { signal: callbacks.signal } : {}),
    }
  )) as FlowState;
  return {
    output: result.output ?? "",
    steps: result.steps ?? [],
    messages: result.messages ?? [],
    draft: result.draft ?? null,
    decision: result.decision ?? "pending",
  };
}
