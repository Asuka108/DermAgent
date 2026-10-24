/**
 * 肤智（DermAgent）纯业务逻辑（v1.1 契约，与 shared/derm_core.py / webui 保持同构）。
 *
 * 状态机（§5.1）：escalated > blocked > awaiting_review；引用白名单、unknown/not_asked 语义、
 * 否定词感知检索、severity 本地权威派生等约束全部引擎侧强制。
 */

import type {
  CaseRecord, SuspectedCondition, MedicationCheck, NextStep,
  Citation,
  ConsultDraft,
  DataCompleteness,
  FlowState,
  ReviewDecision,
  SeverityLevel,
  SymptomAssessmentItem,
  SymptomBasis,
  SymptomStatus,
} from "./state.js";
import { KB_LABEL, KB_SNAPSHOT, SYNTHETIC_KB } from "./knowledge/kb.js";

export const SCHEMA_VERSION = "1.1";
export const SAFETY_POLICY_VERSION = "sp-1.0";
export const DEFAULT_DISCLAIMER =
  "AI 生成内容仅供医生决策支持，不构成诊断或处方；结果可能存在遗漏或错误，" +
  "鉴别诊断仅供医生核对，用药信息禁止自动处方、剂量与疗程建议，必须由专业人员结合实际情况独立判断。";

/** 必填字段（含别名，用于「字段：值」抽取）。变化趋势为选填追问项。 */
export const REQUIRED_FIELDS: Array<{ key: keyof CaseRecord; label: string; aliases: string[] }> = [
  { key: "body_region", label: "身体区域", aliases: ["身体区域", "部位", "皮损部位"] },
  { key: "chief_complaint", label: "主诉", aliases: ["主诉"] },
  { key: "symptoms", label: "症状", aliases: ["症状", "皮损表现", "表现"] },
  { key: "duration", label: "病程", aliases: ["病程", "持续时间", "病了多久"] },
  { key: "triggers", label: "诱因", aliases: ["诱因", "诱发因素"] },
  { key: "past_history", label: "既往史", aliases: ["既往史", "病史"] },
  { key: "allergies", label: "过敏史", aliases: ["过敏史", "过敏"] },
  { key: "current_medications", label: "当前用药", aliases: ["当前用药", "用药", "药物"] },
];

/** 字段有值即视为已答（「无/否认/无特殊」等否定答复也是有效答案）。 */
export function isFilled(value: string | undefined | null): boolean {
  return Boolean(value && value.trim());
}

/** 必填校验：返回缺失字段的中文标签列表。 */
export function computeMissing(c: CaseRecord | null): string[] {
  if (!c) return REQUIRED_FIELDS.map((f) => f.label);
  return REQUIRED_FIELDS.filter((f) => !isFilled(c[f.key])).map((f) => f.label);
}

/** 资料完备度（§6.3 case_summary.data_completeness）。 */
export function deriveDataCompleteness(case_: CaseRecord | null, missing: string[]): DataCompleteness {
  const n = missing.length;
  if (n === 0) return "complete";
  if (!case_ || n >= REQUIRED_FIELDS.length) return "insufficient";
  return n > REQUIRED_FIELDS.length / 2 ? "insufficient" : "partial";
}

/** 红旗筛查（规则版，命中即升级人工审核）。 */
export function detectRedFlags(c: CaseRecord | null): string[] {
  if (!c) return [];
  const text = [c.body_region, c.chief_complaint, c.symptoms, c.trend].join("；");
  const flags: string[] = [];
  const has = (p: string) => new RegExp(p).test(text);
  if (has("水疱|水泡") && has("单侧|一侧|神经|皮节|簇集|带状|肋间") && has("灼痛|刺痛|神经痛|触痛|疼痛|敏感")) {
    flags.push("RF-HZ-001：单侧神经分布簇集水疱伴神经痛——带状疱疹可能，升级人工审核（知识库 DOC-HZ-001 红旗清单）");
  }
  if (has("眼周|眼睑|眼部|鼻尖") && has("水疱|水泡|红肿|疼痛|受累|皮疹")) {
    flags.push("RF-HZ-002：眼周/鼻尖受累——眼部带状疱疹风险待排，升级人工审核");
  }
  if (has("发热|发烧") && has("脓疱|糜烂|渗出|红肿|扩散|疼痛加剧|蜂窝织")) {
    flags.push("RF-INF-001：发热伴感染征象——继发感染待排，升级人工审核");
  }
  if (has("紫癜|瘀点|瘀斑")) {
    flags.push("RF-VAS-001：紫癜/瘀点瘀斑——血管炎或血液系统疾病待排，升级人工审核");
  }
  if (has("呼吸困难|吞咽困难|喉头水肿|声音嘶哑")) {
    flags.push("RF-ANG-001：呼吸道/吞咽受累征象——血管性水肿风险，升级人工审核");
  }
  if (has("大疱|表皮剥脱|尼氏征|剥脱性皮炎|全身红斑|弥漫红肿")) {
    flags.push("RF-SC-001：重症药疹/大疱性疾病征象待排（SJS/TEN 等），升级人工审核");
  }
  return [...new Set(flags)];
}

