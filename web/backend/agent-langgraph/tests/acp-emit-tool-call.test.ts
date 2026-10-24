import { describe, it, expect } from "vitest";
import { emitToolCall } from "../src/surfaces/acp/emit-tool-call.js";
import {
  normalizeToolResult,
  normalizeToolMessageContent,
} from "../src/libs/nodes/tool-result-normalize.js";
import type { ToolCallEvent } from "../src/core/flow-types.js";

function collectUpdates() {
  const updates: Record<string, unknown>[] = [];
  const conn = {
    async sessionUpdate(params: { sessionId: string; update: Record<string, unknown> }) {
      updates.push(params.update);
    },
  };
  return { conn, updates };
}

describe("emitToolCall", () => {
  it("in_progress 发 rawInput（平台问答卡片契约），不发 input", async () => {
    const { conn, updates } = collectUpdates();
    const args = {
      requestId: "demo_1",
      ui: { version: "nuwax.interaction.v2", fields: [] },
    };
    await emitToolCall(conn, "sess-1", {
      toolCallId: "tc-1",
      toolName: "ask-question__nuwax_ask_question",
      args,
      status: "in_progress",
    });
    expect(updates).toHaveLength(1);
    expect(updates[0]).toMatchObject({
      sessionUpdate: "tool_call",
      toolCallId: "tc-1",
      status: "in_progress",
      rawInput: args,
    });
    expect(updates[0]).not.toHaveProperty("input");
  });

  it("read_file in_progress 含 locations", async () => {
    const { conn, updates } = collectUpdates();
    await emitToolCall(
      conn,
      "sess-1",
      {
        toolCallId: "tc-r",
        toolName: "read_file",
        args: { path: "src/a.ts" },
        status: "in_progress",
      },
      { workspaceRoot: "/ws" }
    );
    expect(updates[0]?.locations).toEqual([{ path: "/ws/src/a.ts" }]);
  });

  it("completed 优先用 MCP structuredContent.input 作 rawInput（通用 ACP 契约，非工具名）", async () => {
    const { conn, updates } = collectUpdates();
    const inflight = new Map<string, ToolCallEvent>();
    const llmArgs = {
      requestId: "demo_002",
      sessionId: "demo_session_002",
      title: "偏好设置",
      ui: { presentation: "inline", fields: [{ name: "theme", widget: "radio" }] },
    };
    const mcpNormalizedInput = {
      toolName: "nuwax_ask_question",
      schemaVersion: "nuwax.mcp_ask.v2",
      requestId: "demo_002",
      revision: 1,
      sessionId: "demo_session_002",
      title: "偏好设置",
      ui: {
        version: "nuwax.interaction.v2",
        presentation: "inline",
        fields: [{ name: "theme", widget: "radio" }],
      },
    };
    inflight.set("tc-mcp", {
      toolCallId: "tc-mcp",
      toolName: "ask-question__nuwax_ask_question",
      args: llmArgs,
      status: "in_progress",
    });

    await emitToolCall(
      conn,
      "sess-1",
      {
        toolCallId: "tc-mcp",
        toolName: "custom-mcp-server__some_interactive_tool",
        args: llmArgs,
        status: "completed",
        result: {
          type: "text",
          text: "Stop this turn.",
          structuredContent: {
            status: "pending",
            requestId: "demo_002",
            input: mcpNormalizedInput,
          },
        },
      },
      { inflightTools: inflight }
    );

    expect(updates[0]?.rawInput).toEqual(mcpNormalizedInput);
    expect(updates[0]?.rawInput).not.toEqual(llmArgs);
    expect(updates[0]?.title).toBe("Executing ask-question__nuwax_ask_question");
    expect(updates[0]?.title).toContain("nuwax_ask_question");
  });

  it("completed 识别 MCP CallToolResult 顶层 structuredContent.input", async () => {
    const { conn, updates } = collectUpdates();
    const mcpInput = {
      schemaVersion: "nuwax.mcp_ask.v2",
      ui: { version: "nuwax.interaction.v2", fields: [] },
    };
    await emitToolCall(conn, "sess-1", {
      toolCallId: "tc-ctr",
      toolName: "any-server__any_tool",
      args: { ui: { fields: [] } },
      status: "completed",
      result: {
        content: [{ type: "text", text: "done" }],
        structuredContent: { status: "pending", input: mcpInput },
      },
    });
    expect(updates[0]?.rawInput).toEqual(mcpInput);
    expect(updates[0]?.rawOutput).toMatchObject({ status: "pending" });
  });

  it("completed 从 inflightTools 回填 rawInput 并解析 structuredContent", async () => {
    const { conn, updates } = collectUpdates();
    const inflight = new Map<string, ToolCallEvent>();
    const args = {
      requestId: "demo_1",
      title: "演示",
      ui: { version: "nuwax.interaction.v2", fields: [{ name: "x", widget: "text" }] },
    };
    inflight.set("tc-2", {
      toolCallId: "tc-2",
      toolName: "ask-question__nuwax_ask_question",
      args,
      status: "in_progress",
    });

    const mcpResult = JSON.stringify({
      type: "text",
      text: "Stop this turn.",
      structuredContent: {
        status: "pending",
        requestId: "demo_1",
        input: args,
      },
    });

    await emitToolCall(
      conn,
      "sess-1",
      {
        toolCallId: "tc-2",
        toolName: "ask-question__nuwax_ask_question",
        args: {},
        status: "completed",
        result: mcpResult,
      },
      { inflightTools: inflight }
    );

    expect(updates).toHaveLength(1);
    const u = updates[0]!;
    expect(u.sessionUpdate).toBe("tool_call_update");
    expect(u.status).toBe("completed");
    expect(u.rawInput).toEqual(args);
    expect(u.rawOutput).toMatchObject({ status: "pending", requestId: "demo_1" });
    expect(u).not.toHaveProperty("output");
    expect(u).not.toHaveProperty("input");
    const content = u.content as Array<{ content: { text: string } }>;
    expect(content[0]?.content?.text).toBe("Stop this turn.");
  });

  it("read_file completed 含 markdown 包裹的全文 content", async () => {
    const { conn, updates } = collectUpdates();
    await emitToolCall(conn, "sess-1", {
      toolCallId: "tc-read",
      toolName: "read_file",
      args: { path: "a.ts" },
      status: "completed",
      result: "export const x = 1;",
    });
    const content = updates[0]?.content as Array<{ content: { text: string } }>;
    expect(content[0]?.content?.text).toContain("export const x = 1;");
    expect(updates[0]?.rawOutput).toBe("export const x = 1;");
  });

  it("completed 无 result 时写占位文本，无 output", async () => {
    const { conn, updates } = collectUpdates();
    await emitToolCall(conn, "sess-1", {
      toolCallId: "tc-3",
      toolName: "echo",
      args: {},
      status: "completed",
    });
    const content = updates[0]?.content as Array<{ content: { text: string } }>;
    expect(content[0]?.content?.text).toContain("未返回内容");
    expect(updates[0]).not.toHaveProperty("output");
  });

  it("C-dedupe：同 toolCallId 二次 in_progress 发 tool_call_update 而非重复 tool_call", async () => {
    const { conn, updates } = collectUpdates();
    const emitted = new Set<string>();
    const args = { path: "src/a.ts" };
    const event = {
      toolCallId: "tc-dup",
      toolName: "read_file",
      args,
      status: "in_progress" as const,
    };

    await emitToolCall(conn, "sess-1", event, {
      workspaceRoot: "/ws",
      emittedToolCallIds: emitted,
    });
    await emitToolCall(conn, "sess-1", event, {
      workspaceRoot: "/ws",
      emittedToolCallIds: emitted,
    });

    expect(updates).toHaveLength(2);
    expect(updates[0]?.sessionUpdate).toBe("tool_call");
    expect(updates[1]?.sessionUpdate).toBe("tool_call_update");
    expect(updates[1]).toMatchObject({
      toolCallId: "tc-dup",
      status: "in_progress",
      rawInput: args,
    });
    expect(emitted.has("tc-dup")).toBe(true);
  });

  it("C-dedupe：completed 后 emitted 集合清除，同 id 可再发 tool_call", async () => {
    const { conn, updates } = collectUpdates();
    const emitted = new Set<string>();
    const inflight = new Map<string, ToolCallEvent>();

    await emitToolCall(
      conn,
      "sess-1",
      { toolCallId: "tc-re", toolName: "echo", args: { x: 1 }, status: "in_progress" },
      { emittedToolCallIds: emitted, inflightTools: inflight }
    );
    inflight.set("tc-re", {
      toolCallId: "tc-re",
      toolName: "echo",
      args: { x: 1 },
      status: "in_progress",
    });
    await emitToolCall(
      conn,
      "sess-1",
      { toolCallId: "tc-re", toolName: "echo", args: {}, status: "completed", result: "ok" },
      { emittedToolCallIds: emitted, inflightTools: inflight }
    );

    expect(emitted.has("tc-re")).toBe(false);

    await emitToolCall(
      conn,
      "sess-1",
      { toolCallId: "tc-re", toolName: "echo", args: { x: 2 }, status: "in_progress" },
      { emittedToolCallIds: emitted }
    );
    expect(updates.filter((u) => u.sessionUpdate === "tool_call")).toHaveLength(2);
  });

  it("双轨 terminal 去重：节点直出 completed 后，stream 轨冗余 completed 被跳过", async () => {
    const { conn, updates } = collectUpdates();
    const inflight = new Map<string, ToolCallEvent>();
    const completedIds = new Set<string>();

    // 节点直出 completed：inflightTools 缓存仍在 → 回填 rawInput，发首个 terminal update
    inflight.set("tc-d", {
      toolCallId: "tc-d",
      toolName: "ask-question__nuwax_ask_question",
      args: { requestId: "demo_1", ui: { version: "nuwax.interaction.v2" } },
      status: "in_progress",
    });
    await emitToolCall(
      conn,
      "sess-1",
      {
        toolCallId: "tc-d",
        toolName: "ask-question__nuwax_ask_question",
        args: { requestId: "demo_1", ui: { version: "nuwax.interaction.v2" } },
        status: "completed",
        result: "done",
      },
      { inflightTools: inflight, completedToolCallIds: completedIds }
    );

    // 节点直出 completed 已在外层 inflightTools.delete（见 server.ts:106）；
    // stream on_tool_end 随后到达：args 空（dispatch tool_update 不带 input）、缓存空
    inflight.delete("tc-d");
    await emitToolCall(
      conn,
      "sess-1",
      {
        toolCallId: "tc-d",
        toolName: "ask-question__nuwax_ask_question",
        args: {},
        status: "completed",
        result: "done",
      },
      { inflightTools: inflight, completedToolCallIds: completedIds }
    );

    // 只发一个 terminal update（首个带 rawInput）；stream 冗余 completed 被跳过，不会发无 rawInput 的第二个
    const terminals = updates.filter(
      (u) => u.sessionUpdate === "tool_call_update" && u.status === "completed"
    );
    expect(terminals).toHaveLength(1);
    expect(terminals[0]?.rawInput).toEqual({
      requestId: "demo_1",
      ui: { version: "nuwax.interaction.v2" },
    });
    expect(terminals[0]?.title).toContain("nuwax_ask_question");
    expect(completedIds.has("tc-d")).toBe(true);
  });

  it("completed/failed 带 title+kind，供 Backend ASK_QUESTION 识别", async () => {
    const { conn, updates } = collectUpdates();
    await emitToolCall(conn, "sess-1", {
      toolCallId: "tc-title",
      toolName: "ask-question__nuwax_ask_question",
      args: { requestId: "x", ui: { fields: [{ name: "a", widget: "text" }] } },
      status: "failed",
      error: "boom",
    });
    expect(updates[0]).toMatchObject({
      sessionUpdate: "tool_call_update",
      status: "failed",
      title: "Executing ask-question__nuwax_ask_question",
      kind: "other",
    });
  });

  it("write_file in_progress 含 diff content（presentation 层）", async () => {
    const { conn, updates } = collectUpdates();
    await emitToolCall(
      conn,
      "sess-1",
      {
        toolCallId: "tc-w",
        toolName: "write_file",
        args: { path: "a.ts", content: "hello" },
        status: "in_progress",
      },
      { workspaceRoot: "/ws" }
    );
    const content = updates[0]?.content as Array<{ type: string; newText: string }>;
    expect(content?.[0]).toMatchObject({ type: "diff", newText: "hello" });
  });
});

describe("normalizeToolResult", () => {
  it("解析双重 JSON 字符串中的 structuredContent", () => {
    const inner = {
      type: "text",
      text: "presented",
      structuredContent: { status: "pending", requestId: "r1" },
    };
    const out = normalizeToolResult(JSON.stringify(inner));
    expect(out.text).toBe("presented");
    expect(out.rawOutput).toEqual({ status: "pending", requestId: "r1" });
  });

  it("content block 数组拼接 text", () => {
    const text = normalizeToolMessageContent([
      { type: "text", text: "a" },
      { type: "text", text: "b" },
    ]);
    expect(text).toBe("a\nb");
  });
});
