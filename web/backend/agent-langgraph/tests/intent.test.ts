/**
 * 意图路由（TS 侧）测试 —— 与 tests-server/test_intent.py 同源口径。
 *
 * 覆盖：
 *  - 规格副本与 shared/intent_spec.json 字节级同步（SHA-256）；
 *  - 第一层规则表 + 四道守卫（标注集 L1 子集零误判、非 L1 子集零误抢）；
 *  - 第三层输出校验（枚举/槽位白名单/置信度截断/候选去重）；
 *  - 假模型下的路由闭环（高置信执行、歧义需确认、非法输出降级、L1 命中不调模型）；
 *  - 图拓扑不变（12 节点 / 15 边）、intake 标注写入 intentMeta。
 */

import { describe, it, expect } from "vitest";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  classifyControl,
  classifyRules,
  guardedIntents,
  isMultiTarget,
  type IntentState,
} from "../src/app/intent/rules.js";
import { sanitizeRoute, parseRoutePayload, routeWithLlm, type RouteInvoker } from "../src/app/intent/llm-router.js";
import { routeIntent, routeIntentRules } from "../src/app/intent/index.js";
import { INTENT_SPEC } from "../src/app/intent/spec.js";
import { createIntakeNode } from "../src/app/nodes/derm.js";
import { getFlowTopology } from "../src/app/topology.js";
import type { FlowState } from "../src/app/state.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const SPEC_COPY = resolve(HERE, "../src/app/intent/prototypes.json");
const SPEC_SRC = resolve(HERE, "../../shared/intent_spec.json");
const GOLDEN = resolve(HERE, "../../tests-server/data/intent_golden.jsonl");

function sha256(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

interface GoldenRow {
  text: string;
  phase?: string;
  workflow_state?: string;
  case_empty?: boolean;
  expect: string;
  expect_layer: number;
}

function goldenRows(): GoldenRow[] {
  return readFileSync(GOLDEN, "utf-8")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => JSON.parse(l) as GoldenRow);
}

function stateOf(row: GoldenRow): IntentState {
  const empty = row.case_empty ?? true;
  return {
    phase: row.phase,
    workflow_state: row.workflow_state,
    has_draft: !empty,
    case_empty: empty,
  };
}

/** 造一个固定输出的假模型；记录调用次数。 */
function fakeInvoker(payload: unknown) {
  const calls: Array<{ system: string; user: string }> = [];
  const invoke: RouteInvoker = async (system, user) => {
    calls.push({ system, user });
    return payload;
  };
  return { invoke, calls };
}

describe("规格同步", () => {
  it("prototypes.json 与 shared/intent_spec.json 字节一致", () => {
    // 副本漂移时先改 shared/intent_spec.json，再执行：
    //   cp web/backend/shared/intent_spec.json web/backend/agent-langgraph/src/app/intent/prototypes.json
    expect(sha256(SPEC_COPY), "prototypes.json 已与 shared/intent_spec.json 漂移，请重新复制").toBe(
      sha256(SPEC_SRC)
    );
  });

  it("意图目录覆盖全部业务/控制意图（35 类）", () => {
    expect(Object.keys(INTENT_SPEC.intents)).toHaveLength(35);
    for (const name of ["case_write", "differential", "settings_control", "confirm", "deny"]) {
      expect(INTENT_SPEC.intents[name]).toBeTruthy();
    }
  });
});

describe("第一层：标注集", () => {
  const rows = goldenRows();

  it("L1 子集全部答对，非 L1 子集一条都不抢", () => {
    const bad: Array<[string, string, string | null]> = [];
    for (const row of rows) {
      const hit = classifyRules(row.text, stateOf(row));
      if (row.expect_layer === 1) {
        if (!hit || hit.intent !== row.expect) bad.push([row.text, row.expect, hit?.intent ?? null]);
      } else if (hit) {
        bad.push([row.text, `应下放 L3（${row.expect}）`, hit.intent]);
      }
    }
    expect(bad).toEqual([]);
    expect(rows.filter((r) => r.expect_layer === 1).length).toBeGreaterThan(100);
  });
});

