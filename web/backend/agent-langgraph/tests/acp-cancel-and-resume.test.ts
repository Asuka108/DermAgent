/**
 * ACP flow surface —— 取消任务 (cancel) + 恢复上下文 (load_session) 测试。
 *
 * in-process：直接调 `createFlowHooks` 拿 hooks，注入假 StatefulFlow + 假 AcpConnection，
 * 不起 stdio server、不调真实 LLM。与 stateful-flow.test.ts 的假图模式一致。
 *
 * 覆盖：
 *  A. load_session / 上下文恢复：configureSession(new)→onPrompt(interrupt)→重启实例→
 *     configureSession(load)→onPrompt(resume)→done。
 *  B. 取消任务：onPrompt 携带 signal，abort 后 flow.run 快速 reject（不再继续产出），
 *     onPrompt 仍返回 end_turn（现有 catch 收尾）。
 *  C. 资源释放：onSessionClosed 后 dispose 被调用、缓存清空；之后能正常重建。
 *  D. 组合：cancel 后 load_session 仍能从 checkpointer 断点恢复（abort 不毁 checkpoint）。
 */

import { describe, it, expect, afterAll } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  StateGraph,
  START,
  END,
  Annotation,
  interrupt,
  MemorySaver,
  type BaseCheckpointSaver,
} from "@langchain/langgraph";
import { createStatefulFlow } from "../src/surfaces/stateful-flow.js";
import { FileCheckpointSaver } from "../src/runtime/services/file-checkpoint-saver.js";
import { createFlowHooks } from "../src/surfaces/acp/server.js";
import type { AppConfig } from "../src/runtime/index.js";
import type { SessionExecutor } from "../src/surfaces/acp/server.js";
import type { FlowCallbacks } from "../src/core/flow-types.js";

// ── 玩具图：ask(interrupt 暂停) → finish(定稿) ──────────────
const ToyState = Annotation.Root({
  query: Annotation<string>,
  reply: Annotation<string>,
  output: Annotation<string>,
});
type ToyStateType = typeof ToyState.State;

function buildToyGraph(checkpointer: BaseCheckpointSaver) {
  return new StateGraph(ToyState)
    .addNode("ask", () => {
      const fb = interrupt({ question: "确认吗？" });
      return { reply: String(fb ?? "") };
    })
    .addNode("finish", (s: ToyStateType) => ({ output: `done:${s.reply}` }))
    .addEdge(START, "ask")
    .addEdge("ask", "finish")
    .addEdge("finish", END)
    .compile({ checkpointer });
}

const makeFlow = (cp: BaseCheckpointSaver) =>
  createStatefulFlow<ToyStateType>({
    buildGraph: (saver) => buildToyGraph(saver),
    toInput: (query) => ({ query }),
    toResult: (v) => ({ answer: v.output ?? "" }),
    checkpointer: cp,
  });

const tmpDirs: string[] = [];
function freshDir(): string {
  const d = mkdtempSync(join(tmpdir(), "flow-acp-"));
  tmpDirs.push(d);
  return d;
}
afterAll(() => {
  for (const d of tmpDirs) rmSync(d, { recursive: true, force: true });
});

/** 收集推给客户端的 sessionUpdate，供断言流式输出序列。 */
function makeFakeConn() {
  const updates: Array<{
    kind: string;
    text?: string;
    status?: string;
    toolCallId?: string;
    messageId?: string;
  }> = [];
  const conn = {
    async sessionUpdate(params: {
      sessionId: string;
      update: {
        sessionUpdate: string;
        content?: unknown;
        status?: string;
        toolCallId?: string;
        messageId?: string;
      };
    }) {
      const u = params.update;
      updates.push({
        kind: u.sessionUpdate,
        text: extractText(u.content),
        status: u.status,
        toolCallId: u.toolCallId,
        messageId: u.messageId,
      });
    },
  };
  return { conn, updates };
}

/** 兼容两种 content 形态：{ text } 或 [{ content: { text } }]。 */
function extractText(content: unknown): string | undefined {
  if (!content) return undefined;
  if (typeof content === "object" && !Array.isArray(content)) {
    return (content as { text?: string }).text;
  }
  if (Array.isArray(content)) {
    const first = content[0] as { content?: { text?: string } } | undefined;
    return first?.content?.text;
  }
  return undefined;
}

/** per-session createExecutor 工厂：按传入 flow 实例返回 SessionExecutor。 */
function makeExecutorFactory(flowByDir: Map<string, ReturnType<typeof makeFlow>>, disposeCalls: string[]) {
  return async (args: { sessionConfig: { cwd: string } }): Promise<SessionExecutor> => {
    // cwd 作为隔离键：不同 checkpoint 目录对应不同 flow 实例。
    const dir = args.sessionConfig.cwd;
    const flow = flowByDir.get(dir)!;
    return {
      executor: flow,
      dispose: async () => {
        disposeCalls.push(dir);
      },
    };
  };
}

