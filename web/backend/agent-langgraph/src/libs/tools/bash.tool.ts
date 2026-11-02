/**
 * bash 工具 —— 在沙箱内执行 shell 命令。
 *
 * cwd 锁定 workspace 根；read-only profile 禁用；超时 SIGTERM + 兜底 SIGKILL。
 * Unix 下以 detached 进程组启动，超时时杀整组（避免 find 等子进程导致 Promise 永挂）。
 *
 * 注：bash 无法静态分析命令写哪里，故 path-level deniedWritePaths 不对 bash 生效
 * （仅受 sandbox profile 级别约束）。需要路径级写保护的，用 write_file/edit_file。
 */

import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { spawn, type ChildProcess } from "node:child_process";
import type { FlowSandboxPolicy } from "../../runtime/fs/sandbox.js";
import { validateBashCommand } from "./bash-guard.js";

/** Unix：向进程组发信号；Windows：仅杀直接子进程。 */
function killProcessTree(child: ChildProcess, signal: NodeJS.Signals): void {
  if (!child.pid) return;
  if (process.platform === "win32") {
    child.kill(signal);
    return;
  }
  try {
    process.kill(-child.pid, signal);
  } catch {
    child.kill(signal);
  }
}

export function createBashTool(opts: { workspaceRoot: string; policy: FlowSandboxPolicy }) {
  return tool(
    async ({ command, timeoutMs }) => {
      if (opts.policy.profile === "read-only") {
        return "Error: sandbox profile is read-only; bash execution disabled.";
      }

      const guard = validateBashCommand(command);
      if (guard) return guard;

      // 0/负数视为无效，回退默认
      const timeout = timeoutMs && timeoutMs > 0 ? timeoutMs : 30000;
      const isWin = process.platform === "win32";

      return new Promise<string>((done) => {
        let stdout = "";
        let stderr = "";
        let timedOut = false;
        let settled = false;

        const finish = (message: string) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          clearTimeout(killTimer);
          clearTimeout(forceTimer);
          done(message);
        };

        // Unix：detached 使 bash 成为进程组 leader，超时 kill(-pid) 可终止 find 等子进程
        const child: ChildProcess = isWin
          ? spawn(command, {
              shell: true,
              cwd: opts.workspaceRoot,
              env: { ...process.env },
              stdio: "pipe",
            })
          : spawn("bash", ["-c", command], {
              detached: true,
              cwd: opts.workspaceRoot,
              env: { ...process.env },
              stdio: "pipe",
            });

        const timer = setTimeout(() => {
          timedOut = true;
          killProcessTree(child, "SIGTERM");
        }, timeout);

        // SIGTERM 后 500ms 仍未退出 → SIGKILL 强杀
        const killTimer = setTimeout(() => {
          killProcessTree(child, "SIGKILL");
        }, timeout + 500);

        // 兜底：即使 close 未触发也 resolve，防 Promise 永挂
        const forceTimer = setTimeout(() => {
          const out = (stdout + (stderr ? `\n[stderr]\n${stderr}` : "")).slice(0, 20000);
          finish(`Error: timed out after ${timeout}ms (force resolve)\n${out}`);
        }, timeout + 1000);

        child.stdout?.on("data", (d: Buffer) => {
          stdout += d.toString();
        });
        child.stderr?.on("data", (d: Buffer) => {
          stderr += d.toString();
        });
        child.on("error", (e) => {
          finish(`Error: ${e.message}`);
        });
        child.on("close", (code) => {
          const out = (stdout + (stderr ? `\n[stderr]\n${stderr}` : "")).slice(0, 20000);
          if (timedOut) finish(`Error: timed out after ${timeout}ms\n${out}`);
          else finish(code === 0 ? out || "(no output)" : `Exit ${code}\n${out}`);
        });
      });
    },
    {
      name: "bash",
      description:
        "在沙箱内执行 shell 命令（cwd=workspace 根）。用于构建/运行/git/系统操作。受 sandbox profile 约束（read-only 拒；open 放）。找文件请用 glob/grep 工具，禁止 find / 全盘扫描。Windows 上使用系统默认 shell（cmd.exe）。注：path-level deniedWritePaths 不对 bash 生效，敏感写操作用 write_file/edit_file。",
      schema: z.object({
        command: z.string().describe("要执行的 shell 命令"),
        timeoutMs: z.number().optional().describe("超时毫秒，默认 30000（0/负数视为默认）"),
      }),
    }
  );
}
