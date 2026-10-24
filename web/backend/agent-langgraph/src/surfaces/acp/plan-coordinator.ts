/**
 * ACP plan 是会话级完整快照，没有 subagent/messageId 维度。
 * 本协调器按父 task toolCallId 保存并行 subagent 的最新计划，再合成为一个扁平快照。
 */

import type { PlanEntry, PlanEvent } from "../../core/flow-types.js";

interface SubagentPlan {
  source: string;
  entries: PlanEntry[];
}

function sourceLabel(source: string): string {
  return source.replace(/\s+/g, " ").trim() || "subagent";
}

function prefixEntries(source: string, entries: PlanEntry[]): PlanEntry[] {
  const prefix = `[${sourceLabel(source)}] `;
  return entries.map((entry) => ({
    ...entry,
    content: `${prefix}${entry.content}`,
  }));
}

export class AcpPlanCoordinator {
  private parentEntries: PlanEntry[] = [];
  private readonly subagentPlans = new Map<string, SubagentPlan>();

  /** 当前合并后的完整 plan 快照（发送 ACP 前读取，避免队列中固定旧快照）。 */
  snapshot(): PlanEvent {
    return {
      entries: [
        ...this.parentEntries,
        ...Array.from(this.subagentPlans.values()).flatMap((plan) =>
          prefixEntries(plan.source, plan.entries)
        ),
      ],
    };
  }

  update(event: PlanEvent): PlanEvent {
    if (event.source) {
      const key = event.toolCallId ?? `source:${event.source}`;
      if (event.entries.length === 0) {
        this.subagentPlans.delete(key);
      } else {
        this.subagentPlans.set(key, {
          source: event.source,
          entries: event.entries,
        });
      }
    } else {
      this.parentEntries = event.entries;
    }

    return this.snapshot();
  }
}
