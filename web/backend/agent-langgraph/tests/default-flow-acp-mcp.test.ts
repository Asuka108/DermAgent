/**
 * 默认 flow + ACP session MCP —— 合并后的 server 配置进入 runtime。
 *
 * MCP server 经 runtime-context（@langchain/mcp-adapters）hydrate 加载为 native 工具
 * （ctx.mcpTools），不再经 mcp_tool_bridge 元工具（已移除：它与有状态 server 冲突，
 * 每次 call kill 子进程导致 chrome-devtools 页面反复关闭）。本测试不 spawn 真实 MCP 子进程：
 * 只验证 mcpServerConfigs 合并正确（builtin ask-question + ACP session），以及工具集不再含 bridge。
 */

import { describe, it, expect } from "vitest";
import { MemorySaver } from "@langchain/langgraph";
import { loadFlowConfig } from "../src/runtime/flow-config.js";
import { createRuntimeContext } from "../src/runtime/context/runtime-context.js";
import { createFlowTools } from "../src/app/flow-tools.js";
import { getFlowSandboxPolicy } from "../src/runtime/fs/sandbox.js";
import { recipe } from "../src/app/default-flow.js";
import type { FlowRuntime } from "../src/runtime/flow-runtime.js";
import { createFileCheckpointer } from "../src/runtime/services/file-checkpoint-saver.js";

const ALIEN_WORKSPACE = "/tmp/deepagents-flow-ts-default-flow-acp-mcp";

describe("默认 flow：ACP session mcpServers → 工具集", () => {
  it("mcpServerConfigs 合并 builtin ask-question + ACP 下发 server；工具集不再含 mcp_tool_bridge", () => {
    const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
    const ctx = createRuntimeContext(appConfig, {
      cwd: ALIEN_WORKSPACE,
      mcpServers: {
        time: { command: "npx", args: ["-y", "@modelcontextprotocol/server-time"] },
      },
    });
    const serverNames = Object.keys(ctx.mcpServerConfigs).sort();
    expect(serverNames).toEqual(["ask-question", "time"].sort());

    const policy = getFlowSandboxPolicy(appConfig);
    const allTools = createFlowTools(ctx, {
      workspaceRoot: ALIEN_WORKSPACE,
      policy,
    });
    // MCP 工具走 native（hydrate 加载到 ctx.mcpTools），不再有 mcp_tool_bridge 元工具
    expect(allTools.find((t) => t.name === "mcp_tool_bridge")).toBeUndefined();
  });

  it("default recipe buildGraph 接受含 ACP 合并工具集的 runtime.allTools", () => {
    const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
    const ctx = createRuntimeContext(appConfig, {
      cwd: ALIEN_WORKSPACE,
      mcpServers: { whois: { command: "echo", args: ["stub"] } },
    });
    const policy = getFlowSandboxPolicy(appConfig);
    const allTools = createFlowTools(ctx, {
      workspaceRoot: ALIEN_WORKSPACE,
      policy,
    });
    const runtime = {
      config: appConfig,
      ctx,
      allTools,
      systemPrompt: "test",
      skillsPaths: [],
      skills: [],
      subAgents: [],
      sandbox: policy,
      workspaceRoot: ALIEN_WORKSPACE,
      checkpointer: createFileCheckpointer(appConfig, ALIEN_WORKSPACE),
    } satisfies FlowRuntime;

    const graph = recipe(runtime).buildGraph(new MemorySaver());
    expect(graph).toBeDefined();
    // bridge 已移除：allTools 不应再含 mcp_tool_bridge
    expect(allTools.some((t) => t.name === "mcp_tool_bridge")).toBe(false);
  });
});