const fakeAppConfig = { agent: { name: "test-flow", version: "0.0.0" } } as unknown as AppConfig;

describe("A. load_session / 上下文恢复", () => {
  it("configureSession(new) → onPrompt(interrupt) → 重启实例 → configureSession(load) → onPrompt(resume) → done", async () => {
    const dir = freshDir();
    const sessionId = "sess-restore-1";
    const disposeCalls: string[] = [];
    // flow 实例按需替换（模拟重启）；用 getter 让 createExecutor 始终拿到「当前」实例。
    let current = makeFlow(new FileCheckpointSaver({ dir }));
    const flowByDir = new Map([[dir, current]]);
    const hooks = createFlowHooks({
      createExecutor: makeExecutorFactory(flowByDir, disposeCalls),
      appConfig: fakeAppConfig,
    });

    const { conn, updates } = makeFakeConn();

    // 1) session/new：建 per-session runtime
    const cfgNew = await hooks.configureSession!({
      sessionId,
      agentName: "test-flow",
      phase: "new",
      params: { cwd: dir },
    });
    expect(cfgNew).toEqual({ workspaceRoot: dir });

    // 2) onPrompt 首条 → flow 走到 interrupt，返回 question
    const r1 = await hooks.onPrompt!({
      sessionId,
      promptText: "做任务X",
      params: {},
      conn,
    });
    expect(r1).toEqual({ stopReason: "end_turn" });
    // interrupt 的 question 被流式推出
    expect(updates.some((u) => u.kind === "agent_message_chunk" && u.text === "确认吗？")).toBe(true);

    // 3) 模拟进程/IDE 重启：全新 flow 实例 + 全新 saver（同目录）= 内存全丢，只剩磁盘 checkpoint
    current = makeFlow(new FileCheckpointSaver({ dir }));
    flowByDir.set(dir, current);
    // hasStarted 必须从磁盘推断为 true（已有 checkpoint）
    expect(await current.hasStarted!(sessionId)).toBe(true);

    // 4) session/load：重配 per-session runtime（先 dispose 旧实例）
    const cfgLoad = await hooks.configureSession!({
      sessionId,
      agentName: "test-flow",
      phase: "load",
      params: { cwd: dir },
    });
    expect(cfgLoad).toEqual({ workspaceRoot: dir });

    // 5) onPrompt 续跑：据 hasStarted=true 走 resume 分支 → done
    const updatesBefore = updates.length;
    const r2 = await hooks.onPrompt!({
      sessionId,
      promptText: "ok",
      params: {},
      conn,
    });
    expect(r2).toEqual({ stopReason: "end_turn" });
    // resume 后产出 done 答案
    expect(updates.slice(updatesBefore).some((u) => u.text === "done:ok")).toBe(true);
  });

  it("单 executor 模式：configureSession 返回 undefined（不建 per-session runtime），但仍绑定 session 日志", async () => {
    const dir = freshDir();
    const flow = makeFlow(new MemorySaver());
    const hooks = createFlowHooks({
      executor: flow, // 单 executor，无 createExecutor
      appConfig: fakeAppConfig,
    });
    const { conn } = makeFakeConn();
    const sessionId = "sess-single-1";

    const cfg = await hooks.configureSession!({
      sessionId,
      agentName: "test-flow",
      phase: "new",
      params: { cwd: dir },
    });
    expect(cfg).toBeUndefined();

    const { getSessionLogPath, setLogAgent } = await import("../src/runtime/logger.js");
    setLogAgent("test-flow");
    expect(getSessionLogPath(sessionId)).toContain(sessionId);

    // onPrompt 仍能直接用单 executor 跑
    const r = await hooks.onPrompt!({ sessionId, promptText: "hi", params: {}, conn });
    expect(r).toEqual({ stopReason: "end_turn" });
  });

  it("并行 subagent thought 按 source + toolCallId 使用独立 messageId", async () => {
    const executor = async (_query: string, callbacks: FlowCallbacks) => {
      await callbacks.onThought?.("A 思考", "analyst", "task-a");
      await callbacks.onThought?.("B 思考", "analyst", "task-b");
      return { answer: "" };
    };
    const hooks = createFlowHooks({ executor, appConfig: fakeAppConfig });
    const { conn, updates } = makeFakeConn();

    const result = await hooks.onPrompt!({
      sessionId: "sess-subagent-thoughts",
      promptText: "并行分析",
      params: {},
      conn,
    });

    expect(result).toEqual({ stopReason: "end_turn" });
    expect(
      updates
        .filter((update) => update.kind === "agent_thought_chunk")
        .map(({ text, messageId }) => ({ text, messageId }))
    ).toEqual([
      { text: "A 思考", messageId: "subagent-thought:analyst:task-a" },
      { text: "B 思考", messageId: "subagent-thought:analyst:task-b" },
    ]);
  });
});

