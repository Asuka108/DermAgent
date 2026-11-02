/**
 * Flow 注册表 —— 选图入口（app 层）。
 *
 * 产品工作区仅保留 default = 肤智（DermAgent） 问诊审核流程（HITL 固定管道，图逻辑在
 * `graph.ts` / `default-flow.ts`）。interaction: "approval" → surface 暴露 hasStarted，
 * 首条 query 开题、之后走 interrupt/resume。
 *
 * 选图：配置 `flow.active`（缺省 "default"）。
 * 组合根 index.ts 按 `resolveFlowSelection(raw)` + `resolveFlow(...)` 取 executor / topology。
 */

import type { FlowRuntime } from "../../runtime/flow-runtime.js";
import type {
  FlowInteractionKind,
  FlowProfile,
  StatefulTopologyRecipe,
} from "../../core/flow-types.js";
import { recipe as defaultRecipe } from "../default-flow.js";
import { getFlowTopology, type FlowTopology } from "../topology.js";
import { logger } from "../../runtime/index.js";
import type { PlatformToolRef } from "../../runtime/platform-tools/types.js";

/**
 * 可挂载到 surface 的 flow 定义。
 * createStatefulFlow 的实际调用由组合根 index.ts 的 materializeFlow 完成。
 */
export type FlowDef = {
  name: string;
  kind: "stateful-recipe";
  profile: FlowProfile;
  recipe: (runtime: FlowRuntime) => StatefulTopologyRecipe;
  /**
   * 对话型（多轮对话）。物化时：显式 `conversational` 优先；未设则
   * `profile.interaction === "chat"` → `createStatefulFlow({ conversational: true })`
   * （不暴露 hasStarted，surface 每轮 query + 稳定 threadId 累积历史）。
   */
  conversational?: boolean;
  /**
   * 平台工具引用。平台已登记工具的真实 schema 固化于此；
   * 经 `createFlowRuntime` → `allTools`。**不是**旧 `spec.tools` / flow.json。
   */
  platformToolRefs?: PlatformToolRef[];
  getTopology: () => Promise<FlowTopology>;
};

export interface FlowSelection {
  active: string;
  source: "flow.active" | "activeFlow" | "default";
  defaultInteraction: FlowInteractionKind;
  unknownActivePolicy: "warn-default" | "default";
}

function normalizeInteraction(value: unknown): FlowInteractionKind {
  return value === "pipeline" || value === "approval" ? value : "chat";
}

function normalizeUnknownPolicy(value: unknown): "warn-default" | "default" {
  return value === "default" ? "default" : "warn-default";
}

/** 从 flow 配置解析 active flow，同时兼容旧顶层 activeFlow。 */
export function resolveFlowSelection(raw: Record<string, unknown> = {}): FlowSelection {
  const flow = raw.flow && typeof raw.flow === "object" ? raw.flow as Record<string, unknown> : {};
  const activeFromFlow = typeof flow.active === "string" && flow.active.trim() ? flow.active.trim() : undefined;
  const activeFromLegacy = typeof raw.activeFlow === "string" && raw.activeFlow.trim()
    ? raw.activeFlow.trim()
    : undefined;
  if (activeFromFlow && activeFromLegacy) {
    logger.warn(`flow.active="${activeFromFlow}" 优先于旧 activeFlow="${activeFromLegacy}"；请迁移到 flow.active。`);
  }
  return {
    active: activeFromFlow ?? activeFromLegacy ?? "default",
    source: activeFromFlow ? "flow.active" : activeFromLegacy ? "activeFlow" : "default",
    defaultInteraction: normalizeInteraction(flow.defaultInteraction),
    unknownActivePolicy: normalizeUnknownPolicy(flow.unknownActivePolicy),
  };
}

/** 内置 flow 注册表：仅 default（肤智（DermAgent） 问诊审核流程，HITL）。 */
export const flows: Record<string, FlowDef> = {
  default: {
    name: "default",
    kind: "stateful-recipe",
    profile: {
      interaction: "approval",
      implementation: "default",
      userLabel: "肤智（DermAgent）问诊审核流程",
      summary:
        "结构化问诊 → 必填校验/红旗筛查 → 知识库检索 → 待审核草稿（固定 JSON + 引用）→ 医生确认/修改/拒绝（interrupt/resume）→ 正式报告。医生决策支持演示原型。",
      defaultForAmbiguous: true,
    },
    // HITL durable stateful flow：不设 conversational → 暴露 hasStarted，
    // 首条 query 开题、后续消息走 resume（interrupt 续跑），见 surfaces/stateful-flow.ts。
    recipe: defaultRecipe,
    getTopology: () => getFlowTopology(),
  },
};

/** 按名取 flow，未命中或未指定回落到 default。 */
export function resolveFlow(name?: string, opts: { unknownActivePolicy?: "warn-default" | "default" } = {}): FlowDef {
  if (name && !flows[name] && opts.unknownActivePolicy !== "default") {
    logger.warn(
      `active flow "${name}" 未在 flow 注册表，回落 default。已注册: ${Object.keys(flows).join(", ")}`
    );
  }
  return (name ? flows[name] : undefined) ?? flows.default!;
}

export function listFlowProfiles() {
  return Object.values(flows).map((def) => ({
    name: def.name,
    kind: def.kind,
    profile: def.profile,
    isDefault: def.name === "default",
    conversational: def.conversational ?? def.profile.interaction === "chat",
  }));
}

export function recommendFlows(kind: FlowInteractionKind) {
  return listFlowProfiles()
    .filter((flow) => flow.profile.interaction === kind)
    .sort((a, b) => Number(Boolean(b.profile.defaultForAmbiguous)) - Number(Boolean(a.profile.defaultForAmbiguous)));
}
