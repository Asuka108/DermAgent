/**
 * 肤智（DermAgent）纯业务逻辑测试 —— v1.1 契约（与 tests-server 同源断言）。
 */

import { describe, it, expect } from "vitest";
import {
  computeMissing, detectRedFlags, retrieveCitations, normalizeLlmDraft, classifyReview,
  buildCaseFromFreeText, mergeCaseReply, routeAfterIntake, routeAfterScreen, routeAfterReview,
  buildFallbackDraft, buildFallbackReport, deriveDataCompleteness, deriveSeverityLevel,
  buildSymptomAssessment, buildMedicationChecks, MAX_ASK_ROUNDS, REQUIRED_FIELDS,
  SCHEMA_VERSION, SAFETY_POLICY_VERSION, DEFAULT_DISCLAIMER,
} from "../src/app/logic.js";
import { DEMO_CASES, matchDemoCase } from "../src/app/knowledge/demo-cases.js";
import type { FlowState } from "../src/app/state.js";

const case1 = DEMO_CASES[0]!.case;
const case2 = DEMO_CASES[1]!.case;
const case3 = DEMO_CASES[2]!.case;
const case4 = DEMO_CASES[3]!.case;

describe("问诊字段校验与完备度", () => {
  it("齐全/缺失/完备度", () => {
    expect(computeMissing(case1)).toEqual([]);
    expect(computeMissing(case4)).toEqual([]);
    expect(computeMissing(case3).some((m) => m.includes("病程"))).toBe(true);
    expect(deriveDataCompleteness(case1, [])).toBe("complete");
    expect(deriveDataCompleteness(case3, computeMissing(case3))).toBe("insufficient");
    expect(deriveDataCompleteness(case1, ["病程"])).toBe("partial");
  });
});

describe("红旗与严重程度", () => {
  it("案例2 命中 RF-HZ-001；案例1/4 无红旗", () => {
    expect(detectRedFlags(case2).some((f) => f.startsWith("RF-HZ-001"))).toBe(true);
    expect(detectRedFlags(case1)).toEqual([]);
    expect(detectRedFlags(case4)).toEqual([]);
  });
  it("severity 枚举派生", () => {
    expect(deriveSeverityLevel(detectRedFlags(case2), [])).toBe("needs_review");
    expect(deriveSeverityLevel(detectRedFlags({ ...case1, symptoms: "风团伴呼吸困难" }), [])).toBe("emergency");
    expect(deriveSeverityLevel([], [])).toBe("routine");
    expect(deriveSeverityLevel([], ["病程"])).toBe("unknown");
  });
});

describe("检索（否定词感知）与引用快照", () => {
  it("v1.1 字段与命中", () => {
    const cites = retrieveCitations(case1);
    expect(cites.length).toBeGreaterThan(0);
    expect(cites[0]).toMatchObject({ source_type: "authorized_case" });
    for (const c of cites) {
      expect(c.citation_id).toMatch(/^CIT-/);
      expect(c.document_title).toContain("合成演示版");
      expect(c.quoted_text).toBeTruthy();
    }
    expect(retrieveCitations(case4)).toEqual([]);
  });
});

describe("normalizeLlmDraft（引用白名单/枚举收拢/权威派生）", () => {
  const cites = retrieveCitations(case2);
  it("编造引用剔除 + 枚举收拢 + escalated 派生", () => {
    const draft = normalizeLlmDraft(
      {
        suspected_conditions: [{
          name: "带状疱疹（可能）", confidence: "high",
          supporting_evidence: [{ statement: "s", citation_ids: [cites[0]!.citation_id, "CIT-FAKE"] }],
        }],
        symptom_assessment: [{ symptom: "灼痛", status: "没有", basis: "猜" }],
        severity: { level: "随便写的", basis: ["红旗命中"] },
      } as never,
      cites, detectRedFlags(case2), [], "fake-model", "llm"
    );
    expect(draft.generated_by).toBe("llm");
    expect(draft.citations.every((c) => c.citation_id !== "CIT-FAKE")).toBe(true);
    expect(draft.suspected_conditions[0]?.supporting_evidence[0]?.citation_ids).toEqual([cites[0]!.citation_id]);
    expect(draft.symptom_assessment[0]?.status).toBe("unknown");
    expect(draft.severity.level).toBe("needs_review");
    expect(draft.workflow_state).toBe("escalated");
    expect(draft.red_flag_escalation.is_blocking).toBe(true);
    expect(draft.blocking_reason).toContain("RF-HZ-001");
    expect(draft.safety_policy_version).toBe(SAFETY_POLICY_VERSION);
    expect(draft.schema_version).toBe(SCHEMA_VERSION);
    expect(draft.uncertainties.some((u) => u.includes("非知识库引用"))).toBe(true);
  });
  it("无证据 → 不足以判断标注", () => {
    const draft = normalizeLlmDraft({}, [], [], [], "m", "llm");
    expect(draft.uncertainties.some((u) => u.includes("知识库未覆盖"))).toBe(true);
    expect(draft.follow_up_questions).toEqual([]);
  });
});

