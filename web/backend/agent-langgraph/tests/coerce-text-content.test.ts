/**
 * coerceMessagesToTextContent —— 多模态 content（image_url 等）压成纯文本。
 *
 * 回归：chrome-devtools take_screenshot 写入 image_url 后，智谱 GLM 报
 * `400 messages.content.type 参数非法，取值范围 ['text']`，会话不可恢复。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AIMessage, HumanMessage, ToolMessage, type BaseMessage } from "@langchain/core/messages";
import { tool } from "@langchain/core/tools";
import { z } from "zod";
import {
  coerceContentToText,
  coerceMessagesToTextContent,
  isIllegalContentTypeError,
  messageContentNeedsTextCoerce,
  resolveCoerceMode,
  shouldCoerceToTextOnly,
} from "../src/libs/messages/coerce-text-content.js";
import { repairCheckpointMessages } from "../src/libs/messages/repair-checkpoint.js";
import { createToolExecNode } from "../src/libs/nodes/tools.js";

describe("coerceContentToText", () => {
  it("纯字符串原样返回", () => {
    expect(coerceContentToText("hello")).toBe("hello");
  });

  it("text block 数组拼接", () => {
    expect(
      coerceContentToText([
        { type: "text", text: "Took a screenshot" },
        { type: "text", text: "of viewport" },
      ])
    ).toBe("Took a screenshot\nof viewport");
  });

  it("image_url + text → 保留文本并替换图片为占位", () => {
    const b64 = "A".repeat(1200); // 约 900B 原始
    const out = coerceContentToText([
      { type: "text", text: "Took a screenshot of the current page's viewport." },
      {
        type: "image_url",
        image_url: { url: `data:image/png;base64,${b64}` },
      },
    ]);
    expect(out).toContain("Took a screenshot of the current page's viewport.");
    expect(out).toMatch(/\[image_url omitted: image\/png/);
    expect(out).not.toContain(b64);
  });

  it("MCP 风格 image data block → 占位", () => {
    const out = coerceContentToText([
      { type: "image", data: "AAAA", mimeType: "image/jpeg" },
    ]);
    expect(out).toMatch(/\[image omitted: image\/jpeg/);
  });
});

describe("coerceMessagesToTextContent", () => {
  it("无多模态 → 返回原数组引用", () => {
    const messages = [
      new HumanMessage("hi"),
      new AIMessage("ok"),
      new ToolMessage({ content: "done", tool_call_id: "c1" }),
    ];
    expect(coerceMessagesToTextContent(messages)).toBe(messages);
  });

  it("仅含 thinking / tool_use 的数组 → 不剥（保留 Anthropic 协议块）", () => {
    const messages = [
      new AIMessage({
        content: [
          { type: "thinking", thinking: "plan…" },
          { type: "text", text: "answer" },
        ] as any,
      }),
    ];
    expect(coerceMessagesToTextContent(messages)).toBe(messages);
  });

  it("ToolMessage 含 image_url → 压成纯文本并保留 tool_call_id", () => {
    const messages = [
      new HumanMessage("打开百度"),
      new AIMessage({
        content: "",
        tool_calls: [{ id: "call_shot", name: "take_screenshot", args: {} }],
      }),
      new ToolMessage({
        id: "tm1",
        content: [
          { type: "text", text: "Took a screenshot of the current page's viewport." },
          {
            type: "image_url",
            image_url: {
              url: `data:image/png;base64,${"B".repeat(800)}`,
            },
          },
        ],
        tool_call_id: "call_shot",
        name: "chrome-devtools__take_screenshot",
      }),
    ];
    const out = coerceMessagesToTextContent(messages);
    expect(out).not.toBe(messages);
    const tm = out[2] as ToolMessage;
    expect(typeof tm.content).toBe("string");
    expect(tm.content as string).toContain("Took a screenshot");
    expect(tm.content as string).toMatch(/image_url omitted/);
    expect(tm.tool_call_id).toBe("call_shot");
    expect(tm.name).toBe("chrome-devtools__take_screenshot");
  });

  it("AIMessage 重建时保留 usage_metadata / response_metadata", () => {
    const messages = [
      new AIMessage({
        id: "ai1",
        content: [
          { type: "text", text: "see" },
          { type: "image_url", image_url: { url: "data:image/png;base64,xx" } },
        ] as any,
        response_metadata: { model: "glm-5.2" },
        usage_metadata: { input_tokens: 10, output_tokens: 2, total_tokens: 12 },
      }),
    ];
    const out = coerceMessagesToTextContent(messages);
    const ai = out[0] as AIMessage;
    expect(typeof ai.content).toBe("string");
    expect(ai.response_metadata).toMatchObject({ model: "glm-5.2" });
    expect(ai.usage_metadata).toMatchObject({ input_tokens: 10, total_tokens: 12 });
  });

  it("mode=all-non-string 时连 thinking 一并压扁", () => {
    const messages = [
      new AIMessage({
        content: [
          { type: "thinking", thinking: "secret plan" },
          { type: "text", text: "hi" },
        ] as any,
      }),
    ];
    expect(coerceMessagesToTextContent(messages)).toBe(messages);
    const flat = coerceMessagesToTextContent(messages, { mode: "all-non-string" });
    expect(flat).not.toBe(messages);
    expect(typeof (flat[0] as AIMessage).content).toBe("string");
    expect((flat[0] as AIMessage).content as string).toContain("secret plan");
    expect((flat[0] as AIMessage).content as string).toContain("hi");
  });

  it("checkpoint plain object 反序列化 → 仍能压文本", () => {
    const messages = [
      {
        type: "tool",
        content: [
          { type: "text", text: "ok" },
          { type: "image_url", image_url: { url: "data:image/png;base64,xxx" } },
        ],
        tool_call_id: "call_x",
        id: "tm_plain",
      },
    ] as unknown as BaseMessage[];
    const out = coerceMessagesToTextContent(messages);
    expect(typeof (out[0] as ToolMessage).content).toBe("string");
    expect((out[0] as ToolMessage).tool_call_id).toBe("call_x");
  });
});

describe("messageContentNeedsTextCoerce / shouldCoerceToTextOnly / isIllegalContentTypeError", () => {
  it("detects multimodal content，忽略 thinking", () => {
    expect(messageContentNeedsTextCoerce("plain")).toBe(false);
    expect(messageContentNeedsTextCoerce([{ type: "text", text: "a" }])).toBe(false);
    expect(
      messageContentNeedsTextCoerce([
        { type: "thinking", thinking: "…" },
        { type: "text", text: "a" },
      ])
    ).toBe(false);
    expect(
      messageContentNeedsTextCoerce([
        { type: "text", text: "a" },
        { type: "image_url", image_url: { url: "x" } },
      ])
    ).toBe(true);
  });

  it("shouldCoerceToTextOnly 尊重 supportsVision", () => {
    const prev = process.env.FLOW_SUPPORTS_VISION;
    try {
      delete process.env.FLOW_SUPPORTS_VISION;
      expect(shouldCoerceToTextOnly()).toBe(true);
      expect(
        shouldCoerceToTextOnly({ model: { settings: { supportsVision: true } } })
      ).toBe(false);
      process.env.FLOW_SUPPORTS_VISION = "1";
      expect(shouldCoerceToTextOnly()).toBe(false);
    } finally {
      if (prev === undefined) delete process.env.FLOW_SUPPORTS_VISION;
      else process.env.FLOW_SUPPORTS_VISION = prev;
    }
  });

  it("识别智谱 content.type 非法错误，且不误伤无关文案", () => {
    expect(
      isIllegalContentTypeError(
        new Error("400 messages.content.type 参数非法，取值范围 ['text']")
      )
    ).toBe(true);
    expect(isIllegalContentTypeError(new Error("rate limit"))).toBe(false);
    // 仅有 content.type 字样、无非法语义 → 不认
    expect(isIllegalContentTypeError(new Error("see content.type docs"))).toBe(false);
  });

  it("resolveCoerceMode：anthropic 只剥图；openai 兼容压扁全部非字符串", () => {
    expect(resolveCoerceMode({ model: { provider: "anthropic" } })).toBe("images");
    expect(resolveCoerceMode({ model: { provider: "openai" } })).toBe("all-non-string");
    expect(resolveCoerceMode()).toBe("all-non-string");
  });
});

describe("repairCheckpointMessages + coerce", () => {
  it("run 入口修复：剥掉历史 ToolMessage 中的 image_url", () => {
    const prior = [
      new HumanMessage({ id: "h1", content: "打开百度" }),
      new AIMessage({
        id: "a1",
        content: "",
        tool_calls: [{ id: "call_shot", name: "take_screenshot", args: {} }],
      }),
      new ToolMessage({
        id: "tm1",
        content: [
          { type: "text", text: "Took a screenshot" },
          {
            type: "image_url",
            image_url: { url: `data:image/png;base64,${"C".repeat(400)}` },
          },
        ],
        tool_call_id: "call_shot",
      }),
    ];
    const repaired = repairCheckpointMessages(prior);
    expect(typeof (repaired[2] as ToolMessage).content).toBe("string");
    expect((repaired[2] as ToolMessage).content as string).not.toContain("base64");
  });
});

describe("createToolExecNode 写路径 coerce", () => {
  it("默认 text-only（openai）：工具返回 image_url → checkpoint 写入纯文本", async () => {
    const prev = process.env.FLOW_SUPPORTS_VISION;
    delete process.env.FLOW_SUPPORTS_VISION;
    try {
      const screenshot = tool(
        async () =>
          [
            { type: "text", text: "Took a screenshot" },
            {
              type: "image_url",
              image_url: { url: `data:image/png;base64,${"D".repeat(200)}` },
            },
          ] as any,
        {
          name: "screenshot",
          schema: z.object({}),
          description: "return a screenshot",
        }
      );
      const node = createToolExecNode<{ messages: BaseMessage[] }>({
        tools: [screenshot] as any,
        config: { model: { provider: "openai", settings: {} } } as any,
      });
      const ai = new AIMessage({
        content: "",
        tool_calls: [{ id: "tc_shot", name: "screenshot", args: {} }] as any,
      });
      const out = (await node({ messages: [ai] })) as any;
      const tm = out.messages[0] as ToolMessage;
      expect(typeof tm.content).toBe("string");
      expect(tm.content as string).toContain("Took a screenshot");
      expect(tm.content as string).toMatch(/image_url omitted/);
      expect(tm.content as string).not.toContain("DDDD");
    } finally {
      if (prev === undefined) delete process.env.FLOW_SUPPORTS_VISION;
      else process.env.FLOW_SUPPORTS_VISION = prev;
    }
  });

  it("openai：纯 text block 数组也压成字符串（智谱兼容）", async () => {
    const prev = process.env.FLOW_SUPPORTS_VISION;
    delete process.env.FLOW_SUPPORTS_VISION;
    try {
      const echo = tool(
        async () => [{ type: "text", text: "hello blocks" }] as any,
        {
          name: "echo_blocks",
          schema: z.object({}),
          description: "return text blocks",
        }
      );
      const node = createToolExecNode<{ messages: BaseMessage[] }>({
        tools: [echo] as any,
        config: { model: { provider: "openai", settings: {} } } as any,
      });
      const ai = new AIMessage({
        content: "",
        tool_calls: [{ id: "tc_echo", name: "echo_blocks", args: {} }] as any,
      });
      const out = (await node({ messages: [ai] })) as any;
      expect(typeof (out.messages[0] as ToolMessage).content).toBe("string");
      expect((out.messages[0] as ToolMessage).content).toBe("hello blocks");
    } finally {
      if (prev === undefined) delete process.env.FLOW_SUPPORTS_VISION;
      else process.env.FLOW_SUPPORTS_VISION = prev;
    }
  });
});

/**
 * think 节点：mock resolveModel + 直通 invokeWithResilience（跳过退避重试），
 * 断言送进 LLM 的 messages 已无 image_url，且 content.type 400 时 aggressive 重试。
 */