/** 严重程度枚举（§6.3）：红旗按最高级别映射；无红旗且资料齐全 → routine；资料不全 → unknown。 */
export function deriveSeverityLevel(redFlags: string[], missing: string[]): SeverityLevel {
  if (redFlags.length === 0) return missing.length > 0 ? "unknown" : "routine";
  const codes = redFlags.map((f) => f.split("：")[0] ?? "");
  if (codes.includes("RF-ANG-001")) return "emergency";
  if (codes.includes("RF-INF-001") || codes.includes("RF-SC-001")) return "urgent";
  return "needs_review";
}

/** 关键词命中且前面（≤4 字窗口）无否定词（避免「无疼痛」「否认过敏史」误报）。 */
function keywordHit(text: string, keyword: string): boolean {
  let from = 0;
  for (;;) {
    const idx = text.indexOf(keyword, from);
    if (idx === -1) return false;
    if (!/无|未|不|没|否认|未见/.test(text.slice(Math.max(0, idx - 4), idx))) return true;
    from = idx + keyword.length;
  }
}

/**
 * 本地合成知识库检索（关键词计分 → 引用快照，v1.1 snake_case）。
 * 为何不用 createMcpRetrievalNode：知识库为本地合成 fixture（平台无已审核医学知识库命中，见 project.md）；
 * 真实部署时替换为平台 Knowledge 工具。
 */
export function retrieveCitations(c: CaseRecord | null, maxDocs = 3, maxSectionsPerDoc = 2): Citation[] {
  if (!c) return [];
  const text = [
    c.body_region, c.chief_complaint, c.symptoms, c.trend, c.triggers, c.past_history, c.current_medications,
  ].join("；");
  const scored = SYNTHETIC_KB.map((doc) => {
    const hits = doc.sections
      .map((section, idx) => ({ idx, section, score: section.keywords.filter((kw) => keywordHit(text, kw)).length }))
      .filter((h) => h.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, maxSectionsPerDoc);
    return { doc, hits, total: hits.reduce((acc, h) => acc + h.score, 0) };
  })
    .filter((d) => d.total > 0)
    .sort((a, b) => b.total - a.total)
    .slice(0, maxDocs);
  const citations: Citation[] = [];
  for (const { doc, hits } of scored) {
    for (const h of hits) {
      citations.push({
        citation_id: `CIT-${doc.docId.replace(/^DOC-/, "")}-S${h.idx + 1}`,
        document_title: doc.docName,
        document_version: doc.version,
        source_type: doc.sourceType,
        page_or_section: h.section.title,
        quoted_text: h.section.content,
      });
    }
  }
  return citations;
}

const ESCALATION_ACTIONS: Record<string, string> = {
  "RF-ANG-001": "立即评估气道与生命体征，紧急处置",
  "RF-SC-001": "立即评估表皮剥脱范围与黏膜受累，考虑紧急转诊",
  "RF-INF-001": "评估感染范围与生命体征，考虑系统抗感染治疗（由医生决定）",
};

const STATUS_LABEL: Record<SymptomStatus, string> = {
  present: "有",
  absent: "无（医生确认）",
  unknown: "未知（不得当作「没有」）",
  not_asked: "未询问（不得当作「没有」）",
};