describe("症状评估/用药核对语义", () => {
  it("未询问 → not_asked（≠absent）", () => {
    const assess = buildSymptomAssessment(case3, retrieveCitations(case3));
    expect(assess.find((s) => s.symptom.includes("病程"))?.status).toBe("not_asked");
  });
  it("用药核对项齐全", () => {
    const checks = buildMedicationChecks(case2, retrieveCitations(case2));
    expect(checks.length).toBeGreaterThan(0);
    expect(checks[0]!.items_to_verify).toContain("contraindication");
    expect(checks.every((m) => m.requires_doctor_confirmation)).toBe(true);
  });
});

describe("医生审核与路由（R-G004）", () => {
  const base = { missing: [], askCount: 0, case: case1, decision: "pending" } as unknown as FlowState;
  it("classifyReview", () => {
    expect(classifyReview("确认", false)).toBe("confirmed");
    expect(classifyReview("确认，无修改意见", false)).toBe("confirmed");
    expect(classifyReview("修改：请补充眼科", false)).toBe("modified");
    expect(classifyReview("拒绝：资料不足", false)).toBe("rejected");
  });
  it("route 三条边", () => {
    expect(routeAfterIntake(base)).toBe("screen");
    expect(routeAfterIntake({ ...base, case: null } as unknown as FlowState)).toBe("intake_llm");
    expect(routeAfterScreen({ ...base, missing: ["病程"] })).toBe("ask_more");
    expect(routeAfterScreen({ ...base, missing: ["病程"], askCount: MAX_ASK_ROUNDS })).toBe("retrieve");
    expect(routeAfterReview({ ...base, decision: "confirmed" } as FlowState)).toBe("finalize_report");
    expect(routeAfterReview({ ...base, decision: "modified" } as FlowState)).toBe("revise");
    expect(routeAfterReview({ ...base, decision: "rejected" } as FlowState)).toBe("case_closed");
  });
});

describe("规则抽取与案例匹配", () => {
  it("自由文本/补充合并/触发词", () => {
    const c = buildCaseFromFreeText("身体区域：面部；主诉：红斑3天；症状：瘙痒性红斑；病程：3天；过敏史：否认；当前用药：无");
    expect(c.body_region).toBe("面部");
    const merged = mergeCaseReply(case3, "补充：病程5天；变化趋势逐渐扩大；诱因新换护肤品后出现；"
      + "既往史无特殊；过敏史否认药物食物过敏；当前用药未使用药物");
    expect(computeMissing(merged)).toEqual([]);
    expect(matchDemoCase("案例1")?.id).toBe("1");
    expect(matchDemoCase("案例4")?.id).toBe("4");
    expect(matchDemoCase("你好")).toBeNull();
  });
});

describe("兜底草稿与报告（v1.1 元信息）", () => {
  it("案例2 兜底：escalated + needs_review", () => {
    const draft = buildFallbackDraft(case2, retrieveCitations(case2), detectRedFlags(case2), []);
    expect(draft.workflow_state).toBe("escalated");
    expect(draft.severity.level).toBe("needs_review");
    expect(draft.red_flag_escalation.is_blocking).toBe(true);
  });
  it("案例4 兜底：不编造", () => {
    const draft = buildFallbackDraft(case4, [], [], []);
    expect(draft.suspected_conditions).toEqual([]);
    expect(draft.uncertainties.some((u) => u.includes("不足以判断"))).toBe(true);
    expect(draft.recommended_next_steps.some((n) => n.action.includes("面诊"))).toBe(true);
  });
  it("报告渲染含 v1.1 元信息与 not_asked 语义", () => {
    const report = buildFallbackReport(buildFallbackDraft(case1, retrieveCitations(case1), [], []));
    expect(report).toContain("Schema 1.1");
    expect(report).toContain("安全策略");
    expect(report).toContain("CIT-");
    const partial = buildFallbackReport(buildFallbackDraft(case3, [], [], ["病程"]));
    expect(partial).toContain("未询问");
  });
  it("默认免责声明", () => {
    expect(DEFAULT_DISCLAIMER).toContain("不构成诊断或处方");
  });
  it("症状字段全覆盖（REQUIRED_FIELDS 与 CaseRecord 对齐）", () => {
    expect(REQUIRED_FIELDS.every((f) => f.key in case1)).toBe(true);
  });
});
