/**
 * ACP 工具权限审批 —— 节点层 A2 门控 + surface 层 onPermissionRequest handler。
 *
 * 节点层（createToolExecNode）：被拒/取消的 call 预合成 error ToolMessage 注入 ToolNode，
 *   靠 ToolNode 去重跳过执行；allow 走原路径。不依赖 ACP / LLM。
 * surface 层（createAcpPermissionHandler）：mode/interruptOn/弹窗/降级判定（不缓存，记忆交 client 中枢）。
 */

import { describe, it, expect } from "vitest";
import { AIMessage } from "@langchain/core/messages";
import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { createToolExecNode, createPermissionApprovalNode } from "../src/libs/nodes/index.js";
import {
  createAcpPermissionHandler,
  createAcpApprovalHandler,
} from "../src/surfaces/acp/server.js";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** 造一个记录是否被执行的工具。 */
function spyTool(name: string, executed: string[], result = `${name}-ok`) {
  return tool(
    async () => {
      executed.push(name);
      return result;
    },
    { name, description: name, schema: z.object({}) },
  );
}

function aiWithCalls(calls: Array<{ id: string; name: string }>) {
  return new AIMessage({
    content: "",
    tool_calls: calls.map((c) => ({ id: c.id, name: c.name, args: {}, type: "tool_call" as const })),
  });
}

// ---------- 节点层 A2 门控 ----------
describe("createToolExecNode 审批门控 (A2)", () => {
  it("allow → 工具执行，返回真实结果", async () => {
    const executed: string[] = [];
    const node = createToolExecNode({ tools: [spyTool("danger", executed)] });
    const res: any = await node(
      { messages: [aiWithCalls([{ id: "c1", name: "danger" }])] },
      { configurable: { onPermissionRequest: async () => "allow" } } as any,
    );
    expect(executed).toEqual(["danger"]);
    expect(res.messages).toHaveLength(1);
    expect(res.messages[0].content).toBe("danger-ok");
    expect(res.messages[0].status).not.toBe("error");
  });

  it("reject → 不执行 + 合成 Permission denied ToolMessage(error)", async () => {
    const executed: string[] = [];
    const node = createToolExecNode({ tools: [spyTool("danger", executed)] });
    const res: any = await node(
      { messages: [aiWithCalls([{ id: "c1", name: "danger" }])] },
      { configurable: { onPermissionRequest: async () => "reject" } } as any,
    );
    expect(executed).toEqual([]); // 未执行
    expect(res.messages).toHaveLength(1);
    expect(res.messages[0].tool_call_id).toBe("c1");
    expect(res.messages[0].status).toBe("error");
    expect(String(res.messages[0].content)).toContain("Permission denied");
  });

  it("cancelled → 同 reject：不执行 + error ToolMessage", async () => {
    const executed: string[] = [];
    const node = createToolExecNode({ tools: [spyTool("danger", executed)] });
    const res: any = await node(
      { messages: [aiWithCalls([{ id: "c1", name: "danger" }])] },
      { configurable: { onPermissionRequest: async () => "cancelled" } } as any,
    );
    expect(executed).toEqual([]);
    expect(res.messages[0].status).toBe("error");
  });

  it("无 onPermissionRequest → 全执行（向后兼容 CLI/非 ACP）", async () => {
    const executed: string[] = [];
    const node = createToolExecNode({ tools: [spyTool("danger", executed)] });
    const res: any = await node(
      { messages: [aiWithCalls([{ id: "c1", name: "danger" }])] },
      {} as any,
    );
    expect(executed).toEqual(["danger"]);
    expect(res.messages[0].content).toBe("danger-ok");
  });

  it("批量 [allow, reject] → 2 条 ToolMessage、原序、被拒未执行", async () => {
    const executed: string[] = [];
    const node = createToolExecNode({
      tools: [spyTool("tool_a", executed), spyTool("tool_b", executed)],
    });
    const decisions: Record<string, "allow" | "reject"> = { tool_a: "allow", tool_b: "reject" };
    const res: any = await node(
      {
        messages: [
          aiWithCalls([
            { id: "c1", name: "tool_a" },
            { id: "c2", name: "tool_b" },
          ]),
        ],
      },
      { configurable: { onPermissionRequest: async (e: any) => decisions[e.toolName] } } as any,
    );
    expect(executed).toEqual(["tool_a"]); // 只有 a 执行
    expect(res.messages).toHaveLength(2);
    expect(res.messages[0].tool_call_id).toBe("c1"); // 原 calls 顺序
    expect(res.messages[1].tool_call_id).toBe("c2");
    expect(res.messages[0].content).toBe("tool_a-ok");
    expect(res.messages[1].status).toBe("error");
  });

  it("reject → 补发 failed terminal（in_progress→failed，不卡转圈）", async () => {
    const events: Array<{ status: string; toolCallId: string }> = [];
    const node = createToolExecNode({ tools: [spyTool("danger", [])] });
    await node(
      { messages: [aiWithCalls([{ id: "c1", name: "danger" }])] },
      {
        configurable: {
          onPermissionRequest: async () => "reject",
          onToolCall: (e: any) => events.push({ status: e.status, toolCallId: e.toolCallId }),
        },
      } as any,
    );
    expect(events.some((x) => x.status === "in_progress" && x.toolCallId === "c1")).toBe(true);
    expect(events.some((x) => x.status === "failed" && x.toolCallId === "c1")).toBe(true);
  });

  it("cancelled → 不发 terminal（交 failInflightToolsOnCancel 收尾）", async () => {
    const events: string[] = [];
    const node = createToolExecNode({ tools: [spyTool("danger", [])] });
    await node(
      { messages: [aiWithCalls([{ id: "c1", name: "danger" }])] },
      {
        configurable: {
          onPermissionRequest: async () => "cancelled",
          onToolCall: (e: any) => events.push(e.status),
        },
      } as any,
    );
    expect(events).toContain("in_progress");
    expect(events).not.toContain("failed");
    expect(events).not.toContain("completed");
  });
});

