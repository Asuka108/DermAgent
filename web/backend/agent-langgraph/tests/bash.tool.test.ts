/**
 * bash 工具回归 —— 危险命令预检 + 超时强杀进程组。
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { validateBashCommand } from "../src/libs/tools/bash-guard.js";
import { createBashTool } from "../src/libs/tools/bash.tool.js";

const openPolicy = {
  profile: "open" as const,
  writablePaths: [] as string[],
  deniedWritePaths: [] as string[],
};

describe("validateBashCommand", () => {
  it("拒绝 find / 全盘扫描", () => {
    expect(validateBashCommand('find / -name "get-config.sh"')).toMatch(/禁止全盘/);
    expect(validateBashCommand("find /* -type f")).toMatch(/禁止全盘/);
  });

  it("允许 workspace 内 find", () => {
    expect(validateBashCommand("find . -maxdepth 3 -name '*.sh'")).toBeNull();
    expect(validateBashCommand("find ./scripts -name foo")).toBeNull();
  });

  it("拒绝无范围的 locate", () => {
    expect(validateBashCommand("locate get-config.sh")).toMatch(/禁止/);
  });

  it("拒绝无 -onlyin 的 mdfind", () => {
    expect(validateBashCommand("mdfind get-config.sh")).toMatch(/禁止/);
  });
});

describe("createBashTool", () => {
  let ws: string;

  beforeAll(() => {
    ws = mkdtempSync(join(tmpdir(), "bash-tool-"));
  });

  afterAll(() => {
    rmSync(ws, { recursive: true, force: true });
  });

  const bash = () => createBashTool({ workspaceRoot: ws, policy: openPolicy });

  it("find / 经工具层被预检拒绝", async () => {
    const result = await bash().invoke({
      command: 'find / -path "*/scripts/get-config.sh" -type f',
    });
    expect(result).toMatch(/禁止全盘/);
  });

  it("echo 正常执行", async () => {
    const result = await bash().invoke({ command: "echo ok" });
    expect(String(result)).toContain("ok");
  });

  it("慢命令在 timeoutMs 内返回超时错误", async () => {
    const slow =
      process.platform === "win32" ? "ping -n 6 127.0.0.1 >nul" : "sleep 5";
    const start = Date.now();
    const result = await bash().invoke({ command: slow, timeoutMs: 500 });
    const elapsed = Date.now() - start;
    expect(String(result)).toMatch(/timed out/i);
    expect(elapsed).toBeLessThan(4000);
  }, 15_000);
});
