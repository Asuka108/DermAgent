/**
 * 安全回归测试 —— 钉死 P0 新增的外部输入护栏（input-validation guard；不依赖网络 / 凭证）。
 *
 * - sandbox 写路径符号链接逃逸：resolveRealPath 后越界写被拒；读保持词法校验、不误伤合法软链。
 * - http_request SSRF：默认拦截私有 / loopback / 链路本地 / 元数据端点 / 非 http scheme；
 *   allowPrivateNetwork 显式开启才放行。
 */
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { mkdtempSync, symlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { isPathAllowed } from "../src/runtime/fs/sandbox.js";
import { createHttpRequestTool } from "../src/libs/tools/http-request.tool.js";

describe("isPathAllowed 写路径符号链接逃逸", () => {
  let ws: string;
  let outside: string;

  beforeAll(() => {
    ws = mkdtempSync(join(tmpdir(), "flow-ws-"));
    outside = mkdtempSync(join(tmpdir(), "flow-out-"));
    // workspace 内建符号链接 → workspace 外目录（模拟 `ln -s ~/.ssh/x ./leak`）。
    symlinkSync(outside, join(ws, "leak"));
  });
  afterAll(() => {
    rmSync(ws, { recursive: true, force: true });
    rmSync(outside, { recursive: true, force: true });
  });

  const wsWritePolicy = {
    profile: "workspace-write" as const,
    writablePaths: [],
    deniedWritePaths: [],
  };

  it("写经符号链接逃逸到 workspace 外 → 拒", () => {
    const r = isPathAllowed(join(ws, "leak"), ws, wsWritePolicy, true);
    expect(r.ok).toBe(false);
  });

  it("写 workspace 内正常路径 → 放行", () => {
    const r = isPathAllowed(join(ws, "normal.txt"), ws, wsWritePolicy, true);
    expect(r.ok).toBe(true);
  });

  it("读符号链接仍放行（读保持词法，不误伤合法软链如 node_modules）", () => {
    const r = isPathAllowed(join(ws, "leak"), ws, wsWritePolicy, false);
    expect(r.ok).toBe(true);
  });
});

describe("http_request SSRF 防护（默认 allowPrivateNetwork=false）", () => {
  const tool = createHttpRequestTool();
  const invoke = (url: string) =>
    tool.invoke({
      url,
      method: "GET",
      headers: undefined,
      body: undefined,
      timeout: 1000,
    }) as Promise<string>;

  it("loopback 127.0.0.1 → blocked", async () => {
    expect(await invoke("http://127.0.0.1/")).toMatch(/blocked|loopback|private/i);
  });

  it("云元数据端点 169.254.169.254 → blocked", async () => {
    expect(await invoke("http://169.254.169.254/latest/meta-data/")).toMatch(
      /blocked|loopback|private|169/i
    );
  });

  it("内网 10.0.0.1 → blocked", async () => {
    expect(await invoke("http://10.0.0.1/")).toMatch(/blocked|private/i);
  });

  it("localhost → blocked", async () => {
    expect(await invoke("http://localhost/")).toMatch(/blocked|loopback|private|cannot resolve/i);
  });

  it("非 http scheme (file://) → 失败", async () => {
    expect(await invoke("file:///etc/passwd")).toMatch(/unsupported protocol|failed/i);
  });

  it("allowPrivateNetwork:true 时不再 SSRF block（放行到连接层）", async () => {
    const open = createHttpRequestTool({ allowPrivateNetwork: true });
    const r = (await open.invoke({
      url: "http://127.0.0.1:1/",
      method: "GET",
      headers: undefined,
      body: undefined,
      timeout: 1000,
    })) as string;
    // 不再因 SSRF 被拦；连不上 127.0.0.1:1 → fetch 失败（非 blocked）。
    expect(r).not.toMatch(/blocked|loopback|private/i);
  });
});
