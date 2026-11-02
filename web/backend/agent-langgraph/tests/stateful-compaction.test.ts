/**
 * createStatefulFlow 自动压缩接线单测 —— 验证 C1：appConfig 传入时，基座在「新 query」
 * 入口自动触发 applyCompaction（超阈值 → updateState 被调用且用 RemoveMessage 替换历史），
 * 而 resume 分支 / 无 appConfig / 未超阈值 均 no-op。
 *
 * 无凭证、确定性（compactHistory 在无凭证下仅 trimMessages 裁剪，不调模型）。
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import {
  HumanMessage,
  RemoveMessage,
  type BaseMessage,
} from "@langchain/core/messages";
import { AppConfigSchema, type AppConfig } from "../src/runtime/index.js";
import { createStatefulFlow } from "../src/surfaces/stateful-flow.js";

// 强制无凭证 → compactHistory 走「仅裁剪」分支（不调模型，确定性）。
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

// 小阈值 appConfig（绕过 schema min(1000) 生产约束），让少量消息即可超阈值触发。
const smallConfig = {
  ...AppConfigSchema.parse({}),
  compaction: {
    enabled: true,
    contextWindow: 100,
    triggerThreshold: 0.5,
    reserveTokens: 1000,
    keepRecentTokens: 10,
  },
} as unknown as AppConfig;

/** fake graph：getState 返回给定 messages；updateState 记录调用（供断言压缩是否触发）。 */
function makeFakeGraph(messages: BaseMessage[]) {
  const calls: { values: Record<string, unknown> }[] = [];
  const graph = {
    async getState() {
      return {
        values: { messages },
        config: { configurable: { checkpoint_id: "cp" } },
      };
    },
    async updateState(_config: unknown, values: Record<string, unknown>) {
      calls.push({ values });
      return undefined;
    },
    async stream() {
      async function* empty() {
        /* no chunks */
      }
      return empty();
    },
  };
  return { graph, calls };
}

/** 超阈值历史：6 条 ~20 tokens/条 ≈ 120 tokens > 50（100×0.5）阈值。 */
function bigHistory(): BaseMessage[] {
  return Array.from(
    { length: 6 },
    (_, i) => new HumanMessage({ id: `m${i}`, content: "x".repeat(80) })
  );
}

describe("createStatefulFlow 自动压缩接线（无凭证，确定性）", () => {
  it("appConfig + 超阈值历史（新 query）→ updateState 被调，且用 RemoveMessage 替换旧历史", async () => {
    const { graph, calls } = makeFakeGraph(bigHistory());
    const flow = createStatefulFlow<Record<string, unknown>>({
      buildGraph: () => graph,
      toInput: (q) => ({ query: q }),
      toResult: () => ({ answer: "ok" }),
      appConfig: smallConfig,
    });

    await flow.run({ query: "继续" }, "tid-compaction");

    expect(calls.length).toBe(1);
    const written = calls[0]!.values.messages as BaseMessage[];
    // 替换语义：前置若干 RemoveMessage（按 id 删旧），其后接裁剪后的近期消息。
    expect(written.some((m) => m instanceof RemoveMessage)).toBe(true);
  });

  it("resume 分支不压缩（避免干扰挂起的 interrupt）", async () => {
    const { graph, calls } = makeFakeGraph(bigHistory());
    const flow = createStatefulFlow<Record<string, unknown>>({
      buildGraph: () => graph,
      toInput: (q) => ({ query: q }),
      toResult: () => ({ answer: "ok" }),
      appConfig: smallConfig,
    });

    await flow.run({ resume: "reply" }, "tid-resume");
    expect(calls.length).toBe(0);
  });

  it("未传 appConfig → 不压缩（向后兼容）", async () => {
    const { graph, calls } = makeFakeGraph(bigHistory());
    const flow = createStatefulFlow<Record<string, unknown>>({
      buildGraph: () => graph,
      toInput: (q) => ({ query: q }),
      toResult: () => ({ answer: "ok" }),
      // 无 appConfig —— 老用法不应触发压缩
    });

    await flow.run({ query: "hi" }, "tid-nocfg");
    expect(calls.length).toBe(0);
  });

  it("未超阈值历史 → 不压缩", async () => {
    const tiny = [new HumanMessage({ id: "t0", content: "hi" })];
    const { graph, calls } = makeFakeGraph(tiny);
    const flow = createStatefulFlow<Record<string, unknown>>({
      buildGraph: () => graph,
      toInput: (q) => ({ query: q }),
      toResult: () => ({ answer: "ok" }),
      appConfig: smallConfig,
    });

    await flow.run({ query: "hi" }, "tid-tiny");
    expect(calls.length).toBe(0);
  });
});
