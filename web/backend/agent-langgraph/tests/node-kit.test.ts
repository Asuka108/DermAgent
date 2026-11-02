/**
 * libs/nodes factory + 原语单测 —— AI Agent 改 factory 的安全网。
 *
 * 全部用 mock 模型 / 简单 tool，不依赖 LLM 凭证。
 * 覆盖：
 *  - 原语：extractText / parseJson / isApproval / runTool
 *  - factory：createPrepareNode / createFanout / createLlmNode（含 parse、fallback）/
 *    createLlmStreamNode / createToolExecNode / createApprovalFinalizeNode /
 *    createLlmRouterNode / createMcpRetrievalNode / createHumanApprovalNode（经编译图 interrupt/resume）/
 *    createPlatformToolActionNode / createSubgraphNode
 */

import { describe, it, expect } from "vitest";
import { randomUUID } from "node:crypto";
import { HumanMessage, AIMessage, ToolMessage, type BaseMessage } from "@langchain/core/messages";
import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { Annotation, StateGraph, START, END, MemorySaver, Command, INTERRUPT } from "@langchain/langgraph";
import type { LangGraphRunnableConfig } from "@langchain/langgraph";
import {
  createLlmNode,
  createLlmStreamNode,
  createToolExecNode,
  createPrepareNode,
  createHumanApprovalNode,
  createApprovalFinalizeNode,
  createLlmRouterNode,
  createMcpRetrievalNode,
  createFanout,
  createPlatformToolActionNode,
  createSubgraphNode,
  extractText,
  parseJson,
  isApproval,
  runTool,
} from "../src/libs/nodes/index.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

// ---------- 原语 ----------
describe("extractText", () => {
  it("string 原样", () => expect(extractText("hi")).toBe("hi"));
  it("content block 数组拼接", () =>
    expect(extractText([{ text: "a" }, { text: "b" }])).toBe("ab"));
  it("非文本 block 忽略", () => expect(extractText([{ type: "image" }, { text: "x" }])).toBe("x"));
  it("其它类型 → 空串", () => expect(extractText(123)).toBe(""));
  it("null/undefined → 空串", () => expect(extractText(null)).toBe(""));
});

describe("parseJson", () => {
  it("对象", () => expect(parseJson<{ a: number }>('{"a":1}')).toEqual({ a: 1 }));
  it("数组", () => expect(parseJson<number[]>("[1,2,3]")).toEqual([1, 2, 3]));
  it("容忍 ```json 围栏 + 前后说明", () =>
    expect(parseJson<{ a: number }>("结果:\n```json\n{\"a\":1}\n```\n完")).toEqual({ a: 1 }));
  it("无 JSON → 抛", () => expect(() => parseJson("纯文本无结构")).toThrow());
  it("不完整 → 抛", () => expect(() => parseJson("{")).toThrow());
});

describe("isApproval", () => {
  it("空 / 纯空白 = 通过", () => {
    expect(isApproval("")).toBe(true);
    expect(isApproval("   ")).toBe(true);
  });
  it("中英文通过词", () => {
    for (const w of ["ok", "OK", "通过", "可以", "批准", "approved", "yes", "好的", "lgtm"]) {
      expect(isApproval(w)).toBe(true);
    }
  });
  it("否定 / 调整意见 = 不通过", () => {
    expect(isApproval("不可以")).toBe(false);
    expect(isApproval("再改改")).toBe(false);
    expect(isApproval("no")).toBe(false);
  });
  it("「不可以」不被「可以」误判", () => expect(isApproval("不可以")).toBe(false));
  it("自定义 regex 覆盖", () => {
    expect(isApproval("y", { regex: /^y$/ })).toBe(true);
    expect(isApproval("yes", { regex: /^y$/ })).toBe(false);
  });
});

