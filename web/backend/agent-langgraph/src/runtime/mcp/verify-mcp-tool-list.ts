/**
 * 用 MCP tools/list 确认 server 是否真正连上（不靠 URL/工具名特征推断）。
 */
import type { MultiServerMCPClient } from "@langchain/mcp-adapters";

/** listTools 原始响应中的工具名列表。 */
function parseToolNames(list: unknown): string[] {
  if (!list || typeof list !== "object") return [];
  const tools = (list as { tools?: Array<{ name?: unknown }> }).tools;
  if (!Array.isArray(tools)) return [];
  return tools
    .map((t) => t?.name)
    .filter((n): n is string => typeof n === "string" && n.length > 0);
}

/**
 * 对每个 server 调 tools/list；成功则写入结果，失败或 getClient 为空则跳过。
 * 返回值仅含 **list 成功** 的 server → 供日志与 systemPrompt MCP 段落使用。
 */
export async function verifyMcpServersWithToolList(
  client: MultiServerMCPClient,
  serverNames: string[]
): Promise<Record<string, string[]>> {
  const out: Record<string, string[]> = {};
  // 并行对各 server 调 tools/list（彼此独立），降低多 server 时的串行等待。
  await Promise.all(
    serverNames.map(async (name) => {
      try {
        const c = await client.getClient(name);
        if (!c) return;
        out[name] = parseToolNames(await c.listTools());
      } catch {
        // listTools 失败视为未连接，不写入 out
      }
    })
  );
  return out;
}
