/**
 * sanitizeToolCalls —— 清除 checkpoint 中孤立的 tool_calls / content tool_use。
 */

import { describe, expect, it } from "vitest";
import { AIMessage, HumanMessage, ToolMessage, type BaseMessage } from "@langchain/core/messages";
import {
  isInvalidToolResultsError,
  normalizeAiMessageToolCalls,
  sanitizeToolCalls,
} from "../src/libs/messages/sanitize-tool-calls.js";

describe("sanitizeToolCalls", () => {
  it("完整 tool_calls + ToolMessage → 不变", () => {
    const messages = [
      new HumanMessage("hi"),
      new AIMessage({
        content: "",
        tool_calls: [{ id: "call_1", name: "bash", args: { command: "ls" } }],
      }),
      new ToolMessage({ content: "ok", tool_call_id: "call_1" }),
    ];
    expect(sanitizeToolCalls(messages)).toBe(messages);
  });

  it("孤立 tool_calls（无 ToolMessage）→ 从 AIMessage 移除", () => {
    const messages = [
      new HumanMessage("搜一下"),
      new AIMessage({
        content: "",
        tool_calls: [{ id: "call_orphan", name: "web_search", args: { q: "x" } }],
      }),
    ];
    const out = sanitizeToolCalls(messages);
    const ai = out[1] as AIMessage;
    expect(ai.tool_calls ?? []).toHaveLength(0);
  });

  it("部分孤立：保留有 ToolMessage 的 call，移除孤立的", () => {
    const messages = [
      new AIMessage({
        content: "",
        tool_calls: [
          { id: "call_ok", name: "bash", args: {} },
          { id: "call_bad", name: "web_search", args: {} },
        ],
      }),
      new ToolMessage({ content: "done", tool_call_id: "call_ok" }),
    ];
    const out = sanitizeToolCalls(messages);
    const ai = out[0] as AIMessage;
    expect(ai.tool_calls?.map((c) => c.id)).toEqual(["call_ok"]);
  });

  it("无 tool_calls → 原样返回", () => {
    const messages = [new HumanMessage("hello"), new AIMessage("reply")];
    expect(sanitizeToolCalls(messages)).toBe(messages);
  });

  it("checkpoint 反序列化 plain object（非类实例）→ 仍能清除孤立 tool_calls", () => {
    const messages = [
      { type: "human", content: "搜一下" },
      {
        type: "ai",
        content: "",
        tool_calls: [{ id: "call_orphan", name: "web_search", args: { q: "x" } }],
      },
    ] as unknown as BaseMessage[];
    const out = sanitizeToolCalls(messages);
    const ai = out[1] as AIMessage;
    expect(ai.tool_calls ?? []).toHaveLength(0);
  });

  // 回归：checkpoint 常同时带顶层 + additional_kwargs.tool_calls；
  // 只清顶层时 OpenAI converter 会回退序列化 kwargs → 仍 400 INVALID_TOOL_RESULTS
  it("additional_kwargs.tool_calls 残留 → 一并清除", () => {
    const messages = [
      {
        type: "ai",
        content: "about to call",
        id: "a1",
        tool_calls: [{ id: "call_orphan", name: "travel_guide", args: {} }],
        additional_kwargs: {
          tool_calls: [
            {
              id: "call_orphan",
              name: "travel_guide",
              type: "function",
              function: { name: "travel_guide", arguments: "{}" },
            },
          ],
        },
      },
    ] as unknown as BaseMessage[];
    const out = sanitizeToolCalls(messages);
    const ai = out[0] as AIMessage;
    expect(ai.tool_calls ?? []).toHaveLength(0);
    expect(ai.additional_kwargs?.tool_calls).toBeUndefined();
  });

  it("仅 additional_kwargs.tool_calls（无顶层）→ 仍能识别并清除孤立项", () => {
    const messages = [
      {
        type: "ai",
        content: "about to call",
        id: "a1",
        additional_kwargs: {
          tool_calls: [{ id: "call_only_kwargs", name: "bash", args: {} }],
        },
      },
    ] as unknown as BaseMessage[];
    const out = sanitizeToolCalls(messages);
    const ai = out[0] as AIMessage;
    expect(ai.tool_calls ?? []).toHaveLength(0);
    expect(ai.additional_kwargs?.tool_calls).toBeUndefined();
  });

  // 线上回归：DeepSeek Anthropic 协议把 tool_use 只写在 content[]，tool_calls 为空
  it("content[] 孤立 tool_use（无 tool_calls）→ 剥离块并保留 text", () => {
    const messages = [
      new HumanMessage("做个简历"),
      new AIMessage({
        content: [
          { type: "text", text: "好的，我先收集信息" },
          {
            type: "tool_use",
            id: "call_00_IDLSYHsssZRUSAYR7PJV3652",
            name: "ask-question__nuwax_ask_question",
            input: { title: "确认" },
          },
        ],
      }),
      new HumanMessage("？"),
    ];
    const out = sanitizeToolCalls(messages);
    expect(out).not.toBe(messages);
    const ai = out[1] as AIMessage;
    expect(ai.tool_calls ?? []).toHaveLength(0);
    expect(Array.isArray(ai.content)).toBe(true);
    const blocks = ai.content as Array<{ type?: string; text?: string; id?: string }>;
    expect(blocks.every((b) => b.type !== "tool_use")).toBe(true);
    expect(blocks.some((b) => b.type === "text" && b.text?.includes("收集"))).toBe(true);
  });

  it("ToolMessage 存在但不紧邻 → 仍视为孤立并剥离（Anthropic immediately after）", () => {
    const messages = [
      new AIMessage({
        content: "",
        tool_calls: [{ id: "call_gap", name: "bash", args: {} }],
      }),
      new HumanMessage("插进来的用户消息"),
      new ToolMessage({ content: "late", tool_call_id: "call_gap" }),
    ];
    const out = sanitizeToolCalls(messages);
    const ai = out[0] as AIMessage;
    expect(ai.tool_calls ?? []).toHaveLength(0);
  });

  it("content tool_use + 紧邻 ToolMessage → 保留", () => {
    const messages = [
      new AIMessage({
        content: [
          { type: "text", text: "calling" },
          {
            type: "tool_use",
            id: "call_ok",
            name: "bash",
            input: { command: "ls" },
          },
        ],
        tool_calls: [{ id: "call_ok", name: "bash", args: { command: "ls" } }],
      }),
      new ToolMessage({ content: "ok", tool_call_id: "call_ok" }),
    ];
    expect(sanitizeToolCalls(messages)).toBe(messages);
  });
});

