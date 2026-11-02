/**
 * FileCheckpointSaver —— 文件后端的 checkpoint saver。
 *
 * 继承 MemorySaver（复用全部 serde 序列化 + getTuple/list/put/putWrites 协议逻辑），
 * 仅加一层文件持久化：每次 put/putWrites 后把该 thread 的状态落盘到
 * `<dir>/<thread_id>.json`；首次访问某 thread 时从文件懒加载进内存。
 *
 * 因走标准 checkpointer 协议，graph.compile({ checkpointer }) 后：
 *  - thread_id 隔离（现成）
 *  - getState / setState（现成）
 *  - interrupt / resume **跨进程/重启也恢复**（优于纯 MemorySaver）
 *
 * 不引入 native 依赖（纯 JS JSON），bundle 干净。生产规模可换 sqlite/postgres saver
 * （接口已对齐 BaseCheckpointSaver）。
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync, unlinkSync, renameSync } from "node:fs";
import { resolve, join } from "node:path";
import { homedir } from "node:os";
import { createHash } from "node:crypto";
import { MemorySaver, type BaseCheckpointSaver } from "@langchain/langgraph";
import type { RunnableConfig } from "@langchain/core/runnables";
import { logger, type AppConfig } from "../index.js";

const log = logger.child("file-checkpoint");

export interface FileCheckpointOptions {
  /** 会话存储目录（相对路径按 cwd 解析）。 */
  dir: string;
}

/** 展开 ~/ 前缀（config.memory.dir 可能是 ~/...）。 */
function expandHome(p: string): string {
  if (p.startsWith("~/")) return resolve(homedir(), p.slice(2));
  return p;
}

// 路径/命名常量统一维护在 runtime/paths.ts（FLOWAGENTS_HOME / SESSIONS/ARTIFACTS/LOGS_SUBDIR）。
// 此处 import 自用 + re-export 兼容已有的 `from "./file-checkpoint-saver.js"` 调用（delivery / tests）。
import { FLOWAGENTS_HOME, SESSIONS_SUBDIR, ARTIFACTS_SUBDIR, LOGS_SUBDIR } from "../paths.js";
export { FLOWAGENTS_HOME, SESSIONS_SUBDIR, ARTIFACTS_SUBDIR, LOGS_SUBDIR };

/** workspace 绝对路径 → 12 位 sha256，作为 sessions/artifacts 下的按项目隔离子目录名。 */
export function workspaceHash(workspaceRoot: string): string {
  return createHash("sha256").update(resolve(workspaceRoot)).digest("hex").slice(0, 12);
}

/**
 * 数据根目录：expand config.memory.dir（默认 FLOWAGENTS_HOME=~/.flowagents）。
 * 绝对路径原样用；相对路径按 workspaceRoot 解析（opt-out 回项目内，如 ./.flow-sessions）。
 * 注意：返回的是**根**（不含 workspace hash）——hash 在 sessions/artifacts 子目录下分组。
 */
export function resolveFlowHome(appConfig: AppConfig, workspaceRoot = process.cwd()): string {
  const configured = appConfig.memory?.dir || FLOWAGENTS_HOME;
  const expanded = expandHome(configured);
  return expanded.startsWith("/") ? expanded : resolve(workspaceRoot, expanded);
}

/**
 * checkpoint 目录：~/.flowagents/sessions/<workspace hash>/。与 artifacts 等其他数据隔离；
 * sessions CLI、createFileCheckpointer 共用此口径。
 */
export function resolveCheckpointDir(
  appConfig: AppConfig,
  workspaceRoot = process.cwd()
): string {
  return join(resolveFlowHome(appConfig, workspaceRoot), SESSIONS_SUBDIR, workspaceHash(workspaceRoot));
}

/**
 * 由 AppConfig 造一个文件后端 checkpointer（跨重启恢复 + interrupt/resume 持久化）。
 * StatefulFlow 默认走这个 → durable stateful flow 默认落盘，而非仅内存。
 */
export function createFileCheckpointer(
  appConfig: AppConfig,
  workspaceRoot = process.cwd()
): FileCheckpointSaver {
  return new FileCheckpointSaver({ dir: resolveCheckpointDir(appConfig, workspaceRoot) });
}

/**
 * Durable stateful flow 默认 checkpointer：有 appConfig → FileCheckpointSaver（cross-restart resume）；
 * 无 appConfig（极少数纯单测）→ MemorySaver。单测可注入自己的 checkpointer 覆盖。
 * 各有状态示例的 createXxxFlow 统一经此决定 checkpointer，避免「示例忘了持久化」回归。
 */
export function durableCheckpointer(
  appConfig?: AppConfig,
  injected?: BaseCheckpointSaver
): BaseCheckpointSaver {
  if (injected) return injected;
  return appConfig ? createFileCheckpointer(appConfig) : new MemorySaver();
}

