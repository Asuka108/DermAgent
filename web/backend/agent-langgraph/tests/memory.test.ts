/**
 * 记忆层（TS 侧）测试：
 *  - spec 副本与 shared/memory_spec.json 字节级同步；
 *  - 文件适配器：窗口截断/持久化、事实白名单与版本化、词法召回与最近回合排除、身份抹除；
 *  - intake 装配：档案/情景标注、失败降级、提示词块；
 *  - HTTP 适配器：健康映射与档案读取（服务端负责窗口/情景）；
 *  - 拓扑保持不变（12 节点 / 15 边）。
 */

import { afterEach, describe, expect, it } from "vitest";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { MemorySaver } from "@langchain/langgraph";

import { FileMemoryStore } from "../src/app/memory/store.js";
import { HttpMemoryStore } from "../src/app/memory/http-store.js";
import {
  NULL_MEMORY,
  createMemoryPort,
  readMemoryForTurn,
  renderMemoryPrompt,
  type MemoryPort,
} from "../src/app/memory/index.js";
import { createIntakeNode } from "../src/app/nodes/derm.js";
import { executeFlow } from "../src/app/graph.js";
import { getFlowTopology } from "../src/app/topology.js";
import type { FlowState } from "../src/app/state.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const SPEC_COPY = resolve(HERE, "../src/app/memory/spec.json");
const SPEC_SRC = resolve(HERE, "../../shared/memory_spec.json");

function sha256(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

const dirs: string[] = [];
function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), "derm-memory-"));
  dirs.push(dir);
  return dir;
}

afterEach(() => {
  while (dirs.length) rmSync(dirs.pop()!, { recursive: true, force: true });
});

describe("记忆规格", () => {
  it("spec.json 与 shared/memory_spec.json 字节一致", () => {
    expect(sha256(SPEC_COPY), "spec.json 已漂移，请重新从 shared 复制").toBe(sha256(SPEC_SRC));
  });
});

describe("文件记忆适配器", () => {
  it("窗口只保留最近 20 轮并跨实例持久化", async () => {
    const dir = tempDir();
    const store = new FileMemoryStore({ dir });
    for (let i = 0; i < 25; i += 1) {
      await store.appendTurn("c1", { user: `第${i}次：湿疹渗出`, assistant: `回答${i}` });
    }
    const window = await store.getWindow("c1");
    expect(window).toHaveLength(40);
    expect(window[0]?.role).toBe("user");
    expect(window[0]?.content).toContain("第5次");

    const reopened = new FileMemoryStore({ dir });
    expect(await reopened.getWindow("c1")).toHaveLength(40);
  });

  it("事实白名单/多值去重/单值版本化，越权类别与字段被丢弃", async () => {
    const dir = tempDir();
    const store = new FileMemoryStore({ dir });
    await store.importFacts("c1", [
      { category: "allergy", field: "allergen", value: "青霉素" },
      { category: "allergy", field: "allergen", value: "青霉 素" }, // 归一化后重复
      { category: "allergy", field: "allergen", value: "磺胺" },
      { category: "personal_history", field: "smoking", value: "已戒烟" },
      { category: "personal_history", field: "smoking", value: "吸烟 10 年" },
      { category: "identity", field: "name", value: "张三" },
      { category: "allergy", field: "bogus", value: "x" },
    ]);
    const profile = await store.getProfile("c1");
    expect(profile.facts.filter((f) => f.category === "allergy")).toHaveLength(2);
    expect(profile.facts.filter((f) => f.category === "personal_history")).toHaveLength(1);
    expect(profile.facts.find((f) => f.field === "smoking")?.value).toBe("吸烟 10 年");
    expect(profile.text).toContain("【患者档案");
    expect(profile.text).not.toContain("张三");

    const raw = JSON.parse(readFileSync(join(dir, "c1.json"), "utf-8")) as {
      facts: Array<{ value: string; status: string }>;
    };
    expect(raw.facts.find((f) => f.value === "已戒烟")?.status).toBe("superseded");
  });

  it("词法召回命中相关话题、排除最近回合并标注降级", async () => {
    const dir = tempDir();
    const store = new FileMemoryStore({ dir });
    await store.appendTurn("c1", { user: "湿疹急性期渗出怎么处理", assistant: "先湿敷，再外用弱效激素" });
    await store.appendTurn("c1", { user: "银屑病生物制剂怎么选", assistant: "评估结核与肝肾功能" });
    await store.appendTurn("c1", { user: "带状疱疹抗病毒时机", assistant: "72 小时内开始" });
    await store.appendTurn("c1", { user: "脚气反复发作", assistant: "足疗程抗真菌" });

    const hit = await store.recall("c1", "上次说的湿疹渗出怎么处理");
    expect(hit.hits.length).toBeGreaterThan(0);
    expect(hit.hits[0]?.text).toContain("湿疹");
    expect(hit.hits[0]?.scope).toBe("case");
    expect(hit.degraded).toBe(true);
    expect(hit.detail).toBe("lexical");

    const recent = await store.recall("c1", "脚气反复发作怎么办");
    expect(recent.hits).toHaveLength(0); // 最近 2 轮不参与召回
  });

  it("情景切片落盘前抹除姓名/手机/身份证", async () => {
    const dir = tempDir();
    const store = new FileMemoryStore({ dir });
    await store.appendTurn("c1", {
      user: "患者姓名：张三，电话 13812345678，身份证 110101199001011234，青霉素过敏",
      assistant: "记录过敏史，避免青霉素。",
    });
    const raw = readFileSync(join(dir, "c1.json"), "utf-8");
    expect(raw).not.toContain("张三");
    expect(raw).not.toContain("13812345678");
    expect(raw).not.toContain("110101199001011234");
    expect(raw).toContain("[患者]");
    expect(raw).toContain("[手机号]");
    expect(raw).toContain("[身份证]");
    expect(raw).toContain("青霉素");
  });

  it("删除病例清除本地文件", async () => {
    const dir = tempDir();
    const store = new FileMemoryStore({ dir });
    await store.appendTurn("c1", { user: "a", assistant: "b" });
    await store.deleteCase("c1");
    expect(await store.getWindow("c1")).toHaveLength(0);
    const health = await store.health();
    expect(health.ok).toBe(true);
    expect(health.degraded.episodic).toBe(true);
    expect(health.degraded.worker).toBe(true);
  });
});

