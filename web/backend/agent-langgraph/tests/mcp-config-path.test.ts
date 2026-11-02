/**
 * MCP 配置路径单测 —— 用户 workspace cwd 与 Agent 包根分离时，默认 MCP 仍从包内加载。
 */

import { describe, it, expect } from "vitest";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { loadFlowConfig } from "../src/runtime/flow-config.js";
import { createRuntimeContext } from "../src/runtime/context/runtime-context.js";
import { resolvePackageRoot } from "../src/runtime/package-root.js";
import { resolveBuiltinTemplateConfig } from "../src/runtime/config/config-paths.js";

const ALIEN_WORKSPACE = "/tmp/deepagents-flow-ts-alien-workspace-test";

describe("resolvePackageRoot", () => {
  it("定位到含 config/flow-agent.config.json 的包根", () => {
    const root = resolvePackageRoot(import.meta.url);
    expect(existsSync(resolve(root, "config/flow-agent.config.json"))).toBe(true);
    expect(existsSync(resolve(root, "config/mcp.default.json"))).toBe(true);
  });
});

describe("loadFlowConfig + MCP 默认路径", () => {
  it("ACP 用户 workspace 与包根不同时，mcp.configPath 仍指向包内 mcp.default.json", () => {
    const pkgRoot = resolvePackageRoot(import.meta.url);
    const { appConfig, configPath } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });

    const builtin = resolveBuiltinTemplateConfig("flowAgent");
    expect(configPath).toBe(builtin.path);
    expect(appConfig.mcp.configPath).toBe(resolve(pkgRoot, "config/mcp.default.json"));
    expect(existsSync(appConfig.mcp.configPath)).toBe(true);
  });
});

describe("createRuntimeContext 默认 MCP servers", () => {
  it("默认 mcp.default.json 内置 ask-question", () => {
    const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
    const ctx = createRuntimeContext(appConfig, { cwd: ALIEN_WORKSPACE });
    expect(Object.keys(ctx.mcpServerConfigs)).toEqual(["ask-question"]);
    expect(ctx.mcpServerConfigs["ask-question"]).toMatchObject({
      command: "npx",
      args: ["-y", "nuwax-ask-question-mcp@latest"],
    });
  });

  it("ACP session 下发的 mcpServers 与默认合并（session-wins 同名覆盖）", () => {
    const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
    const ctx = createRuntimeContext(appConfig, {
      cwd: ALIEN_WORKSPACE,
      mcpServers: {
        "doc-mcp": { command: "echo", args: ["acp-override"] },
        whois: { command: "npx", args: ["-y", "@whois-mcp/example"] },
      },
    });
    expect(Object.keys(ctx.mcpServerConfigs).sort()).toEqual(
      ["ask-question", "doc-mcp", "whois"].sort()
    );
    expect(ctx.mcpServerConfigs["doc-mcp"]).toMatchObject({
      command: "echo",
      args: ["acp-override"],
    });
    expect(ctx.mcpServerConfigs.whois).toMatchObject({
      command: "npx",
      args: ["-y", "@whois-mcp/example"],
    });
  });

  it("平台同名 ask-question 覆盖内置（session-wins）", () => {
    const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
    const ctx = createRuntimeContext(appConfig, {
      cwd: ALIEN_WORKSPACE,
      mcpServers: {
        "ask-question": { command: "echo", args: ["platform"] },
      },
    });
    expect(ctx.mcpServerConfigs["ask-question"]).toMatchObject({
      command: "echo",
      args: ["platform"],
    });
  });
});
