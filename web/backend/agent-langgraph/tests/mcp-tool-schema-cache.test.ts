import { DynamicStructuredTool } from "@langchain/core/tools";
import { beforeEach, describe, expect, it, vi } from "vitest";

const lazyMcp = vi.hoisted(() => ({
  getTools: vi.fn(),
  innerFunc: vi.fn(),
}));

vi.mock("@langchain/mcp-adapters", () => ({
  MultiServerMCPClient: class {
    getTools = lazyMcp.getTools;
  },
}));

import {
  buildLazyToolsFromCache,
  computeMcpConfigFingerprint,
  extractCacheEntriesFromTools,
  type CachedToolSchemas,
} from "../src/runtime/mcp/tool-schema-cache.js";

describe("MCP 工具 schema 缓存", () => {
  beforeEach(() => {
    lazyMcp.getTools.mockReset();
    lazyMcp.innerFunc.mockReset();
  });

  it("鉴权或 stdio 环境变化时配置指纹必须变化", () => {
    const base = {
      mcp: {
        command: "node",
        args: ["server.js"],
        env: { TENANT: "one" },
        headers: { Authorization: "Bearer old" },
      },
    };

    expect(computeMcpConfigFingerprint(base)).not.toBe(
      computeMcpConfigFingerprint({
        mcp: { ...base.mcp, headers: { Authorization: "Bearer new" } },
      })
    );
    expect(computeMcpConfigFingerprint(base)).not.toBe(
      computeMcpConfigFingerprint({
        mcp: { ...base.mcp, env: { TENANT: "two" } },
      })
    );
  });

  it("缓存原始 MCP 名称，同时保存可绑定模型的规范化名称", () => {
    const tool = new DynamicStructuredTool({
      name: "search__查询 工具",
      description: "查询",
      schema: { type: "object", properties: {} },
      func: async () => "ok",
    });

    expect(extractCacheEntriesFromTools([tool], ["search"])).toEqual([
      expect.objectContaining({
        // 与运行时 sanitizeLoadedMcpTools 保持完全一致。
        name: "search",
        rawName: "查询 工具",
        server: "search",
      }),
    ]);
  });

  it("缓存工具保持 MCP 的富内容格式、metadata 与当前超时", () => {
    const cached: CachedToolSchemas = {
      version: 2,
      sessionId: "sess-cache-test",
      fingerprint: "fingerprint",
      createdAt: new Date().toISOString(),
      tools: [
        {
          name: "search__lookup",
          rawName: "lookup",
          server: "search",
          description: "查询",
          schema: { type: "object", properties: {} },
          metadata: { annotations: { readOnlyHint: true } },
        },
      ],
    };

    const { tools } = buildLazyToolsFromCache(
      cached,
      { search: { defaultToolTimeout: 1234 } }
    );
    const [tool] = tools as Array<DynamicStructuredTool>;

    expect(tool.responseFormat).toBe("content_and_artifact");
    expect(tool.defaultConfig).toEqual({ timeout: 1234 });
    expect(tool.metadata).toEqual({ annotations: { readOnlyHint: true } });
  });

  it("首次工具调用委托给 adapter 原生工具，并保留取消信号与超时", async () => {
    const cached: CachedToolSchemas = {
      version: 2,
      sessionId: "sess-cache-test",
      fingerprint: "fingerprint",
      createdAt: new Date().toISOString(),
      tools: [{
        name: "search__lookup",
        rawName: "lookup",
        server: "search",
        description: "查询",
        schema: { type: "object", properties: {} },
      }],
    };
    const controller = new AbortController();
    const signal = controller.signal;
    lazyMcp.innerFunc.mockResolvedValue([[{ type: "text", text: "ok" }], []]);
    lazyMcp.getTools.mockResolvedValue([{
      name: "search__lookup",
      schema: { type: "object", properties: {} },
      func: lazyMcp.innerFunc,
    }]);

    const { tools } = buildLazyToolsFromCache(
      cached,
      { search: { defaultToolTimeout: 1234 } }
    );
    await tools[0]!.invoke({ query: "hi" }, { signal });

    expect(lazyMcp.getTools).toHaveBeenCalledWith("search");
    const [args, , config] = lazyMcp.innerFunc.mock.calls[0]!;
    expect(args).toEqual({ query: "hi" });
    expect(config).toEqual(expect.objectContaining({
      metadata: expect.objectContaining({ timeoutMs: 1234 }),
    }));
    const forwardedSignal = (config as { signal?: AbortSignal }).signal;
    expect(forwardedSignal).toBeDefined();
    controller.abort();
    expect(forwardedSignal?.aborted).toBe(true);
  });

  it("远端不再提供缓存工具时清理缓存，避免下次继续命中陈旧 schema", async () => {
    const cached: CachedToolSchemas = {
      version: 2,
      sessionId: "sess-cache-test",
      fingerprint: "fingerprint",
      createdAt: new Date().toISOString(),
      tools: [{
        name: "search__lookup",
        rawName: "lookup",
        server: "search",
        description: "查询",
        schema: { type: "object", properties: {} },
      }],
    };
    const clear = vi.fn();
    lazyMcp.getTools.mockResolvedValue([]);
    const { tools } = buildLazyToolsFromCache(cached, { search: {} }, undefined, clear);

    await expect(tools[0]!.invoke({})).rejects.toThrow("未提供缓存的工具");
    expect(clear).toHaveBeenCalledWith("search", "lookup", "remote_tool_missing");
  });

  it("同名工具的 schema 变化时清理缓存，不用旧 schema 调用远端", async () => {
    const cached: CachedToolSchemas = {
      version: 2,
      sessionId: "sess-cache-test",
      fingerprint: "fingerprint",
      createdAt: new Date().toISOString(),
      tools: [{
        name: "search__lookup",
        rawName: "lookup",
        server: "search",
        description: "查询",
        schema: { type: "object", properties: { query: { type: "string" } } },
      }],
    };
    const clear = vi.fn();
    lazyMcp.getTools.mockResolvedValue([{
      name: "search__lookup",
      schema: {
        type: "object",
        properties: { query: { type: "string" }, limit: { type: "number" } },
        required: ["query", "limit"],
      },
      func: lazyMcp.innerFunc,
    }]);
    const { tools } = buildLazyToolsFromCache(cached, { search: {} }, undefined, clear);

    await expect(tools[0]!.invoke({ query: "hi" })).rejects.toThrow("schema 已变化");
    expect(clear).toHaveBeenCalledWith("search", "lookup", "schema_changed");
    expect(lazyMcp.innerFunc).not.toHaveBeenCalled();
  });
});