describe("第一层：四道守卫", () => {
  it("否定守卫", () => {
    expect(classifyRules("请只写现病史，不要生成诊断和治疗建议")?.intent).toBe("history");
    expect(classifyRules("先不要导出报告")).toBeNull();
    expect(classifyRules("暂时不生成报告")).toBeNull();
  });

  it("引号守卫", () => {
    expect(classifyRules("医生说「鉴别诊断」这个词不用管，先写现病史")?.intent).toBe("history");
    expect(classifyRules("「导出报告」是什么意思")).toBeNull();
  });

  it("长度守卫", () => {
    const longText =
      "患者男35岁，皮疹三天，先帮我看看这个情况，另外我想问一下是否需要做鉴别诊断，" +
      "还想知道有没有必要做真菌镜检，以及后续怎么随访";
    expect(longText.length).toBeGreaterThan(40);
    expect(classifyRules(longText)).toBeNull();
  });

  it("否定守卫覆盖「不需要/不必/无须」等情态短语", () => {
    for (const text of [
      "不需要分析图片",
      "不必做鉴别诊断",
      "不需要再导出报告",
      "没有做用药核对",
      "未做脱敏",
      "没必要做鉴别诊断",
    ]) {
      expect(classifyRules(text), text).toBeNull();
    }
    expect(classifyRules("不同部位的鉴别诊断")?.intent).toBe("differential");
    expect(classifyRules("不错的鉴别诊断")?.intent).toBe("differential");
  });

  it("控制指令被否定时不得执行成相反动作", () => {
    for (const text of ["不要跳过3D", "别打开人体模型", "无需撤销刚才的信息", "不要保存当前病例"]) {
      expect(classifyRules(text), text).toBeNull();
    }
    expect(classifyControl("不要跳过3D")).toBe("");
    expect(classifyRules("不用3D")?.intent).toBe("skip_3d");
    expect(classifyRules("打开人体模型")?.intent).toBe("open_model");
  });

  it("被否定/引号包裹的意图进 guarded 集合", () => {
    expect(guardedIntents("不要跳过3D").has("skip_3d")).toBe(true);
    expect(guardedIntents("不需要分析图片").has("image_analyze")).toBe(true);
    expect(guardedIntents("「打开人体模型」这个按钮在哪").has("open_model")).toBe(true);
    expect(guardedIntents("不用3D").has("skip_3d")).toBe(false);
    expect(guardedIntents("这个病还要考虑哪些别的诊断").has("differential")).toBe(false);
  });

  it("多目标守卫", () => {
    expect(classifyRules("帮我写现病史，然后再做鉴别诊断")).toBeNull();
    // 只命中一个关键词但并列了多个动作，也不能只挑一个执行
    expect(classifyRules("分析一下这张图，然后整理病历，再导出报告")).toBeNull();
    expect(isMultiTarget("分析一下这张图，然后整理病历，再导出报告")).toBe(true);
    expect(isMultiTarget("打喷嚏，然后皮疹加重")).toBe(false);
    expect(classifyRules("请整理现病史")?.intent).toBe("history");
    expect(classifyRules("再帮我写一份病历")?.intent).toBe("case_write");
  });

  it("确认/拒绝受状态约束", () => {
    expect(classifyRules("确认")).toBeNull();
    expect(classifyRules("确认", { workflow_state: "awaiting_review" })?.intent).toBe("confirm");
    expect(classifyRules("拒绝")).toBeNull();
    expect(classifyRules("拒绝", { workflow_state: "planning" })?.intent).toBe("deny");
  });

  it("控制指令优先于业务规则，病例对比带 case_id 槽位", () => {
    expect(classifyRules("跳过3D")?.intent).toBe("skip_3d");
    expect(classifyRules("保存当前病例")?.intent).toBe("save_case");
    expect(classifyRules("把模型改成 qwen")?.intent).toBe("settings_control");
    const hit = classifyRules("对比 abcdef12");
    expect(hit?.intent).toBe("case_compare");
    expect(hit?.slots).toEqual({ case_id: "abcdef12" });
  });
});

describe("第三层：输出校验", () => {
  it("枚举外意图/非 JSON → null", () => {
    expect(sanitizeRoute({ intent: "not_an_intent" })).toBeNull();
    expect(sanitizeRoute("完全不是 JSON")).toBeNull();
    expect(sanitizeRoute(null)).toBeNull();
  });

  it("槽位白名单 + 置信度截断 + 候选去重", () => {
    const route = sanitizeRoute({
      intent: "settings_control",
      confidence: 1.7,
      ambiguity: false,
      slots: { language: "en", model_hint: "qwen", case_id: "abc" },
      alternatives: [
        { intent: "settings_control", reason: "same" },
        { intent: "summary" },
        { intent: "history" },
        { intent: "referral" },
        { intent: "followup" },
        { intent: "patient_edu" },
      ],
      reason: "改设置",
    });
    expect(route).not.toBeNull();
    expect(route!.confidence).toBe(1);
    expect(route!.slots).toEqual({ language: "en", model_hint: "qwen" });
    expect(route!.alternatives.map((a) => a.intent)).toEqual([
      "summary",
      "history",
      "referral",
      "followup",
    ]);
  });

  it("非法语言值被丢弃；case_compare 只留 case_id", () => {
    const a = sanitizeRoute({ intent: "settings_control", confidence: 0.8, slots: { language: "fr" } });
    expect(a!.slots).toEqual({});
    const b = sanitizeRoute({
      intent: "case_compare",
      confidence: 0.9,
      slots: { case_id: "abcdef12", language: "en" },
    });
    expect(b!.slots).toEqual({ case_id: "abcdef12" });
  });

  it("能从围栏/夹带文本中取出 JSON", () => {
    const raw = parseRoutePayload('好的，结果如下：\n```json\n{"intent":"summary","confidence":0.9}\n```');
    expect(raw).toMatchObject({ intent: "summary", confidence: 0.9 });
    const route = sanitizeRoute('{"intent":"summary","confidence":0.9,"ambiguity":false,"reason":"r"}');
    expect(route?.intent).toBe("summary");
  });
});

