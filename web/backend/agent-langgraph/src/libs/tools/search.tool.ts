/**
 * 搜索工具 —— grep（内容搜索）/ glob（文件名匹配）。限 workspace 内，跳过 node_modules/dist/.git。
 *
 * 执行策略：
 * - 系统已安装 ripgrep（rg）时优先走 rg（更快、自带忽略规则）；
 * - 否则回退纯 Node 递归 walk + readFileSync。
 *
 * 沙箱校验：root 解析后用 isPathAllowed 拦截越界（与 fs 工具一致）。
 */

import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { isPathAllowed, toAbsolutePath, type FlowSandboxPolicy } from "../../runtime/fs/sandbox.js";
import { toPosixPath, toWorkspaceRelativePosix } from "../../runtime/fs/path-utils.js";
import { isRipgrepAvailable, ripgrepGlob, ripgrepGrep } from "../../runtime/fs/ripgrep.js";

const SKIP = new Set(["node_modules", ".git", "dist", ".flow-sessions", ".pnpm"]);

function walk(dir: string, base: string, out: string[], max = 5000): void {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return;
  }
  for (const name of entries) {
    if (out.length >= max) return;
    if (SKIP.has(name)) continue;
    const full = join(dir, name);
    let st;
    try {
      st = statSync(full);
    } catch {
      continue;
    }
    if (st.isDirectory()) walk(full, base, out, max);
    else out.push(toWorkspaceRelativePosix(full, base));
  }
}

/** glob 串转正则：** → .*、* → [^/]*、? → .（输入先规范为 POSIX）。 */
function globToRegex(g: string): RegExp {
  const posix = toPosixPath(g);
  const esc = posix
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*\*/g, "::DS::")
    .replace(/\*/g, "[^/]*")
    .replace(/::DS::/g, ".*")
    .replace(/\?/g, ".");
  return new RegExp(esc);
}

function resolveRoot(path: string | undefined, workspaceRoot: string): string {
  if (!path) return workspaceRoot;
  return toAbsolutePath(path, workspaceRoot);
}

/** Node fallback：递归读文件 + 正则匹配 */
function grepWithNode(root: string, pattern: string, glob?: string): string {
  const files: string[] = [];
  walk(root, root, files);
  const re = new RegExp(pattern);
  const globRe = glob ? globToRegex(glob) : null;
  const matches: string[] = [];
  for (const f of files) {
    if (globRe && !globRe.test(f)) continue;
    let content: string;
    try {
      content = readFileSync(join(root, f), "utf-8");
    } catch {
      continue;
    }
    const lines = content.split("\n");
    for (let i = 0; i < lines.length; i++) {
      if (re.test(lines[i]!)) {
        matches.push(`${f}:${i + 1}: ${lines[i]!.trim().slice(0, 200)}`);
        if (matches.length >= 200) break;
      }
    }
    if (matches.length >= 200) break;
  }
  return matches.length ? matches.join("\n") : "(no matches)";
}

/** Node fallback：walk + glob 正则过滤 */
function globWithNode(root: string, pattern: string): string {
  const files: string[] = [];
  walk(root, root, files);
  const re = globToRegex(pattern);
  const matched = files.filter((f) => re.test(f)).slice(0, 200);
  return matched.length ? matched.join("\n") : "(no matches)";
}

const grepBackendHint = isRipgrepAvailable()
  ? "优先使用 ripgrep（rg）；未安装时自动回退 Node 扫描。"
  : "纯 Node 扫描（安装 rg 可加速）。";

export function createSearchTools(opts: { workspaceRoot: string; policy: FlowSandboxPolicy }) {
  const grep = tool(
    async ({ pattern, path, glob }) => {
      const root = resolveRoot(path, opts.workspaceRoot);
      const guard = isPathAllowed(root, opts.workspaceRoot, opts.policy, false);
      if (!guard.ok) return `Error: ${guard.reason}`;

      const viaRg = ripgrepGrep({ root, pattern, glob });
      if (viaRg !== null) return viaRg;

      try {
        return grepWithNode(root, pattern, glob);
      } catch {
        return `Error: invalid regex pattern: ${pattern}`;
      }
    },
    {
      name: "grep",
      description: `正则搜索文件内容（限 workspace 内）。可选 glob 过滤文件名。${grepBackendHint}`,
      schema: z.object({
        pattern: z.string().describe("正则表达式"),
        path: z.string().optional().describe("搜索起始目录，默认 workspace 根"),
        glob: z.string().optional().describe('文件名 glob，如 "**/*.ts"'),
      }),
    }
  );

  const globTool = tool(
    async ({ pattern, path }) => {
      const root = resolveRoot(path, opts.workspaceRoot);
      const guard = isPathAllowed(root, opts.workspaceRoot, opts.policy, false);
      if (!guard.ok) return `Error: ${guard.reason}`;

      const viaRg = ripgrepGlob({ root, pattern });
      if (viaRg !== null) return viaRg;

      return globWithNode(root, pattern);
    },
    {
      name: "glob",
      description: `按 glob 模式列文件路径（如 "**/*.ts"）。${grepBackendHint}`,
      schema: z.object({
        pattern: z.string(),
        path: z.string().optional(),
      }),
    }
  );

  return [grep, globTool];
}