export function followUpQuestions(missing: string[]): ConsultDraft["follow_up_questions"] {
  return missing.map((m) => ({ question: `请补充：${m}`, display_priority: "blocking" as const, action_type: "ask_user" as const }));
}

/** 由病例 + 检索快照构造症状评估（not_asked 显式保留原义）。 */
export function buildSymptomAssessment(c: CaseRecord | null, citations: Citation[]): SymptomAssessmentItem[] {
  if (!c) return [];
  const citeIds = citations.slice(0, 1).map((x) => x.citation_id);
  const items: SymptomAssessmentItem[] = [];
  const add = (symptom: string, filled: boolean, basis: SymptomBasis) => {
    items.push({
      symptom,
      status: (filled ? "present" : "not_asked") as SymptomStatus,
      basis: (filled ? basis : "inference") as SymptomBasis,
      citation_ids: filled ? citeIds : [],
    });
  };
  for (const { key, label } of REQUIRED_FIELDS) {
    add(`${label}：${c[key] || "未询问"}`, isFilled(c[key]), "patient_report");
  }
  if (isFilled(c.trend)) add(`变化趋势：${c.trend}`, true, "patient_report");
  if (isFilled(c.photo_note)) {
    items.push({ symptom: `照片（脱敏/合成）：${c.photo_note}`, status: "present", basis: "image_description", citation_ids: [] });
  }
  return items;
}

/** 用药核对项（§6.4：只列核对项，禁止自动处方、剂量与疗程）。 */
export function buildMedicationChecks(c: CaseRecord | null, citations: Citation[]): ConsultDraft["medication_checks"] {
  if (!c || !isFilled(c.current_medications)) return [];
  const citeIds = citations.map((x) => x.citation_id).slice(0, 2);
  const names = c.current_medications.split(/[、，,;；/]/).map((s) => s.trim())
    .filter((s) => s && !/^(无|未使用(药物)?|未用药|否认)$/.test(s)).slice(0, 4);
  const docTitles = new Set(citations.map((x) => x.document_title));
  const notes = SYNTHETIC_KB.filter((d) => docTitles.has(d.docName)).flatMap((d) => d.medReviewNotes);
  return names.map((name) => ({
    generic_name: name,
    purpose: notes[0] ?? "医生核对药物与病情的相关性（禁止自动处方、剂量与疗程）",
    items_to_verify: ["indication", "allergy", "contraindication", "interaction"],
    requires_doctor_confirmation: true,
    citation_ids: citeIds,
  }));
}

function workflowStateOf(redFlags: string[]): ConsultDraft["workflow_state"] {
  return redFlags.length > 0 ? "escalated" : "awaiting_review";
}

function blockingReasonOf(redFlags: string[]): string | null {
  return redFlags.length > 0
    ? `命中红旗 ${redFlags[0]!.split("：")[0]}，普通分析流程被阻断，须医生人工审核`
    : null;
}

