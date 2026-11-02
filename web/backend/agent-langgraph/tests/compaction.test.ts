/**
 * compaction 单测 —— 估算 + 裁剪/摘要分支（强制无凭证 → 不调模型）。
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { HumanMessage, AIMessage, RemoveMessage } from "@langchain/core/messages";
import { AppConfigSchema, type AppConfig } from "../src/runtime/index.js";
import { estimateTokens, compactHistory, compactionUpdate } from "../src/libs/compaction.js";

describe("estimateTokens", () => {
  it("按 char/4 向上取整", () => {
    expect(estimateTokens([new HumanMessage("abcd")])).toBe(1);
    expect(estimateTokens([new HumanMessage("ab")])).toBe(1);
    expect(estimateTokens([new HumanMessage("abcdefgh")])).toBe(2);
  });
});

describe("compactionUpdate（替换更新，纯函数）", () => {
  const withIds = (n: number) =>
    Array.from({ length: n }, (_, i) => new HumanMessage({ id: `m${i}`, content: `msg-${i}` }));

  it("压缩变短 → 先删全部旧消息(RemoveMessage)，再写回压缩结果", () => {
    const prior = withIds(10);
    const compacted = [new AIMessage({ id: "sum", content: "摘要" }), prior[8]!, prior[9]!];
    const update = compactionUpdate(prior, compacted);
    // 前 10 条是删除指令（按 id），其后接 3 条压缩结果
    expect(update.length).toBe(13);
    expect(update.slice(0, 10).every((m) => m instanceof RemoveMessage)).toBe(true);
    expect((update[0] as RemoveMessage).id).toBe("m0");
    expect(update.slice(10)).toEqual(compacted);
  });

  it("没变短（没触发压缩）→ 返回 []（调用方跳过 updateState）", () => {
    const prior = withIds(5);
    expect(compactionUpdate(prior, prior)).toEqual([]);
  });

  it("旧消息无 id（删不了）→ 返回 []", () => {
    const prior = [new HumanMessage("a"), new HumanMessage("b")];
    const compacted = [new HumanMessage("a")];
    expect(compactionUpdate(prior, compacted)).toEqual([]);
  });
});

describe("compactHistory（无凭证分支）", () => {
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

  // 用小阈值测裁剪逻辑（绕过 schema 的 min(1000) 生产约束）
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

  it("未超阈值 → 原样返回（同一引用）", async () => {
    const msgs = [new HumanMessage("hi"), new AIMessage("hello")];
    expect(await compactHistory(msgs, smallConfig)).toBe(msgs);
  });

  it("超阈值 + 无凭证 → 仅裁剪（结果短于原）", async () => {
    const msgs = Array.from({ length: 20 }, (_, i) => new HumanMessage(`msg-${i}-` + "x".repeat(20)));
    const result = await compactHistory(msgs, smallConfig);
    expect(result.length).toBeLessThan(msgs.length);
    expect(result.length).toBeGreaterThan(0);
  });

  it("disabled → 原样返回", async () => {
    const cfg = AppConfigSchema.parse({ compaction: { enabled: false } });
    const msgs = Array.from({ length: 20 }, (_, i) => new HumanMessage("x".repeat(50)));
    expect(await compactHistory(msgs, cfg)).toBe(msgs);
  });
});