// ---------- surface 层 handler ----------
function fakeConn(opts?: {
  outcome?: { outcome: string; optionId?: string };
  throws?: boolean;
  noPermission?: boolean;
}) {
  const requests: Array<Record<string, unknown>> = [];
  const conn: any = { async sessionUpdate() {} };
  if (!opts?.noPermission) {
    conn.requestPermission = async (params: Record<string, unknown>) => {
      requests.push(params);
      if (opts?.throws) throw new Error("method not found");
      return { outcome: opts?.outcome ?? { outcome: "selected", optionId: "allow-once" } };
    };
  }
  return { conn, requests };
}

const ASK = { mode: "ask", interruptOn: ["bash"] };
const evt = { toolCallId: "t1", toolName: "bash", args: {} };

describe("createAcpPermissionHandler (ACP surface)", () => {
  it("mode=yolo → allow，不弹窗", async () => {
    const { conn, requests } = fakeConn();
    const h = createAcpPermissionHandler(conn, "s", { mode: "yolo", interruptOn: ["bash"] }, "/ws");
    expect(await h(evt)).toBe("allow");
    expect(requests).toHaveLength(0);
  });

  it("非 interruptOn 工具 → allow，不弹窗", async () => {
    const { conn, requests } = fakeConn();
    const h = createAcpPermissionHandler(conn, "s", ASK, "/ws");
    expect(await h({ toolCallId: "t1", toolName: "read_file", args: {} })).toBe("allow");
    expect(requests).toHaveLength(0);
  });

  it("allow-once → allow", async () => {
    const { conn, requests } = fakeConn({ outcome: { outcome: "selected", optionId: "allow-once" } });
    const h = createAcpPermissionHandler(conn, "s", ASK, "/ws");
    expect(await h(evt)).toBe("allow");
    expect(requests).toHaveLength(1);
  });

  it("allow-always → allow（agent 不缓存，每次仍弹；always 记忆交 client 中枢）", async () => {
    const { conn, requests } = fakeConn({ outcome: { outcome: "selected", optionId: "allow-always" } });
    const h = createAcpPermissionHandler(conn, "s", ASK, "/ws");
    expect(await h(evt)).toBe("allow");
    expect(await h(evt)).toBe("allow");
    expect(requests).toHaveLength(2); // 不缓存 → 两次都发请求
  });

  it("reject-once → reject", async () => {
    const { conn } = fakeConn({ outcome: { outcome: "selected", optionId: "reject-once" } });
    const h = createAcpPermissionHandler(conn, "s", ASK, "/ws");
    expect(await h(evt)).toBe("reject");
  });

  it("reject-always → reject（agent 不缓存，每次仍弹）", async () => {
    const { conn, requests } = fakeConn({ outcome: { outcome: "selected", optionId: "reject-always" } });
    const h = createAcpPermissionHandler(conn, "s", ASK, "/ws");
    expect(await h(evt)).toBe("reject");
    expect(await h(evt)).toBe("reject");
    expect(requests).toHaveLength(2);
  });

  it("outcome=cancelled → cancelled", async () => {
    const { conn } = fakeConn({ outcome: { outcome: "cancelled" } });
    const h = createAcpPermissionHandler(conn, "s", ASK, "/ws");
    expect(await h(evt)).toBe("cancelled");
  });

  it("预置 aborted signal → cancelled，不弹窗", async () => {
    const { conn, requests } = fakeConn();
    const ac = new AbortController();
    ac.abort();
    const h = createAcpPermissionHandler(conn, "s", ASK, "/ws", ac.signal);
    expect(await h(evt)).toBe("cancelled");
    expect(requests).toHaveLength(0);
  });

  it("conn 无 requestPermission → 降级 allow", async () => {
    const { conn } = fakeConn({ noPermission: true });
    const h = createAcpPermissionHandler(conn, "s", ASK, "/ws");
    expect(await h(evt)).toBe("allow");
  });

  it("requestPermission 抛错 → graceful allow", async () => {
    const { conn } = fakeConn({ throws: true });
    const h = createAcpPermissionHandler(conn, "s", ASK, "/ws");
    expect(await h(evt)).toBe("allow");
  });
});