interface ThreadFileData {
  storage?: Record<string, unknown>;
  writes?: Record<string, unknown>;
}

/**
 * MemorySaver 的 storage/writes 里存的是 serde 产出的 **Uint8Array**（二进制 checkpoint/metadata）。
 * 朴素 JSON.stringify 会把 Uint8Array 序列化成 `{"0":..,"1":..}` 普通对象，重载后
 * serde.loadsTyped 期望 ArrayBufferView → 抛 "list argument must be ... ArrayBufferView"。
 *
 * 明文存储（用户诉求）：serde 的 Uint8Array 本就是 UTF-8 JSON 文本，落盘时解码内联成可读对象
 * （__u8a_json__），载入时 JSON.stringify → TextEncoder 还原字节。语义级 round-trip 安全
 * （serde 载入也是 JSON.parse）。非 UTF-8/非 JSON 的真正二进制回退 base64（__u8a_b64__）保真；
 * 旧 base64 文件仍能 revive。这是跨进程/重启续跑真正生效的关键（纯 MemorySaver 不落盘，无此问题）。
 */
const U8_JSON_TAG = "__u8a_json__";
const U8_B64_TAG = "__u8a_b64__";
function replaceBytes(_key: string, value: unknown): unknown {
  if (value instanceof Uint8Array) {
    try {
      // fatal:true → 非法 UTF-8 抛错走 base64 回退，避免静默替换字符损坏字节。
      const text = new TextDecoder("utf-8", { fatal: true }).decode(value);
      return { [U8_JSON_TAG]: JSON.parse(text) };
    } catch {
      return { [U8_B64_TAG]: Buffer.from(value).toString("base64") };
    }
  }
  return value;
}
function reviveBytes(_key: string, value: unknown): unknown {
  if (value && typeof value === "object") {
    const v = value as Record<string, unknown>;
    // 明文：对象 → JSON.stringify → UTF-8 字节（语义级还原；serde 再 JSON.parse）。
    if (v[U8_JSON_TAG] !== undefined) {
      return new TextEncoder().encode(JSON.stringify(v[U8_JSON_TAG]));
    }
    // 兼容旧 base64 文件。
    if (typeof v[U8_B64_TAG] === "string") {
      return new Uint8Array(Buffer.from(v[U8_B64_TAG] as string, "base64"));
    }
  }
  return value;
}

export class FileCheckpointSaver extends MemorySaver {
  private dir: string;
  private loaded = new Set<string>();
  /** 加载失败（文件损坏）的 thread：标记后短路，避免每个 graph step 重读 + 刷 warn。 */
  private corrupted = new Set<string>();

  constructor(opts: FileCheckpointOptions) {
    super();
    this.dir = resolve(opts.dir);
    if (!existsSync(this.dir)) mkdirSync(this.dir, { recursive: true });
    log.info("FileCheckpointSaver ready", { dir: this.dir });
  }

  /** thread_id → 安全文件名（防路径穿越；非安全字符加 __hex__ 前缀编码，保证可逆）。 */
  private threadFile(threadId: string): string {
    const safe = /^[\w\-]+$/.test(threadId)
      ? threadId
      : `__hex__${Buffer.from(threadId).toString("hex")}`;
    return join(this.dir, `${safe}.json`);
  }

  /** 首次访问某 thread 时，从文件懒载入内存（合并进 MemorySaver 的 storage/writes）。 */
  private ensureLoaded(threadId: string): void {
    if (!threadId || this.loaded.has(threadId)) return;
    // 损坏文件：标记后短路，不再每个 graph step 重读 + 刷 warn（否则 N 步 = N 次读盘 + N 条日志）。
    if (this.corrupted.has(threadId)) return;
    const file = this.threadFile(threadId);
    if (!existsSync(file)) {
      this.loaded.add(threadId);
      return;
    }
    try {
      const data = JSON.parse(readFileSync(file, "utf-8"), reviveBytes) as ThreadFileData;
      const storage = this.storage as unknown as Record<string, unknown>;
      const writes = this.writes as unknown as Record<string, unknown>;
      if (data.storage?.[threadId]) storage[threadId] = data.storage[threadId];
      if (data.writes) for (const [k, v] of Object.entries(data.writes)) writes[k] = v;
      this.loaded.add(threadId);
      log.debug("loaded checkpoint from file", { threadId });
    } catch (err) {
      // 标记损坏：该 thread 视为新开题（无法恢复，但不反复读盘刷日志）。
      this.corrupted.add(threadId);
      log.warn("failed to load checkpoint file (will treat as new thread)", { threadId, error: String(err) });
    }
  }