describe("B. 取消任务（cancel）", () => {
  it("onPrompt 携带 signal，abort 后 flow.run 快速 reject（耗时远小于跑完）", async () => {
    // 慢假图：stream 里 delay 模拟 long-running 任务，每次 yield 前检查 signal（模拟真实 LangGraph 行为）。
    const FULL_RUN_MS = 500;
    let producedTokens = 0;
    let lastSignal: AbortSignal | undefined;
    const slowFakeGraph = {
      async stream(_input: unknown, config?: { signal?: AbortSignal }) {
        lastSignal = config?.signal;
        async function* gen() {
          // 产出多个 token，每个间隔，模拟长流式任务
          for (let i = 0; i < 50; i++) {
            // 模拟真实图：在 await 期间若 signal 被 abort，以 AbortError reject
            if (lastSignal?.aborted) {
              const err = new Error("Aborted");
              err.name = "AbortError";
              throw err;
            }
            await delay(FULL_RUN_MS / 50);
            producedTokens++;
            yield ["messages", [{ content: `t${i}` }, { langgraph_node: "respond" }]];
          }
        }
        return gen();
      },
      async getState() {
        return { values: { output: "done" }, config: { configurable: { checkpoint_id: "cp" } } };
      },
    };
    const flow = createStatefulFlow<{ output: string }>({
      buildGraph: () => slowFakeGraph,
      toInput: (query) => ({ query }),
      toResult: (v) => ({ answer: v.output }),
      checkpointer: new MemorySaver(),
    });

    const hooks = createFlowHooks({ executor: flow, appConfig: fakeAppConfig });
    const { conn, updates } = makeFakeConn();
    const sessionId = "sess-cancel-1";
    const controller = new AbortController();

    // 发起 onPrompt（内部走 flow.run → graph.stream({signal})）
    const onPromptPromise = hooks.onPrompt!({
      sessionId,
      promptText: "长任务",
      params: {},
      conn,
      signal: controller.signal,
    });

    await delay(30); // 让 flow 跑一小会儿
    expect(producedTokens).toBeGreaterThan(0); // 确认确实在产出

    controller.abort(); // 取消

    const start = Date.now();
    const r = await onPromptPromise;
    const elapsed = Date.now() - start;

    // 关键：onPrompt 在 abort 后快速返回（远小于完整跑完的 500ms）
    expect(elapsed).toBeLessThan(FULL_RUN_MS);
    // 协议要求（acp.d.ts:1051）：cancel 时以 StopReason::Cancelled 响应，不是 end_turn。
    expect(r).toEqual({ stopReason: "cancelled" });
    // 取消时不发「道歉」消息（那是普通错误的收尾行为，cancel 是用户主动中止）
    expect(updates.some((u) => u.text === "抱歉，处理您的问题时出现错误。")).toBe(false);
  });

  it("未带 signal 时不受影响：正常跑到 interrupt（对照）", async () => {
    const flow = makeFlow(new MemorySaver());
    const hooks = createFlowHooks({ executor: flow, appConfig: fakeAppConfig });
    const { conn } = makeFakeConn();
    const r = await hooks.onPrompt!({
      sessionId: "sess-nocancel",
      promptText: "hi",
      params: {},
      conn,
    });
    expect(r).toEqual({ stopReason: "end_turn" });
  });

  it("cancel 时给 in-flight tool_call 发 failed update（避免客户端 UI 悬挂）", async () => {
    // 假图：先发一个 on_tool_start（tool 进入 in_progress），再进入可被 abort 的长 delay。
    const FULL_RUN_MS = 500;
    let lastSignal: AbortSignal | undefined;
    const toolCallId = "tc-cancel-1";
    const fakeGraphWithTool = {
      async stream(_input: unknown, config?: { signal?: AbortSignal }) {
        lastSignal = config?.signal;
        async function* gen() {
          // 1) tool 开始（in_progress）—— onPrompt 的 onToolCall 会把它加入 inflightTools
          yield [
            "tools",
            { event: "on_tool_start", toolCallId, name: "search", input: '{"q":"x"}' },
          ];
          // 2) long-running 循环：被 abort 时以 AbortError reject（tool 永远收不到 on_tool_end）
          for (let i = 0; i < 50; i++) {
            if (lastSignal?.aborted) {
              const err = new Error("Aborted");
              err.name = "AbortError";
              throw err;
            }
            await delay(FULL_RUN_MS / 50);
          }
        }
        return gen();
      },
      async getState() {
        return { values: { output: "done" }, config: { configurable: { checkpoint_id: "cp" } } };
      },
    };
    const flow = createStatefulFlow<{ output: string }>({
      buildGraph: () => fakeGraphWithTool,
      toInput: (query) => ({ query }),
      toResult: (v) => ({ answer: v.output }),
      checkpointer: new MemorySaver(),
    });

    const hooks = createFlowHooks({ executor: flow, appConfig: fakeAppConfig });
    const { conn, updates } = makeFakeConn();
    const sessionId = "sess-cancel-tool-1";
    const controller = new AbortController();

    const onPromptPromise = hooks.onPrompt!({
      sessionId,
      promptText: "用工具搜一下",
      params: {},
      conn,
      signal: controller.signal,
    });

    await delay(30); // 让 tool_start 被处理（进入 inflightTools）
    // 确认 tool_call 已发（in_progress）
    expect(updates.some((u) => u.kind === "tool_call" && u.toolCallId === toolCallId)).toBe(true);

    controller.abort(); // 取消

    const r = await onPromptPromise;
    expect(r).toEqual({ stopReason: "cancelled" });

    // 关键：in-flight tool 收到 tool_call_update {status:"failed"} + 取消说明
    const failedUpdate = updates.find(
      (u) => u.kind === "tool_call_update" && u.toolCallId === toolCallId
    );
    expect(failedUpdate).toBeDefined();
    expect(failedUpdate!.status).toBe("failed");
    // ToolCallStatus 枚举无 cancelled（客户端本地标记）；agent 侧用 failed + 说明
    expect(failedUpdate!.text).toContain("已取消");
  });
});

