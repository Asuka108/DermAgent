/**
 * ACP per-session 配置提取单测 —— sessionConfigFromParams / acpMcpToRecord 纯函数。
 *
 * 验证 D 的实质数据流：从 ACP session/new|load 的 raw params 提取 cwd / mcpServers / model，
 * 且 mcpServers 的 array[{name}] 与 record 两种形态都能归一化成 Record<name, cfg>。
 * 无凭证、纯函数、确定性。
 */

import { afterEach, describe, it, expect } from "vitest";
import {
  acpMcpToRecord,
  extractRequestIdFromAcpParams,
  resolveAcpSessionConfig,
  sessionConfigFromParams,
} from "../src/surfaces/acp/server.js";

describe("acpMcpToRecord（MCP 形态归一化）", () => {
  it("undefined / null / 非对象 → undefined", () => {
    expect(acpMcpToRecord(undefined)).toBeUndefined();
    expect(acpMcpToRecord(null)).toBeUndefined();
    expect(acpMcpToRecord("not-an-object")).toBeUndefined();
  });

  it("array [{name, command, ...}] → {name: {command, ...}}", () => {
    const r = acpMcpToRecord([
      { name: "ctx7", command: "npx", args: ["-y", "x"] },
      { name: "fs", url: "http://x" },
    ]);
    expect(r).toEqual({
      ctx7: { command: "npx", args: ["-y", "x"] },
      fs: { url: "http://x" },
    });
  });

  it("array 中无 name 项被跳过；全无 name → undefined", () => {
    expect(acpMcpToRecord([{ command: "x" }, { name: "ok", command: "y" }])).toEqual({
      ok: { command: "y" },
    });
    expect(acpMcpToRecord([{ command: "x" }, { url: "y" }])).toBeUndefined();
  });

  it("record 形态 → 规范化键名（中文 server 名替换）", () => {
    const rec = { "A股股票查询": { command: "npx" }, ctx7: { command: "npx" } };
    const out = acpMcpToRecord(rec);
    expect(out).toEqual({ A: { command: "npx" }, ctx7: { command: "npx" } });
    expect(out).not.toBe(rec);
  });
});

describe("sessionConfigFromParams（cwd / mcpServers / model 提取）", () => {
  it("有 cwd → sessionConfig.cwd + workspaceRoot 都用该 cwd", () => {
    const { sessionConfig, workspaceRoot } = sessionConfigFromParams({ cwd: "/tmp/ws" });
    expect(workspaceRoot).toBe("/tmp/ws");
    expect(sessionConfig.cwd).toBe("/tmp/ws");
    expect(sessionConfig.mcpServers).toBeUndefined();
    expect(sessionConfig.model).toBeUndefined();
  });

  it("无 cwd → 回退 process.cwd()", () => {
    const { sessionConfig, workspaceRoot } = sessionConfigFromParams({});
    expect(workspaceRoot).toBe(process.cwd());
    expect(sessionConfig.cwd).toBe(process.cwd());
  });

  it("cwd + mcpServers(array) + model → 三字段齐全，mcpServers 归一化为 record", () => {
    const { sessionConfig } = sessionConfigFromParams({
      cwd: "/w",
      mcpServers: [{ name: "a", command: "x" }],
      model: "gpt-4o",
    });
    expect(sessionConfig.cwd).toBe("/w");
    expect(sessionConfig.mcpServers).toEqual({ a: { command: "x" } });
    expect(sessionConfig.model).toBe("gpt-4o");
  });

  it("mcpServers(record) → 透传", () => {
    const { sessionConfig } = sessionConfigFromParams({
      cwd: "/w",
      mcpServers: { a: { command: "x" } },
    });
    expect(sessionConfig.mcpServers).toEqual({ a: { command: "x" } });
  });

  it("model 非字符串 → 忽略", () => {
    const { sessionConfig } = sessionConfigFromParams({ cwd: "/w", model: 123 });
    expect(sessionConfig.model).toBeUndefined();
  });

  it("无任何字段 → 仅 cwd 回退，无 mcpServers/model", () => {
    const { sessionConfig } = sessionConfigFromParams({ unrelated: "x" });
    expect(sessionConfig.cwd).toBe(process.cwd());
    expect(Object.keys(sessionConfig)).toEqual(["cwd"]);
  });
});