const mockInvoke = vi.fn();

vi.mock("../src/runtime/index.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/runtime/index.js")>();
  return {
    ...actual,
    resolveModel: () => ({
      bindTools: () => ({
        invoke: (...args: unknown[]) => mockInvoke(...args),
      }),
    }),
  };
});

vi.mock("../src/runtime/services/llm-resilience.js", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../src/runtime/services/llm-resilience.js")>();
  return {
    ...actual,
    // 直通：避免 content.type 错误被 resilience 连打 3 次 + 退避拖慢单测
    invokeWithResilience: async (
      model: { invoke: (m: BaseMessage[], o?: { signal?: AbortSignal }) => Promise<unknown> },
      messages: BaseMessage[],
      opts?: { signal?: AbortSignal }
    ) => model.invoke(messages, { signal: opts?.signal }),
    resolveLlmResilience: () => ({
      shortTimeoutMs: 5_000,
      longTimeoutMs: 5_000,
      maxConcurrent: 2,
    }),
  };
});

describe("createThinkNode content coerce + 自愈重试", () => {
  const originalApiKey = process.env.OPENAI_API_KEY;

  beforeEach(() => {
    process.env.OPENAI_API_KEY = "test-key-for-think-coerce";
    mockInvoke.mockReset();
  });

  afterEach(() => {
    if (originalApiKey === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = originalApiKey;
  });

  it("调 LLM 前剥掉 image_url；content.type 400 时 aggressive 重试并写回历史", async () => {
    const seen: BaseMessage[][] = [];
    let calls = 0;
    mockInvoke.mockImplementation(async (msgs: BaseMessage[]) => {
      seen.push(msgs);
      calls += 1;
      if (calls === 1) {
        throw new Error("400 messages.content.type 参数非法，取值范围 ['text']");
      }
      return new AIMessage({ content: "ok after retry" });
    });

    const { createThinkNode } = await import("../src/app/nodes/think.js");
    const node = createThinkNode({
      config: {
        model: {
          provider: "openai",
          model: "gpt-test",
          settings: {},
        },
      } as any,
      allTools: [],
      systemPrompt: "sys",
    });

    const poisoned = [
      new HumanMessage({ id: "h1", content: "打开百度" }),
      new ToolMessage({
        id: "tm1",
        content: [
          { type: "text", text: "shot" },
          {
            type: "image_url",
            image_url: { url: `data:image/png;base64,${"E".repeat(100)}` },
          },
        ],
        tool_call_id: "c1",
      }),
    ];

    const out = await node({
      messages: poisoned,
      input: "打开百度",
      steps: [],
    } as any);

    expect(calls).toBe(2);
    expect(out.steps).toContain("think#history-writeback");
    // RemoveMessage ×2 + coerced human/tool + AI
    expect((out.messages ?? []).length).toBeGreaterThan(1);
    const last = out.messages![out.messages!.length - 1] as AIMessage;
    expect(last.content).toBe("ok after retry");

    // 写回的 ToolMessage 应为纯字符串
    const writtenTm = (out.messages ?? []).find(
      (m) => m._getType?.() === "tool"
    ) as ToolMessage | undefined;
    expect(writtenTm).toBeDefined();
    expect(typeof writtenTm!.content).toBe("string");

    for (const msgs of seen) {
      const tm = msgs.find((m) => m._getType?.() === "tool") as ToolMessage | undefined;
      expect(tm).toBeDefined();
      expect(typeof tm!.content).toBe("string");
      expect(tm!.content as string).not.toContain("EEEE");
      expect(String(tm!.content)).toMatch(/image_url omitted|shot/);
    }
  });

  it("supportsVision 开启时首轮保留图，content.type 400 后强制剥图重试", async () => {
    const seen: BaseMessage[][] = [];
    let calls = 0;
    mockInvoke.mockImplementation(async (msgs: BaseMessage[]) => {
      seen.push(msgs);
      calls += 1;
      if (calls === 1) {
        throw new Error("400 messages.content.type 参数非法，取值范围 ['text']");
      }
      return new AIMessage({ content: "vision fallback ok" });
    });

    const { createThinkNode } = await import("../src/app/nodes/think.js");
    const node = createThinkNode({
      config: {
        model: {
          provider: "openai",
          model: "gpt-vision",
          settings: { supportsVision: true },
        },
      } as any,
      allTools: [],
    });

    const poisoned = [
      new ToolMessage({
        content: [
          { type: "text", text: "shot" },
          {
            type: "image_url",
            image_url: { url: `data:image/png;base64,${"F".repeat(80)}` },
          },
        ],
        tool_call_id: "c_vision",
      }),
    ];

    const out = await node({
      messages: poisoned,
      input: "x",
      steps: [],
    } as any);

    expect(calls).toBe(2);
    const last = out.messages![out.messages!.length - 1] as AIMessage;
    expect(last.content).toBe("vision fallback ok");
    expect(out.steps).toContain("think#history-writeback");

    // 首轮未 coerce：仍带 array content（含 image_url）
    const firstTm = seen[0]!.find((m) => m._getType?.() === "tool") as ToolMessage;
    expect(Array.isArray(firstTm.content)).toBe(true);
    expect(firstTm.content).toEqual(
      expect.arrayContaining([expect.objectContaining({ type: "image_url" })])
    );

    // 重试轮：aggressive 已压成字符串
    const secondTm = seen[1]!.find((m) => m._getType?.() === "tool") as ToolMessage;
    expect(typeof secondTm.content).toBe("string");
    expect(secondTm.content as string).toMatch(/image_url omitted|shot/);
  });

  it("孤立 tool_calls sanitize 后写回 checkpoint", async () => {
    mockInvoke.mockResolvedValue(new AIMessage({ content: "继续规划" }));

    const { createThinkNode } = await import("../src/app/nodes/think.js");
    const { RemoveMessage } = await import("@langchain/core/messages");
    const node = createThinkNode({
      config: {
        model: { provider: "openai", model: "gpt-test", settings: {} },
      } as any,
      allTools: [],
    });

    const poisoned = [
      new HumanMessage({ id: "h1", content: "继续" }),
      new AIMessage({
        id: "a1",
        content: "准备调工具",
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
      }),
    ];

    const out = await node({
      messages: poisoned,
      input: "继续",
      steps: [],
    } as any);

    expect(out.steps).toContain("think#history-writeback");
    const removals = (out.messages ?? []).filter((m) => m instanceof RemoveMessage);
    expect(removals).toHaveLength(2);
    const repairedAi = (out.messages ?? []).find(
      (m) => m._getType?.() === "ai" && m !== out.messages!.at(-1)
    ) as AIMessage | undefined;
    expect(repairedAi?.tool_calls ?? []).toHaveLength(0);
    expect(repairedAi?.additional_kwargs?.tool_calls).toBeUndefined();
  });

  it("content 空且 reasoning_content 有可见文本时提升到 content", async () => {
    mockInvoke.mockResolvedValue(
      new AIMessage({
        content: "",
        additional_kwargs: {
          reasoning_content: "你好！我是旅行规划助手",
        },
      })
    );

    const { createThinkNode } = await import("../src/app/nodes/think.js");
    const node = createThinkNode({
      config: {
        model: { provider: "openai", model: "deepseek-v4-flash", settings: {} },
      } as any,
      allTools: [],
    });

    const out = await node({
      messages: [new HumanMessage({ content: "你好" })],
      input: "你好",
      steps: [],
    } as any);

    const last = out.messages![out.messages!.length - 1] as AIMessage;
    expect(last.content).toBe("你好！我是旅行规划助手");
    expect(out.steps).toContain("think#reasoning_content→content");
  });

  it("有 tool_calls 时不把 reasoning_content 提升为 content", async () => {
    mockInvoke.mockResolvedValue(
      new AIMessage({
        content: "",
        tool_calls: [{ id: "c1", name: "web_search_1", args: { q: "大理" } }],
        additional_kwargs: {
          reasoning_content: "先查一下目的地信息",
        },
      })
    );

    const { createThinkNode } = await import("../src/app/nodes/think.js");
    const node = createThinkNode({
      config: {
        model: { provider: "openai", model: "deepseek-v4-flash", settings: {} },
      } as any,
      allTools: [],
    });

    const out = await node({
      messages: [new HumanMessage({ content: "大理怎么玩" })],
      input: "大理怎么玩",
      steps: [],
    } as any);

    const last = out.messages![out.messages!.length - 1] as AIMessage;
    expect(last.content).toBe("");
    expect(out.steps).not.toContain("think#reasoning_content→content");
    expect(last.tool_calls).toHaveLength(1);
  });
});