function fakePort(overrides: Partial<MemoryPort> = {}): MemoryPort {
  const base: MemoryPort = {
    backend: "file",
    getWindow: async () => [],
    appendTurn: async () => undefined,
    getProfile: async () => ({
      facts: [{ category: "allergy", field: "allergen", value: "青霉素" }],
      text: "【患者档案（跨就诊慢病信息，仅供参考，当前病例以本轮资料为准）】\n- 过敏史·过敏原：青霉素",
      degraded: false,
    }),
    recall: async () => ({
      hits: [{ scope: "case", text: "医生：湿疹怎么处理\n助手：先湿敷", score: 0.3 }],
      text: "【相关历史话题（模糊召回，仅供参考，不改变当前病例事实）】\n- [病例级] 湿疹",
      degraded: false,
    }),
    health: async () => ({
      ok: true,
      backend: "file",
      degraded: { short_term: false, profile: false, episodic: true, worker: true },
    }),
  };
  return { ...base, ...overrides };
}

describe("intake 记忆装配", () => {
  it("读取档案与情景并写入 memoryMeta/steps", async () => {
    const intake = createIntakeNode({ memory: fakePort(), caseId: "c1" });
    const out = await intake({ input: "请整理现病史", messages: [] } as unknown as FlowState);
    expect(out.memoryMeta).toMatchObject({
      enabled: true,
      source: "file",
      profile_facts: 1,
      episodic_hits: 1,
      degraded: { profile: false, episodic: false },
    });
    expect(out.steps).toContain("memory:file facts=1 hits=1");

    const prompt = renderMemoryPrompt(out.memoryMeta);
    expect(prompt).toContain("患者档案");
    expect(prompt).toContain("相关历史话题");
    expect(prompt).toContain("不改变本轮病例事实");
  });

  it("记忆读取失败只降级、不影响 intake", async () => {
    const intake = createIntakeNode({
      memory: fakePort({
        getProfile: async () => {
          throw new Error("mysql down");
        },
      }),
      caseId: "c1",
    });
    const out = await intake({ input: "请整理现病史", messages: [] } as unknown as FlowState);
    expect(out.intentMeta?.intent).toBe("history");
    expect(out.memoryMeta?.degraded).toMatchObject({ profile: true, episodic: true });
    expect(out.steps?.some((s) => s.endsWith(":degraded"))).toBe(true);
    expect(renderMemoryPrompt(out.memoryMeta)).toBe("");
  });

  it("不配置记忆时行为不变", async () => {
    const intake = createIntakeNode();
    const out = await intake({ input: "请整理现病史", messages: [] } as unknown as FlowState);
    expect(out.memoryMeta).toBeNull();
    expect(out.steps?.some((s) => s.startsWith("memory:"))).toBe(false);
    expect(renderMemoryPrompt(null)).toBe("");
  });

  it("readMemoryForTurn 无病例句柄时不读存储", async () => {
    let called = 0;
    const meta = await readMemoryForTurn(
      fakePort({
        getProfile: async () => {
          called += 1;
          return { facts: [], text: "", degraded: false };
        },
      }),
      undefined,
      "x"
    );
    expect(meta).toBeNull();
    expect(called).toBe(0);
  });
});

