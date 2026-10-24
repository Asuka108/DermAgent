import { describe, expect, it } from "vitest";
import { AIMessage, type BaseMessage } from "@langchain/core/messages";
import type { PlanEvent } from "../src/core/flow-types.js";
import { createToolExecNode } from "../src/libs/nodes/tools.js";
import { writeTodosTool } from "../src/libs/tools/todo.tool.js";

describe("writeTodosTool", () => {
  it("把完整 todo 快照转换为 ACP plan，并兼容 cancelled → skipped", async () => {
    const plans: PlanEvent[] = [];

    const result = await writeTodosTool.invoke(
      {
        todos: [
          { id: "a", content: "检索资料", status: "completed", priority: "high" },
          { id: "b", content: "整理结论", status: "in_progress" },
          { id: "c", content: "废弃步骤", status: "cancelled" },
        ],
      },
      {
        configurable: {
          onPlan: (event: PlanEvent) => plans.push(event),
        },
      }
    );

    expect(plans).toEqual([
      {
        entries: [
          { content: "检索资料", status: "completed", priority: "high" },
          { content: "整理结论", status: "in_progress" },
          { content: "废弃步骤", status: "skipped" },
        ],
      },
    ]);
    expect(result).toContain("3 items, 1 completed");
  });

  it("非 ACP surface 未提供 onPlan 时仍可正常完成", async () => {
    await expect(
      writeTodosTool.invoke({
        todos: [{ content: "完成任务", status: "pending" }],
      })
    ).resolves.toContain("1 items");
  });

  it("兼容 createFlowGraph 构造期 callbacks 注入", async () => {
    const plans: PlanEvent[] = [];
    const exec = createToolExecNode<{ messages: BaseMessage[] }>({
      tools: [writeTodosTool],
      callbacks: {
        onPlan: (event) => plans.push(event),
      },
    });

    await exec({
      messages: [
        new AIMessage({
          content: "",
          tool_calls: [
            {
              id: "todo-1",
              name: "write_todos",
              args: {
                todos: [{ content: "执行验证", status: "in_progress" }],
              },
            },
          ],
        }),
      ],
    });

    expect(plans).toEqual([
      {
        entries: [{ content: "执行验证", status: "in_progress" }],
      },
    ]);
  });
});