describe("runTool", () => {
  it("成功 → in_progress→completed", async () => {
    const statuses: string[] = [];
    const { result, ok } = await runTool("t", { x: 1 }, async () => "done", (e) => {
      statuses.push(e.status);
    });
    expect(ok).toBe(true);
    expect(result).toBe("done");
    expect(statuses).toEqual(["in_progress", "completed"]);
  });
  it("抛错 → failed,result=错误信息", async () => {
    const { ok, result } = await runTool("t", {}, async () => {
      throw new Error("boom");
    });
    expect(ok).toBe(false);
    expect(result).toBe("boom");
  });
  it('"Unknown tool: xxx" 不抛但判失败', async () => {
    const { ok } = await runTool("t", {}, async () => "Unknown tool: foo");
    expect(ok).toBe(false);
  });
});

// ---------- createPrepareNode ----------
describe("createPrepareNode", () => {
  const S = Annotation.Root({
    input: Annotation<string>,
    messages: Annotation<BaseMessage[]>({ reducer: (a, b) => [...a, ...b], default: () => [] }),
  });
  type S_ = typeof S.State;

  it("input → HumanMessage", async () => {
    const out = (await createPrepareNode<S_>()({ input: "hi", messages: [] } as S_)) as any;
    expect(out.messages).toHaveLength(1);
    expect(out.messages[0]._getType()).toBe("human");
  });
  it("无 input → 空更新", async () => {
    expect(await createPrepareNode<S_>()({ input: "", messages: [] } as S_)).toEqual({});
  });
  it("systemPrompt 前置 SystemMessage", async () => {
    const out = (await createPrepareNode<S_>({ systemPrompt: "sys" })({
      input: "hi",
      messages: [],
    } as S_)) as any;
    expect(out.messages[0]._getType()).toBe("system");
    expect(out.messages[1]._getType()).toBe("human");
  });
});

// ---------- createFanout ----------
describe("createFanout", () => {
  it("每个 item → 一个 Send(同 target)", () => {
    const fanout = createFanout<string, { items: string[] }>({
      items: (s) => s.items,
      target: "proc",
      input: (it) => ({ item: it }),
    });
    const sends = fanout({ items: ["a", "b", "c"] });
    expect(sends).toHaveLength(3);
  });
  it("空 items → 空 Send[]", () => {
    const fanout = createFanout<string, { items: string[] }>({
      items: (s) => s.items,
      target: "proc",
      input: (it) => ({ item: it }),
    });
    expect(fanout({ items: [] })).toEqual([]);
  });
});

// ---------- createLlmNode ----------
describe("createLlmNode", () => {
  const mockModel = (content: unknown) => ({ invoke: async () => ({ content }) });

  it("一次调 → write(content)", async () => {
    const node = createLlmNode<{ q: string; out?: string }>({
      model: mockModel("hello") as any,
      prompt: (s) => [new HumanMessage(s.q)],
      write: (r) => ({ out: r.content }),
    });
    expect((await node({ q: "x" })).out).toBe("hello");
  });
  it("content block 数组也被 extractText 成文本", async () => {
    const node = createLlmNode<any>({
      model: mockModel([{ text: "a" }, { text: "b" }]) as any,
      prompt: () => [],
      write: (r) => ({ out: r.content }),
    });
    expect((await node({})).out).toBe("ab");
  });
  it("parse 结构化输出", async () => {
    const node = createLlmNode<any>({
      model: mockModel('{"n":5}') as any,
      prompt: () => [],
      parse: (t) => parseJson<{ n: number }>(t),
      write: (r) => ({ n: (r.parsed as { n: number }).n }),
    });
    expect((await node({})).n).toBe(5);
  });
  it("调用失败 → fallback(attempts:1 不重试,提速)", async () => {
    const node = createLlmNode<any>({
      model: { invoke: async () => Promise.reject(new Error("fail")) } as any,
      prompt: () => [],
      write: () => ({}),
      attempts: 1,
      fallback: () => ({ out: "fallback" }),
    });
    expect((await node({})).out).toBe("fallback");
  });
});

