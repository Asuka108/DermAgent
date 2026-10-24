/**
 * MCP 标识符规范化单测 —— 中文 server 名、冲突去重、工具名兜底。
 */

import { describe, it, expect } from "vitest";
import {
  sanitizeMcpServerName,
  sanitizeMcpServerRecord,
  sanitizeMcpToolName,
  MCP_IDENTIFIER_PATTERN,
} from "../src/runtime/mcp/sanitize-mcp-name.js";
import { acpMcpToRecord } from "../src/surfaces/acp/session-config.js";
import { createRuntimeContext } from "../src/runtime/context/runtime-context.js";
import { loadFlowConfig } from "../src/runtime/flow-config.js";

const ALIEN_WORKSPACE = "/tmp/deepagents-flow-ts-sanitize-mcp-test";

describe("sanitizeMcpServerName", () => {
  it("ASCII 名保持不变", () => {
    expect(sanitizeMcpServerName("chrome-devtools")).toBe("chrome-devtools");
    expect(sanitizeMcpServerName("ask-question")).toBe("ask-question");
  });

  it("中文名替换为下划线并合并", () => {
    const safe = sanitizeMcpServerName("A股股票查询");
    expect(MCP_IDENTIFIER_PATTERN.test(safe)).toBe(true);
    expect(safe).toBe("A");
  });

  it("纯中文回退 mcp_server", () => {
    expect(sanitizeMcpServerName("股票查询")).toBe("mcp_server");
  });
});

describe("sanitizeMcpServerRecord", () => {
  it("冲突时追加数字后缀", () => {
    const { servers, renames } = sanitizeMcpServerRecord({
      "A股": { command: "a" },
      "B股": { command: "b" },
    });
    expect(renames["A股"]).toBe("A");
    expect(renames["B股"]).toBe("B");
    expect(servers.A).toEqual({ command: "a" });
    expect(servers.B).toEqual({ command: "b" });
  });
});

describe("acpMcpToRecord + createRuntimeContext", () => {
  it("array 形态中文 server 名被规范化", () => {
    const r = acpMcpToRecord([{ name: "A股股票查询", command: "node", args: ["x.js"] }]);
    expect(r).toEqual({ A: { command: "node", args: ["x.js"] } });
  });

  it("record 形态中文 server 名被规范化", () => {
    const r = acpMcpToRecord({ "A股股票查询": { command: "echo" } });
    expect(r).toEqual({ A: { command: "echo" } });
  });

  it("createRuntimeContext 合并后 server 键名为 LLM 合法标识符", () => {
    const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
    const ctx = createRuntimeContext(appConfig, {
      cwd: ALIEN_WORKSPACE,
      mcpServers: {
        "A股股票查询": { command: "echo", args: ["stub"] },
      },
    });
    expect(ctx.mcpServerConfigs.A).toMatchObject({ command: "echo", args: ["stub"] });
    expect(ctx.mcpServerConfigs["A股股票查询"]).toBeUndefined();
    for (const name of Object.keys(ctx.mcpServerConfigs)) {
      expect(MCP_IDENTIFIER_PATTERN.test(name)).toBe(true);
    }
  });
});

describe("sanitizeMcpToolName", () => {
  it("带中文前缀的工具名规范化", () => {
    const safe = sanitizeMcpToolName("A股股票查询__query_stock");
    expect(MCP_IDENTIFIER_PATTERN.test(safe)).toBe(true);
    expect(safe).toBe("A_query_stock");
  });
});
