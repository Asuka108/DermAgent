/**
 * write_todos —— 把复杂任务的完整待办快照更新到 FlowCallbacks.onPlan。
 *
 * ACP 的 plan 更新是完整快照而非增量 patch，因此模型每次调用都必须提交全部条目。
 * 工具本身无状态，可安全复用于父 Agent 与并行 subagent。
 */

import { tool, type ToolRuntime } from "@langchain/core/tools";
import { z } from "zod";
import type { FlowCallbacks, PlanEntry } from "../../core/flow-types.js";

const TodoStatusSchema = z.enum([
  "pending",
  "in_progress",
  "completed",
  "skipped",
  "cancelled",
]);

const TodoEntrySchema = z.object({
  id: z.string().optional().describe("可选稳定标识；仅供模型整理，ACP plan 不消费该字段"),
  content: z.string().trim().min(1).describe("清晰、可执行的待办事项"),
  status: TodoStatusSchema.describe("当前状态；cancelled 会映射为 ACP skipped"),
  priority: z.enum(["high", "medium", "low"]).optional(),
});

function toPlanEntries(
  todos: Array<z.infer<typeof TodoEntrySchema>>
): PlanEntry[] {
  return todos.map((todo) => ({
    content: todo.content,
    status: todo.status === "cancelled" ? "skipped" : todo.status,
    ...(todo.priority ? { priority: todo.priority } : {}),
  }));
}

export const writeTodosTool = tool(
  async ({ todos }, runtime: ToolRuntime) => {
    const entries = toPlanEntries(todos);
    const onPlan = runtime.configurable?.onPlan as FlowCallbacks["onPlan"] | undefined;
    await onPlan?.({ entries });

    const completed = entries.filter((entry) => entry.status === "completed").length;
    return `Todo list updated: ${entries.length} items, ${completed} completed.`;
  },
  {
    name: "write_todos",
    description:
      "为复杂、多步骤任务创建或更新 ACP 待办清单。每次必须传入完整清单快照；执行过程中及时把状态从 pending 更新为 in_progress/completed。简单任务不要调用。",
    schema: z.object({
      todos: z
        .array(TodoEntrySchema)
        .min(1)
        .max(100)
        .describe("完整待办清单快照，不是仅包含本次变化的增量"),
    }),
  }
);
