/**
 * MCP 访问层 —— LangGraph 图内「主动检索」节点的补充路径（非 agent 主 ToolNode 链路）。
 *
 * **Agent 主路径（LangGraph 官方接入）**：`MultiServerMCPClient.getTools()` → `StructuredTool[]`
 * → think `bindTools` → `createToolExecNode` / `ToolNode`（见 runtime-context）。
 *
 * **本模块**：同一 `@langchain/mcp-adapters` client 上的按-server list/call，供 retrieve / doc-retrieval 等
 * 主动检索节点；底层 `@modelcontextprotocol/sdk` Client.callTool。
 *
 * - 有状态 server（chrome-devtools）必须经 runtime 持久连接，不经「每次 call 新进程」。
 * - 检索节点优先复用 runtime 注入的 `mcpClient`（`createAccessorFromClient`）；否则 fallback
 *   自管临时 client（`createAccessorFromConfig`，用完 close）。
 *
 * 超时：callTool 经 SDK RequestOptions{timeout}；连接经 withTimeout 兜底。
 */
import { MultiServerMCPClient } from "@langchain/mcp-adapters";
import { inferMcpTransport } from "../../runtime/mcp/infer-mcp-transport.js";

/** 多 transport MCP server 配置（stdio / http / sse）。省略 transport/type 时按 inferMcpTransport 推断。 */
export interface McpServerConfig {
  command?: string;
  args?: string[];
  url?: string;
  env?: Record<string, string>;
  transport?: "stdio" | "sse" | "http";
  /** 平台/ACP 惯用字段，与 transport 等价。 */
  type?: "stdio" | "sse" | "http";
  headers?: Record<string, string>;
  restart?: { enabled: boolean; maxAttempts?: number; delayMs?: number };
  reconnect?: { enabled: boolean; maxAttempts?: number; delayMs?: number };
  defaultToolTimeout?: number;
  /** Streamable HTTP 失败时回退 SSE（默认 true）。 */
  automaticSSEFallback?: boolean;
}

/** MultiServerMCPClient.getClient() 返回的底层 MCP SDK Client（有 callTool / listTools）。 */
type McpRawClient = NonNullable<Awaited<ReturnType<MultiServerMCPClient["getClient"]>>>;

/** 仅保留 plain object 且值为 string 的键值（ACP/外部下发可能是脏数据，如 array）。 */
function cleanStringMap(raw: unknown): Record<string, string> | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof v === "string") out[k] = v;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

const DEFAULT_SSE_RECONNECT = {
  enabled: true,
  maxAttempts: 3,
  delayMs: 1000,
};

/** 单 server 配置 → mcp-adapters connection（多 transport）。无 url/command 返回 null。 */
export function toMcpConnection(config: McpServerConfig): Record<string, unknown> | null {
  const headers = cleanStringMap(config.headers);
  const timeout =
    typeof config.defaultToolTimeout === "number" ? config.defaultToolTimeout : undefined;
  const kind = inferMcpTransport(config);

  if (config.url && kind === "sse") {
    const reconnect = config.reconnect ?? DEFAULT_SSE_RECONNECT;
    return {
      transport: "sse",
      url: config.url,
      ...(headers ? { headers } : {}),
      reconnect,
      ...(timeout ? { defaultToolTimeout: timeout } : {}),
    };
  }
  if (config.url) {
    return {
      transport: "http",
      url: config.url,
      automaticSSEFallback: config.automaticSSEFallback ?? true,
      ...(headers ? { headers } : {}),
      ...(timeout ? { defaultToolTimeout: timeout } : {}),
    };
  }
  if (config.command) {
    const env = cleanStringMap(config.env);
    return {
      transport: "stdio",
      command: config.command,
      args: config.args ?? [],
      ...(env ? { env } : {}),
      ...(timeout ? { defaultToolTimeout: timeout } : {}),
    };
  }
  return null;
}

/** 从 tools/call 或 listTools 响应提取纯文本（{ content: [{type:"text",text}] }）。 */
export function extractMcpText(result: unknown): string {
  if (!result || typeof result !== "object") return "";
  const r = result as { content?: Array<{ type?: string; text?: string }> };
  if (!Array.isArray(r.content)) return JSON.stringify(result);
  return r.content
    .filter((b) => b.type === "text" && typeof b.text === "string")
    .map((b) => b.text as string)
    .join("\n");
}