/** 无模型凭证 / 调用失败 / 校验失败时的规则版草稿（确定性生成，标记 generated_by）。 */
export function buildFallbackDraft(
  c: CaseRecord | null,
  citations: Citation[],
  redFlags: string[],
  missing: string[],
  opts: { generatedBy?: string; modelVersion?: string; reviewNote?: string } = {}
): ConsultDraft {
  const docTitles = new Set(citations.map((x) => x.document_title));
  const dxSeen = new Set<string>();
  const suspected: ConsultDraft["suspected_conditions"] = [];
  for (const doc of SYNTHETIC_KB) {
    if (!docTitles.has(doc.docName)) continue;
    for (const dx of doc.suggestedDx) {
      if (!dxSeen.has(dx)) {
        dxSeen.add(dx);
        const cite = citations.find((x) => x.document_title === doc.docName);
        suspected.push({
          name: dx,
          confidence: "low",
          supporting_evidence: [{
            statement: cite ? `知识库匹配：${cite.page_or_section}` : "知识库匹配",
            citation_ids: cite ? [cite.citation_id] : [],
          }],
          missing_information: [...missing],
          red_flags: [...redFlags],
        });
      }
    }
  }
  const uncertainties = ["本草稿由规则引擎生成（合成演示模式，未经模型生成）"];
  if (opts.reviewNote) uncertainties.push(opts.reviewNote);
  if (citations.length === 0) {
    uncertainties.push("知识库未覆盖本病例主题，结论缺乏已审核证据支撑，不足以判断，请医生自行评估");
  }
  uncertainties.push(`证据来源：${KB_LABEL}`);
  const severityLevel = deriveSeverityLevel(redFlags, missing);
  const nextAction = redFlags.length > 0
    ? "红旗升级人工审核：由主诊医生复核评估后再决定处理"
    : citations.length === 0
      ? "知识库未覆盖，建议皮肤科门诊面诊评估（本草稿不足以判断）"
      : "结合引文核对鉴别诊断与用药，由医生决定下一步";
  const urgency = redFlags.some((f) => f.startsWith("RF-ANG"))
    ? ("emergency" as const)
    : redFlags.length > 0 ? ("soon" as const) : ("routine" as const);
  const symptomSummary = c
    ? `${c.chief_complaint || "主诉未提供"}；症状：${c.symptoms || "未提供"}；病程：${c.duration || "未提供"}`
    : "病例信息缺失";
  const draft: ConsultDraft = {
    schema_version: SCHEMA_VERSION,
    workflow_state: workflowStateOf(redFlags),
    blocking_reason: blockingReasonOf(redFlags),
    disclaimer: DEFAULT_DISCLAIMER,
    case_summary: {
      selected_body_region: c?.body_region ?? "",
      symptom_summary: symptomSummary,
      data_completeness: deriveDataCompleteness(c, missing),
    },
    follow_up_questions: followUpQuestions(missing),
    suspected_conditions: suspected,
    symptom_assessment: buildSymptomAssessment(c, citations),
    medication_checks: buildMedicationChecks(c, citations),
    severity: {
      level: severityLevel,
      basis: [
        redFlags.length > 0 ? "命中红旗规则，需人工审核" : "基于症状与病程的初步评估（仅供医生参考）",
        ...(missing.length > 0 ? [`资料不全（缺失 ${missing.length} 项）`] : []),
      ],
      citation_ids: citations.slice(0, 1).map((x) => x.citation_id),
    },
    red_flag_escalation: {
      triggered: redFlags.length > 0,
      reasons: [...redFlags],
      recommended_action: redFlags.length > 0
        ? ESCALATION_ACTIONS[redFlags[0]!.split("：")[0] ?? ""] ?? "升级人工审核：由主诊医生复核后再继续"
        : "未触发红旗，按常规医生审核流程处理",
      is_blocking: redFlags.length > 0,
    },
    recommended_next_steps: [{ action: nextAction, urgency, citation_ids: citations.slice(0, 1).map((x) => x.citation_id) }],
    uncertainties: [...new Set(uncertainties)],
    citations: [...citations],
    model_version: opts.modelVersion ?? "rule-engine@demo",
    knowledge_snapshot: KB_SNAPSHOT,
    safety_policy_version: SAFETY_POLICY_VERSION,
    generated_at: new Date().toISOString(),
    review_status: "pending",
    requires_doctor_confirmation: true,
    generated_by: opts.generatedBy ?? "rule-fallback",
  };
  return draft;
}

/**
 * LLM 输出归一化（§6.4 服务端校验）：枚举收拢、引用白名单（含结论内 citation_ids）、
 * workflow_state/is_blocking 本地权威派生、强制元信息字段。
 */
