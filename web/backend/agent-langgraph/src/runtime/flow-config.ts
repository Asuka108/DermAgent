/**
 * Flow 配置加载（通用）
 *
 * 单一配置文件同时承载：
 *  - 标准 AppConfig 字段（agent / model / mcp …）→ 交给底层 loadConfig 解析
 *    （未知的自定义键会被其 Zod schema 自动剥离）
 *  - 任意自定义顶层块 → 这里把完整原始 JSON 一并返回（raw），各 flow 自取所需
 *    （例：RAG 示例读 raw.rag）
 *
 * 无任何写死路径：默认配置相对包根解析（基于 import.meta.url）。
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { loadConfig, type AppConfig, type ACPSessionConfig } from "./index.js";
import { resolvePackageRoot } from "./package-root.js";

/** Agent 模板包根；dev 与 esbuild bundle 下均由 resolvePackageRoot 统一解析。 */
const PACKAGE_ROOT = resolvePackageRoot(import.meta.url);

export interface LoadedFlowConfig {
  appConfig: AppConfig;
  /** 完整原始 JSON —— 各 flow 自取自定义块（如 raw.rag） */
  raw: Record<string, unknown>;
  configPath: string;
}

/** 解析配置文件路径：显式传入优先，否则用包内默认配置。 */
export function resolveConfigPath(
  configPath?: string,
  fallback = "config/flow-agent.config.json"
): string {
  return configPath ?? resolve(PACKAGE_ROOT, fallback);
}

/** 加载配置：返回校验后的 AppConfig + 完整原始 JSON。 */
export function loadFlowConfig(
  opts: {
    configPath?: string;
    workspaceRoot?: string;
    fallback?: string;
    /** ACP 会话级覆盖（model/cwd/mcpServers/…，最高优先级，见 loadConfig 第 6 层）。 */
    sessionConfig?: ACPSessionConfig;
  } = {}
): LoadedFlowConfig {
  const configPath = resolveConfigPath(opts.configPath, opts.fallback);
  const appConfig = loadConfig({
    configPath,
    workspaceRoot: opts.workspaceRoot ?? process.cwd(),
    sessionConfig: opts.sessionConfig,
  });

  let raw: Record<string, unknown> = {};
  try {
    raw = JSON.parse(readFileSync(configPath, "utf-8")) as Record<string, unknown>;
  } catch {
    raw = {};
  }

  return { appConfig, raw, configPath };
}
