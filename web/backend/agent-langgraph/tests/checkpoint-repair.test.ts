/**
 * checkpoint 消息修复 —— cancel 补 ToolMessage + RemoveMessage 写回。
 */

import { describe, expect, it } from "vitest";
import {
  AIMessage,
  HumanMessage,
  RemoveMessage,
  ToolMessage,
} from "@langchain/core/messages";
import {
  applyCheckpointMessageRepair,
  checkpointRepairUpdate,
  completeOrphanedToolCalls,
  repairCheckpointMessages,
} from "../src/libs/messages/repair-checkpoint.js";
import { createStatefulFlow } from "../src/surfaces/stateful-flow.js";

describe("repairCheckpointMessages", () => {
  it("cancel 路径：为 in-flight tool_call 补 ToolMessage", () => {
    const prior = [
      new HumanMessage("搜"),
      new AIMessage({
        content: "",
        tool_calls: [{ id: "tc_1", name: "web_search", args: {} }],
      }),
    ];
    const repaired = repairCheckpointMessages(prior, {
      cancelledToolCallIds: ["tc_1"],
      cancelReason: "已取消",
    });
    expect(repaired).toHaveLength(3);
    expect(repaired[2]).toBeInstanceOf(ToolMessage);
    expect((repaired[2] as ToolMessage).tool_call_id).toBe("tc_1");
    // 补全 ToolMessage 后 tool_calls 保留（已成对，不再孤立）
    expect((repaired[1] as AIMessage).tool_calls ?? []).toHaveLength(1);
  });

  it("无 cancel id 时仅 sanitize", () => {
    const prior = [
      new AIMessage({
        content: "",
        tool_calls: [{ id: "orphan", name: "bash", args: {} }],
      }),
    ];
    const repaired = repairCheckpointMessages(prior);
    expect((repaired[0] as AIMessage).tool_calls ?? []).toHaveLength(0);
  });
});

describe("checkpointRepairUpdate", () => {
  it("有 id 的消息 → RemoveMessage + 全量 repaired", () => {
    const prior = [
      new HumanMessage({ id: "h1", content: "hi" }),
      new AIMessage({
        id: "a1",
        content: "",
        tool_calls: [{ id: "tc_1", name: "bash", args: {} }],
      }),
    ];
    const repaired = completeOrphanedToolCalls(prior, ["tc_1"]);
    const update = checkpointRepairUpdate(prior, repaired);
    expect(update[0]).toBeInstanceOf(RemoveMessage);
    expect(update.some((m) => m instanceof ToolMessage)).toBe(true);
  });

  it("消息无 id → 跳过写回（返回空）", () => {
    const prior = [
      new AIMessage({
        content: "",
        tool_calls: [{ id: "orphan", name: "bash", args: {} }],
      }),
    ];
    const repaired = repairCheckpointMessages(prior);
    expect(checkpointRepairUpdate(prior, repaired)).toEqual([]);
  });
});

describe("applyCheckpointMessageRepair", () => {
  it("损坏 checkpoint → updateState 写回修复结果", async () => {
    const prior = [
      new HumanMessage({ id: "h1", content: "hi" }),
      new AIMessage({
        id: "a1",
        content: "",
        tool_calls: [{ id: "bad", name: "web_search", args: {} }],
      }),
    ];
    const calls: Record<string, unknown>[] = [];
    const graph = {
      async getState() {
        return { values: { messages: prior } };
      },
      async updateState(_config: unknown, values: Record<string, unknown>) {
        calls.push(values);
      },
    };

    const ok = await applyCheckpointMessageRepair(graph, {
      configurable: { thread_id: "sess-1" },
    });
    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    const written = calls[0]!.messages as unknown[];
    expect(written.some((m) => m instanceof RemoveMessage)).toBe(true);
  });
});

describe("createStatefulFlow.repairCheckpoint", () => {
  it("StatefulFlow 暴露 repairCheckpoint 并写回", async () => {
    const prior = [
      new HumanMessage({ id: "h1", content: "hi" }),
      new AIMessage({
        id: "a1",
        content: "",
        tool_calls: [{ id: "tc_x", name: "bash", args: {} }],
      }),
    ];
    const updateCalls: Record<string, unknown>[] = [];
    const graph = {
      async getState() {
        return { values: { messages: prior } };
      },
      async updateState(_config: unknown, values: Record<string, unknown>) {
        updateCalls.push(values);
      },
      async stream() {
        async function* empty() {}
        return empty();
      },
    };
    const flow = createStatefulFlow({
      buildGraph: () => graph,
      toInput: (q) => ({ query: q }),
      toResult: () => ({ answer: "ok" }),
    });

    const ok = await flow.repairCheckpoint!("sess-repair", {
      cancelledToolCallIds: ["tc_x"],
    });
    expect(ok).toBe(true);
    expect(updateCalls.length).toBeGreaterThan(0);
  });
});
