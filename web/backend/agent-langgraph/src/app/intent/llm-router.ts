/**
 * 第三层：LLM 结构化路由（intent.py layer3 的同构实现）。
 *
 * 本包没有向量层（Node 端不引入嵌入模型），所以这一层承接第一层漏下的全部模糊输入。
 * 与 Python 端一致：LLM 只输出 `{intent, confidence, ambiguity, slots, alternatives, reason}`，
 * 服务端/调用方按白名单校验后再决定是否执行；本层绝不生成医学内容、不抽取病例字段。
 *
 * 与 Python 的差异只在调用形态：Python 用 OpenAI 兼容的强制工具调用；
 * 本包的模型接口（ChatModelLike）只有文本 invoke，因此要求模型输出同一 schema 的 JSON，
 * 校验逻辑逐条对齐 —— 调用方可注入任意 `RouteInvoker`（测试用假实现，生产用 createChatInvoker）。
 */

import { HumanMessage, SystemMessage } from "@langchain/core/messages";
import { extractText, type ChatModelLike } from "../../libs/nodes/llm.js";
import {
  ALLOWED_INTENTS,
  isKnownIntent,
  layer3ExecuteThreshold,
} from "./spec.js";
import type { IntentState } from "./rules.js";

/** 调用方注入的模型适配器：system + user → 模型原始输出（JSON 文本或已解析对象）。 */
export type RouteInvoker = (system: string, user: string) => Promise<unknown>;

export interface LlmAlternative {
  intent: string;
  reason: string;
}

export interface LlmRoute {
  intent: string;
  confidence: number;
  ambiguity: boolean;
  slots: Record<string, string>;
  alternatives: LlmAlternative[];
  reason: string;
}

const SLOT_WHITELIST: Record<string, readonly string[]> = {
  case_compare: ["case_id"],
  settings_control: ["language", "model_hint"],
};

export const ROUTE_SYSTEM = [
  "你是皮肤科医生工作站的意图路由器。只输出 JSON，不回答医学问题、",
  "不生成诊断或治疗方案、不抽取病例字段。",
  "输出 schema：",
  `{"intent": <枚举>, "confidence": 0~1, "ambiguity": true|false,`,
  ' "slots": {"case_id"?: string, "language"?: "zh"|"en", "model_hint"?: string},',
  ' "alternatives": [{"intent": <枚举>, "reason": string}], "reason": string}',
  `intent 枚举：${ALLOWED_INTENTS.join(" / ")}`,
  "规则：",
  "1) 医生在描述患者症状/病史/皮损，或要求新建病历 → case_write。",
  "2) 医生在发出明确任务指令 → 对应业务意图（鉴别/治疗/用药/检查/患者教育/文书整理/导出等）。",
  "3) 医生在说界面操作（跳过3D/保存/撤销/打开面板/改设置）→ 对应控制意图。",
  "4) 拿不准就 ambiguity=true，并在 alternatives 给 2-4 个最可能意图。",
  "5) confidence 是 0-1 的实数，表示你对该判断的把握。",
  "6) 只输出 JSON 对象本身（不要 markdown 代码围栏、不要解释文字）。",
].join("\n");

/** 会话上下文：只给标签与布尔，不给病例字段值（路由不需要、也不该看到）。 */
function buildContext(state: IntentState): Record<string, unknown> {
  return {
    phase: state.phase ?? state.workflow_state ?? "",
    prev_task: state.task_type ?? "",
    has_draft: Boolean(state.has_draft ?? state.draft),
    filled_fields: (state.filled_fields ?? []).slice(0, 12),
    recent: (state.recent_user ?? []).slice(-2),
  };
}