// ---------- createApprovalFinalizeNode ----------
describe("createApprovalFinalizeNode", () => {
  const mockModel = (content: unknown) => ({ invoke: async () => ({ content }) });
  type S = { draft: string; feedback: string; output?: string };

  it("feedback 通过词 → approvedOutput 短路(不调 LLM)", async () => {
    let llmCalled = false;
    const node = createApprovalFinalizeNode<S>({
      approvedOutput: (s) => ({ output: `✅ ${s.draft}` }),
      rejectedLlm: {
        model: () => {
          llmCalled = true;
          return mockModel("x") as any;
        },
        prompt: () => [],
        write: () => ({}),
        timeoutMs: 5000,
      },
    });
    const res = await node({ draft: "d", feedback: "ok" });
    expect(res.output).toBe("✅ d");
    expect(llmCalled).toBe(false);
  });

  it("feedback 非通过词 → rejectedLlm 改写", async () => {
    const node = createApprovalFinalizeNode<S>({
      approvedOutput: () => ({ output: "不应到这里" }),
      rejectedLlm: {
        model: mockModel("改写后") as any,
        prompt: (s) => [new HumanMessage(`${s.draft}|${s.feedback}`)],
        write: (r) => ({ output: `✏️ ${r.text}` }),
        timeoutMs: 5000,
      },
    });
    const res = await node({ draft: "d", feedback: "改短一点" });
    expect(res.output).toBe("✏️ 改写后");
  });

  it("feedbackField 自定义 + isApproved 自定义", async () => {
    const node = createApprovalFinalizeNode<{ v: string; out?: string }>({
      feedbackField: "v",
      isApproved: (fb) => fb === "Y",
      approvedOutput: () => ({ out: "approved" }),
      rejectedLlm: {
        model: mockModel("r") as any,
        prompt: () => [],
        write: () => ({ out: "rejected" }),
        timeoutMs: 5000,
      },
    });
    expect((await node({ v: "Y" })).out).toBe("approved");
    expect((await node({ v: "N" })).out).toBe("rejected");
  });
});

// ---------- createLlmRouterNode ----------
describe("createLlmRouterNode", () => {
  const mockModel = (content: unknown) => ({ invoke: async () => ({ content }) });
  type S = { draft: string; verdict?: string; attempts: number };

  const buildOpts = (model: any) => ({
    model,
    prompt: (s: S) => [new HumanMessage(s.draft)],
    parse: (t: string) => parseJson<{ verdict?: string }>(t),
    route: (v: any, s: S) => {
      const verdict = v.verdict === "fail" ? "fail" : "pass";
      const goto = verdict === "fail" && s.attempts < 2 ? "redo" : "done";
      return { goto, update: { verdict } };
    },
    routeFallback: () => ({ goto: "done", update: { verdict: "pass" } }),
    attempts: 1,
  });

  it("成功 fail & 未达上限 → Command(goto=redo, update.verdict=fail)", async () => {
    const node = createLlmRouterNode<S>(buildOpts(mockModel('{"verdict":"fail"}')) as any);
    const cmd = (await node({ draft: "d", attempts: 0 })) as any;
    // Command 把 goto string 包成数组
    expect(cmd.goto).toEqual(["redo"]);
    expect(cmd.update).toMatchObject({ verdict: "fail" });
  });

  it("成功 pass → goto=done", async () => {
    const node = createLlmRouterNode<S>(buildOpts(mockModel('{"verdict":"pass"}')) as any);
    expect((await node({ draft: "d", attempts: 0 }) as any).goto).toEqual(["done"]);
  });

  it("达上限 → 即便 fail 也 goto=done（route 内判定）", async () => {
    const node = createLlmRouterNode<S>(buildOpts(mockModel('{"verdict":"fail"}')) as any);
    expect((await node({ draft: "d", attempts: 2 }) as any).goto).toEqual(["done"]);
  });

  it("无模型 → routeFallback(no-model)", async () => {
    const node = createLlmRouterNode<S>(buildOpts(null) as any);
    const cmd = (await node({ draft: "d", attempts: 0 })) as any;
    expect(cmd.goto).toEqual(["done"]);
    expect(cmd.update).toMatchObject({ verdict: "pass" });
  });

  it("调用失败 → routeFallback(error)", async () => {
    const node = createLlmRouterNode<S>(
      buildOpts({ invoke: async () => Promise.reject(new Error("x")) }) as any
    );
    expect((await node({ draft: "d", attempts: 0 }) as any).goto).toEqual(["done"]);
  });
});

