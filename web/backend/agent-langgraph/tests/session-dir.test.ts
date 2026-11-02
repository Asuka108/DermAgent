import { describe, expect, it, afterAll } from "vitest";
import { homedir, tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { HumanMessage } from "@langchain/core/messages";
import { AppConfigSchema } from "../src/runtime/index.js";
import {
  FileCheckpointSaver,
  resolveFlowHome,
  resolveCheckpointDir,
  workspaceHash,
  FLOWAGENTS_HOME,
} from "../src/runtime/services/file-checkpoint-saver.js";

describe("resolveFlowHome（数据根）", () => {
  it("FLOWAGENTS_HOME 常量 = ~/.flowagents", () => {
    expect(FLOWAGENTS_HOME).toBe("~/.flowagents");
  });

  it("default = ~/.flowagents（数据根，不含 workspace hash）", () => {
    const config = AppConfigSchema.parse({});
    expect(resolveFlowHome(config, "/tmp/project-a")).toBe(join(homedir(), ".flowagents"));
  });

  it("relative paths opt out to workspace-local storage", () => {
    const config = AppConfigSchema.parse({ memory: { dir: "./.flow-sessions" } });
    expect(resolveFlowHome(config, "/tmp/project-a")).toBe(
      resolve("/tmp/project-a", "./.flow-sessions")
    );
  });

  it("non-default absolute paths are used as-is", () => {
    const config = AppConfigSchema.parse({ memory: { dir: "/tmp/custom-flow-sessions" } });
    expect(resolveFlowHome(config, "/tmp/project-a")).toBe("/tmp/custom-flow-sessions");
  });
});

describe("resolveCheckpointDir + workspaceHash", () => {
  it("checkpoint dir = home/sessions/<workspace hash>", () => {
    const config = AppConfigSchema.parse({});
    const home = resolveFlowHome(config, "/tmp/project-a");
    const hash = workspaceHash("/tmp/project-a");
    expect(resolveCheckpointDir(config, "/tmp/project-a")).toBe(join(home, "sessions", hash));
    expect(hash).toMatch(/^[a-f0-9]{12}$/);
  });

  it("不同 workspace 的 checkpoint 目录不同（按 hash 隔离）", () => {
    const config = AppConfigSchema.parse({});
    expect(resolveCheckpointDir(config, "/tmp/project-a")).not.toBe(
      resolveCheckpointDir(config, "/tmp/project-b")
    );
  });
});

describe("FileCheckpointSaver 明文落盘 + round-trip", () => {
  const dirs: string[] = [];
  afterAll(() => {
    while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true });
  });

  it("checkpoint 落盘为明文（__u8a_json__），消息内容肉眼可读，且新实例（模拟重启）能还原", async () => {
    const dir = mkdtempSync(join(tmpdir(), "flow-plain-"));
    dirs.push(dir);
    const saver = new FileCheckpointSaver({ dir });
    const tid = "plain-thread";
    const cfg = { configurable: { thread_id: tid } };
    await saver.put(
      cfg,
      {
        v: 4,
        id: "cp1",
        ts: "2026-06-16T00:00:00.000Z",
        channel_values: { messages: [new HumanMessage("hello-明文")] },
        channel_versions: {},
        versions_seen: {},
      } as never,
      { source: "loop", step: 0, parents: {} } as never
    );

    const file = readFileSync(join(dir, `${tid}.json`), "utf8");
    expect(file).toContain("__u8a_json__");
    expect(file).not.toContain("__u8a_b64__");
    expect(file).toContain("hello-明文");

    // round-trip：新实例（模拟进程重启）读同文件 → serde 还原 checkpoint
    const saver2 = new FileCheckpointSaver({ dir });
    const tuple = await saver2.getTuple(cfg);
    const msgs = (
      tuple?.checkpoint as { channel_values?: { messages?: unknown[] } } | undefined
    )?.channel_values?.messages;
    expect(msgs).toHaveLength(1);
    expect((msgs![0] as HumanMessage).content).toBe("hello-明文");
  });
});