export function normalizeLlmDraft(
  parsed: Partial<ConsultDraft> & Record<string, unknown>,
  citations: Citation[],
  redFlags: string[],
  missing: string[],
  modelVersion: string,
  generatedBy: string
): ConsultDraft {
  const allowed = new Set(citations.map((x) => x.citation_id));
  let dropped = 0;
  const s = (v: unknown, fb = ""): string => (typeof v === "string" && v.trim() ? v.trim() : fb);
  const ids = (v: unknown): string[] => {
    const raw = (Array.isArray(v) ? v : []).map(String).filter(Boolean);
    const out = raw.filter((id) => allowed.has(id));
    dropped += raw.length - out.length;
    return out;
  };
  const arr = (v: unknown): string[] => (Array.isArray(v) ? v.map(String).filter(Boolean) : []);
  const pick = (...keys: string[]) => {
    for (const k of keys) if (parsed[k] !== undefined) return parsed[k];
    return undefined;
  };

  const conditions = (Array.isArray(pick("suspected_conditions"))
    ? (pick("suspected_conditions") as SuspectedCondition[])
    : []
  ).slice(0, 6).map((c) => {
    const evs = (Array.isArray(c?.supporting_evidence) ? c.supporting_evidence : []).slice(0, 8).map((e) => ({
      statement: s(e?.statement),
      citation_ids: ids(e?.citation_ids),
    }));
    return {
      name: s(c?.name, "待排（未命名）"),
      confidence: (["low", "medium", "high"].includes(String(c?.confidence))
        ? c?.confidence : "unknown") as ConsultDraft["suspected_conditions"][number]["confidence"],
      supporting_evidence: evs,
      missing_information: arr(c?.missing_information),
      red_flags: arr(c?.red_flags),
    };
  });

  const assessment = (Array.isArray(pick("symptom_assessment"))
    ? (pick("symptom_assessment") as SymptomAssessmentItem[])
    : []
  ).slice(0, 12).map((x) => ({
    symptom: s(x?.symptom),
    status: (["present", "absent", "not_asked"].includes(String(x?.status))
      ? x?.status : "unknown") as SymptomStatus,
    basis: (["patient_report", "doctor_observation", "image_description", "test_result"].includes(String(x?.basis))
      ? x?.basis : "inference") as SymptomBasis,
    citation_ids: ids(x?.citation_ids),
  }));

  const meds = (Array.isArray(pick("medication_checks"))
    ? (pick("medication_checks") as MedicationCheck[])
    : []
  ).slice(0, 6).map((m) => ({
    generic_name: s(m?.generic_name),
    purpose: s(m?.purpose, "医生核对药物与病情的相关性"),
    items_to_verify: arr(m?.items_to_verify).length
      ? arr(m?.items_to_verify)
      : ["indication", "allergy", "contraindication", "interaction"],
    requires_doctor_confirmation: true,
    citation_ids: ids(m?.citation_ids ?? m?.citation_ids),
  }));

  const nextSteps = (Array.isArray(pick("recommended_next_steps"))
    ? (pick("recommended_next_steps") as NextStep[])
    : []
  ).slice(0, 6).map((n) => ({
    action: s(n?.action),
    urgency: (["routine", "soon", "urgent", "emergency"].includes(String(n?.urgency))
      ? n?.urgency : "routine") as ConsultDraft["recommended_next_steps"][number]["urgency"],
    citation_ids: ids(n?.citation_ids),
  }));

  const rawCitations = Array.isArray(pick("citations")) ? (pick("citations") as Citation[]) : [];
  dropped += rawCitations.filter((c) => !allowed.has(c?.citation_id)).length;
  const keptCitations = [...citations]; // 引用发布恒为本次检索快照

  const uncertainties = arr(pick("uncertainties"));
  if (dropped > 0) uncertainties.push(`检测到 ${dropped} 条非知识库引用，已按「禁止编造引用」约束剔除`);
  if (citations.length === 0) {
    uncertainties.push("知识库未覆盖本病例主题，结论缺乏已审核证据支撑，不足以判断，请医生自行评估");
  }
  uncertainties.push(`证据来源：${KB_LABEL}`);
  const cs = (pick("case_summary") ?? {}) as Partial<ConsultDraft["case_summary"]>;

  return {
    schema_version: SCHEMA_VERSION,
    workflow_state: workflowStateOf(redFlags),
    blocking_reason: blockingReasonOf(redFlags),
    disclaimer: s(pick("disclaimer"), DEFAULT_DISCLAIMER) || DEFAULT_DISCLAIMER,
    case_summary: {
      selected_body_region: s(cs?.selected_body_region),
      symptom_summary: s(cs?.symptom_summary),
      data_completeness: missing.length === 0 ? "complete" : deriveDataCompleteness(null, missing),
    },
    follow_up_questions: followUpQuestions(missing),
    suspected_conditions: conditions,
    symptom_assessment: assessment,
    medication_checks: meds,
    severity: {
      level: deriveSeverityLevel(redFlags, missing), // 本地权威派生
      basis: arr((pick("severity") as ConsultDraft["severity"] | undefined)?.basis).length
        ? arr((pick("severity") as ConsultDraft["severity"] | undefined)?.basis)
        : [redFlags.length > 0 ? "命中红旗规则，需人工审核" : "基于症状与病程的初步评估（仅供医生参考）"],
      citation_ids: ids((pick("severity") as ConsultDraft["severity"] | undefined)?.citation_ids),
    },
    red_flag_escalation: {
      triggered: redFlags.length > 0,
      reasons: [...redFlags],
      recommended_action: redFlags.length > 0
        ? ESCALATION_ACTIONS[redFlags[0]!.split("：")[0] ?? ""] ?? "升级人工审核：由主诊医生复核后再继续"
        : "未触发红旗，按常规医生审核流程处理",
      is_blocking: redFlags.length > 0,
    },
    recommended_next_steps: nextSteps,
    uncertainties: [...new Set(uncertainties)],
    citations: keptCitations,
    model_version: modelVersion,
    knowledge_snapshot: KB_SNAPSHOT,
    safety_policy_version: SAFETY_POLICY_VERSION,
    generated_at: new Date().toISOString(),
    review_status: "pending",
    requires_doctor_confirmation: true,
    generated_by: generatedBy,
  };
}