// ---------- createMcpRetrievalNode ----------
describe("createMcpRetrievalNode", () => {
  type S = { q: string; out?: string };
  // 真实 MCP 调用需 npx + 网络，不纳入单测；此处只测无副作用的 guard 分支（retrieve 返回 null / server 未配置）。

  it("retrieve 返回 null → 写回空结果(ok:false)", async () => {
    const node = createMcpRetrievalNode<S>({
      mcpServers: {},
      retrieve: () => null,
      write: (r) => ({ out: `${r.ok}:${r.text}` }),
    });
    expect((await node({ q: "x" })).out).toBe("false:");
  });

  it("server 未配置 → 写回错误信息(ok:false)", async () => {
    const node = createMcpRetrievalNode<S>({
      mcpServers: { a: { command: "x" } },
      retrieve: () => ({ server: "missing", tool: "t", args: {} }),
      write: (r) => ({ out: `${r.ok}:${r.text}` }),
    });
    const res = await node({ q: "x" });
    expect(res.out).toMatch(/^false:.*missing/);
  });
});

// ---------- createLlmStreamNode ----------
describe("createLlmStreamNode", () => {
  it("有 onToken sink + 模型支持 stream → write({text, streamed:true})", async () => {
    const chunks = [{ content: "he" }, { content: "llo" }];
    const mockStream = {
      invoke: async () => ({ content: "" }),
      stream: async function* () {
        for (const c of chunks) yield c;
      },
    };
    const node = createLlmStreamNode<any>({
      model: mockStream as any,
      prompt: () => [],
      write: (r) => ({ text: r.text, streamed: r.streamed }),
      timeoutMs: 5000,
    });
    // 需 configurable.onToken 才走 stream 分支（streamLLMText 的 hasVisibleTokenSink）
    const out = await node({}, { configurable: { onToken: () => undefined } } as LangGraphRunnableConfig);
    expect(out.text).toBe("hello");
    expect(out.streamed).toBe(true);
  });

  it("累积全文 chunk（provider 返回前缀续写）→ 只 emit delta，避免叠词", async () => {
    const chunks = [{ content: "你" }, { content: "你好" }, { content: "你好世界" }];
    const tokens: string[] = [];
    const mockStream = {
      invoke: async () => ({ content: "" }),
      stream: async function* () {
        for (const c of chunks) yield c;
      },
    };
    const node = createLlmStreamNode<any>({
      model: mockStream as any,
      prompt: () => [],
      write: (r) => ({ text: r.text, streamed: r.streamed }),
      timeoutMs: 5000,
    });
    const out = await node(
      {},
      { configurable: { onToken: (t: string) => tokens.push(t) } } as LangGraphRunnableConfig
    );
    expect(out.text).toBe("你好世界");
    expect(out.streamed).toBe(true);
    expect(tokens).toEqual(["你", "好", "世界"]);
  });

  it("无 onToken sink → 退回 invoke(streamed:false)", async () => {
    const mock = { invoke: async () => ({ content: "full" }) };
    const node = createLlmStreamNode<any>({
      model: mock as any,
      prompt: () => [],
      write: (r) => ({ text: r.text, streamed: r.streamed }),
      timeoutMs: 5000,
    });
    const out = await node({}, undefined);
    expect(out.text).toBe("full");
    expect(out.streamed).toBe(false);
  });
});

