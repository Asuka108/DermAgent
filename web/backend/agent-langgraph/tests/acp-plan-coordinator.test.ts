import { describe, expect, it } from "vitest";
import { AcpPlanCoordinator } from "../src/surfaces/acp/plan-coordinator.js";

describe("AcpPlanCoordinator", () => {
  it("合并父计划和并行同名 subagent，并按父 task id 独立更新", () => {
    const coordinator = new AcpPlanCoordinator();

    coordinator.update({
      entries: [{ content: "总体交付", status: "pending" }],
    });
    coordinator.update({
      source: "researcher",
      toolCallId: "task-1",
      entries: [{ content: "搜索 A", status: "in_progress" }],
    });
    expect(
      coordinator.update({
        source: "researcher",
        toolCallId: "task-2",
        entries: [{ content: "搜索 B", status: "pending", priority: "high" }],
      })
    ).toEqual({
      entries: [
        { content: "总体交付", status: "pending" },
        { content: "[researcher] 搜索 A", status: "in_progress" },
        {
          content: "[researcher] 搜索 B",
          status: "pending",
          priority: "high",
        },
      ],
    });

    expect(
      coordinator.update({
        source: "researcher",
        toolCallId: "task-1",
        entries: [{ content: "搜索 A", status: "completed" }],
      })
    ).toEqual({
      entries: [
        { content: "总体交付", status: "pending" },
        { content: "[researcher] 搜索 A", status: "completed" },
        {
          content: "[researcher] 搜索 B",
          status: "pending",
          priority: "high",
        },
      ],
    });
  });

  it("空 subagent 快照移除对应分桶，不影响其他计划", () => {
    const coordinator = new AcpPlanCoordinator();
    coordinator.update({
      source: "writer",
      toolCallId: "task-1",
      entries: [{ content: "写作", status: "pending" }],
    });

    expect(
      coordinator.update({
        source: "writer",
        toolCallId: "task-1",
        entries: [],
      })
    ).toEqual({ entries: [] });
  });

  it("snapshot 反映最近一次 update 后的合并状态", () => {
    const coordinator = new AcpPlanCoordinator();
    coordinator.update({
      entries: [{ content: "父任务", status: "pending" }],
    });
    coordinator.update({
      source: "researcher",
      toolCallId: "task-1",
      entries: [{ content: "步骤 A", status: "in_progress" }],
    });
    coordinator.update({
      source: "researcher",
      toolCallId: "task-2",
      entries: [{ content: "步骤 B", status: "pending" }],
    });

    expect(coordinator.snapshot()).toEqual({
      entries: [
        { content: "父任务", status: "pending" },
        { content: "[researcher] 步骤 A", status: "in_progress" },
        { content: "[researcher] 步骤 B", status: "pending" },
      ],
    });
  });
});
