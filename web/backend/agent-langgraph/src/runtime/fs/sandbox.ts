/**
 * Flow 工具沙箱策略 —— 从 AppConfig 解析出工具执行前的路径/能力校验规则。
 *
 * flow-ts 走显式图、不用 deepagents FilesystemMiddleware，故工具（bash/fs/search）
 * 在执行前自己用这套策略校验。语义对齐标准 sandbox profile
 * （workspace-write / read-only / open / custom），实现是模板自管的轻量匹配。
 */

import { relative } from "node:path";
import type { AppConfig } from "../index.js";
import {
  toAbsolutePath,
  matchPosixGlob,
  normalizeAbsolutePath,
  resolveRealPath,
} from "./path-utils.js";

export { toAbsolutePath } from "./path-utils.js";

export interface FlowSandboxPolicy {
  profile: "custom" | "workspace-write" | "read-only" | "open";
  writablePaths: string[];
  deniedWritePaths: string[];
}

/** 从 AppConfig 解析出工具沙箱策略。 */
export function getFlowSandboxPolicy(config: AppConfig): FlowSandboxPolicy {
  const sandbox = config.sandbox;
  if (sandbox.profile === "open") {
    return { profile: "open", writablePaths: ["/**"], deniedWritePaths: [] };
  }
  if (sandbox.profile === "read-only") {
    return { profile: "read-only", writablePaths: [], deniedWritePaths: ["/"] };
  }
  if (sandbox.profile === "workspace-write") {
    return {
      profile: "workspace-write",
      writablePaths: sandbox.writablePaths,
      deniedWritePaths: sandbox.deniedWritePaths,
    };
  }
  return {
    profile: "custom",
    writablePaths: config.permissions.allowedPaths,
    deniedWritePaths: config.permissions.deniedPaths,
  };
}

/**
 * 判定一次路径访问是否放行。
 * - read：非 read-only 即放行，但限在 workspace 内（open 除外）
 * - write：read-only 全拒；命中 deniedWritePaths 拒；其余放行（writablePaths 作允许提示）
 */
export function isPathAllowed(
  absPath: string,
  workspaceRoot: string,
  policy: FlowSandboxPolicy,
  write: boolean
): { ok: boolean; reason?: string } {
  // 读：非 read-only 即放行，但限在 workspace 内（open 除外）。读保持词法校验——
  // workspace 内的合法符号链接（node_modules / pnpm 软链等）需可读，realpath 会误伤。
  if (!write) {
    if (policy.profile === "open") return { ok: true };
    const rel = relative(normalizeAbsolutePath(workspaceRoot), normalizeAbsolutePath(absPath));
    if (rel.startsWith("..")) {
      return { ok: false, reason: `path outside workspace: ${absPath}` };
    }
    return { ok: true };
  }

  // 写：read-only 全拒。
  if (policy.profile === "read-only") {
    return { ok: false, reason: "sandbox is read-only" };
  }

  // 写路径先 realpath 解析符号链接，再做边界 / denied 校验——防 `ln -s ~/.ssh/x ./x` 后
  // write_file/edit_file 写穿到 workspace 外（normalizeAbsolutePath 是纯词式，不跟随 symlink）。
  const realAbs = resolveRealPath(absPath);
  const realRoot = resolveRealPath(workspaceRoot);

  if (policy.profile !== "open") {
    const rel = relative(realRoot, realAbs);
    if (rel.startsWith("..")) {
      return { ok: false, reason: `path outside workspace: ${absPath}` };
    }
  }
  for (const denied of policy.deniedWritePaths) {
    const deniedAbs = toAbsolutePath(denied, workspaceRoot);
    if (matchPosixGlob(realAbs, resolveRealPath(deniedAbs))) {
      return { ok: false, reason: `path denied by sandbox: ${denied}` };
    }
  }
  return { ok: true };
}
