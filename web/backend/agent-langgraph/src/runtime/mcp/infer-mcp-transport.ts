/**
 * MCP transport 推断 —— 从 server 配置判断 stdio / sse / http。
 *
 * 优先级（高 → 低）：
 * 1. 显式 `transport`（deepagents 配置惯用字段）
 * 2. 显式 `type`（ACP / 平台 MCP JSON 惯用字段，如 `{ type: "sse", url }`）
 * 3. URL 路径特征（如 `/api/mcp/sse`）→ sse
 * 4. 有 `url` 未指定 → http（mcp-adapters 可 automaticSSEFallback）
 * 5. 有 `command` → stdio
 *
 * 平台 SSE 网关示例：`{ type: "sse", url: ".../api/mcp/sse?ak=..." }` 应直接走 SSE，
 * 避免先 Streamable HTTP 再回退带来的延迟与误判。
 */

export type McpTransportKind = "stdio" | "sse" | "http";

const TRANSPORT_KINDS = new Set<McpTransportKind>(["stdio", "sse", "http"]);

/** 判断字符串是否为合法的 transport kind。 */
function isTransportKind(value: string): value is McpTransportKind {
  return TRANSPORT_KINDS.has(value as McpTransportKind);
}

/**
 * 从 `transport` / `type` 读取显式声明；`transport` 优先于 `type`（两者同时存在时）。
 * 平台与 ACP 常用 `type`，deepagents 配置常用 `transport`，此处统一归一。
 */
export function resolveExplicitTransport(config: {
  transport?: string;
  type?: string;
}): McpTransportKind | undefined {
  if (typeof config.transport === "string" && isTransportKind(config.transport)) {
    return config.transport;
  }
  if (typeof config.type === "string" && isTransportKind(config.type)) {
    return config.type;
  }
  return undefined;
}

/**
 * 按 URL 路径特征推断 transport（仅在未显式声明 transport/type 时使用）。
 *
 * - 平台聚合网关：`/api/mcp/sse`、`/mcp/sse` 等 → sse
 * - 路径以 `/sse` 结尾（含 query）→ sse
 * - 其余有 url 的场景由调用方默认 http
 */
export function inferTransportFromUrl(url: string): McpTransportKind {
  const lower = url.toLowerCase();
  // 平台 Workflow/Plugin 聚合 SSE 网关（testagent.../api/mcp/sse?ak=...）
  if (lower.includes("/api/mcp/sse") || lower.includes("/mcp/sse")) {
    return "sse";
  }
  try {
    const pathname = new URL(url).pathname.toLowerCase();
    if (pathname.endsWith("/sse")) {
      return "sse";
    }
  } catch {
    // 非绝对 URL 时 fallback 到字符串匹配
    if (/\/sse(?:\?|#|$)/i.test(url)) {
      return "sse";
    }
  }
  return "http";
}

/**
 * 推断 MCP server 应使用的 transport。
 * 显式 transport/type 优先；否则按 URL 特征；最后 url→http、command→stdio。
 */
export function inferMcpTransport(config: {
  command?: string;
  url?: string;
  transport?: McpTransportKind;
  /** ACP / 平台 MCP JSON 的 `type` 字段，与 `transport` 语义相同。 */
  type?: McpTransportKind | string;
}): McpTransportKind | null {
  const explicit = resolveExplicitTransport(config);
  if (explicit) {
    return explicit;
  }
  if (config.url) {
    return inferTransportFromUrl(config.url);
  }
  if (config.command) {
    return "stdio";
  }
  return null;
}