/** 纯函数：从 tools/list 名称里选最合适的工具名（精确 → alias → 模糊 → 仅一个则用之）。 */
export function chooseMcpToolName(
  available: string[],
  preferred: string,
  aliases: string[] = []
): string {
  if (available.includes(preferred)) return preferred;
  for (const alias of aliases) {
    if (available.includes(alias)) return alias;
  }
  const prefLower = preferred.toLowerCase();
  const fuzzy = available.find(
    (n) => n.toLowerCase().includes(prefLower) || prefLower.includes(n.toLowerCase())
  );
  if (fuzzy) return fuzzy;
  if (available.length === 1) return available[0]!;
  throw new Error(`MCP 工具未找到: ${preferred}；可用工具: ${available.join(", ") || "(none)"}`);
}

/** 给 Promise 套一个超时上限（ms<=0 表示不限）。连接/调用挂起时 reject，避免图节点永久 await。 */
function withTimeout<T>(p: Promise<T>, ms: number | undefined, label: string): Promise<T> {
  if (!ms || ms <= 0) return p;
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`${label} 超时（${ms}ms）`)),
      ms
    );
    p.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (e) => {
        clearTimeout(timer);
        reject(e);
      }
    );
  });
}

/** McpAccessor：按 server 维度的 list/call 能力。 */
export interface McpAccessor {
  listTools(): Promise<string[]>;
  listToolsRaw(): Promise<unknown>;
  callTool(name: string, args: Record<string, unknown>): Promise<string>;
  callResolved(
    preferred: string,
    args: Record<string, unknown>,
    opts?: { aliases?: string[] }
  ): Promise<string>;
}

async function rawListTools(c: McpRawClient): Promise<{ tools?: Array<{ name?: string }> }> {
  return (await c.listTools()) as { tools?: Array<{ name?: string }> };
}

/** 调一个工具并提取文本。timeoutMs 经 SDK RequestOptions{timeout} 强制单次调用超时。 */
async function rawCallTool(
  c: McpRawClient,
  name: string,
  args: Record<string, unknown>,
  timeoutMs?: number
): Promise<string> {
  const result = await c.callTool(
    { name, arguments: args } as never,
    undefined,
    timeoutMs ? ({ timeout: timeoutMs } as never) : undefined
  );
  return extractMcpText(result);
}

/**
 * 注入式 accessor：复用 runtime 已加载的持久 MultiServerMCPClient。
 * getClient(server) 会触发 initializeConnections（幂等，已连则直接返回缓存）。
 * timeoutMs 作用于每次 callTool（SDK RequestOptions）。
 */
export function createAccessorFromClient(
  client: MultiServerMCPClient,
  server: string,
  timeoutMs?: number
): McpAccessor {
  const get = async (): Promise<McpRawClient> => {
    const c = await client.getClient(server);
    if (!c) throw new Error(`MCP server "${server}" 未连接`);
    return c;
  };
  return {
    async listTools() {
      const list = await rawListTools(await get());
      return (list.tools ?? []).map((t) => t.name).filter((n): n is string => Boolean(n));
    },
    async listToolsRaw() {
      return rawListTools(await get());
    },
    async callTool(name, args) {
      return rawCallTool(await get(), name, args, timeoutMs);
    },
    async callResolved(preferred, args, opts) {
      const c = await get();
      const list = await rawListTools(c);
      const available = (list.tools ?? [])
        .map((t) => t.name)
        .filter((n): n is string => Boolean(n));
      const toolName = chooseMcpToolName(available, preferred, opts?.aliases);
      return rawCallTool(c, toolName, args, timeoutMs);
    },
  };
}

/** 自管临时 client 的 accessor：用完须 close 释放 stdio 子进程 / http 连接。 */
export interface DisposableMcpAccessor extends McpAccessor {
  close(): Promise<void>;
}

/** 自管临时 client 用的占位 server 名（单 server 场景）。 */
const SINGLE_SERVER = "__mcp_access_single__";

