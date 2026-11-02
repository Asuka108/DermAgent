/**
 * 包根目录解析 —— dev（dist/runtime）与 esbuild 单文件 bundle（dist/bundle.mjs）共用。
 *
 * ACP 会话 cwd 是用户工作区，与 Agent 安装目录分离；config/mcp.default.json、
 * prompts/、skills/ 等资源必须相对包根解析，不能误用 process.cwd()。
 */
import { existsSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * 从模块 URL 定位 nuwax-flow-ts 包根（含 config/flow-agent.config.json）。
 *
 * 路径规则：
 * - dist/bundle.mjs：包根 = dist 的上一级
 * - runtime/config 下模块：上溯 config → runtime → dist|src → 包根
 * - runtime 下其它模块（如 flow-config.js）：上溯 runtime → dist|src → 包根
 * - 兜底：向上查找 config/flow-agent.config.json
 */
export function resolvePackageRoot(moduleUrl: string = import.meta.url): string {
  const filePath = fileURLToPath(moduleUrl);
  const dir = dirname(filePath);
  const fileName = basename(filePath);

  // esbuild 单文件 bundle：dist/bundle.mjs
  if (/^bundle\.(mjs|js|cjs)$/.test(fileName)) {
    return resolve(dir, "..");
  }

  // src/runtime/config 或 dist/runtime/config
  if (basename(dir) === "config" && basename(dirname(dir)) === "runtime") {
    return resolve(dir, "..", "..", "..");
  }

  // src/runtime 或 dist/runtime（flow-config 等）
  if (basename(dir) === "runtime") {
    return resolve(dir, "..", "..");
  }

  // 兜底：向上查找模板标记文件
  let current = dir;
  for (let i = 0; i < 6; i++) {
    if (existsSync(join(current, "config", "flow-agent.config.json"))) {
      return current;
    }
    const parent = dirname(current);
    if (parent === current) break;
    current = parent;
  }

  return resolve(dir, "..", "..");
}
