/**
 * MCP 工具 schema 磁盘缓存（sessionId 失效策略）。
 *
 * 目的：ACP per-session 进程模型下，每次会话装配都要 `MultiServerMCPClient.getTools()`
 * （建连接 + 枚举工具），冷启动大头（日志实测 ~1.7s）。本模块把「已枚举的工具 schema」
 * 落盘缓存，同一 sessionId 的进程重启（ACP `phase=load`）直接从缓存重建**懒工具桩**，
 * 完全跳过 getTools()；工具桩被首次调用时才真正连服务器（懒连接），纯对话（不调工具）零连接成本。
 *
 * 失效策略（与用户确认）：
 *  - 主键 = sessionId：仅同 sessionId 复用，绝不跨会话串味（不同会话 mcpServers/model 可能不同）。
 *  - `phase=new`：先清除该 sessionId 旧缓存 → 不读 → 走正常 getTools → 写新缓存（保证新会话必新鲜）。
 *  - `phase=load`：读缓存，指纹一致则命中。
 *  - 配置指纹（merged mcpServers 的 hash）二级校验：同会话内改了 MCP 配置也会失效重建。
 *  - TTL：读时顺手清理过期文件（默认 1 天），避免无限增长。
 *
 * schema 可序列化性：@langchain/mcp-adapters 的 DynamicStructuredTool.schema 存的就是
 * 原始 JSON Schema 对象（loadMcpTools 里的 simplifiedSchema），可直接 JSON.stringify，
 * 也能直接喂回 DynamicStructuredTool 重建（与库内部构造方式一致），无需 zod 往返转换。
 *
 * 失败安全：任何读/写/解析异常都降级为「未命中」，绝不阻断启动。
 */

import { createHash } from "node:crypto";
import {
  mkdirSync,
  existsSync,
  readFileSync,
  writeFileSync,
  renameSync,
  unlinkSync,
  readdirSync,
  statSync,
} from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { DynamicStructuredTool, type StructuredTool } from "@langchain/core/tools";
import { MultiServerMCPClient } from "@langchain/mcp-adapters";
import { FLOWAGENTS_DIRNAME, CACHE_SUBDIR } from "../paths.js";
import { logger } from "../logger.js";
import { sanitizeMcpToolName } from "./sanitize-mcp-name.js";

const log = logger.child("mcp-tool-cache");

/** 缓存格式版本（结构变更时 +1，旧文件因 version 不符自动失效）。 */
const CACHE_VERSION = 2;
/** 过期时长：远端 MCP 未提供工具版本通知时，最多复用一天。 */
const CACHE_TTL_MS = 24 * 60 * 60 * 1000;
/** cache 根下的 MCP 工具缓存子目录。 */
const MCP_TOOLS_SUBDIR = "mcp-tools";

/** 文件名安全化：非 [a-zA-Z0-9_\-.] 替成 _（防 sessionId 含路径字符）。 */
function safeFilename(s: string): string {
  return s.replace(/[^a-zA-Z0-9_\-.]/g, "_");
}

/** 单条缓存工具项。 */
export interface CachedToolEntry {
  /** 最终工具名（带 server 前缀、已 sanitize）——bindTools 用，须与 getTools 产物一致。 */
  name: string;
  /** 原始（未前缀）MCP 工具名——callTool 用。 */
  rawName: string;
  /** 所属 server（sanitize 后的连接键）。 */
  server: string;
  description: string;
  /** JSON Schema 对象（DynamicStructuredTool 可直接消费）。 */
  schema: unknown;
  /** MCP adapter 透传的工具元数据（如 annotations）。 */
  metadata?: Record<string, unknown>;
}

/** 磁盘缓存文件结构。 */
export interface CachedToolSchemas {
  version: number;
  sessionId: string;
  fingerprint: string;
  createdAt: string;
  tools: CachedToolEntry[];
}

/** 缓存读取结果；reason 直接写入 perf 日志，供验证命中/失效原因。 */
export interface ToolSchemaCacheReadResult {
  cached: CachedToolSchemas | null;
  reason: "hit" | "not_found" | "ttl_expired" | "version_changed" | "session_changed" | "config_changed" | "invalid_payload" | "read_error";
}

/** 缓存根目录（~/.flowagents/cache/mcp-tools）。 */
function cacheDir(): string {
  return join(homedir(), FLOWAGENTS_DIRNAME, CACHE_SUBDIR, MCP_TOOLS_SUBDIR);
}

