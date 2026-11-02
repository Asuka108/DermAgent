/**
 * 意图规格（单一真源）——`prototypes.json` 是 shared/intent_spec.json 的字节级副本。
 *
 * 三端（服务层 intent_core.py / 桌面 mock / 本包）共用同一份意图目录、原型句与阈值；
 * tests/intent.test.ts 用 SHA-256 断言副本未漂移（改规格必须先改 shared 文件再复制）。
 *
 * 本包不做向量层（Node 端不引入嵌入模型依赖），只消费：
 *   - intents.*.label / class / slots（路由与候选卡标签）
 *   - thresholds.layer3_execute（第三层执行阈值）
 */

import specJson from "./prototypes.json" with { type: "json" };

export type IntentClass = "business" | "medical" | "control" | "smalltalk";

export interface IntentEntry {
  label: string;
  desc?: string;
  class: IntentClass;
  slots?: string[];
  prototypes: string[];
}

export interface IntentSpec {
  version: string;
  thresholds: Record<string, unknown>;
  intents: Record<string, IntentEntry>;
}

export const INTENT_SPEC = specJson as unknown as IntentSpec;

export const ALLOWED_INTENTS: readonly string[] = Object.keys(INTENT_SPEC.intents);

const INTENT_SET = new Set(ALLOWED_INTENTS);

export const MEDICAL_INTENTS: ReadonlySet<string> = new Set(
  ALLOWED_INTENTS.filter((k) => INTENT_SPEC.intents[k]?.class === "medical")
);

export function isKnownIntent(intent: unknown): intent is string {
  return typeof intent === "string" && INTENT_SET.has(intent);
}

export function intentLabel(intent: string): string {
  return INTENT_SPEC.intents[intent]?.label || intent;
}

export function intentClassOf(intent: string): IntentClass {
  return INTENT_SPEC.intents[intent]?.class ?? "business";
}

export function numericThreshold(key: string, fallback: number): number {
  const raw = INTENT_SPEC.thresholds[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : fallback;
}

/** 第三层（LLM 路由）执行阈值；低于它必须反问确认。 */
export function layer3ExecuteThreshold(): number {
  return numericThreshold("layer3_execute", 0.75);
}

/** 候选卡上限（与 Python 端 max_candidates 同口径）。 */
export function maxCandidates(): number {
  return Math.max(2, Math.min(4, Math.round(numericThreshold("max_candidates", 4))));
}

export const SPEC_VERSION = INTENT_SPEC.version;
