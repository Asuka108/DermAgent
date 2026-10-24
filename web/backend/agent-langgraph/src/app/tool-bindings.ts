import type { StructuredTool } from "@langchain/core/tools";

/**
 * 按工具名从全量工具集选取子集。
 *
 * - `names` 为空 → 返回全部：节点未声明工具名时用全部工具（向后兼容，默认 ReAct think 绑全部）。
 * - 平台工具经运行环境（MCP）注入 `allTools`；图节点在自身 params 里声明要用的工具名即可，
 *   不再需要「能力位/bindTo」分组层。
 *
 * 单工具节点（platform-tool）直接用 allTools + toolName 定位，无需调用本函数；
 * 工具集合节点（tool-exec）用本函数按名取子集后交给 ToolNode。
 */
export function pickTools(allTools: StructuredTool[], names: string[]): StructuredTool[] {
  if (!names || names.length === 0) return allTools;
  const wanted = new Set(names);
  return allTools.filter((t) => wanted.has(t.name));
}