/** 某 session 的缓存文件路径。 */
function cacheFilePath(sessionId: string): string {
  return join(cacheDir(), `${safeFilename(sessionId)}.json`);
}

/**
 * 计算 MCP 配置指纹（顺序无关）：仅纳入影响工具集的字段（command/args/url/transport/type/headers），
 * server 名排序后稳定序列化再 sha256。日志中 phase=new 与 phase=load 的 server 顺序不同但内容一致，
 * 顺序无关保证不因排序差异误失效。
 */
export function computeMcpConfigFingerprint(
  servers: Record<string, {
    command?: string;
    args?: string[];
    url?: string;
    transport?: string;
    type?: string;
    env?: Record<string, string>;
    headers?: Record<string, string>;
  }>
): string {
  const normalized = Object.keys(servers)
    .sort()
    .map((name) => {
      const s = servers[name] ?? {};
      return {
        name,
        command: s.command ?? null,
        args: s.args ?? null,
        url: s.url ?? null,
        transport: s.transport ?? null,
        type: s.type ?? null,
        // env / headers 的值会改变 MCP 的身份或能力集；仅哈希，不写入缓存文件或日志。
        env: normalizeStringRecord(s.env),
        headers: normalizeStringRecord(s.headers),
      };
    });
  return createHash("sha256").update(JSON.stringify(normalized)).digest("hex");
}

function normalizeStringRecord(record?: Record<string, string>): Record<string, string> | null {
  if (!record) return null;
  return Object.fromEntries(
    Object.entries(record).sort(([a], [b]) => a.localeCompare(b))
  );
}

/** 清除某 session 缓存（phase=new 时调；best-effort，文件不存在忽略）。 */
export function clearToolSchemaCache(sessionId: string, reason = "explicit"): void {
  try {
    const p = cacheFilePath(sessionId);
    if (existsSync(p)) {
      unlinkSync(p);
      log.info("MCP 工具缓存已清除", { sessionId, reason });
    }
  } catch (err) {
    log.warn("清除 MCP 工具缓存失败（忽略）", { sessionId, error: String(err) });
  }
}

/** 顺手清理过期缓存文件（读路径调用，异常忽略）。 */
function sweepExpired(skipPath?: string): void {
  try {
    const dir = cacheDir();
    if (!existsSync(dir)) return;
    const now = Date.now();
    for (const file of readdirSync(dir)) {
      if (!file.endsWith(".json")) continue;
      const full = join(dir, file);
      if (full === skipPath) continue;
      try {
        if (now - statSync(full).mtimeMs > CACHE_TTL_MS) {
          unlinkSync(full);
          log.info("MCP 工具缓存已过期清理", { file });
        }
      } catch {
        /* 单文件清理失败忽略 */
      }
    }
  } catch {
    /* 清理整体失败忽略，不影响主流程 */
  }
}

/**
 * 读缓存：命中且（version + fingerprint）一致才返回，否则 null。
 * 任何异常（文件缺失/损坏/解析失败）均返回 null（降级为 MISS，不阻断启动）。
 */
export function readToolSchemaCache(
  sessionId: string,
  fingerprint: string
): ToolSchemaCacheReadResult {
  try {
    const p = cacheFilePath(sessionId);
    sweepExpired(p);
    if (!existsSync(p)) return { cached: null, reason: "not_found" };
    if (Date.now() - statSync(p).mtimeMs > CACHE_TTL_MS) {
      unlinkSync(p);
      log.info("MCP 工具缓存已过期清理", { sessionId });
      return { cached: null, reason: "ttl_expired" };
    }
    const parsed = JSON.parse(readFileSync(p, "utf8")) as CachedToolSchemas;
    if (!parsed || !Array.isArray(parsed.tools)) return { cached: null, reason: "invalid_payload" };
    if (parsed.version !== CACHE_VERSION) return { cached: null, reason: "version_changed" };
    if (parsed.sessionId !== sessionId) return { cached: null, reason: "session_changed" };
    if (parsed.fingerprint !== fingerprint) return { cached: null, reason: "config_changed" };
    return { cached: parsed, reason: "hit" };
  } catch (err) {
    log.warn("读取 MCP 工具缓存失败（视为未命中）", { sessionId, error: String(err) });
    return { cached: null, reason: "read_error" };
  }
}

