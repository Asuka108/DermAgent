/**
 * ACP 会话配置诊断单测 —— summarize / predict 根因信号（env 加载见 acp-session-config.test.ts）。
 */

import { describe, expect, it } from "vitest";
import {
  predictSystemPromptSource,
  summarizeAcpSessionParams,
  summarizeMcpServerEntry,
  systemPromptParamSource,
} from "../src/surfaces/acp/session-diagnostics.js";

describe("summarizeAcpSessionParams / systemPromptParamSource", () => {
  it("顶层 systemPrompt", () => {
    const params = { cwd: "/w", systemPrompt: "dev-agent-prompt" };
    expect(systemPromptParamSource(params)).toBe("params-top-level");
    expect(summarizeAcpSessionParams(params)).toMatchObject({
      hasTopLevelSystemPrompt: true,
      topLevelSystemPromptChars: "dev-agent-prompt".length,
    });
  });

  it("configOptions.systemPrompt 别名", () => {
    const params = { cwd: "/w", configOptions: { systemPrompt: "alias-prompt" } };
    expect(systemPromptParamSource(params)).toBe("params-configOptions");
  });

  it("无 systemPrompt", () => {
    expect(systemPromptParamSource({ cwd: "/w" })).toBe("none");
  });
});

describe("predictSystemPromptSource", () => {
  it("sessionConfig 有 prompt → acp-session", () => {
    expect(
      predictSystemPromptSource({
        sessionConfig: { systemPrompt: "x" },
        workspaceRoot: "/tmp",
      }).source
    ).toBe("acp-session");
  });

  it("无 session、有 inline → config-inline", () => {
    expect(
      predictSystemPromptSource({
        configInlinePrompt: "inline",
        workspaceRoot: "/tmp",
      }).source
    ).toBe("config-inline");
  });
});

describe("summarizeMcpServerEntry", () => {
  it("stdio: 敏感 arg/env 脱敏", () => {
    expect(
      summarizeMcpServerEntry({
        command: "npx",
        args: ["-y", "@example/doc-mcp", "API_KEY=sk-secret"],
        env: { DOC_MCP_KEY: "x", PATH: "/bin" },
      })
    ).toEqual({
      command: "npx",
      args: ["-y", "@example/doc-mcp", "API_KEY=***"],
      envKeys: ["DOC_MCP_KEY", "PATH"],
    });
  });

  it("url 敏感 query 脱敏", () => {
    expect(
      summarizeMcpServerEntry({
        url: "https://x.com/mcp?token=secret&keep=1",
      })
    ).toEqual({
      url: "https://x.com/mcp?token=***&keep=1",
    });
  });
});