describe("sessionConfigFromParams（systemPrompt 提取，ACP 最高优先级链路）", () => {
  it("顶层 params.systemPrompt → sessionConfig.systemPrompt", () => {
    const { sessionConfig } = sessionConfigFromParams({
      cwd: "/w",
      systemPrompt: "你是 dev-agent",
    });
    expect(sessionConfig.systemPrompt).toBe("你是 dev-agent");
  });

  it("顶层无 systemPrompt → 回退 configOptions.systemPrompt 别名", () => {
    const { sessionConfig } = sessionConfigFromParams({
      cwd: "/w",
      configOptions: { systemPrompt: "来自 configOptions" },
    });
    expect(sessionConfig.systemPrompt).toBe("来自 configOptions");
  });

  it("顶层优先于 configOptions.systemPrompt", () => {
    const { sessionConfig } = sessionConfigFromParams({
      cwd: "/w",
      systemPrompt: "顶层",
      configOptions: { systemPrompt: "别名" },
    });
    expect(sessionConfig.systemPrompt).toBe("顶层");
  });

  it("空串 / 全空白 systemPrompt → 忽略（不写入 sessionConfig）", () => {
    const { sessionConfig } = sessionConfigFromParams({ cwd: "/w", systemPrompt: "   " });
    expect(sessionConfig.systemPrompt).toBeUndefined();
    expect(Object.keys(sessionConfig)).toEqual(["cwd"]);
  });

  it("configOptions 非对象 → 不抛、systemPrompt 缺省", () => {
    const { sessionConfig } = sessionConfigFromParams({ cwd: "/w", configOptions: "x" });
    expect(sessionConfig.systemPrompt).toBeUndefined();
  });

  it("_meta.systemPrompt → sessionConfig.systemPrompt", () => {
    const { sessionConfig } = sessionConfigFromParams({
      cwd: "/w",
      _meta: { systemPrompt: "来自 _meta" },
    });
    expect(sessionConfig.systemPrompt).toBe("来自 _meta");
  });

  it("_meta.sessionConfig.systemPrompt 嵌套", () => {
    const { sessionConfig } = sessionConfigFromParams({
      cwd: "/w",
      _meta: { sessionConfig: { systemPrompt: "meta-session" } },
    });
    expect(sessionConfig.systemPrompt).toBe("meta-session");
  });
});

describe("resolveAcpSessionConfig（env + params 合并）", () => {
  const ENV_KEY = "ACP_SESSION_CONFIG_JSON";

  afterEach(() => {
    delete process.env[ENV_KEY];
  });

  it("env 有 systemPrompt、params 无 → 合并后保留 env prompt", () => {
    process.env[ENV_KEY] = JSON.stringify({ systemPrompt: "env-prompt", model: "gpt-4o" });
    const { sessionConfig, fromParams } = resolveAcpSessionConfig({ cwd: "/w" });
    expect(fromParams.systemPrompt).toBeUndefined();
    expect(sessionConfig.systemPrompt).toBe("env-prompt");
    expect(sessionConfig.model).toBe("gpt-4o");
    expect(sessionConfig.cwd).toBe("/w");
  });

  it("params systemPrompt 优先于 env", () => {
    process.env[ENV_KEY] = JSON.stringify({ systemPrompt: "env-prompt" });
    const { sessionConfig } = resolveAcpSessionConfig({
      cwd: "/w",
      systemPrompt: "params-prompt",
    });
    expect(sessionConfig.systemPrompt).toBe("params-prompt");
  });

  it("params mcpServers 优先，env model 补齐", () => {
    process.env[ENV_KEY] = JSON.stringify({ model: "from-env" });
    const { sessionConfig } = resolveAcpSessionConfig({
      cwd: "/w",
      mcpServers: [{ name: "ctx", command: "npx" }],
    });
    expect(sessionConfig.model).toBe("from-env");
    expect(sessionConfig.mcpServers).toEqual({ ctx: { command: "npx" } });
  });
});

describe("extractRequestIdFromAcpParams（NuwaClaw _meta 透传）", () => {
  it("优先读 _meta.requestId", () => {
    expect(
      extractRequestIdFromAcpParams({
        _meta: { requestId: "rid-001", request_id: "rid-ignored" },
      })
    ).toBe("rid-001");
  });

  it("requestId 缺失时回退 request_id", () => {
    expect(
      extractRequestIdFromAcpParams({ _meta: { request_id: "rid-snake" } })
    ).toBe("rid-snake");
  });

  it("无 _meta / 空串 → undefined", () => {
    expect(extractRequestIdFromAcpParams(undefined)).toBeUndefined();
    expect(extractRequestIdFromAcpParams({})).toBeUndefined();
    expect(extractRequestIdFromAcpParams({ _meta: { requestId: "  " } })).toBeUndefined();
  });
});
