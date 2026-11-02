/**
 * 记忆规格（单一真源）——`spec.json` 是 shared/memory_spec.json 的字节级副本。
 *
 * tests/memory.test.ts 用 SHA-256 断言副本未漂移；改规格必须先改 shared 文件再复制。
 */

import specJson from "./spec.json" with { type: "json" };
import type { MemoryFact, MemoryHit } from "./types.js";

export interface MemorySpec {
  version: string;
  profile: {
    categories: Record<string, string[]>;
    multi_valued: string[];
    min_confidence: number;
    max_value_chars: number;
    max_evidence_chars: number;
  };
  episodic: {
    case_collection: string;
    doctor_collection: string;
    dim: number;
    embed_model: string;
    chunk_max_chars: number;
    topic_max_chars: number;
    top_k_case: number;
    top_k_doctor: number;
    score_threshold: number;
    max_injected: number;
    exclude_recent_turns: number;
  };
  short_term: { rounds: number; ttl_seconds: number };
  budget: { profile_chars: number; episodic_chars: number };
}

export const MEMORY_SPEC = specJson as unknown as MemorySpec;
export const MEMORY_SPEC_VERSION = MEMORY_SPEC.version;

export const CATEGORY_LABELS: Record<string, string> = {
  allergy: "过敏史",
  past_history: "既往史",
  personal_history: "个人史",
  medication: "用药",
  family_history: "家族史",
  diagnosis_history: "诊断史",
  lab: "检验",
};

const FIELD_LABELS: Record<string, string> = {
  allergen: "过敏原",
  reaction: "反应",
  severity: "严重度",
  disease: "疾病",
  year: "年份",
  status: "状态",
  smoking: "吸烟",
  alcohol: "饮酒",
  occupation: "职业",
  travel: "旅行",
  name: "名称",
  dose: "剂量",
  frequency: "频次",
  since: "起始",
  indication: "指征",
  relation: "关系",
  diagnosis: "诊断",
  certainty: "确定性",
  date: "日期",
  test: "检验项目",
  value: "结果",
  unit: "单位",
};

const SOURCE_LABELS: Record<string, string> = {
  doctor_modified: "医生修改",
  doctor_input: "医生输入",
  case_import: "病例导入",
  llm_extract: "自动抽取，需医生核对",
};

export function clip(text: string, limit: number): string {
  return String(text ?? "").slice(0, Math.max(0, Math.trunc(limit)));
}

/** 白名单校验：类别/字段必须来自 spec，值长度不超过上限。 */
export function isAllowedFact(category: string, field: string, value: string): boolean {
  const fields = MEMORY_SPEC.profile.categories[category];
  if (!fields || !fields.includes(field)) return false;
  const v = String(value ?? "").trim();
  return v.length > 0 && v.length <= MEMORY_SPEC.profile.max_value_chars;
}

export function isMultiValued(category: string): boolean {
  return MEMORY_SPEC.profile.multi_valued.includes(category);
}

/** 患者档案片段：只渲染临床事实，超预算从末尾裁并如实标注。 */
export function renderProfile(facts: MemoryFact[], limit = MEMORY_SPEC.budget.profile_chars): string {
  if (!facts.length) return "";
  const order = new Map(Object.keys(CATEGORY_LABELS).map((cat, i) => [cat, i]));
  const rows = [...facts].sort((a, b) => {
    const ao = order.get(a.category) ?? 99;
    const bo = order.get(b.category) ?? 99;
    if (ao !== bo) return ao - bo;
    return Number(a.fact_id ?? 0) - Number(b.fact_id ?? 0);
  });
  const lines: string[] = [];
  let used = 0;
  for (const fact of rows) {
    const cat = CATEGORY_LABELS[fact.category] || fact.category;
    const field = FIELD_LABELS[fact.field] || fact.field;
    const src = SOURCE_LABELS[fact.source ?? ""] || "";
    let line = `- ${cat}·${field}：${String(fact.value ?? "").trim()}`;
    if (src) line += `（${src}）`;
    if (used + line.length + 1 > limit) {
      lines.push("- ……（患者档案超出预算已截断）");
      break;
    }
    lines.push(line);
    used += line.length + 1;
  }
  return "【患者档案（跨就诊慢病信息，仅供参考，当前病例以本轮资料为准）】\n" + lines.join("\n");
}

/** 情景片段：每条标注层级/日期/标题，并明确「不改变当前病例事实」。 */
export function renderEpisodic(hits: MemoryHit[], limit = MEMORY_SPEC.budget.episodic_chars): string {
  if (!hits.length) return "";
  const lines: string[] = [];
  let used = 0;
  for (const hit of hits) {
    const scope = hit.scope === "case" ? "病例级" : "医生级";
    const date = String(hit.created_at ?? "").slice(0, 10);
    const title = String(hit.title ?? "").trim().slice(0, 60);
    const text = String(hit.text ?? "").trim();
    const head = `- [${scope}]${date ? ` ${date}` : ""}${title ? ` ${title}` : ""}`;
    const line = `${head}\n  ${text}`;
    if (used + line.length + 1 > limit) {
      lines.push("- ……（情景记忆超出预算已截断）");
      break;
    }
    lines.push(line);
    used += line.length + 1;
  }
  return "【相关历史话题（模糊召回，仅供参考，不改变当前病例事实）】\n" + lines.join("\n");
}