describe("第三层：假模型闭环", () => {
  it("高置信 → 直接定案（不确认）", async () => {
    const { invoke } = fakeInvoker({
      intent: "differential",
      confidence: 0.91,
      ambiguity: false,
      alternatives: [],
      reason: "医生在问还考虑哪些诊断",
    });
    const meta = await routeIntent("这个病还要考虑哪些别的诊断", {}, { invoke });
    expect(meta.layer).toBe(3);
    expect(meta.source).toBe("llm");
    expect(meta.intent).toBe("differential");
    expect(meta.needs_confirm).toBe(false);
    expect(meta.degraded).toBe(false);
    expect(meta.candidates).toEqual([]);
  });

  it("歧义 / 低于阈值 → needs_confirm 且给候选", async () => {
    const { invoke } = fakeInvoker({
      intent: "differential",
      confidence: 0.52,
      ambiguity: true,
      alternatives: [{ intent: "treatment_ref", reason: "也可能是在问治疗" }],
      reason: "两种理解",
    });
    const meta = await routeIntent("这个病还要考虑哪些别的诊断", {}, { invoke });
    expect(meta.needs_confirm).toBe(true);
    expect(meta.intent).toBe("differential");
    expect(meta.candidates.map((c) => c.intent)).toEqual(["differential", "treatment_ref"]);
    expect(meta.candidates[1]!.label).toBeTruthy();
  });

  it("非法输出 / 抛错 → 降级且不执行", async () => {
    const bad = await routeIntent("嗯", {}, { invoke: fakeInvoker({ intent: "nope" }).invoke });
    expect(bad.degraded).toBe(true);
    expect(bad.intent).toBeNull();
    expect(bad.needs_confirm).toBe(false);

    const boom = await routeIntent("嗯", {}, {
      invoke: async () => {
        throw new Error("network down");
      },
    });
    expect(boom.degraded).toBe(true);
    expect(boom.intent).toBeNull();
  });

  it("LLM 把被否定的动作路由回来也不得直接执行", async () => {
    const { invoke } = fakeInvoker({ intent: "skip_3d", confidence: 0.95, ambiguity: false, reason: "r" });
    const meta = await routeIntent("不要跳过3D", {}, { invoke });
    expect(meta.needs_confirm).toBe(true);
    expect(meta.intent).toBe("skip_3d"); // 仅作标注，调用方必须尊重 needs_confirm
  });

  it("无模型 → 规则层降级（不伪造 AI 路由）", async () => {
    const meta = await routeIntent("这个病还要考虑哪些别的诊断", {});
    expect(meta.degraded).toBe(true);
    expect(meta.source).toBe("fallback");
    expect(meta.intent).toBeNull();
  });

  it("第一层命中不解析模型、不调 LLM", async () => {
    let resolved = 0;
    const { invoke, calls } = fakeInvoker({ intent: "summary", confidence: 0.99 });
    const meta = await routeIntent("请整理现病史", {}, {
      resolveInvoke: () => {
        resolved += 1;
        return invoke;
      },
    });
    expect(meta.layer).toBe(1);
    expect(meta.intent).toBe("history");
    expect(resolved).toBe(0);
    expect(calls).toHaveLength(0);
  });

  it("routeWithLlm 拒绝无枚举的模型输出", async () => {
    const { invoke } = fakeInvoker("我觉得这是鉴别诊断");
    expect(await routeWithLlm("啊", {}, invoke)).toBeNull();
  });
});

describe("图接入：标注不改拓扑", () => {
  it("节点 12 / 边 15 不变", async () => {
    const { nodes, edges } = await getFlowTopology();
    expect(nodes).toHaveLength(12);
    expect(edges).toHaveLength(15);
  });

  it("intake 写入 intentMeta 与轨迹", async () => {
    const intake = createIntakeNode();
    const out = await intake({ input: "请整理现病史", messages: [] } as unknown as FlowState);
    expect(out.intentMeta?.intent).toBe("history");
    expect(out.intentMeta?.layer).toBe(1);
    expect(out.steps?.some((s) => s.startsWith("intent:rule@L1:history"))).toBe(true);
  });

  it("合成案例：确定性路径不调用路由回调", async () => {
    let called = 0;
    const intake = createIntakeNode({
      route: () => {
        called += 1;
        return routeIntentRules("x");
      },
    });
    const out = await intake({ input: "案例1", messages: [] } as unknown as FlowState);
    expect(out.case).toBeTruthy();
    expect(called).toBe(0);
    expect(out.intentMeta).not.toBeNull();
  });
});
