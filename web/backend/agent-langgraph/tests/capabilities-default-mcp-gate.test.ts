/**
 * flow capabilities CLI —— DEEPAGENTS_DEFAULT_MCP=disabled 时静态输出与 runtime 一致。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ENV_KEY = "DEEPAGENTS_DEFAULT_MCP";

describe("runCapabilities + DEEPAGENTS_DEFAULT_MCP", () => {
  let saved: string | undefined;
  let stdoutChunks: string[];

  beforeEach(() => {
    saved = process.env[ENV_KEY];
    stdoutChunks = [];
    vi.spyOn(process.stdout, "write").mockImplementation((chunk) => {
      stdoutChunks.push(String(chunk));
      return true;
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    if (saved === undefined) delete process.env[ENV_KEY];
    else process.env[ENV_KEY] = saved;
  });

  it("disabled 时 mcpServers 为空对象", async () => {
    process.env[ENV_KEY] = "disabled";
    const { runCapabilities } = await import("../src/surfaces/cli/capabilities.js");
    await runCapabilities();
    const payload = JSON.parse(stdoutChunks.join("")) as { mcpServers: Record<string, unknown> };
    expect(payload.mcpServers).toEqual({});
  });

  it("未设置时仍列出 mcp.default.json 内置 server", async () => {
    delete process.env[ENV_KEY];
    const { runCapabilities } = await import("../src/surfaces/cli/capabilities.js");
    await runCapabilities();
    const payload = JSON.parse(stdoutChunks.join("")) as { mcpServers: Record<string, unknown> };
    expect(Object.keys(payload.mcpServers)).toEqual(["ask-question"]);
  });
});
