/**
 * 记忆层入口（TS 侧）：端口工厂 + 每轮装配 + 提示词片段。
 *
 * 选型：`DERM_MEMORY_HTTP` 配置时读服务端；否则用本地文件存储（词法召回，明确降级）；
 * `dir: null` 时返回空实现，行为与未接入记忆层一致。
 */

import { HttpMemoryStore } from "./http-store.js";
import { redactIdentity } from "./redact.js";
import { FileMemoryStore } from "./store.js";
import type {
  MemoryBackend,
  MemoryHealth,
  MemoryMeta,
  MemoryPort,
  MemoryProfile,
  MemoryRecall,
  MemoryTurn,
} from "./types.js";

export type {
  MemoryBackend,
  MemoryDegraded,
  MemoryFact,
  MemoryHealth,
  MemoryHit,
  MemoryMeta,
  MemoryPort,
  MemoryProfile,
  MemoryRecall,
  MemoryRole,
  MemoryScope,
  MemoryTurn,
  MemoryTurnInput,
} from "./types.js";
export { FileMemoryStore, type FileMemoryStoreOptions } from "./store.js";
export { HttpMemoryStore, type HttpMemoryStoreOptions } from "./http-store.js";
export { MEMORY_SPEC, MEMORY_SPEC_VERSION } from "./spec.js";

class NullMemoryStore implements MemoryPort {
  readonly backend = "none" as const;

  async getWindow(): Promise<MemoryTurn[]> {
    return [];
  }

  async appendTurn(): Promise<void> {
    /* 记忆层关闭：不写任何内容 */
  }

  async getProfile(): Promise<MemoryProfile> {
    return { facts: [], text: "", degraded: false, detail: "disabled" };
  }

  async recall(): Promise<MemoryRecall> {
    return { hits: [], text: "", degraded: false, detail: "disabled" };
  }

  async health(): Promise<MemoryHealth> {
    return {
      ok: false,
      backend: "none",
      detail: "disabled",
      degraded: { short_term: false, profile: false, episodic: false, worker: false },
    };
  }
}

export const NULL_MEMORY: MemoryPort = new NullMemoryStore();

export interface CreateMemoryOptions {
  /** 服务端基址；缺省读 `DERM_MEMORY_HTTP`。 */
  httpUrl?: string;
  /** 本地存储目录；缺省读 `DERM_MEMORY_DIR` 或 `~/.dermagent/memory`；传 null 关闭。 */
  dir?: string | null;
  fetchImpl?: ConstructorParameters<typeof HttpMemoryStore>[0]["fetchImpl"];
}

export function createMemoryPort(options: CreateMemoryOptions = {}): MemoryPort {
  const httpUrl = options.httpUrl ?? process.env.DERM_MEMORY_HTTP;
  if (httpUrl) return new HttpMemoryStore({ baseUrl: httpUrl, fetchImpl: options.fetchImpl });
  if (options.dir === null) return NULL_MEMORY;
  return new FileMemoryStore({ dir: options.dir ?? process.env.DERM_MEMORY_DIR });
}

/** 每轮装配：任一层失败都不抛出，只把对应 `degraded` 置真。 */
export async function readMemoryForTurn(
  port: MemoryPort | undefined,
  caseId: string | undefined,
  query: string
): Promise<MemoryMeta | null> {
  if (!port || !caseId) return null;
  const started = Date.now();
  try {
    const [profile, recall] = await Promise.all([port.getProfile(caseId), port.recall(caseId, query)]);
    return {
      enabled: true,
      source: port.backend,
      profile_facts: profile.facts.length,
      episodic_hits: recall.hits.length,
      latency_ms: Date.now() - started,
      degraded: {
        short_term: false,
        profile: profile.degraded,
        episodic: recall.degraded,
      },
      profile_text: profile.text,
      episodic_text: recall.text,
    };
  } catch {
    return {
      enabled: true,
      source: port.backend as MemoryBackend,
      profile_facts: 0,
      episodic_hits: 0,
      latency_ms: Date.now() - started,
      degraded: { short_term: false, profile: true, episodic: true },
      profile_text: "",
      episodic_text: "",
    };
  }
}

/** 把档案/情景拼成提示词块；出口再做一次身份替换（防御性）。 */
export function renderMemoryPrompt(meta: MemoryMeta | null | undefined): string {
  if (!meta?.enabled) return "";
  const parts = [meta.profile_text, meta.episodic_text].filter((t) => String(t ?? "").trim());
  if (!parts.length) return "";
  return redactIdentity(
    ["以下为记忆层提供的参考信息，不改变本轮病例事实，医生本轮口述优先：", ...parts].join("\n\n")
  );
}
