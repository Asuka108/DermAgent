/**
 * MCP server / tool 标识符规范化。
 *
 * LLM 工具 API（Anthropic / OpenAI 兼容代理）要求 function.name 匹配 `^[a-zA-Z0-9_-]+$`。
 * nuwaclaw 本地 MCP 配置、ACP session 下发可能含中文或空格（如「A股股票查询」），
 * mcp-adapters 的 prefixToolNameWithServerName 会把 server 名拼进工具名，导致 bindTools 400。
 *
 * 与 nuwaclaw `mcpServerName.ts` 保持同一替换规则：`[^a-zA-Z0-9_-]` → `_`。
 */

/** LLM / OpenAI function.name 允许的字符集。 */
export const MCP_IDENTIFIER_PATTERN = /^[a-zA-Z0-9_-]+$/;

/**
 * 将单个 MCP server 或 tool 名规范为 LLM 可接受的标识符。
 *
 * 规则：
 * 1. 非 `[a-zA-Z0-9_-]` 字符 → `_`
 * 2. 连续 `_` 合并为一个
 * 3. 去掉首尾 `_`
 * 4. 空串或仍不合规 → `mcp_server` / `mcp_tool`
 */
export function sanitizeMcpIdentifier(raw: string, fallback = "mcp_server"): string {
  let s = raw.replace(/[^a-zA-Z0-9_-]/g, "_");
  s = s.replace(/_+/g, "_").replace(/^_|_$/g, "");
  if (!s || !MCP_IDENTIFIER_PATTERN.test(s)) {
    return fallback;
  }
  return s;
}

/** server 名专用 fallback（与 tool 区分，便于日志排查）。 */
export function sanitizeMcpServerName(raw: string): string {
  return sanitizeMcpIdentifier(raw, "mcp_server");
}

/** MCP 工具名专用（bindTools 前对 StructuredTool.name 二次兜底）。 */
export function sanitizeMcpToolName(raw: string): string {
  return sanitizeMcpIdentifier(raw, "mcp_tool");
}

export interface SanitizeMcpServerRecordResult<T> {
  servers: Record<string, T>;
  /** 原名 → 规范名（仅发生重命名时存在条目）。 */
  renames: Record<string, string>;
}

/**
 * 批量规范化 MCP server 配置键名；同名冲突时追加 `_2`、`_3`…
 */
export function sanitizeMcpServerRecord<T>(
  servers: Record<string, T>
): SanitizeMcpServerRecordResult<T> {
  const out: Record<string, T> = {};
  const renames: Record<string, string> = {};
  const used = new Set<string>();

  for (const [rawName, cfg] of Object.entries(servers)) {
    let safe = sanitizeMcpServerName(rawName);
    let candidate = safe;
    let suffix = 2;
    while (used.has(candidate)) {
      candidate = `${safe}_${suffix}`;
      suffix += 1;
    }
    used.add(candidate);
    out[candidate] = cfg;
    if (candidate !== rawName) {
      renames[rawName] = candidate;
    }
  }

  return { servers: out, renames };
}
