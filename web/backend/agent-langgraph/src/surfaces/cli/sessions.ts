/**
 * flow sessions —— 会话生命周期 CLI（列出 / 删除已持久化的 thread）。
 *
 * 静态读取 resolveCheckpointDir(appConfig)（= ~/.flowagents/sessions/<workspace 散列>/，
 * 与 createFileCheckpointer 同口径），不加载 MCP、不需凭证。
 * 「恢复某个会话」走 ACP：同一 sessionId → checkpointer 续跑（见 surfaces/acp/server.ts），
 * 此处仅做 list / delete 管理。
 */

import { existsSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { loadFlowConfig } from "../../runtime/flow-config.js";
import {
  FileCheckpointSaver,
  resolveCheckpointDir,
} from "../../runtime/services/file-checkpoint-saver.js";

export interface SessionsArgs {
  action?: "list" | "delete";
  id?: string;
}

/** 文件名 stem → 真实 thread id（还原 FileCheckpointSaver 的 __hex__ 编码）。 */
function decodeId(stem: string): string {
  return stem.startsWith("__hex__")
    ? Buffer.from(stem.slice("__hex__".length), "hex").toString("utf-8")
    : stem;
}

export async function runSessions(args: SessionsArgs = {}): Promise<void> {
  const { appConfig } = loadFlowConfig();
  const dir = resolveCheckpointDir(appConfig, process.cwd());

  if (args.action === "delete") {
    if (!args.id) {
      process.stderr.write("用法: sessions delete <thread_id>\n");
      process.exitCode = 1;
      return;
    }
    // deleteThread 内部处理 __hex__ 编码 + 删文件 + 清内存。
    await new FileCheckpointSaver({ dir }).deleteThread(args.id);
    process.stdout.write(JSON.stringify({ deleted: args.id, dir }, null, 2) + "\n");
    return;
  }

  // 默认 list
  const sessions: { id: string; mtime: string }[] = [];
  if (existsSync(dir)) {
    for (const f of readdirSync(dir)) {
      if (!f.endsWith(".json")) continue;
      try {
        sessions.push({
          id: decodeId(f.slice(0, -5)),
          mtime: statSync(join(dir, f)).mtime.toISOString(),
        });
      } catch {
        /* skip */
      }
    }
  }
  sessions.sort((a, b) => b.mtime.localeCompare(a.mtime));

  process.stdout.write(
    JSON.stringify({ dir, count: sessions.length, sessions }, null, 2) + "\n"
  );
}