  /** 把该 thread 的 storage + 相关 writes 落盘。 */
  private persist(threadId: string): void {
    if (!threadId) return;
    const storage = this.storage as unknown as Record<string, unknown>;
    const writes = this.writes as unknown as Record<string, unknown>;
    const threadStorage = storage[threadId];
    const writesForThread: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(writes)) {
      try {
        const parsed = JSON.parse(k) as [string, string?, string?];
        if (parsed[0] === threadId) writesForThread[k] = v;
      } catch {
        /* skip malformed keys */
      }
    }
    // 原子写：先写 .tmp 再 rename 覆盖（同目录 rename 在 POSIX 上原子）——
    // 进程在写入中途崩溃（OOM / SIGKILL / 磁盘满）不会把既有 checkpoint 截断成半截 JSON
    // 而致下次 ensureLoaded 永久损坏（跨重启续跑依赖此文件）。
    const final = this.threadFile(threadId);
    const tmp = `${final}.tmp`;
    try {
      writeFileSync(
        tmp,
        JSON.stringify(
          { storage: { [threadId]: threadStorage }, writes: writesForThread },
          replaceBytes,
          2
        )
      );
      renameSync(tmp, final);
    } catch (err) {
      log.warn("failed to persist checkpoint", { threadId, error: String(err) });
      try {
        if (existsSync(tmp)) unlinkSync(tmp);
      } catch {
        /* best-effort cleanup of stale .tmp */
      }
    }
  }

  override async getTuple(config: RunnableConfig) {
    const tid = config.configurable?.thread_id as string | undefined;
    if (tid) this.ensureLoaded(tid);
    return super.getTuple(config);
  }

  override async *list(config: RunnableConfig, options?: unknown) {
    const tid = config.configurable?.thread_id as string | undefined;
    if (tid) {
      this.ensureLoaded(tid);
    } else if (existsSync(this.dir)) {
      // 扫描目录所有 thread 文件，懒载入内存后交给基类 list（经 listThreadIds 解码真实 ID）
      for (const realId of this.listThreadIds()) {
        this.ensureLoaded(realId);
      }
    }
    yield* super.list(config, options as Parameters<MemorySaver["list"]>[1]);
  }

  override async put(
    config: RunnableConfig,
    checkpoint: Parameters<MemorySaver["put"]>[1],
    metadata: Parameters<MemorySaver["put"]>[2]
  ): Promise<RunnableConfig> {
    // 防御：若重启后首次操作是 put（而非 getTuple），先 load 旧 checkpoint，避免 persist 覆盖丢失历史
    const tid = config.configurable?.thread_id as string | undefined;
    if (tid) this.ensureLoaded(tid);
    const ret = await super.put(config, checkpoint, metadata);
    if (tid) {
      this.persist(tid);
      this.loaded.add(tid); // 内存已就绪，后续 ensureLoaded 无需读盘
      this.corrupted.delete(tid); // 已写入健康 checkpoint，不再视为损坏
    }
    return ret;
  }

  override async putWrites(
    config: RunnableConfig,
    writes: Parameters<MemorySaver["putWrites"]>[1],
    taskId: string
  ): Promise<void> {
    const tid = config.configurable?.thread_id as string | undefined;
    if (tid) this.ensureLoaded(tid);
    await super.putWrites(config, writes, taskId);
    if (tid) {
      this.persist(tid);
      this.loaded.add(tid);
      this.corrupted.delete(tid);
    }
  }

  /** 列出所有已持久化的 thread id（供 CLI sessions list）。 */
  listThreadIds(): string[] {
    if (!existsSync(this.dir)) return [];
    return readdirSync(this.dir)
      .filter((f) => f.endsWith(".json"))
      .map((f) => {
        const stem = f.slice(0, -".json".length);
        return stem.startsWith("__hex__")
          ? Buffer.from(stem.slice("__hex__".length), "hex").toString("utf-8")
          : stem;
      });
  }

  /** 删除某 thread 的持久化文件 + 内存。 */
  async deleteThread(threadId: string): Promise<void> {
    try {
      if (typeof super.deleteThread === "function") {
        await super.deleteThread(threadId);
      }
    } catch {
      // super 对非法 threadId 抛错（assertSafeStorageKey）时仍继续清理本地文件/标志，避免残留。
    }
    const file = this.threadFile(threadId);
    if (existsSync(file)) unlinkSync(file);
    const tmp = `${file}.tmp`;
    if (existsSync(tmp)) {
      try {
        unlinkSync(tmp);
      } catch {
        /* best-effort cleanup of stale .tmp */
      }
    }
    this.loaded.delete(threadId);  // 允许同进程内以相同 threadId 重新开题
    this.corrupted.delete(threadId);
  }
}