describe("normalizeAiMessageToolCalls", () => {
  it("content tool_use 且 tool_calls 为空 → 同步进 tool_calls", () => {
    const ai = new AIMessage({
      content: [
        { type: "text", text: "hi" },
        {
          type: "tool_use",
          id: "call_sync",
          name: "web_search",
          input: { q: "x" },
        },
      ],
    });
    const out = normalizeAiMessageToolCalls(ai);
    expect(out).not.toBe(ai);
    expect(out.tool_calls?.map((c) => c.id)).toEqual(["call_sync"]);
    expect(out.tool_calls?.[0]?.name).toBe("web_search");
    expect(out.tool_calls?.[0]?.args).toEqual({ q: "x" });
  });

  it("tool_calls 已含同 id → 原样返回", () => {
    const ai = new AIMessage({
      content: [
        {
          type: "tool_use",
          id: "call_dup",
          name: "bash",
          input: {},
        },
      ],
      tool_calls: [{ id: "call_dup", name: "bash", args: {} }],
    });
    expect(normalizeAiMessageToolCalls(ai)).toBe(ai);
  });

  it("无 tool_use → 原样返回", () => {
    const ai = new AIMessage("plain");
    expect(normalizeAiMessageToolCalls(ai)).toBe(ai);
  });
});

describe("isInvalidToolResultsError", () => {
  it("识别 Anthropic tool_use without tool_result", () => {
    expect(
      isInvalidToolResultsError(
        new Error(
          '400 {"error":{"message":"messages.26: `tool_use` ids were found without `tool_result` blocks immediately after: call_00_x"}}'
        )
      )
    ).toBe(true);
  });

  it("识别 LangChain INVALID_TOOL_RESULTS", () => {
    expect(
      isInvalidToolResultsError(
        new Error("INVALID_TOOL_RESULTS\nTroubleshooting URL: https://docs.langchain.com/...")
      )
    ).toBe(true);
  });

  it("无关错误 → false", () => {
    expect(isInvalidToolResultsError(new Error("rate limit"))).toBe(false);
  });
});