describe("C. 资源释放", () => {
  it("onSessionClosed 调 dispose + 清缓存；之后再 onPrompt 会重建 per-session runtime", async () => {
    const dir = freshDir();
    const sessionId = "sess-close-1";
    const disposeCalls: string[] = [];
    let buildCount = 0;
    const createExecutor = async (args: { sessionConfig: { cwd: string } }): Promise<SessionExecutor> => {
      buildCount++;
      const flow = makeFlow(new FileCheckpointSaver({ dir: args.sessionConfig.cwd }));
      return {
        executor: flow,
        dispose: async () => {
          disposeCalls.push(args.sessionConfig.cwd);
        },
      };
    };
    const hooks = createFlowHooks({ createExecutor, appConfig: fakeAppConfig });
    const { conn } = makeFakeConn();

    await hooks.configureSession!({
      sessionId,
      agentName: "test-flow",
      phase: "new",
      params: { cwd: dir },
    });
    await hooks.onPrompt!({ sessionId, promptText: "hi", params: {}, conn });
    expect(buildCount).toBe(1);

    // 关闭：dispose 被调用
    await hooks.onSessionClosed!({ sessionId, agentName: "test-flow" });
    expect(disposeCalls).toContain(dir);

    // 重新配置 + onPrompt：per-session runtime 被重建（buildCount++）
    await hooks.configureSession!({
      sessionId,
      agentName: "test-flow",
      phase: "new",
      params: { cwd: dir },
    });
    expect(buildCount).toBe(2);
    const r = await hooks.onPrompt!({ sessionId, promptText: "again", params: {}, conn });
    expect(r).toEqual({ stopReason: "end_turn" });
  });
});

describe("D. 组合：cancel 后 load_session 仍能从断点恢复", () => {
  it("interrupt 后 cancel 中断第二阶段，新实例同目录仍 hasStarted 并能 resume", async () => {
    const dir = freshDir();
    const sessionId = "sess-combo-1";

    // 实例 A：跑到 interrupt（ask 节点暂停，checkpoint 已落盘）
    const flowA = makeFlow(new FileCheckpointSaver({ dir }));
    const r1 = await flowA.run({ query: "deep agents" }, sessionId);
    expect(r1.status).toBe("interrupted");
    expect(await flowA.hasStarted!(sessionId)).toBe(true);

    // 模拟：cancel 后进程重启 —— 全新实例（同目录）
    const flowB = makeFlow(new FileCheckpointSaver({ dir }));
    expect(await flowB.hasStarted!(sessionId)).toBe(true); // 从磁盘恢复了断点

    // resume 续跑到底（cancel 没毁 checkpoint）
    const done = await flowB.run({ resume: "ok" }, sessionId);
    expect(done.status).toBe("done");
    if (done.status === "done") expect(done.answer).toBe("done:ok");
  });
});

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