describe("HTTP 记忆适配器", () => {
  function fakeFetch(): (url: string) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }> {
    return async (url: string) => {
      if (url.endsWith("/api/memory/health")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            enabled: true,
            short_term: { ok: true, detail: "redis" },
            profile: { ok: false, detail: "mysql down" },
            episodic: { ok: false, detail: "not_available" },
            worker: { ok: false, detail: "not_available" },
          }),
        };
      }
      if (url.endsWith("/api/cases/c1/memory")) {
        return { ok: true, status: 200, json: async () => ({ patient: { patient_id: "p1" } }) };
      }
      if (url.endsWith("/api/patients/p1/facts")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            facts: [
              {
                category: "allergy",
                field: "allergen",
                value: "青霉素",
                source: "doctor_input",
                status: "active",
              },
            ],
          }),
        };
      }
      throw new Error(`unexpected url ${url}`);
    };
  }

  it("健康状态如实映射降级项", async () => {
    const store = new HttpMemoryStore({ baseUrl: "http://x", fetchImpl: fakeFetch() });
    const health = await store.health();
    expect(health.ok).toBe(true);
    expect(health.degraded).toMatchObject({ short_term: false, profile: true, episodic: true });
    expect(health.detail).toContain("mysql down");
  });

  it("读取患者事实并渲染档案；窗口/情景明确由服务端负责", async () => {
    const store = new HttpMemoryStore({ baseUrl: "http://x", fetchImpl: fakeFetch() });
    const profile = await store.getProfile("c1");
    expect(profile.patient_id).toBe("p1");
    expect(profile.text).toContain("过敏史·过敏原：青霉素");
    const recall = await store.recall("c1", "湿疹");
    expect(recall.hits).toHaveLength(0);
    expect(recall.detail).toBe("server_side");
    expect(await store.getWindow("c1")).toHaveLength(0);
  });
});

describe("工厂与拓扑", () => {
  it("按配置选择本地/HTTP/空实现", () => {
    expect(createMemoryPort({ dir: null })).toBe(NULL_MEMORY);
    expect(createMemoryPort({ dir: tempDir() })).toBeInstanceOf(FileMemoryStore);
    expect(createMemoryPort({ httpUrl: "http://x", fetchImpl: fakeFetchForFactory() })).toBeInstanceOf(
      HttpMemoryStore
    );
  });

  it("executeFlow 传入 memory+caseId 时收口写回一轮", async () => {
    const dir = tempDir();
    const store = new FileMemoryStore({ dir });
    const result = await executeFlow("案例1", {
      checkpointer: new MemorySaver(),
      memory: store,
      caseId: "c-exec",
    });
    expect(result.steps.length).toBeGreaterThan(0);
    expect(await store.getWindow("c-exec")).toHaveLength(2);
  });

  it("拓扑保持 12 节点 / 15 边", async () => {
    const topo = await getFlowTopology();
    expect(topo.nodes).toHaveLength(12);
    expect(topo.edges).toHaveLength(15);
  });
});

function fakeFetchForFactory(): (url: string) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }> {
  return async () => ({ ok: true, status: 200, json: async () => ({}) });
}