/** 兼容旧调用点（graph revise / analyze fallback）：等价于 normalizeLlmDraft。 */
export function sanitizeDraftCitations(
  draft: Partial<ConsultDraft> & { generated_by?: string },
  citations: Citation[],
  redFlags: string[],
  missing: string[],
  opts: { modelVersion?: string } = {}
): ConsultDraft {
  return normalizeLlmDraft(draft, citations, redFlags, missing, opts.modelVersion ?? "unknown", String(draft.generated_by ?? "llm"));
}

export const CONFIRM_RE =
  /^(ok|okay|确认|确定|同意|通过|批准|confirm(ed)?|approved?|yes|好的?|lgtm)([\s!！。.~，,].*)?$/i;

/** 医生审核决定分类：拒绝 > 确认 > 修改。 */
export function classifyReview(feedback: string, approved: boolean): ReviewDecision {
  const fb = feedback.trim();
  if (/^(拒绝|否决|驳回|reject)/i.test(fb)) return "rejected";
  if (approved || CONFIRM_RE.test(fb)) return "confirmed";
  return "modified";
}

type FieldSpec = { key: keyof CaseRecord; label: string; aliases?: string[] };

function buildAliasMap(fields: FieldSpec[]): Map<string, keyof CaseRecord> {
  const map = new Map<string, keyof CaseRecord>();
  for (const f of fields) {
    map.set(f.label, f.key);
    for (const a of f.aliases ?? []) map.set(a, f.key);
  }
  return map;
}

function matchAliasPrefix(seg: string, aliasMap: Map<string, keyof CaseRecord>): { key: keyof CaseRecord; value: string } | null {
  for (const alias of [...aliasMap.keys()].sort((a, b) => b.length - a.length)) {
    if (seg.startsWith(alias)) {
      const value = seg.slice(alias.length).replace(/^[\s:：]*/, "").trim();
      if (value) return { key: aliasMap.get(alias)!, value };
    }
  }
  return null;
}

/** 「字段：值」抽取（兼容「补充：病程5天」嵌套与「变化趋势逐渐扩大」无冒号形态）。 */
export function extractFields(text: string, fields: FieldSpec[]) {
  const aliasMap = buildAliasMap(fields);
  const pairs: Array<{ key: keyof CaseRecord; value: string }> = [];
  const leftovers: string[] = [];
  for (const seg of text.split(/[;；\n。！？]/).map((x) => x.trim()).filter(Boolean)) {
    const stripped = seg.replace(/^补充\s*[:：]?\s*/, "").trim();
    if (!stripped) continue;
    if (stripped.includes("：") || stripped.includes(":")) {
      const idx = stripped.search(/[:：]/);
      const head = stripped.slice(0, idx).trim();
      const value = stripped.slice(idx + 1).trim();
      const key = aliasMap.get(head);
      if (key && value) { pairs.push({ key, value }); continue; }
      const nested = matchAliasPrefix(value, aliasMap);
      if (nested) { pairs.push(nested); continue; }
      leftovers.push(seg);
      continue;
    }
    const byPrefix = matchAliasPrefix(stripped, aliasMap);
    if (byPrefix) { pairs.push(byPrefix); continue; }
    leftovers.push(seg);
  }
  return { pairs, leftovers };
}

