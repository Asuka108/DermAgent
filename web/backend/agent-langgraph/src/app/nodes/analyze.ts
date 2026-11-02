/**
 * analyze 节点 —— 生成待审核草稿（bespoke，为何不用 createLlmNode）：
 *
 * 计划 §6.2 要求「非法 JSON 或安全校验失败 → 尝试一次受限修复，仍失败标记未通过系统校验」，
 * 这是**两段式**调用（首次生成 → 失败后至多一次修复），createLlmNode 的单段 parse + fallback
 * 覆盖不了「修复一次」语义，故手写节点；解析/韧性/兜底仍复用 libs 原语（invokeWithResilience /
 * parseJson / extractText / buildFallbackDraft）。
 *
 * R-G008：节点返回 Partial，不 mutate；R-G001：parse 仅当消费 parsed（此处手工 parse）。
 */

import { HumanMessage, SystemMessage, type BaseMessage } from "@langchain/core/messages";
import type { LangGraphRunnableConfig } from "@langchain/langgraph";
import { extractText, parseJson } from "../../libs/nodes/index.js";
import type { LLMLike } from "../../libs/nodes/index.js";
import { resolveLlmResilience, invokeWithResilience } from "../../runtime/services/llm-resilience.js";
import type { AppConfig } from "../../runtime/index.js";
import type { ConsultDraft, FlowState } from "../state.js";
import { tryResolveLlm } from "./derm.js";
import {
  buildFallbackDraft,
  sanitizeDraftCitations,
} from "../logic.js";

const ANALYZE_SYSTEM = [
  "你是皮肤科循证分析引擎，基于病例与知识库检索结果生成待审核草稿。",
  '只输出 JSON 本体（固定 schema，schema_version=1.1，snake_case）：',
  '{"workflow_state":"awaiting_review|escalated",',
  ' "case_summary":{"selected_body_region":str,"symptom_summary":str,"data_completeness":"complete|partial|insufficient"},',
  ' "suspected_conditions":[{"name":str,"confidence":"low|medium|high|unknown","supporting_evidence":[{"statement":str,"citation_ids":[str]}],"missing_information":[str],"red_flags":[str]}],',
  ' "symptom_assessment":[{"symptom":str,"status":"present|absent|unknown|not_asked","basis":"patient_report|doctor_observation|image_description|test_result|inference","citation_ids":[str]}],',
  ' "medication_checks":[{"generic_name":str,"purpose":str,"items_to_verify":["indication","allergy","contraindication","interaction"],"requires_doctor_confirmation":true,"citation_ids":[str]}],',
  ' "severity":{"level":"routine|needs_review|urgent|emergency|unknown","basis":[str],"citation_ids":[str]},',
  ' "recommended_next_steps":[{"action":str,"urgency":"routine|soon|urgent|emergency","citation_ids":[str]}],',
  ' "uncertainties":[str],',
  ' "citations":[{"citation_id":str,"document_title":str,"document_version":str,"source_type":"guideline|encyclopedia|book|authorized_case","page_or_section":str,"quoted_text":str}],',
  ' "disclaimer":str}',
  "硬约束：",
  "1) 鉴别诊断仅供医生核对，措辞用「待排/可能」，confidence 拿不准就填 unknown；",
  "2) 引用只能使用知识库检索结果中存在的 citation_id，禁止编造引用；知识库未覆盖要如实说明（suspected_conditions 可为空数组）；",
  "3) status=unknown/not_asked 不得写成 absent；",
  "4) medication_checks 只列核对项，禁止自动处方、剂量与疗程；",
  "5) 只输出 JSON 本体，不要围栏以外的任何文字；简体中文。",
].join("\n");

const REPAIR_SYSTEM = [
  "你是 JSON 修复器。用户会给出一段非法或不符合 schema 的 JSON 输出，",
  "请在不改变原意的前提下修复为合法 JSON 并只输出 JSON 本体（不要围栏、不要解释）。字段枚举值若非法改为 unknown。",
].join("");

