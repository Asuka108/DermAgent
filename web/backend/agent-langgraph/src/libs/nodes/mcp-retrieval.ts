/**
 * createMcpRetrievalNode —— 收口「真实 MCP 检索」bespoke 模式（×3：travel research / RAG retrieve / deep-research research）。
 *
 * 模式：按 state 选 MCP server+tool+args → rateLimited 节流 → callResolvedMcpTool 调用 → runTool 三态透出 → 写回。
 * 与 createToolExecNode 互补：后者执行模型 tool_calls（ToolNode 模式，工具已知）；本 factory 是**主动检索**——
 * 节点自己决定调哪个 MCP server 的哪个 tool（RAG/调研场景）。
 *
 * 多源并行取优（如 deep-research 的 docs + web 启发式合并）**不收口**——保留 bespoke subgraph（选项会膨胀）；
 * 本 factory 覆盖单源 / 简单多源（多源时各走一次 factory，外层 reducer 聚合）。
 *
 * @example
 * const research = createMcpRetrievalNode<MyState>({
 *   mcpServers: { search: { command: "npx", args: ["-y", "@platform/search-mcp"] } },
 *   retrieve: (s) => ({ server: "search", tool: "search", args: { query: s.currentAspect } }),
 *   write: (r, s) => ({ findings: [{ aspect: s.currentAspect, suggestion: r.ok ? r.text.slice(0, 800) : `（失败：${r.text}）` }] }),
 * });
 */
import type { LangGraphRunnableConfig } from "@langchain/langgraph";
import type { MultiServerMCPClient } from "@langchain/mcp-adapters";
import type { FlowCallbacks } from "../../core/flow-types.js";
import { runTool } from "./tools.js";
import {
  resolveAccessor,
  rateLimited,
  type McpServerConfig,
} from "../mcp/mcp-access.js";

export interface McpRetrievalNodeOptions<S> {
  /** MCP 服务器配置（语义名 → stdio spec），或按 state 解析。 */
  mcpServers:
    | Record<string, McpServerConfig>
    | ((state: S) => Record<string, McpServerConfig>);
  /** 从 state 选要调的 server+tool+args；返回 null → 跳过（写回空结果）。 */
  retrieve: (
    state: S
  ) => { server: string; tool: string; args: Record<string, unknown> } | null;
  /** 是否走全局 rateLimited 闸门（默认 true；Send 扇出并行时必须）。 */
  rateLimited?: boolean;
  /** 单次调用超时（默认 20000ms）。 */
  timeoutMs?: number;
  /** 把检索结果写回 state。result.ok=false 时 result.text 是错误信息。 */
  write: (result: { text: string; ok: boolean }, state: S) => Partial<S>;
  /** 日志/事件 label。 */
  label?: string;
}

/**
 * 造一个「主动 MCP 检索」节点。返回 `(state, config?) => Promise<Partial<S>>`。
 * 从 config?.configurable?.onToolCall 读三态回调（surface 透出「工具调用过程」），与 createToolExecNode 一致。
 */
export function createMcpRetrievalNode<S>(
  opts: McpRetrievalNodeOptions<S>
): (state: S, config?: LangGraphRunnableConfig) => Promise<Partial<S>> {
  const timeoutMs = opts.timeoutMs ?? 20000;
  return async (state: S, config?: LangGraphRunnableConfig): Promise<Partial<S>> => {
    const servers =
      typeof opts.mcpServers === "function" ? opts.mcpServers(state) : opts.mcpServers;
    const target = opts.retrieve(state);
    if (!target) {
      return opts.write({ text: "", ok: false }, state);
    }
    const serverCfg = servers[target.server];
    if (!serverCfg) {
      return opts.write({ text: `MCP server "${target.server}" 未配置`, ok: false }, state);
    }
    const onToolCall = config?.configurable?.onToolCall as
      | FlowCallbacks["onToolCall"]
      | undefined;
    // 注入持久 mcpClient 优先（该 server 已连则复用）；否则自管临时 client。透传 timeoutMs。
    const mcpClient = config?.configurable?.mcpClient as
      | MultiServerMCPClient
      | undefined;
    const fn = () => {
      const call = async () => {
        const { accessor, dispose } = await resolveAccessor({
          client: mcpClient,
          server: target.server,
          config: serverCfg,
          timeoutMs,
        });
        try {
          return await accessor.callResolved(target.tool, target.args);
        } finally {
          await dispose?.();
        }
      };
      return opts.rateLimited === false ? call() : rateLimited(call);
    };
    const { result, ok } = await runTool(target.tool, target.args, fn, onToolCall);
    return opts.write({ text: result, ok }, state);
  };
}
