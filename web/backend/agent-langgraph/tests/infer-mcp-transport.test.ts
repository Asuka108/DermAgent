import { describe, expect, it } from "vitest";
import {
  inferMcpTransport,
  inferTransportFromUrl,
  resolveExplicitTransport,
} from "../src/runtime/mcp/infer-mcp-transport.js";
import { toConnections } from "../src/runtime/context/runtime-context.js";
import { toMcpConnection } from "../src/libs/mcp/mcp-access.js";

describe("resolveExplicitTransport", () => {
  it("transport 优先于 type", () => {
    expect(
      resolveExplicitTransport({ transport: "http", type: "sse" })
    ).toBe("http");
  });

  it("仅有 type 时识别 sse/http/stdio", () => {
    expect(resolveExplicitTransport({ type: "sse" })).toBe("sse");
    expect(resolveExplicitTransport({ type: "http" })).toBe("http");
  });

  it("非法 type 忽略", () => {
    expect(resolveExplicitTransport({ type: "websocket" })).toBeUndefined();
  });
});

describe("inferTransportFromUrl", () => {
  it("平台 /api/mcp/sse 路径 → sse", () => {
    expect(
      inferTransportFromUrl("https://testagent.xspaceagi.com/api/mcp/sse?ak=secret")
    ).toBe("sse");
  });

  it("路径以 /sse 结尾 → sse", () => {
    expect(inferTransportFromUrl("https://example.com/v1/sse")).toBe("sse");
  });

  it("普通 MCP HTTP 端点 → http", () => {
    expect(
      inferTransportFromUrl("http://127.0.0.1:57155/mcp/chrome-devtools")
    ).toBe("http");
  });
});

describe("inferMcpTransport", () => {
  it("显式 transport 优先", () => {
    expect(
      inferMcpTransport({
        url: "https://x.com/api/mcp/sse",
        transport: "sse",
      })
    ).toBe("sse");
    expect(
      inferMcpTransport({
        url: "https://x.com/api/mcp/sse",
        transport: "http",
      })
    ).toBe("http");
  });

  it("显式 type:sse 优先于 URL 默认", () => {
    expect(
      inferMcpTransport({
        type: "sse",
        url: "https://testagent.xspaceagi.com/api/mcp/sse?ak=secret",
      })
    ).toBe("sse");
  });

  it("type:http 可覆盖 SSE URL 特征", () => {
    expect(
      inferMcpTransport({
        type: "http",
        url: "https://testagent.xspaceagi.com/api/mcp/sse?ak=secret",
      })
    ).toBe("http");
  });

  it("无显式声明时按 URL 特征推断 sse", () => {
    expect(
      inferMcpTransport({
        url: "https://testagent.xspaceagi.com/api/mcp/sse?ak=secret",
      })
    ).toBe("sse");
    expect(inferMcpTransport({ url: "https://example.com/v1/sse" })).toBe("sse");
  });

  it("无 SSE 特征的 url → http", () => {
    expect(
      inferMcpTransport({ url: "http://127.0.0.1:57155/mcp/chrome-devtools" })
    ).toBe("http");
  });

  it("有 command → stdio", () => {
    expect(inferMcpTransport({ command: "npx", args: ["-y", "pkg"] })).toBe("stdio");
  });

  it("无 url/command → null", () => {
    expect(inferMcpTransport({})).toBeNull();
  });
});

describe("toConnections", () => {
  it("type:sse 直接走 sse 并挂默认 reconnect", () => {
    const conn = toConnections({
      proxy: {
        type: "sse",
        url: "https://testagent.xspaceagi.com/api/mcp/sse?ak=ak-test",
      },
    });
    expect(conn.proxy).toEqual({
      transport: "sse",
      url: "https://testagent.xspaceagi.com/api/mcp/sse?ak=ak-test",
      reconnect: { enabled: true, maxAttempts: 3, delayMs: 1000 },
    });
  });

  it("平台 SSE URL 无 type 时按特征走 sse", () => {
    const conn = toConnections({
      platform: {
        url: "https://testagent.xspaceagi.com/api/mcp/sse?ak=ak-test",
      },
    });
    expect(conn.platform).toEqual({
      transport: "sse",
      url: "https://testagent.xspaceagi.com/api/mcp/sse?ak=ak-test",
      reconnect: { enabled: true, maxAttempts: 3, delayMs: 1000 },
    });
  });

  it("显式 transport:sse 走 sse", () => {
    const conn = toConnections({
      platform: {
        url: "https://x.com/api/mcp/sse",
        transport: "sse",
      },
    });
    expect(conn.platform).toEqual({
      transport: "sse",
      url: "https://x.com/api/mcp/sse",
      reconnect: { enabled: true, maxAttempts: 3, delayMs: 1000 },
    });
  });

  it("普通 HTTP URL 走 http + automaticSSEFallback", () => {
    const conn = toConnections({
      "chrome-devtools": { url: "http://127.0.0.1:57155/mcp/chrome-devtools" },
    });
    expect(conn["chrome-devtools"]).toEqual({
      transport: "http",
      url: "http://127.0.0.1:57155/mcp/chrome-devtools",
      automaticSSEFallback: true,
    });
  });

  it("显式 automaticSSEFallback:false 可关闭回退", () => {
    const conn = toConnections({
      platform: {
        url: "https://x.com/mcp",
        automaticSSEFallback: false,
      },
    });
    expect((conn.platform as { automaticSSEFallback?: boolean }).automaticSSEFallback).toBe(
      false
    );
  });
});

describe("toMcpConnection 与 toConnections 一致", () => {
  it("type:sse 两端均为 sse", () => {
    const cfg = {
      type: "sse" as const,
      url: "https://testagent.xspaceagi.com/api/mcp/sse?ak=1",
    };
    expect(toMcpConnection(cfg)).toEqual({
      transport: "sse",
      url: cfg.url,
      reconnect: { enabled: true, maxAttempts: 3, delayMs: 1000 },
    });
    expect(toConnections({ s: cfg }).s).toEqual({
      transport: "sse",
      url: cfg.url,
      reconnect: { enabled: true, maxAttempts: 3, delayMs: 1000 },
    });
  });

  it("SSE URL 特征推断两端一致", () => {
    const url = "https://testagent.xspaceagi.com/api/mcp/sse?ak=1";
    expect(toMcpConnection({ url })).toEqual({
      transport: "sse",
      url,
      reconnect: { enabled: true, maxAttempts: 3, delayMs: 1000 },
    });
    expect(toConnections({ s: { url } }).s).toEqual({
      transport: "sse",
      url,
      reconnect: { enabled: true, maxAttempts: 3, delayMs: 1000 },
    });
  });
});