function withAgentPrompt(system: string, agentPrompt?: string): string {
  return agentPrompt ? `${system}\n\n# 智能体身份与约束\n${agentPrompt}` : system;
}

export function createAnalyzeNode(appConfig?: AppConfig, agentPrompt?: string) {
  const { longTimeoutMs } = resolveLlmResilience(appConfig);
  return async (state: FlowState, lgConfig?: LangGraphRunnableConfig): Promise<Partial<FlowState>> => {
    const citations = state.citations;
    const redFlags = state.redFlags;
    const missing = state.missing;
    const caseJson = JSON.stringify(state.case, null, 2);
    const humanText = [
      `病例：${caseJson}`,
      `知识库检索结果（引用只能从中选取，citationId 白名单）：${JSON.stringify(citations.map((c) => c.citation_id))}`,
      `检索片段：${JSON.stringify(citations, null, 2)}`,
      `红旗筛查：${redFlags.join("；") || "无"}`,
      `缺失信息：${missing.join("；") || "无"}`,
    ].join("\n\n");
    const messages: BaseMessage[] = [
      new SystemMessage(withAgentPrompt(ANALYZE_SYSTEM, agentPrompt)),
      new HumanMessage(humanText),
    ];

    const sanitize = (parsed: Partial<ConsultDraft>, generated_by: string) =>
      sanitizeDraftCitations({ ...parsed, generated_by }, citations, redFlags, missing, {
        modelVersion: appConfig?.model?.name ?? "unknown",
      });

    const model: LLMLike | null = tryResolveLlm(appConfig);
    if (!model) {
      return {
        draft: buildFallbackDraft(state.case, citations, redFlags, missing, {
          reviewNote: "无模型凭证：规则生成合成演示草稿",
        }),
        steps: ["analyze:无模型凭证 → 规则生成合成演示草稿"],
      };
    }

    try {
      const ai = await invokeWithResilience(model, messages, {
        timeoutMs: longTimeoutMs,
        label: "analyze",
        retryLabel: "analyze 生成草稿",
        useSharedLimiter: true,
        config: appConfig,
        signal: lgConfig?.signal,
      });
      const text = extractText(ai.content);
      try {
        const parsed = parseJson<Partial<ConsultDraft>>(text);
        return { draft: sanitize(parsed, "llm"), steps: ["analyze:生成待审核草稿（LLM）"] };
      } catch {
        // 受限修复：至多一次；仍失败 → 标记未通过系统校验（不得当作正常医疗建议展示）
        try {
          const ai2 = await invokeWithResilience(model, [
            new SystemMessage(REPAIR_SYSTEM),
            new HumanMessage(text),
          ], {
            timeoutMs: longTimeoutMs,
            label: "analyze.repair",
            retryLabel: "analyze 受限修复",
            useSharedLimiter: true,
            attempts: 1,
            config: appConfig,
            signal: lgConfig?.signal,
          });
          const parsed2 = parseJson<Partial<ConsultDraft>>(extractText(ai2.content));
          const draft = sanitize(parsed2, "llm-repaired");
          draft.uncertainties.push("原始模型输出为非法 JSON，经一次受限修复后通过结构化校验");
          return { draft, steps: ["analyze:LLM 输出经一次受限修复后通过校验"] };
        } catch {
          const draft = buildFallbackDraft(state.case, citations, redFlags, missing, {
            generatedBy: "llm-invalid",
            reviewNote: "未通过系统校验：模型输出非法 JSON 且一次受限修复失败",
          });
          draft.uncertainties.push("未通过系统校验：模型输出非法 JSON 且一次受限修复失败，本草稿为规则降级稿");
          return { draft, steps: ["analyze:非法 JSON 修复失败 → 标记未通过系统校验（规则降级稿）"] };
        }
      }
    } catch (err) {
      return {
        draft: buildFallbackDraft(state.case, citations, redFlags, missing, {
          reviewNote: `模型调用失败（${String(err).slice(0, 120)}）：规则降级稿`,
        }),
        steps: ["analyze:模型调用失败 → 规则生成合成演示草稿"],
      };
    }
  };
}