/** 原子写缓存：临时文件 + rename，best-effort（失败仅 warn）。 */
export function writeToolSchemaCache(
  sessionId: string,
  fingerprint: string,
  tools: CachedToolEntry[]
): void {
  try {
    if (tools.length === 0) return;
    mkdirSync(cacheDir(), { recursive: true });
    const payload: CachedToolSchemas = {
      version: CACHE_VERSION,
      sessionId,
      fingerprint,
      createdAt: new Date().toISOString(),
      tools,
    };
    const target = cacheFilePath(sessionId);
    const tmp = `${target}.${process.pid}.tmp`;
    writeFileSync(tmp, JSON.stringify(payload), "utf8");
    renameSync(tmp, target);
    log.info("MCP 工具缓存已写入", { sessionId, tools: tools.length });
  } catch (err) {
    log.warn("写入 MCP 工具缓存失败（忽略）", { sessionId, error: String(err) });
  }
}

/** schema 是否为可序列化的 JSON Schema 对象（排除 zod 实例：带 _def / parse 的一律跳过）。 */
function isJsonSchemaObject(schema: unknown): boolean {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) return false;
  const s = schema as Record<string, unknown>;
  if ("_def" in s || typeof (s as { parse?: unknown }).parse === "function") return false;
  return true;
}

/**
 * 从已加载的 mcpTools（getTools 产物）抽取可缓存条目。
 * 按原始 server 名前缀（`<server>__`）归属工具、剥离前缀得 rawName；同时保存
 * 模型调用使用的规范化名称，不能再从规范化名称反推 rawName。
 * schema 非 JSON Schema 对象（罕见）的工具跳过缓存，保证回读可重建。
 */
export function extractCacheEntriesFromTools(
  mcpTools: StructuredTool[],
  serverNames: string[]
): CachedToolEntry[] {
  // 长前缀优先，避免 server 名互为前缀时误归属。
  const sorted = [...serverNames].sort((a, b) => b.length - a.length);
  const entries: CachedToolEntry[] = [];
  for (const tool of mcpTools) {
    const server = sorted.find((s) => tool.name.startsWith(`${s}__`));
    if (!server) continue; // 非 MCP 工具或无法归属，跳过
    const schema = (tool as unknown as { schema?: unknown }).schema;
    if (!isJsonSchemaObject(schema)) continue;
    entries.push({
      name: sanitizeMcpToolName(tool.name),
      rawName: tool.name.slice(server.length + 2),
      server,
      description: tool.description ?? "",
      schema,
      metadata: serializableRecord((tool as unknown as { metadata?: unknown }).metadata),
    });
  }
  return entries;
}

/** 仅保留可 JSON 序列化的 metadata；异常或非对象则不缓存。 */
function serializableRecord(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  try {
    return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
  } catch {
    return undefined;
  }
}

/**
 * 懒连接器：工具桩首次被调用时，才按 server 建一个 MultiServerMCPClient 并连接。
 * 每 server 仅建一次（Map 去重）；新建的 client 经 onClientCreated 交给 runtime 统一 destroy。
 */
class LazyMcpConnector {
  private clients = new Map<string, MultiServerMCPClient>();
  private loadedTools = new Map<string, Promise<StructuredTool[]>>();

  constructor(
    private readonly connections: Record<string, unknown>,
    private readonly onClientCreated?: (client: MultiServerMCPClient) => void,
    private readonly onCachedToolInvalid?: (
      server: string,
      rawName: string,
      reason: CachedToolInvalidationReason
    ) => void
  ) {}

  /**
   * 首次调用时才让 mcp-adapters 枚举该 server 并取得其原生工具，再直接复用其 func。
   * 不自行实现 tools/call，因此取消信号、超时、富内容/结构化结果与 adapter 保持一致。
   */
  async callTool(
    entry: CachedToolEntry,
    args: Record<string, unknown>,
    runManager?: unknown,
    config?: unknown
  ): Promise<unknown> {
    const tool = await this.getTool(entry);
    return tool.func(args, runManager as never, config as never);
  }

  private async getTool(entry: CachedToolEntry): Promise<DynamicStructuredTool> {
    const tools = await this.getServerTools(entry.server);
    const expectedName = `${entry.server}__${entry.rawName}`;
    const tool = tools.find((candidate) => candidate.name === expectedName);
    if (!tool || !("func" in tool) || typeof tool.func !== "function") {
      this.onCachedToolInvalid?.(entry.server, entry.rawName, "remote_tool_missing");
      throw new Error(`MCP server "${entry.server}" 未提供缓存的工具 "${entry.rawName}"；请刷新工具缓存后重试`);
    }
    if (!schemasEqual(entry.schema, (tool as { schema?: unknown }).schema)) {
      this.onCachedToolInvalid?.(entry.server, entry.rawName, "schema_changed");
      throw new Error(`MCP server "${entry.server}" 的工具 "${entry.rawName}" 参数 schema 已变化；缓存已清除，请刷新会话后重试`);
    }
    return tool as DynamicStructuredTool;
  }