export function emptyCase(): CaseRecord {
  return {
    body_region: "", chief_complaint: "", symptoms: "", duration: "", trend: "",
    triggers: "", past_history: "", allergies: "", current_medications: "", photo_note: "",
  };
}

/** 自由文本 → 病例（规则抽取：字段对 + 其余并入症状）。 */
export function buildCaseFromFreeText(input: string): CaseRecord {
  const c = emptyCase();
  const { pairs, leftovers } = extractFields(input, REQUIRED_FIELDS);
  for (const { key, value } of pairs) c[key] = value;
  const rest = leftovers.join("；").trim();
  if (!isFilled(c.symptoms) && rest) c.symptoms = rest.slice(0, 300);
  return c;
}

/** 把补充回复合并进病例。 */
export function mergeCaseReply(c: CaseRecord, reply: string): CaseRecord {
  const merged: CaseRecord = { ...c };
  const fields: FieldSpec[] = [
    ...REQUIRED_FIELDS,
    { key: "trend", label: "变化趋势", aliases: ["变化", "趋势"] },
  ];
  const { pairs, leftovers } = extractFields(reply, fields);
  for (const { key, value } of pairs) merged[key] = value;
  const rest = leftovers.join("；").replace(/^补充\s*[:：]?\s*/, "").trim();
  if (rest) {
    merged.symptoms = isFilled(merged.symptoms) ? `${merged.symptoms}；补充：${rest.slice(0, 200)}` : rest.slice(0, 200);
  }
  return merged;
}

// —— 路由纯函数（条件边返回值必须 ∈ targets，R-G004） ——

export function routeAfterIntake(s: FlowState): "screen" | "intake_llm" {
  return s.case ? "screen" : "intake_llm";
}

export const MAX_ASK_ROUNDS = 2;

export function routeAfterScreen(s: FlowState): "ask_more" | "retrieve" {
  return s.missing.length > 0 && s.askCount < MAX_ASK_ROUNDS ? "ask_more" : "retrieve";
}

export function routeAfterReview(s: FlowState): "finalize_report" | "revise" | "case_closed" {
  if (s.decision === "confirmed") return "finalize_report";
  if (s.decision === "rejected") return "case_closed";
  return "revise";
}

// —— 渲染 ——

export function renderAskQuestion(missing: string[], note?: string): string {
  const lines = [
    "📋 **资料不足（workflow_state=awaiting_follow_up）**",
    "",
    "以下必填信息缺失，请按「字段：值」补充（可一次补全多项，如「病程：3天；过敏史：否认」）：",
    ...missing.map((m) => `- ${m}`),
    "",
    note ? `💡 ${note}` : "",
    "",
    "为保障演示完整，连续两轮未补全后将带缺失项继续（草稿会明确标注缺失信息，仍需医生审核）。",
  ];
  return lines.filter((l) => l !== "").join("\n");
}

export function renderReviewQuestion(draft: ConsultDraft | null, redFlags: string[]): string {
  if (!draft) return "草稿生成失败，请重新发起问诊。";
  const banner: string[] = [];
  if (redFlags.length > 0) {
    banner.push(`🚩 **红旗警示（workflow_state=${draft.workflow_state}，阻断普通分析）**\n${redFlags.map((f) => `- ${f}`).join("\n")}`);
  }
  if (draft.generated_by === "llm-invalid") {
    banner.push("⚠️ **本草稿未通过系统校验**，仅作降级演示，不得当作正常医疗建议。");
  }
  if (draft.citations.length === 0) {
    banner.push("ℹ️ **知识库未覆盖本病例主题**：不给出鉴别诊断（不足以判断），不会编造引用。");
  }
  const symptomLines = draft.symptom_assessment
    .map((s) => `- ${s.symptom}｜状态：${STATUS_LABEL[s.status]}｜依据：${s.basis}`)
    .join("\n");
  return [
    ...banner,
    "",
    "📄 **待审核草稿**（仅为草稿，不构成诊疗建议；鉴别诊断仅供医生核对）",
    "",
    "```json",
    JSON.stringify(draft, null, 2),
    "```",
    "",
    "**症状评估（unknown/未询问 ≠ 没有）**：",
    symptomLines || "- 无",
    "",
    "**医生审核操作**：",
    "- 回复「确认」→ 生成辅助报告；",
    "- 回复「修改：你的意见」→ 按意见修订后再次提交审核；",
    "- 回复「拒绝：理由」→ 终止，不生成报告。",
  ].join("\n");
}