/** 从模型文本里抠出 JSON 对象：纯 JSON / ```json 围栏 / 首个平衡花括号段。 */
export function parseRoutePayload(raw: unknown): unknown {
  if (raw && typeof raw === "object") return raw;
  const text = String(raw ?? "").trim();
  if (!text) return null;
  const candidates = [text];
  const fenced = /```(?:json)?\s*([\s\S]*?)```/i.exec(text);
  if (fenced?.[1]) candidates.push(fenced[1].trim());
  const start = text.indexOf("{");
  if (start >= 0) {
    let depth = 0;
    let inStr = false;
    let esc = false;
    for (let i = start; i < text.length; i += 1) {
      const ch = text[i]!;
      if (inStr) {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === '"') inStr = false;
        continue;
      }
      if (ch === '"') inStr = true;
      else if (ch === "{") depth += 1;
      else if (ch === "}") {
        depth -= 1;
        if (depth === 0) {
          candidates.push(text.slice(start, i + 1));
          break;
        }
      }
    }
  }
  for (const c of candidates) {
    try {
      const parsed = JSON.parse(c);
      if (parsed && typeof parsed === "object") return parsed;
    } catch {
      // 继续尝试下一个候选片段
    }
  }
  return null;
}

/**
 * 校验 LLM 输出（与 Python `_sanitize_route` 同口径）：
 * 意图必须在枚举内；槽位按意图白名单过滤；置信度截断；alternatives 去重且 ≤ 4。
 * 返回 null 表示输出非法 —— 调用方必须降级，不得执行。
 */
export function sanitizeRoute(raw: unknown): LlmRoute | null {
  const args = parseRoutePayload(raw);
  if (!args || typeof args !== "object") return null;
  const obj = args as Record<string, unknown>;
  const intent = String(obj.intent ?? "").trim();
  if (!isKnownIntent(intent)) return null;

  let conf = Number(obj.confidence);
  if (!Number.isFinite(conf)) conf = 0;
  conf = Math.min(1, Math.max(0, conf));

  const allowed = SLOT_WHITELIST[intent] ?? [];
  const slots: Record<string, string> = {};
  const rawSlots = obj.slots;
  if (rawSlots && typeof rawSlots === "object") {
    for (const [k, v] of Object.entries(rawSlots as Record<string, unknown>)) {
      if (!allowed.includes(k)) continue;
      const s = String(v ?? "").trim();
      if (!s) continue;
      if (k === "language" && s !== "zh" && s !== "en") continue;
      slots[k] = s.slice(0, 80);
    }
  }

  const alternatives: LlmAlternative[] = [];
  const seen = new Set<string>([intent]);
  const rawAlts = obj.alternatives;
  if (Array.isArray(rawAlts)) {
    for (const a of rawAlts) {
      const item = (a && typeof a === "object" ? a : {}) as Record<string, unknown>;
      const al = String(item.intent ?? "").trim();
      if (isKnownIntent(al) && !seen.has(al)) {
        seen.add(al);
        alternatives.push({ intent: al, reason: String(item.reason ?? "").slice(0, 120) });
      }
      if (alternatives.length >= 4) break;
    }
  }

  return {
    intent,
    confidence: Math.round(conf * 10000) / 10000,
    ambiguity: Boolean(obj.ambiguity),
    slots,
    alternatives,
    reason: String(obj.reason ?? "").slice(0, 200),
  };
}

/** 调模型并校验；不可用 / 超时 / 输出非法 → null（调用方按兜底矩阵降级）。 */
export async function routeWithLlm(
  text: string,
  state: IntentState,
  invoke: RouteInvoker
): Promise<LlmRoute | null> {
  const ctx = JSON.stringify(buildContext(state)).slice(0, 1500);
  const user = `对话状态：${ctx}\n医生这句话：${String(text).slice(0, 300)}`;
  let raw: unknown;
  try {
    raw = await invoke(ROUTE_SYSTEM, user);
  } catch {
    return null;
  }
  return sanitizeRoute(raw);
}

/** LangChain 文本模型 → RouteInvoker（生产适配器）。 */
export function createChatInvoker(model: ChatModelLike): RouteInvoker {
  return async (system, user) => {
    const res = await model.invoke([new SystemMessage(system), new HumanMessage(user)]);
    return extractText(res.content);
  };
}

/** 该路由是否可直接执行（不歧义且达到阈值）。 */
export function isExecutableRoute(route: LlmRoute): boolean {
  return !route.ambiguity && route.confidence >= layer3ExecuteThreshold();
}