/**
 * 自管式 accessor：独立运行的示例 topology（无 runtime 注入）用。
 * 内部 new 一个官方 MultiServerMCPClient（多 transport），用 getClient 触发连接（比 getTools 轻，
 * 不加载全部工具 schema），用完须 close。连接失败（getClient 返回 undefined）或超时直接抛。
 */
export async function createAccessorFromConfig(
  server: string,
  config: McpServerConfig,
  timeoutMs?: number
): Promise<DisposableMcpAccessor> {
  const conn = toMcpConnection(config);
  if (!conn) throw new Error(`MCP server "${server}" 配置无效（无 url/command）`);
  const client = new MultiServerMCPClient({
    mcpServers: { [server]: conn },
    // 连接失败由下方 getClient 的 undefined 检查显式抛出，不依赖 handler-throw 的版本耦合。
    onConnectionError: "ignore",
  } as never);
  // getClient 触发 connect+initialize（initializeConnections 幂等）；超时兜底；未连上显式抛。
  await withTimeout(
    client.getClient(server).then((c) => {
      if (!c) throw new Error(`MCP server "${server}" 连接失败`);
      return c;
    }),
    timeoutMs,
    `MCP server "${server}" 连接`
  );
  const base = createAccessorFromClient(client, server, timeoutMs);
  return {
    ...base,
    close: () => client.close(),
  };
}

/** resolveAccessor 的结果：accessor + 可选 dispose（自管临时 client 时需 close，注入式为 undefined）。 */
export interface ResolvedAccessor {
  accessor: McpAccessor;
  /** 自管临时 client 时返回 close；注入持久 client 时为 undefined（由 runtime 统一销毁）。 */
  dispose?: () => Promise<void>;
}

/**
 * 统一「注入持久 client 优先，否则自管临时 client」的决策（图内检索节点推荐入口）。
 * 关键：注入的持久 client 中该 server 若未连上（bulk hydrate 时失败被 onConnectionError 跳过），
 * getClient 返回 undefined → 自动 fallback 到自管临时 client，而非抛错断裂检索。
 */
export async function resolveAccessor(opts: {
  client?: MultiServerMCPClient;
  server: string;
  config: McpServerConfig;
  timeoutMs?: number;
}): Promise<ResolvedAccessor> {
  if (opts.client) {
    // getClient 触发 initializeConnections（幂等）；失败/未连的 server 返回 undefined（不重试）。
    const connected = await opts.client.getClient(opts.server).catch(() => undefined);
    if (connected) {
      return { accessor: createAccessorFromClient(opts.client, opts.server, opts.timeoutMs) };
    }
    // 持久 client 中该 server 未连上 → fallback 自管临时 client
  }
  const accessor = await createAccessorFromConfig(opts.server, opts.config, opts.timeoutMs);
  return { accessor, dispose: () => accessor.close() };
}

/**
 * 先 resolve 工具名再调用（单次自管临时 client 内 list→选名→call）。
 * 独立文档检索节点用；honors options.timeoutMs（SDK RequestOptions）。
 */
export async function callResolvedMcpTool(
  config: McpServerConfig,
  preferred: string,
  args: Record<string, unknown>,
  options: { aliases?: string[]; timeoutMs?: number } = {}
): Promise<string> {
  const accessor = await createAccessorFromConfig(SINGLE_SERVER, config, options.timeoutMs);
  try {
    return await accessor.callResolved(preferred, args, { aliases: options.aliases });
  } finally {
    await accessor.close();
  }
}

/**
 * 全局节流：把并发调用串行化，每次之间至少间隔 minGapMs。
 * 接有 rate limit 的外部 API 时必备——图并行，但外部请求错峰执行。
 * 注：gate 是模块级全局，跨 server/session 共享串行（沿用旧实现；如需 per-server 节流另行改造）。
 */
let gate: Promise<unknown> = Promise.resolve();
export function rateLimited<T>(fn: () => Promise<T>, minGapMs = 1200): Promise<T> {
  const run = gate.then(async () => {
    try {
      return await fn();
    } finally {
      await new Promise((r) => setTimeout(r, minGapMs));
    }
  });
  // gate 推进到本次完成（吞掉错误，避免链断）
  gate = run.then(
    () => undefined,
    () => undefined
  );
  return run as Promise<T>;
}
