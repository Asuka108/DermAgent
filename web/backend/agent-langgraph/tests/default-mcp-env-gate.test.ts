/**
 * DEEPAGENTS_DEFAULT_MCP 环境开关单测 —— disabled 时跳过默认 MCP server 的加载/连接。
 *
 * 语义：默认视为 enabled（现状行为不变）；值为 disabled（大小写不敏感，兼容 off/none）时
 * 模板/配置自带的默认 MCP（如 mcp.default.json 的 ask-question）不进入 mcpServerConfigs，
 * hydrate 因零连接直接返回——不建 client、不连接、不注册 MCP 工具，模板照常运行。
 * ACP session 下发的 mcpServers 属平台层，不受此开关影响。
 * 全程无 LLM 凭证、无真实网络（零连接时 hydrate 提前返回）。
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { loadFlowConfig } from "../src/runtime/flow-config.js";
import {
  createRuntimeContext,
  hydrateRuntimeContext,
  isDefaultMcpEnabled,
} from "../src/runtime/context/runtime-context.js";

const ALIEN_WORKSPACE = "/tmp/deepagents-flow-ts-default-mcp-gate-test";
const ENV_KEY = "DEEPAGENTS_DEFAULT_MCP";

describe("isDefaultMcpEnabled", () => {
  let saved: string | undefined;
  beforeEach(() => {
    saved = process.env[ENV_KEY];
  });
  afterEach(() => {
    if (saved === undefined) delete process.env[ENV_KEY];
    else process.env[ENV_KEY] = saved;
  });

  it("未设置时默认 enabled", () => {
    delete process.env[ENV_KEY];
    expect(isDefaultMcpEnabled()).toBe(true);
  });

  it("disabled 关闭（大小写不敏感）", () => {
    process.env[ENV_KEY] = "disabled";
    expect(isDefaultMcpEnabled()).toBe(false);
    process.env[ENV_KEY] = "DISABLED";
    expect(isDefaultMcpEnabled()).toBe(false);
    process.env[ENV_KEY] = " Disabled ";
    expect(isDefaultMcpEnabled()).toBe(false);
  });

  it("off / none 同样关闭", () => {
    process.env[ENV_KEY] = "off";
    expect(isDefaultMcpEnabled()).toBe(false);
    process.env[ENV_KEY] = "NONE";
    expect(isDefaultMcpEnabled()).toBe(false);
  });

  it("其他值（含 enabled / 1）不关闭", () => {
    process.env[ENV_KEY] = "enabled";
    expect(isDefaultMcpEnabled()).toBe(true);
    process.env[ENV_KEY] = "1";
    expect(isDefaultMcpEnabled()).toBe(true);
  });
});

describe("DEEPAGENTS_DEFAULT_MCP gate 默认 MCP 加载", () => {
  let saved: string | undefined;
  beforeEach(() => {
    saved = process.env[ENV_KEY];
  });
  afterEach(() => {
    if (saved === undefined) delete process.env[ENV_KEY];
    else process.env[ENV_KEY] = saved;
  });

  it("默认行为不变：mcp.default.json 的 ask-question 照常合并", () => {
    delete process.env[ENV_KEY];
    const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
    const ctx = createRuntimeContext(appConfig, { cwd: ALIEN_WORKSPACE });
    expect(Object.keys(ctx.mcpServerConfigs)).toEqual(["ask-question"]);
  });

  it("off / none 同样跳过默认 MCP 加载", () => {
    for (const value of ["off", "NONE"]) {
      process.env[ENV_KEY] = value;
      const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
      const ctx = createRuntimeContext(appConfig, { cwd: ALIEN_WORKSPACE });
      expect(ctx.mcpServerConfigs).toEqual({});
    }
  });

  it("disabled 时默认 MCP 不进入配置；hydrate 不建 client、不注册工具", async () => {
    process.env[ENV_KEY] = "disabled";
    const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
    const ctx = createRuntimeContext(appConfig, { cwd: ALIEN_WORKSPACE });
    expect(ctx.mcpServerConfigs).toEqual({});

    // 零连接 → hydrate 提前返回：无 MultiServerMCPClient、无网络连接、无 MCP 工具。
    const hydrated = await hydrateRuntimeContext(ctx);
    expect(hydrated.mcpServerConfigs).toEqual({});
    expect(hydrated.mcpTools).toEqual([]);
    expect(hydrated.mcpClient).toBeNull();
    expect(hydrated.mcpFallbackClients).toEqual([]);
    expect(hydrated.mcpServerToolLists).toEqual({});
  });

  it("disabled 只作用于默认层：ACP session 下发的 mcpServers 仍合并", () => {
    process.env[ENV_KEY] = "disabled";
    const { appConfig } = loadFlowConfig({ workspaceRoot: ALIEN_WORKSPACE });
    const ctx = createRuntimeContext(appConfig, {
      cwd: ALIEN_WORKSPACE,
      mcpServers: {
        "doc-mcp": { command: "echo", args: ["acp"] },
      },
    });
    expect(Object.keys(ctx.mcpServerConfigs)).toEqual(["doc-mcp"]);
    expect(ctx.mcpServerConfigs["doc-mcp"]).toMatchObject({ command: "echo" });
  });
});
