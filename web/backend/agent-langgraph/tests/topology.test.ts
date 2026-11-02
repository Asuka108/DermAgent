/**
 * 图拓扑导出测试 —— 守住「可视化对接」的公开 API getFlowTopology()。
 * 拓扑从编译图反射,所以这同时是 graph.ts 连线的回归守卫:
 * 改了 addNode/addEdge/addConditionalEdges,这里会跟着变(或报警)。
 */

import { describe, it, expect } from "vitest";
import { getFlowTopology } from "../src/app/topology.js";

const ALL_NODES = [
  "intake",
  "intake_llm",
  "screen",
  "ask_more",
  "retrieve",
  "analyze",
  "doctor_review",
  "revise",
  "finalize_report",
  "case_closed",
];

describe("getFlowTopology", () => {
  it("导出全部业务节点(+ start/end)", async () => {
    const { nodes } = await getFlowTopology();
    const ids = nodes.map((n) => n.id);
    expect(ids).toEqual(expect.arrayContaining(["__start__", ...ALL_NODES, "__end__"]));
    expect(nodes.every((n) => n.label.length > 0)).toBe(true);
  });

  it("条件边只在 intake / screen / doctor_review 出口，且被标记 conditional", async () => {
    const { edges } = await getFlowTopology();
    const conditional = edges.filter((e) => e.conditional);
    // 反射把每条条件边按分支展开：intake×2 + screen×2 + doctor_review×3 = 7 条
    expect(conditional).toHaveLength(7);
    expect(conditional.map((e) => e.source).sort()).toEqual(
      ["doctor_review", "doctor_review", "doctor_review", "intake", "intake", "screen", "screen"].sort()
    );
    expect(conditional.map((e) => e.target).sort()).toEqual(
      [
        "screen",
        "intake_llm",
        "ask_more",
        "retrieve",
        "revise",
        "finalize_report",
        "case_closed",
      ].sort()
    );
  });

  it("主干顺序边存在且非条件", async () => {
    const { edges } = await getFlowTopology();
    const has = (source: string, target: string) =>
      edges.some((e) => e.source === source && e.target === target && !e.conditional);
    expect(has("__start__", "intake")).toBe(true);
    expect(has("intake_llm", "screen")).toBe(true);
    expect(has("ask_more", "screen")).toBe(true);
    expect(has("retrieve", "analyze")).toBe(true);
    expect(has("analyze", "doctor_review")).toBe(true);
    expect(has("revise", "doctor_review")).toBe(true);
    expect(has("finalize_report", "__end__")).toBe(true);
    expect(has("case_closed", "__end__")).toBe(true);
  });

  it("产出可渲染的 Mermaid 源", async () => {
    const { mermaid } = await getFlowTopology();
    expect(mermaid).toContain("graph TD");
    expect(mermaid).toContain("doctor_review");
    expect(mermaid).toContain("finalize_report");
  });
});
