/**
 * Config path resolution.
 *
 * Filesystem locations for the template package, the user's ~/.flowagents home,
 * and the built-in template config presets. Pure path logic — no config parsing
 * or merging. Extracted from config-loader.ts.
 */
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";
import { logger } from "../logger.js";
import { resolvePackageRoot } from "../package-root.js";
import { FLOWAGENTS_DIRNAME } from "../paths.js";
import {
  BUILTIN_TEMPLATE_CONFIGS,
  DEFAULT_BUILTIN_TEMPLATE_CONFIG,
  type BuiltinTemplateConfigName,
} from "./config-schema.js";

/** Agent 模板包根（含 config/、prompts/）；与 ACP 用户 workspace cwd 无关。 */
export const TEMPLATE_PACKAGE_ROOT = resolvePackageRoot(import.meta.url);

export function readBuiltinTemplateConfigNameFromEnv(): BuiltinTemplateConfigName | undefined {
  const name = process.env.DEEPAGENTS_BUILTIN_CONFIG;
  if (!name) {
    return undefined;
  }
  if (name in BUILTIN_TEMPLATE_CONFIGS) {
    return name as BuiltinTemplateConfigName;
  }
  logger.warn("Unknown DEEPAGENTS_BUILTIN_CONFIG; falling back to default", {
    requested: name,
    available: Object.keys(BUILTIN_TEMPLATE_CONFIGS),
  });
  return undefined;
}

export function resolveBuiltinTemplateConfig(
  name: BuiltinTemplateConfigName = DEFAULT_BUILTIN_TEMPLATE_CONFIG
): { path: string; resourceBase: string } {
  const config = BUILTIN_TEMPLATE_CONFIGS[name];
  return {
    path: resolve(TEMPLATE_PACKAGE_ROOT, config.path),
    resourceBase: resolve(TEMPLATE_PACKAGE_ROOT, config.resourceBase),
  };
}

/** ~/.flowagents —— 全局配置目录（config.json / models.json / mcp.json / skills / plugins / sessions）。 */
export function flowAgentsHome(): string {
  return resolve(process.env.FLOWAGENTS_HOME || join(homedir(), FLOWAGENTS_DIRNAME));
}


export function resolveConfigResourcePath(path: string, baseDir: string): string {
  if (path.startsWith("~/") || isAbsolute(path)) {
    return path;
  }
  return resolve(baseDir, path);
}

export function resolvePath(filePath: string, baseDir = process.cwd()): string {
  // ~/.flowagents — 全局配置根
  if (filePath === `~/${FLOWAGENTS_DIRNAME}`) {
    return flowAgentsHome();
  }
  if (filePath.startsWith(`~/${FLOWAGENTS_DIRNAME}/`)) {
    return resolve(flowAgentsHome(), filePath.slice(`~/${FLOWAGENTS_DIRNAME}/`.length));
  }
  if (filePath.startsWith("~/")) {
    return resolve(homedir(), filePath.slice(2));
  }
  if (isAbsolute(filePath)) {
    return filePath;
  }
  return resolve(baseDir, filePath);
}
