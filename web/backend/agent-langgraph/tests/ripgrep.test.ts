/**
 * ripgrep 封装单测 —— mock spawnSync，不依赖本机是否安装 rg。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const spawnSyncMock = vi.hoisted(() => vi.fn());

vi.mock("node:child_process", () => ({
  spawnSync: (...args: unknown[]) => spawnSyncMock(...args),
}));

import {
  resetRipgrepCache,
  resolveRipgrepBinary,
  ripgrepGrep,
  ripgrepGlob,
} from "../src/runtime/fs/ripgrep.js";

function mockRgFound(bin = "/usr/bin/rg") {
  spawnSyncMock.mockImplementation((cmd: string) => ({
    status: cmd === "sh" || String(cmd).includes("where") ? 0 : 1,
    stdout: cmd === "sh" || String(cmd).includes("where") ? `${bin}\n` : "",
    stderr: "",
    pid: 0,
    output: [],
    signal: null,
  }));
}

describe("ripgrep", () => {
  beforeEach(() => {
    resetRipgrepCache();
    spawnSyncMock.mockReset();
  });

  afterEach(() => {
    resetRipgrepCache();
  });

  it("未安装 rg 时 resolveRipgrepBinary 返回 null", () => {
    spawnSyncMock.mockReturnValue({
      status: 1,
      stdout: "",
      stderr: "",
      pid: 0,
      output: [],
      signal: null,
    });
    expect(resolveRipgrepBinary()).toBeNull();
  });

  it("已安装 rg 时 grep 返回格式化行", () => {
    mockRgFound();
    spawnSyncMock.mockImplementation((cmd: string, args?: string[]) => {
      if (cmd === "sh" || String(cmd).includes("where")) {
        return { status: 0, stdout: "/usr/bin/rg\n", stderr: "", pid: 0, output: [], signal: null };
      }
      expect(args).toContain("--no-heading");
      return {
        status: 0,
        stdout: "src\\app.ts:10: hello world\n",
        stderr: "",
        pid: 0,
        output: [],
        signal: null,
      };
    });

    resetRipgrepCache();
    const out = ripgrepGrep({ root: "/proj", pattern: "hello" });
    expect(out).toBe("src/app.ts:10: hello world");
  });

  it("rg 正则错误（exit 2）返回 null 以触发 fallback", () => {
    mockRgFound();
    spawnSyncMock.mockImplementation((cmd: string) => {
      if (cmd === "sh" || String(cmd).includes("where")) {
        return { status: 0, stdout: "/usr/bin/rg\n", stderr: "", pid: 0, output: [], signal: null };
      }
      return { status: 2, stdout: "", stderr: "regex parse error", pid: 0, output: [], signal: null };
    });

    resetRipgrepCache();
    expect(ripgrepGrep({ root: "/proj", pattern: "(" })).toBeNull();
  });

  it("glob --files 输出 POSIX 路径", () => {
    mockRgFound();
    spawnSyncMock.mockImplementation((cmd: string, args?: string[]) => {
      if (cmd === "sh" || String(cmd).includes("where")) {
        return { status: 0, stdout: "/usr/bin/rg\n", stderr: "", pid: 0, output: [], signal: null };
      }
      expect(args).toContain("--files");
      return {
        status: 0,
        stdout: "src\\a.ts\nsrc\\b.ts\n",
        stderr: "",
        pid: 0,
        output: [],
        signal: null,
      };
    });

    resetRipgrepCache();
    const out = ripgrepGlob({ root: "/proj", pattern: "**/*.ts" });
    expect(out).toBe("src/a.ts\nsrc/b.ts");
  });
});
