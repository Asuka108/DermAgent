/**
 * 服务端记忆适配器：配置 `DERM_MEMORY_HTTP` 时读服务端患者档案与健康状态。
 *
 * 边界：服务端自己负责窗口、抽取与情景召回（本轮提示词由 Python 侧装配）；
 * 本适配器只读临床事实，窗口/情景返回空并显式标注 `server_side`，不伪造召回结果。
 */

import { renderProfile } from "./spec.js";
import type {
  MemoryFact,
  MemoryHealth,
  MemoryPort,
  MemoryProfile,
  MemoryRecall,
  MemoryTurn,
  MemoryTurnInput,
} from "./types.js";

type FetchLike = (
  input: string,
  init?: { method?: string; headers?: Record<string, string>; signal?: AbortSignal }
) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

export interface HttpMemoryStoreOptions {
  baseUrl: string;
  fetchImpl?: FetchLike;
  timeoutMs?: number;
}

interface ServerHealth {
  enabled?: boolean;
  short_term?: { ok?: boolean; detail?: string };
  profile?: { ok?: boolean; detail?: string };
  episodic?: { ok?: boolean; detail?: string };
  worker?: { ok?: boolean; detail?: string };
}

interface CaseMemory {
  patient?: { patient_id?: string } | null;
}

interface PatientFacts {
  facts?: MemoryFact[];
}

export class HttpMemoryStore implements MemoryPort {
  readonly backend = "http" as const;
  readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;
  private readonly timeoutMs: number;

  constructor(options: HttpMemoryStoreOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.fetchImpl = options.fetchImpl ?? ((globalThis as { fetch?: FetchLike }).fetch as FetchLike);
    this.timeoutMs = options.timeoutMs ?? 3000;
  }

  private async getJson<T>(path: string): Promise<T> {
    if (!this.fetchImpl) throw new Error("fetch 不可用");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await this.fetchImpl(`${this.baseUrl}${path}`, {
        headers: { accept: "application/json" },
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return (await res.json()) as T;
    } finally {
      clearTimeout(timer);
    }
  }

  async getWindow(): Promise<MemoryTurn[]> {
    return [];
  }

  async appendTurn(_caseId: string, _turn: MemoryTurnInput): Promise<void> {
    /* 服务端每条消息已写入短期窗口；TS 侧不重复写。 */
  }

  async getProfile(caseId: string): Promise<MemoryProfile> {
    try {
      const snap = await this.getJson<CaseMemory>(`/api/cases/${encodeURIComponent(caseId)}/memory`);
      const patientId = snap.patient?.patient_id;
      if (!patientId) return { facts: [], text: "", degraded: false, detail: "no_patient" };
      const body = await this.getJson<PatientFacts>(
        `/api/patients/${encodeURIComponent(patientId)}/facts`
      );
      const facts = (body.facts ?? []).filter((f) => (f.status ?? "active") === "active");
      return { patient_id: patientId, facts, text: renderProfile(facts), degraded: false };
    } catch (err) {
      return { facts: [], text: "", degraded: true, detail: String(err) };
    }
  }

  async recall(): Promise<MemoryRecall> {
    return { hits: [], text: "", degraded: true, detail: "server_side" };
  }

  async health(): Promise<MemoryHealth> {
    try {
      const h = await this.getJson<ServerHealth>("/api/memory/health");
      const degraded = {
        short_term: h.short_term?.ok === false,
        profile: h.profile?.ok === false,
        episodic: h.episodic?.ok === false,
        worker: h.worker?.ok === false,
      };
      const detail = [h.short_term, h.profile, h.episodic, h.worker]
        .map((s) => s?.detail)
        .filter(Boolean)
        .join("; ");
      return { ok: h.enabled !== false, backend: "http", detail: detail || undefined, degraded };
    } catch (err) {
      return {
        ok: false,
        backend: "http",
        detail: String(err),
        degraded: { short_term: true, profile: true, episodic: true, worker: true },
      };
    }
  }
}
