/**
 * extractVisibleTextFromMessage —— content 优先，reasoning_content 兜底。
 *
 * 回归：deepseek-v4-flash 等模型偶发把用户可见回答写入 reasoning_content，
 * content=""，导致 respond / ACP 流式无输出。
 */

import { describe, expect, it } from "vitest";
import { AIMessage } from "@langchain/core/messages";
import {
  extractReasoningTextFromMessage,
  extractVisibleTextFromMessage,
} from "../src/libs/nodes/llm.js";
import { createRespondNode } from "../src/app/nodes/respond.js";

describe("extractVisibleTextFromMessage", () => {
  it("优先返回 content", () => {
    const msg = new AIMessage({
      content: "可见回复",
      additional_kwargs: { reasoning_content: "内部推理" },
    });
    expect(extractVisibleTextFromMessage(msg)).toBe("可见回复");
  });

  it("content 空时兜底 reasoning_content", () => {
    const msg = new AIMessage({
      content: "",
      additional_kwargs: { reasoning_content: "你好！我是旅行规划助手" },
    });
    expect(extractVisibleTextFromMessage(msg)).toBe("你好！我是旅行规划助手");
    expect(extractReasoningTextFromMessage(msg)).toBe("你好！我是旅行规划助手");
  });

  it("content 与 reasoning 皆空 → 空串", () => {
    expect(extractVisibleTextFromMessage(new AIMessage({ content: "" }))).toBe("");
    expect(extractVisibleTextFromMessage(null)).toBe("");
  });
});

describe("createRespondNode reasoning fallback", () => {
  it("最后一条 AIMessage content 空时用 reasoning_content 写 output", async () => {
    const node = createRespondNode();
    const out = await node({
      messages: [
        new AIMessage({
          content: "",
          additional_kwargs: { reasoning_content: "欢迎使用旅行规划助手" },
        }),
      ],
      input: "你好",
      steps: [],
      output: "",
    } as any);

    expect(out.output).toBe("欢迎使用旅行规划助手");
    expect(out.steps).toEqual(["respond"]);
  });
});
