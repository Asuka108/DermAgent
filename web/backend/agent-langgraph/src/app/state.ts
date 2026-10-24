/**
 * 肤智（DermAgent） flow 的状态（皮肤科智能问诊与循证辅助 · HITL 图）。
 *
 * messages:  标准消息流（MessagesAnnotation.spec）——保留 checkpointer / 修复 / 压缩兼容。
 * input:     本轮用户输入（首条 = 病例描述或合成案例指令；resume 轮不重写）。
 * output:    最终回答（报告 / 拒绝收尾文案），供 surface toResult 读取。
 * case:      结构化问诊九要素 + 脱敏照片说明。
 * missing:   缺失必填项（中文标签）。
 * redFlags:  红旗筛查命中（编码 + 描述，命中即「升级人工审核」）。
 * askCount:  补充资料轮次（防 ask_more ↔ screen 无限循环）。
 * citations: 本地合成知识库检索引用（检索快照，引用白名单的唯一来源）。
 * draft:     待审核草稿（参赛计划 §6.1 固定 JSON 契约）。
 * feedback / decision: 医生审核意见与决定（confirmed / modified / rejected / pending）。
 * report:    医生确认后的正式报告。
 * steps:     人类可读轨迹（便于测试 / 展示）。
 */

import { Annotation, MessagesAnnotation } from "@langchain/langgraph";

const lastValue = <T>(_: T, n: T): T => n;

/** 结构化问诊九要素 + 照片说明（v1.1 契约，snake_case）。 */
export interface CaseRecord {
  body_region: string;
  chief_complaint: string;
  symptoms: string;
  duration: string;
  trend: string;
  triggers: string;
  past_history: string;
  allergies: string;
  current_medications: string;
  photo_note: string;
}

/** 知识库引用（§6.3 v1.1）：引用快照白名单的唯一来源。 */
export interface Citation {
  citation_id: string;
  document_title: string;
  document_version: string;
  source_type: "guideline" | "encyclopedia" | "book" | "authorized_case";
  page_or_section: string;
  quoted_text: string;
}

export type Confidence = "low" | "medium" | "high" | "unknown";
export type SeverityLevel = "routine" | "needs_review" | "urgent" | "emergency" | "unknown";
export type SymptomStatus = "present" | "absent" | "unknown" | "not_asked";
export type SymptomBasis =
  | "patient_report"
  | "doctor_observation"
  | "image_description"
  | "test_result"
  | "inference";
export type Urgency = "routine" | "soon" | "urgent" | "emergency";
export type DataCompleteness = "complete" | "partial" | "insufficient";
export type WorkflowState =
  | "awaiting_follow_up"
  | "escalated"
  | "awaiting_review"
  | "approved"
  | "rejected"
  | "blocked";
export type ReviewStatus = "pending" | "approved" | "edited" | "rejected";

export interface FollowUpQuestion {
  question: string;
  display_priority: "blocking" | "high" | "normal";
  action_type: "ask_user" | "doctor_confirm";
}

export interface EvidenceItem {
  statement: string;
  citation_ids: string[];
}

export interface SuspectedCondition {
  name: string;
  confidence: Confidence;
  supporting_evidence: EvidenceItem[];
  missing_information: string[];
  red_flags: string[];
}

export interface SymptomAssessmentItem {
  symptom: string;
  status: SymptomStatus;
  basis: SymptomBasis;
  citation_ids: string[];
}

export interface MedicationCheck {
  generic_name: string;
  purpose: string;
  items_to_verify: string[];
  requires_doctor_confirmation: boolean;
  citation_ids: string[];
}

export interface NextStep {
  action: string;
  urgency: Urgency;
  citation_ids: string[];
}

/** 固定 JSON 草稿契约（参赛计划 §6.3，schema_version 1.1）——结果只能是待审核草稿。 */
export interface ConsultDraft {
  schema_version: string;
  workflow_state: WorkflowState;
  blocking_reason: string | null;
  disclaimer: string;
  case_summary: {
    selected_body_region: string;
    symptom_summary: string;
    data_completeness: DataCompleteness;
  };
  follow_up_questions: FollowUpQuestion[];
  suspected_conditions: SuspectedCondition[];
  symptom_assessment: SymptomAssessmentItem[];
  medication_checks: MedicationCheck[];
  severity: {
    level: SeverityLevel;
    basis: string[];
    citation_ids: string[];
  };
  red_flag_escalation: {
    triggered: boolean;
    reasons: string[];
    recommended_action: string;
    is_blocking: boolean;
  };
  recommended_next_steps: NextStep[];
  uncertainties: string[];
  citations: Citation[];
  model_version: string;
  knowledge_snapshot: string;
  safety_policy_version: string;
  generated_at: string;
  review_status: ReviewStatus;
  requires_doctor_confirmation: true;
  /** llm / llm-repaired / rule-fallback / llm-invalid。 */
  generated_by: string;
  /** 结构化校验未通过时的显式标记。 */
  review_note?: string;
}

export type ReviewDecision = "confirmed" | "modified" | "rejected" | "pending";

export const FlowStateAnnotation = Annotation.Root({
  ...MessagesAnnotation.spec,
  input: Annotation<string>({ value: lastValue<string>, default: () => "" }),
  output: Annotation<string>({ value: lastValue<string>, default: () => "" }),
  case: Annotation<CaseRecord | null>({ value: lastValue<CaseRecord | null>, default: () => null }),
  missing: Annotation<string[]>({ value: lastValue<string[]>, default: () => [] }),
  redFlags: Annotation<string[]>({ value: lastValue<string[]>, default: () => [] }),
  askCount: Annotation<number>({ value: lastValue<number>, default: () => 0 }),
  intakeNote: Annotation<string>({ value: lastValue<string>, default: () => "" }),
  citations: Annotation<Citation[]>({ value: lastValue<Citation[]>, default: () => [] }),
  draft: Annotation<ConsultDraft | null>({ value: lastValue<ConsultDraft | null>, default: () => null }),
  feedback: Annotation<string>({ value: lastValue<string>, default: () => "" }),
  decision: Annotation<ReviewDecision>({ value: lastValue<ReviewDecision>, default: () => "pending" }),
  report: Annotation<string>({ value: lastValue<string>, default: () => "" }),
  steps: Annotation<string[]>({
    reducer: (prev, next) => [...prev, ...next],
    default: () => [],
  }),
});

export type FlowState = typeof FlowStateAnnotation.State;
