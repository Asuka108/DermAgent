/**
 * 意图路由入口（TS 侧）：第一层规则 → 第三层 LLM 结构化路由。
 *
 * 倒金字塔在本包的形态：没有第二层（Node 端不加载嵌入模型），规则未定案的输入直接交给
 * LLM 路由；LLM 也不确定时只把 `needs_confirm` 交给调用方，不在本包内执行任何业务动作。
 *
 * 与本包固定问诊管道的关系：只在 intake 节点旁做标注（`FlowState.intentMeta`），
 * 不新增节点 / 边，不改变问诊图的路由（routeAfterIntake 仍只看 case 是否存在）。
 */

import { classifyRules, guardedIntents, type IntentState, type RuleHit } from "./rules.js";
import { intentLabel, maxCandidates } from "./spec.js";
import {
  isExecutableRoute,
  routeWithLlm,
  type LlmRoute,
  type RouteInvoker,
} from "./llm-router.js";

export type { IntentState, RuleHit } from "./rules.js";
export type { LlmRoute, RouteInvoker } from "./llm-router.js";
export { createChatInvoker } from "./llm-router.js";

export interface IntentCandidate {
  intent: string;
  label: string;
  confidence: number;
  reason?: string;
}

export interface IntentMeta {
  /** 定案意图；未定案（降级/无候选）为 null。 */
  intent: string | null;
  /** 做出该决策的层：1 规则；3 LLM。 */
  layer: 1 | 3;
  confidence: number;
  margin: number;
  reason: string;
  source: "rule" | "llm" | "fallback";
  slots: Record<string, string>;
  candidates: IntentCandidate[];
  /** 未走完正常路径（无模型 / 输出非法 / 无候选）。 */
  degraded: boolean;
  /** 需要医生确认后才能执行；调用方必须尊重该标记。 */
  needs_confirm: boolean;
  latency_ms: number;
}

export interface RouteIntentOptions {
  /** 直接注入模型适配器（测试用假实现）。 */
  invoke?: RouteInvoker;
  /** 惰性解析模型适配器：只在第一层漏判时才调用，避免无谓地构造模型。 */
  resolveInvoke?: () => RouteInvoker | undefined;
}

function fromRule(hit: RuleHit, latencyMs: number): IntentMeta {
  return {
    intent: hit.intent,
    layer: 1,
    confidence: hit.confidence,
    margin: 0,
    reason: hit.reason,
    source: "rule",
    slots: hit.slots,
    candidates: [],
    degraded: false,
    needs_confirm: false,
    latency_ms: latencyMs,
  };
}

function missMeta(layer: 1 | 3, reason: string, latencyMs: number): IntentMeta {
  return {
    intent: null,
    layer,
    confidence: 0,
    margin: 0,
    reason,
    source: "fallback",
    slots: {},
    candidates: [],
    degraded: true,
    needs_confirm: false,
    latency_ms: latencyMs,
  };
}

function candidatesOf(route: LlmRoute): IntentCandidate[] {
  const out: IntentCandidate[] = [
    {
      intent: route.intent,
      label: intentLabel(route.intent),
      confidence: route.confidence,
      ...(route.reason ? { reason: route.reason } : {}),
    },
  ];
  for (const alt of route.alternatives) {
    if (out.length >= maxCandidates()) break;
    out.push({ intent: alt.intent, label: intentLabel(alt.intent), confidence: 0, ...(alt.reason ? { reason: alt.reason } : {}) });
  }
  return out;
}

/** 只跑第一层（同步）：intake 默认标注用。 */
export function routeIntentRules(text: string, state: IntentState = {}): IntentMeta {
  const hit = classifyRules(text, state);
  return hit ? fromRule(hit, 0) : missMeta(1, "rules_miss", 0);
}

/** 完整路由：第一层命中即终局；否则调 LLM，仍不确定则标记 needs_confirm。 */
export async function routeIntent(
  text: string,
  state: IntentState = {},
  options: RouteIntentOptions = {}
): Promise<IntentMeta> {
  const started = Date.now();
  const hit = classifyRules(text, state);
  if (hit) return fromRule(hit, Date.now() - started);

  const invoke = options.invoke ?? options.resolveInvoke?.();
  if (!invoke) return missMeta(1, "rules_miss_no_llm", Date.now() - started);

  const route = await routeWithLlm(text, state, invoke);
  if (!route) return missMeta(3, "llm_unavailable_or_invalid", Date.now() - started);

  const executable = isExecutableRoute(route) && !guardedIntents(text).has(route.intent);
  return {
    intent: route.intent,
    layer: 3,
    confidence: route.confidence,
    margin: 0,
    reason: route.reason || (route.ambiguity ? "llm_ambiguous" : "llm_route"),
    source: "llm",
    slots: route.slots,
    candidates: executable ? [] : candidatesOf(route),
    degraded: false,
    needs_confirm: !executable,
    latency_ms: Date.now() - started,
  };
}