// ---------- B 弹窗式审批节点 ----------
describe("createPermissionApprovalNode (B 节点层)", () => {
  const mkNode = () =>
    createPermissionApprovalNode<{ x: number }>({
      request: () => ({ title: "确认发布?", detail: "draft" }),
      approved: () => ({ x: 1 }),
      rejected: () => ({ x: 0 }),
    });

  it("allow → approved 分支", async () => {
    const res: any = await mkNode()(
      { x: 9 },
      { configurable: { onApprovalRequest: async () => "allow" } } as any,
    );
    expect(res).toEqual({ x: 1 });
  });

  it("reject → rejected 分支", async () => {
    const res: any = await mkNode()(
      { x: 9 },
      { configurable: { onApprovalRequest: async () => "reject" } } as any,
    );
    expect(res).toEqual({ x: 0 });
  });

  it("cancelled → rejected 分支", async () => {
    const res: any = await mkNode()(
      { x: 9 },
      { configurable: { onApprovalRequest: async () => "cancelled" } } as any,
    );
    expect(res).toEqual({ x: 0 });
  });

  it("无 onApprovalRequest → 默认 approved（向后兼容 CLI/非 ACP）", async () => {
    const res: any = await mkNode()({ x: 9 }, {} as any);
    expect(res).toEqual({ x: 1 });
  });
});

const APPROVE_EVT = { title: "确认发布?", detail: "draft" };
const ASK_B = { mode: "ask", interruptOn: [] as string[] };

describe("createAcpApprovalHandler (B surface)", () => {
  it("mode=yolo → allow，不弹窗", async () => {
    const { conn, requests } = fakeConn();
    const h = createAcpApprovalHandler(conn, "s", { mode: "yolo", interruptOn: [] }, "/ws");
    expect(await h(APPROVE_EVT)).toBe("allow");
    expect(requests).toHaveLength(0);
  });

  it("弹窗 allow-once → allow（总弹，无 interruptOn 名单约束）", async () => {
    const { conn, requests } = fakeConn({ outcome: { outcome: "selected", optionId: "allow-once" } });
    const h = createAcpApprovalHandler(conn, "s", ASK_B, "/ws");
    expect(await h(APPROVE_EVT)).toBe("allow");
    expect(requests).toHaveLength(1);
  });

  it("reject-once → reject", async () => {
    const { conn } = fakeConn({ outcome: { outcome: "selected", optionId: "reject-once" } });
    const h = createAcpApprovalHandler(conn, "s", ASK_B, "/ws");
    expect(await h(APPROVE_EVT)).toBe("reject");
  });

  it("outcome=cancelled → cancelled", async () => {
    const { conn } = fakeConn({ outcome: { outcome: "cancelled" } });
    const h = createAcpApprovalHandler(conn, "s", ASK_B, "/ws");
    expect(await h(APPROVE_EVT)).toBe("cancelled");
  });

  it("conn 无 requestPermission → 降级 allow", async () => {
    const { conn } = fakeConn({ noPermission: true });
    const h = createAcpApprovalHandler(conn, "s", ASK_B, "/ws");
    expect(await h(APPROVE_EVT)).toBe("allow");
  });
});