  private getServerTools(server: string): Promise<StructuredTool[]> {
    const existing = this.loadedTools.get(server);
    if (existing) return existing;
    const loading = this.ensure(server).getTools(server).catch((err) => {
      this.loadedTools.delete(server);
      throw err;
    });
    this.loadedTools.set(server, loading);
    return loading;
  }

  private ensure(server: string): MultiServerMCPClient {
    let client = this.clients.get(server);
    if (client) return client;
    const conn = this.connections[server];
    if (!conn) throw new Error(`MCP server "${server}" 无连接配置（懒连接）`);
    client = new MultiServerMCPClient({
      mcpServers: { [server]: conn },
      onConnectionError: "ignore",
      prefixToolNameWithServerName: true,
    } as never);
    this.clients.set(server, client);
    this.onClientCreated?.(client);
    return client;
  }
}

/** 导致缓存失效的远端工具变更原因。 */
export type CachedToolInvalidationReason = "remote_tool_missing" | "schema_changed";

/**
 * JSON Schema 的对象键顺序无语义，比较前递归排序以避免服务端仅调整字段顺序就误失效。
 * 缓存只接受 JSON Schema 对象；真实工具未提供同类 schema 时保守视为不一致。
 */
function schemasEqual(cached: unknown, current: unknown): boolean {
  if (!isJsonSchemaObject(cached) || !isJsonSchemaObject(current)) return false;
  try {
    return JSON.stringify(sortJsonObject(cached)) === JSON.stringify(sortJsonObject(current));
  } catch {
    return false;
  }
}

function sortJsonObject(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortJsonObject);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, nested]) => [key, sortJsonObject(nested)])
  );
}

/** buildLazyToolsFromCache 结果。 */
export interface LazyToolsResult {
  /** 重建的懒工具桩（可直接进 bindTools / ToolNode）。 */
  tools: StructuredTool[];
  /** server → 缓存中的原始工具名列表（仅供缓存命中日志，不代表 tools/list 已验证）。 */
  toolLists: Record<string, string[]>;
}

/**
 * 由缓存条目重建懒工具桩：schema 直接复用缓存的 JSON Schema；func 走 LazyMcpConnector 懒连接。
 * @param connections toConnections() 产物（mcp-adapters connection），懒连接建 client 用。
 * @param onClientCreated 懒建的 client 回调（runtime 用于 destroy 时统一 close）。
 * @param onCachedToolInvalid 远端工具缺失或 schema 变化时清理缓存，下一次装配会重新枚举。
 */
export function buildLazyToolsFromCache(
  cached: CachedToolSchemas,
  connections: Record<string, unknown>,
  onClientCreated?: (client: MultiServerMCPClient) => void,
  onCachedToolInvalid?: (
    server: string,
    rawName: string,
    reason: CachedToolInvalidationReason
  ) => void
): LazyToolsResult {
  const connector = new LazyMcpConnector(connections, onClientCreated, onCachedToolInvalid);
  const tools: StructuredTool[] = [];
  const toolLists: Record<string, string[]> = {};

  for (const entry of cached.tools) {
    (toolLists[entry.server] ??= []).push(entry.rawName);
    tools.push(
      new DynamicStructuredTool({
        name: entry.name,
        description: entry.description,
        // 缓存的 JSON Schema 直接作为 schema（与 mcp-adapters loadMcpTools 构造方式一致）。
        schema: entry.schema as never,
        responseFormat: "content_and_artifact",
        ...(entry.metadata ? { metadata: entry.metadata } : {}),
        ...defaultToolConfig(connections, entry.server),
        func: async (args: Record<string, unknown>, runManager, config) =>
          connector.callTool(entry, args, runManager, config) as never,
      })
    );
  }

  return { tools, toolLists };
}

/** 缓存仅保存 schema；执行超时始终读取本轮最新连接配置。 */
function defaultToolConfig(
  connections: Record<string, unknown>,
  server: string
): { defaultConfig?: { timeout: number } } {
  const timeout = (connections[server] as { defaultToolTimeout?: unknown } | undefined)
    ?.defaultToolTimeout;
  return typeof timeout === "number" ? { defaultConfig: { timeout } } : {};
}