// ---------- createToolExecNode ----------
describe("createToolExecNode", () => {
  it("执行 tool_call + 三态 onToolCall(in_progress→completed)", async () => {
    const echo = tool(async ({ x }: { x: string }) => `got:${x}`, {
      name: "echo",
      schema: z.object({ x: z.string() }),
      description: "echo back",
    });
    const statuses: string[] = [];
    const node = createToolExecNode<{ messages: BaseMessage[] }>({
      tools: [echo] as any,
      callbacks: { onToolCall: (e) => void statuses.push(e.status) } as any,
    });
    const ai = new AIMessage({
      content: "",
      tool_calls: [{ id: "tc1", name: "echo", args: { x: "hi" } }] as any,
    });
    const out = (await node({ messages: [ai] })) as any;
    expect(out.messages).toHaveLength(1);
    expect(statuses).toContain("in_progress");
    expect(statuses).toContain("completed");
  });
  it("无 tool_calls → 空 messages", async () => {
    const node = createToolExecNode<{ messages: BaseMessage[] }>({ tools: [] as any });
    const out = (await node({ messages: [new AIMessage({ content: "no calls" })] })) as any;
    expect(out.messages).toEqual([]);
  });

  it("supportsVision=true 时工具写路径保留 image_url content block", async () => {
    const screenshot = tool(
      async () =>
        [
          { type: "text", text: "screenshot" },
          {
            type: "image_url",
            image_url: { url: `data:image/png;base64,${"A".repeat(120)}` },
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
      config: { model: { settings: { supportsVision: true } } } as any,
    });
    const ai = new AIMessage({
      content: "",
      tool_calls: [{ id: "tc_vision", name: "screenshot", args: {} }] as any,
    });

    const out = (await node({ messages: [ai] })) as any;
    const tm = out.messages[0] as ToolMessage;

    expect(Array.isArray(tm.content)).toBe(true);
    expect(tm.content).toEqual(
      expect.arrayContaining([expect.objectContaining({ type: "image_url" })])
    );
  });
});

// ---------- createHumanApprovalNode（经编译图 interrupt/resume：节点体内 interrupt() 只能在图运行时执行） ----------
describe("createHumanApprovalNode", () => {
  const S = Annotation.Root({
    input: Annotation<string>({ value: (_: string, n: string) => n, default: () => "" }),
    feedback: Annotation<string>({ value: (_: string, n: string) => n, default: () => "" }),
    decision: Annotation<string>({ value: (_: string, n: string) => n, default: () => "" }),
  });
  type S_ = typeof S.State;

  it("interrupt 抛出问题 → resume 通过词 → write(approved=true)", async () => {
    const node = createHumanApprovalNode<S_>({
      question: (s) => `确认:${s.input}`,
      write: (feedback, approved) => ({ feedback, decision: approved ? "approved" : "changes" }),
    });
    const graph = new StateGraph(S)
      .addNode("review", node)
      .addEdge(START, "review")
      .addEdge("review", END)
      .compile({ checkpointer: new MemorySaver() });
    const cfg = { configurable: { thread_id: randomUUID() } };

    const first = (await graph.invoke({ input: "draft" }, cfg)) as any;
    // interrupt 未 resume 时，invoke 返回体带 __interrupt__，携带 question 载荷
    expect(first[INTERRUPT]?.[0]?.value).toEqual({ question: "确认:draft" });

    const resumed = (await graph.invoke(new Command({ resume: "ok" }), cfg)) as any;
    expect(resumed.decision).toBe("approved");
    expect(resumed.feedback).toBe("ok");
  });

  it("resume 修改意见（非通过词）→ write(approved=false)", async () => {
    const node = createHumanApprovalNode<S_>({
      question: () => "确认?",
      write: (feedback, approved) => ({ feedback, decision: approved ? "approved" : "changes" }),
    });
    const graph = new StateGraph(S)
      .addNode("review", node)
      .addEdge(START, "review")
      .addEdge("review", END)
      .compile({ checkpointer: new MemorySaver() });
    const cfg = { configurable: { thread_id: randomUUID() } };

    await graph.invoke({ input: "x" }, cfg);
    const resumed = (await graph.invoke(new Command({ resume: "再改短一点" }), cfg)) as any;
    expect(resumed.decision).toBe("changes");
    expect(resumed.feedback).toBe("再改短一点");
  });

  it("route 变体：打回走 Command goto（返回上一节点）", async () => {
    const node = createHumanApprovalNode<S_>({
      question: () => "确认大纲?",
      route: (approved) =>
        approved
          ? new Command({ goto: "next", update: { decision: "go" } })
          : new Command({ goto: "plan", update: { decision: "redo" } }),
    });
    const graph = new StateGraph(S)
      .addNode("review", node, { ends: ["plan", "next"] })
      .addNode("plan", (s: S_) => ({ decision: `${s.decision}-planned` }))
      .addNode("next", (s: S_) => ({ decision: `${s.decision}-nexted` }))
      .addEdge(START, "review")
      .addEdge("plan", END)
      .addEdge("next", END)
      .compile({ checkpointer: new MemorySaver() });
    const cfg = { configurable: { thread_id: randomUUID() } };

    await graph.invoke({ input: "outline" }, cfg);
    const resumed = (await graph.invoke(new Command({ resume: "不行" }), cfg)) as any;
    expect(resumed.decision).toBe("redo-planned");
  });
});

// ---------- createPlatformToolActionNode（主动调用注入的工具集合，非 tool_calls 驱动） ----------
describe("createPlatformToolActionNode", () => {
  it("按 toolName 选工具 + 三态 onToolCall + write(raw)", async () => {
    const quote = tool(async ({ city }: { city: string }) => `price:${city}`, {
      name: "quote_price",
      schema: z.object({ city: z.string() }),
      description: "quote a city",
    });
    const statuses: string[] = [];
    const node = createPlatformToolActionNode<{ city: string; out?: string }>({
      tools: [quote] as any,
      toolName: "quote_price",
      args: (s) => ({ city: s.city }),
      write: (r) => ({ out: String(r.raw) }),
      callbacks: { onToolCall: (e) => void statuses.push(e.status) } as any,
    });
    const out = await node({ city: "SF" });
    expect(out.out).toBe("price:SF");
    expect(statuses).toEqual(["in_progress", "completed"]);
  });

  it("toolName 缺省 → 用 tools[0]", async () => {
    const echo = tool(async ({ x }: { x: string }) => `got:${x}`, {
      name: "echo",
      schema: z.object({ x: z.string() }),
      description: "echo",
    });
    const node = createPlatformToolActionNode<{ x: string; out?: string }>({
      tools: [echo] as any,
      args: (s) => ({ x: s.x }),
      write: (r) => ({ out: String(r.raw) }),
    });
    expect((await node({ x: "hi" })).out).toBe("got:hi");
  });

  it("toolName 未匹配 → 抛错", async () => {
    const node = createPlatformToolActionNode<any>({
      tools: [] as any,
      toolName: "nope",
      args: () => ({}),
      write: () => ({}),
    });
    await expect(node({})).rejects.toThrow(/未找到可用工具/);
  });

  it("工具抛错 → 透出 failed 状态并向上抛", async () => {
    const boom = tool(
      async () => {
        throw new Error("tool boom");
      },
      { name: "boom", schema: z.object({}), description: "always throws" }
    );
    const statuses: string[] = [];
    const node = createPlatformToolActionNode<any>({
      tools: [boom] as any,
      toolName: "boom",
      args: () => ({}),
      write: () => ({}),
      callbacks: { onToolCall: (e) => void statuses.push(e.status) } as any,
    });
    await expect(node({})).rejects.toThrow(/tool boom/);
    expect(statuses).toEqual(["in_progress", "failed"]);
  });
});

// ---------- createSubgraphNode（子图编译后作节点用） ----------
describe("createSubgraphNode", () => {
  const Sub = Annotation.Root({
    n: Annotation<number>({ value: (_: number, x: number) => x, default: () => 0 }),
  });
  type Sub_ = typeof Sub.State;

  const makeDoubler = () =>
    createSubgraphNode<Sub_>({
      state: Sub,
      nodes: { double: (s) => ({ n: s.n * 2 }) },
      edges: [
        [START, "double"],
        ["double", END],
      ],
    });

  it("编译出的子图可独立 invoke", async () => {
    const out = (await makeDoubler().invoke({ n: 3 })) as Sub_;
    expect(out.n).toBe(6);
  });

  it("子图作父图节点，经共享 channel(n) 映射", async () => {
    const parent = new StateGraph(Sub)
      .addNode("sub", makeDoubler())
      .addEdge(START, "sub")
      .addEdge("sub", END)
      .compile();
    const out = (await parent.invoke({ n: 4 })) as Sub_;
    expect(out.n).toBe(8);
  });
});
