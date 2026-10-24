/**
 * 文件工具 —— read_file / write_file / edit_file（限 workspace 内，自带 sandbox 校验）。
 *
 * 跨 provider 可移植的轻量 StructuredTool（不依赖 deepagents FilesystemMiddleware）。
 */

import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { isPathAllowed, toAbsolutePath, type FlowSandboxPolicy } from "../../runtime/fs/sandbox.js";

export function createFsTools(opts: { workspaceRoot: string; policy: FlowSandboxPolicy }) {
  const readFile = tool(
    async ({ path }) => {
      const abs = toAbsolutePath(path, opts.workspaceRoot);
      const guard = isPathAllowed(abs, opts.workspaceRoot, opts.policy, false);
      if (!guard.ok) return `Error: ${guard.reason}`;
      if (!existsSync(abs)) return `Error: file not found: ${path}`;
      return readFileSync(abs, "utf-8").slice(0, 50000);
    },
    {
      name: "read_file",
      description: "读取文件内容（限 workspace 内，最多 50000 字符）。",
      schema: z.object({ path: z.string().describe("workspace 相对或绝对路径") }),
    }
  );

  const writeFile = tool(
    async ({ path, content }) => {
      const abs = toAbsolutePath(path, opts.workspaceRoot);
      const guard = isPathAllowed(abs, opts.workspaceRoot, opts.policy, true);
      if (!guard.ok) return `Error: ${guard.reason}`;
      writeFileSync(abs, content);
      return `wrote ${content.length} chars to ${path}`;
    },
    {
      name: "write_file",
      description: "写入文件（受 sandbox 写权限约束）。",
      schema: z.object({
        path: z.string(),
        content: z.string(),
      }),
    }
  );

  const editFile = tool(
    async ({ path, find, replace }) => {
      const abs = toAbsolutePath(path, opts.workspaceRoot);
      const guard = isPathAllowed(abs, opts.workspaceRoot, opts.policy, true);
      if (!guard.ok) return `Error: ${guard.reason}`;
      if (!existsSync(abs)) return `Error: file not found: ${path}`;
      const orig = readFileSync(abs, "utf-8");
      if (!orig.includes(find)) return `Error: pattern not found in ${path}`;
      writeFileSync(abs, orig.replace(find, replace));
      return `edited ${path}`;
    },
    {
      name: "edit_file",
      description: "查找替换编辑文件（首个匹配）。",
      schema: z.object({
        path: z.string(),
        find: z.string().describe("要替换的原文（精确匹配）"),
        replace: z.string(),
      }),
    }
  );

  return [readFile, writeFile, editFile];
}