/** 修订兜底（无模型）：把医生意见记录进草稿，标记待人工落实。 */
export function buildFallbackRevisedDraft(draft: ConsultDraft, feedback: string): ConsultDraft {
  return {
    ...draft,
    uncertainties: [
      ...draft.uncertainties.filter((u) => !u.startsWith("医生修改意见")),
      `医生修改意见待人工落实（合成演示模式无模型修订能力）：${feedback.slice(0, 200)}`,
    ],
    review_status: "edited",
    generated_by: "rule-fallback",
  };
}

/** 辅助报告兜底（无模型）：确定性渲染（unknown/not_asked 不显示为「没有」）。 */
export function buildFallbackReport(draft: ConsultDraft | null): string {
  if (!draft) return "未找到已确认的草稿，无法生成报告。";
  const d = draft.suspected_conditions.map((x) => `- ${x.name}（置信度：${x.confidence}）：${x.supporting_evidence.map((e) => e.statement).join("；") || "无证据条目"}`).join("\n") || "- 知识库未覆盖，不足以判断";
  const assess = draft.symptom_assessment.map((s) => `- ${s.symptom}｜状态：${STATUS_LABEL[s.status]}｜依据：${s.basis}`).join("\n") || "- 无";
  const meds = draft.medication_checks.map((m) => `- ${m.generic_name}：核对项【${m.items_to_verify.join("、") || "适应证/过敏/禁忌/相互作用"}】${m.requires_doctor_confirmation ? "（需医生确认）" : ""}`).join("\n") || "- 无用药信息";
  const next = draft.recommended_next_steps.map((n) => `- ${n.action}（紧急度：${n.urgency}）`).join("\n") || "- 无";
  const cites = draft.citations.map((c) => `- ${c.citation_id}｜${c.document_title}（${c.document_version}，${c.source_type}）§${c.page_or_section}：「${c.quoted_text}」`).join("\n") || "- 知识库未覆盖";
  const missing = draft.suspected_conditions[0]?.missing_information ?? [];
  return [
    "# 肤智（DermAgent）辅助报告（医生已确认）",
    "",
    `> Schema ${draft.schema_version}｜工作流状态 ${draft.workflow_state}｜模型 ${draft.model_version}｜知识库快照 ${draft.knowledge_snapshot}｜安全策略 ${draft.safety_policy_version}｜生成时间 ${draft.generated_at}`,
    "",
    "## 一、病例摘要",
    `${draft.case_summary.selected_body_region || "（部位未记录）"}：${draft.case_summary.symptom_summary}（资料完备度：${draft.case_summary.data_completeness}）`,
    "",
    "## 二、鉴别诊断（仅供医生核对）", d,
    "",
    "## 三、症状依据", assess,
    "",
    "## 四、用药核对（禁止自动处方、剂量与疗程）", meds,
    "",
    `## 五、严重程度（${draft.severity.level}）`,
    draft.severity.basis.map((b) => `- ${b}`).join("\n") || "- 无",
    "",
    "## 六、红旗与升级",
    draft.red_flag_escalation.triggered
      ? [...draft.red_flag_escalation.reasons.map((f) => `- ${f}`), `- 建议动作：${draft.red_flag_escalation.recommended_action}`].join("\n")
      : "- 未触发红旗",
    "",
    "## 七、缺失信息",
    missing.length ? missing.map((f) => `- ${f}`).join("\n") : "- 无",
    "",
    "## 八、建议下一步（由医生决定）", next,
    "",
    "## 九、不确定性",
    draft.uncertainties.map((u) => `- ${u}`).join("\n"),
    "",
    "## 十、引用", cites,
    "",
    "---",
    "## 免责声明", draft.disclaimer,
  ].join("\n");
}
