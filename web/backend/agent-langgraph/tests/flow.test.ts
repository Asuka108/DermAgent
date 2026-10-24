/**
 * 肤智（DermAgent） flow 测试（无凭证 fallback，全链路确定性）——v1.1 契约。
 * 覆盖四条内置合成案例与 HITL 状态流转（interrupt/resume）。
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { Command, MemorySaver } from "@langchain/langgraph";
import { AppConfigSchema } from "../src/runtime/index.js";
import { createFlowGraph } from "../src/app/graph.js";
import type { FlowState } from "../src/app/state.js";

describe("肤智（DermAgent） 图（无凭证 fallback · v1.1 契约）", () => {
  const credVars = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"];
  const saved: Record<string, string | undefined> = {};

  beforeAll(() => {
    for (const v of credVars) {
      saved[v] = process.env[v];
      delete process.env[v];
    }
  });
  afterAll(() => {
    for (const v of credVars) {
      if (saved[v] === undefined) delete process.env[v];
      else process.env[v] = saved[v]!;
    }
  });

  function makeDeps() {
    return { checkpointer: new MemorySaver(), config: AppConfigSchema.parse({}), systemPrompt: "" };
  }

  it("案例1 资料充分：草稿（v1.1）→ 医生确认 → 辅助报告", async () => {
    const graph = createFlowGraph(makeDeps());
    const threadId = "t-case1";
    const first = (await graph.invoke(
      { input: "案例1", messages: [] } as unknown as FlowState,
      { configurable: { thread_id: threadId } }
    )) as FlowState & { __interrupt__: Array<{ value: { question: string } }> };

    expect(first.case?.chief_complaint).toContain("红斑");
    expect(first.missing).toEqual([]);
    expect(first.redFlags).toEqual([]);
    const d = first.draft!;
    expect(d.schema_version).toBe("1.1");
    expect(d.workflow_state).toBe("awaiting_review");
    expect(d.review_status).toBe("pending");
    expect(d.requires_doctor_confirmation).toBe(true);
    expect(d.case_summary.data_completeness).toBe("complete");
    expect(d.severity.level).toBe("routine");
    expect(d.citations.length).toBeGreaterThan(0);
    expect(d.safety_policy_version).toBeTruthy();
    expect(d.knowledge_snapshot).toContain("synthetic-kb@");
    expect(d.disclaimer).toContain("不构成诊断或处方");

    expect(first.__interrupt__?.[0]?.value?.question).toContain("待审核草稿");

    const final = (await graph.invoke(new Command({ resume: "确认" }), {
      configurable: { thread_id: threadId },
    })) as FlowState;
    expect(final.decision).toBe("confirmed");
    expect(final.report).toContain("辅助报告");
    expect(final.report).toContain("免责声明");
    expect(final.output).toBe(final.report);
  });

  it("案例2 红旗：escalated 阻断 → 修改 → 再审核 → 确认", async () => {
    const graph = createFlowGraph(makeDeps());
    const threadId = "t-case2";
    const first = (await graph.invoke(
      { input: "案例2", messages: [] } as unknown as FlowState,
      { configurable: { thread_id: threadId } }
    )) as FlowState & { __interrupt__: Array<{ value: { question: string } }> };

    expect(first.redFlags.some((f) => f.startsWith("RF-HZ-001"))).toBe(true);
    const d = first.draft!;
    expect(d.workflow_state).toBe("escalated");
    expect(d.red_flag_escalation.is_blocking).toBe(true);
    expect(d.blocking_reason).toContain("RF-HZ-001");
    expect(d.severity.level).toBe("needs_review");
    expect(first.__interrupt__?.[0]?.value?.question).toContain("escalated");

    const revised = (await graph.invoke(new Command({ resume: "修改：请补充疼痛评估与眼科会诊提示" }), {
      configurable: { thread_id: threadId },
    })) as FlowState;
    expect(revised.decision).toBe("pending");
    expect(
      revised.draft?.uncertainties.some((u) => u.includes("医生修改意见") || u.includes("修订") || u.includes("眼科"))
    ).toBe(true);

    const final = (await graph.invoke(new Command({ resume: "确认" }), {
      configurable: { thread_id: threadId },
    })) as FlowState;
    expect(final.report).toContain("辅助报告");
    expect(final.report).toContain("RF-HZ-001");
  });

  it("案例3 信息不足：awaiting_follow_up → 补充 → 草稿 → 拒绝不生成报告", async () => {
    const graph = createFlowGraph(makeDeps());
    const threadId = "t-case3";
    const first = (await graph.invoke(
      { input: "案例3", messages: [] } as unknown as FlowState,
      { configurable: { thread_id: threadId } }
    )) as FlowState & { __interrupt__: Array<{ value: { question: string } }> };

    expect(first.missing.length).toBeGreaterThan(0);
    expect(first.__interrupt__?.[0]?.value?.question).toContain("awaiting_follow_up");

    const after = (await graph.invoke(
      new Command({
        resume:
          "补充：病程5天；变化趋势逐渐扩大；诱因新换护肤品后出现；既往史无特殊；过敏史否认药物食物过敏；当前用药未使用药物",
      }),
      { configurable: { thread_id: threadId } }
    )) as FlowState;
    expect(after.missing).toEqual([]);
    expect(after.case?.duration).toContain("5天");
    expect(after.draft?.workflow_state).toBe("awaiting_review");

    const final = (await graph.invoke(new Command({ resume: "拒绝：资料仍不够，建议门诊面诊" }), {
      configurable: { thread_id: threadId },
    })) as FlowState;
    expect(final.decision).toBe("rejected");
    expect(final.report).toBe("");
    expect(final.output).toContain("不生成报告");
  });

  it("案例4 无可靠证据：不编造鉴别诊断/引用，建议面诊", async () => {
    const graph = createFlowGraph(makeDeps());
    const threadId = "t-case4";
    const first = (await graph.invoke(
      { input: "案例4", messages: [] } as unknown as FlowState,
      { configurable: { thread_id: threadId } }
    )) as FlowState & { __interrupt__: Array<{ value: { question: string } }> };

    expect(first.citations).toEqual([]);
    const d = first.draft!;
    expect(d.suspected_conditions).toEqual([]);
    expect(d.citations).toEqual([]);
    expect(d.uncertainties.some((u) => u.includes("知识库未覆盖") || u.includes("不足以判断"))).toBe(true);
    expect(d.recommended_next_steps.some((n) => n.action.includes("面诊"))).toBe(true);
    expect(first.__interrupt__?.[0]?.value?.question).toContain("知识库未覆盖");

    const final = (await graph.invoke(new Command({ resume: "确认" }), {
      configurable: { thread_id: threadId },
    })) as FlowState;
    expect(final.report).toContain("不足以判断");
    expect(final.report).toContain("面诊");
  });

  it("问候语入口容错（R-G002）", async () => {
    const graph = createFlowGraph(makeDeps());
    const first = (await graph.invoke(
      { input: "你好", messages: [] } as unknown as FlowState,
      { configurable: { thread_id: "t-greet" } }
    )) as FlowState & { __interrupt__: Array<{ value: { question: string } }> };
    expect(first.missing.length).toBeGreaterThan(0);
  });
});
