/**
 * destroyRuntimeContext —— session 结束时关闭 MCP client（stdio / SSE / HTTP 连接）。
 */

import { describe, expect, it, vi } from "vitest";
import {
  createRuntimeContext,
  destroyRuntimeContext,
} from "../src/runtime/context/runtime-context.js";
import type { AppConfig } from "../src/runtime/config/config-loader.js";

const minimalConfig = {
  agent: { name: "test", description: "" },
  model: { provider: "anthropic", name: "test" },
  mcp: { servers: {} },
  skills: { directories: [] },
  agentsDirectories: [],
} as unknown as AppConfig;

describe("destroyRuntimeContext", () => {
  it("关闭 bulk mcpClient 并清空引用", async () => {
    const ctx = createRuntimeContext(minimalConfig);
    const close = vi.fn().mockResolvedValue(undefined);
    ctx.mcpClient = { close } as never;

    await destroyRuntimeContext(ctx);

    expect(close).toHaveBeenCalledOnce();
    expect(ctx.mcpClient).toBeNull();
    expect(ctx.mcpFallbackClients).toEqual([]);
  });

  it("关闭 per-server fallback clients", async () => {
    const ctx = createRuntimeContext(minimalConfig);
    const closeA = vi.fn().mockResolvedValue(undefined);
    const closeB = vi.fn().mockResolvedValue(undefined);
    ctx.mcpFallbackClients = [{ close: closeA }, { close: closeB }] as never[];

    await destroyRuntimeContext(ctx);

    expect(closeA).toHaveBeenCalledOnce();
    expect(closeB).toHaveBeenCalledOnce();
    expect(ctx.mcpFallbackClients).toEqual([]);
  });

  it("无 client 时安全 no-op", async () => {
    const ctx = createRuntimeContext(minimalConfig);
    await expect(destroyRuntimeContext(ctx)).resolves.toBeUndefined();
  });
});
