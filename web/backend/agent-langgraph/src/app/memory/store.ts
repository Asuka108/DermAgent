/**
 * 文件版记忆适配器（TS 侧本地降级）——`~/.dermagent/memory/<case_id>.json`。
 *
 * 与桌面 `memory_local.py` 同语义：窗口截断、事实白名单/版本化、词法召回。
 * 无向量模型时情景检索为 2-gram 词法（`degraded.episodic=true`），不伪装成语义召回。
 */

import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

import { redactIdentity } from "./redact.js";
import {
  MEMORY_SPEC,
  clip,
  isAllowedFact,
  isMultiValued,
  renderEpisodic,
  renderProfile,
} from "./spec.js";
import type {
  MemoryFact,
  MemoryHealth,
  MemoryHit,
  MemoryPort,
  MemoryProfile,
  MemoryRecall,
  MemoryScope,
  MemoryTurn,
  MemoryTurnInput,
} from "./types.js";

/** 本地词法召回阈值：与 Python 本地实现同口径（低于向量阈值，因分数量纲不同）。 */
const LEXICAL_THRESHOLD = 0.25;
const MAX_FACTS = 200;
const MAX_EPISODIC = 200;

interface StoredEpisodic {
  id: string;
  scope: MemoryScope;
  title?: string;
  text: string;
  created_at: string;
  turn_seq: number;
}

interface CaseFile {
  version: 1;
  case_id: string;
  turns: MemoryTurn[];
  facts: MemoryFact[];
  episodic: StoredEpisodic[];
  updated_at: string;
}

function emptyFile(caseId: string): CaseFile {
  return {
    version: 1,
    case_id: caseId,
    turns: [],
    facts: [],
    episodic: [],
    updated_at: new Date().toISOString(),
  };
}

function normalizeValue(value: string): string {
  return String(value ?? "").replace(/\s+/g, "").toLowerCase().slice(0, 255);
}

function bigrams(text: string): Set<string> {
  // 用 Unicode 字母/数字切分：JS 的 \W 只认 ASCII，中文会被整体剥掉。
  const t = String(text ?? "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
  if (t.length < 2) return t ? new Set([t]) : new Set();
  const out = new Set<string>();
  for (let i = 0; i < t.length - 1; i += 1) out.add(t.slice(i, i + 2));
  return out;
}

function lexicalScore(query: Set<string>, text: Set<string>): number {
  if (query.size === 0 || text.size === 0) return 0;
  let overlap = 0;
  for (const gram of query) if (text.has(gram)) overlap += 1;
  const dice = (2 * overlap) / (query.size + text.size);
  return 0.5 * dice + 0.5 * (overlap / query.size);
}

function hashChunk(parts: string[]): string {
  return createHash("sha1").update(parts.join("|")).digest("hex").slice(0, 32);
}

export interface FileMemoryStoreOptions {
  /** 存储目录；缺省 `DERM_MEMORY_DIR` 或 `~/.dermagent/memory`。 */
  dir?: string;
}

export class FileMemoryStore implements MemoryPort {
  readonly backend = "file" as const;
  readonly dir: string;
  private readonly chains = new Map<string, Promise<unknown>>();

  constructor(options: FileMemoryStoreOptions = {}) {
    this.dir = options.dir || process.env.DERM_MEMORY_DIR || join(homedir(), ".dermagent", "memory");
  }

  private pathOf(caseId: string): string {
    const safe = String(caseId || "").replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 64) || "unknown";
    return join(this.dir, `${safe}.json`);
  }

  /** 同一病例文件的读改写串行化，避免并发 append 覆盖。 */
  private locked<T>(caseId: string, fn: () => Promise<T>): Promise<T> {
    const key = this.pathOf(caseId);
    const prev = this.chains.get(key) ?? Promise.resolve();
    const next = prev.then(fn, fn);
    this.chains.set(
      key,
      next.catch(() => undefined)
    );
    return next;
  }

  private async readFile(caseId: string): Promise<CaseFile> {
    try {
      const raw = await readFile(this.pathOf(caseId), "utf-8");
      const parsed = JSON.parse(raw) as Partial<CaseFile>;
      return {
        ...emptyFile(caseId),
        ...parsed,
        turns: Array.isArray(parsed.turns) ? parsed.turns : [],
        facts: Array.isArray(parsed.facts) ? parsed.facts : [],
        episodic: Array.isArray(parsed.episodic) ? parsed.episodic : [],
      };
    } catch {
      return emptyFile(caseId);
    }
  }

  private async writeFile(file: CaseFile): Promise<void> {
    await mkdir(this.dir, { recursive: true });
    const target = this.pathOf(file.case_id);
    const tmp = `${target}.${process.pid}.${randomUUID().slice(0, 8)}.tmp`;
    file.updated_at = new Date().toISOString();
    await writeFile(tmp, JSON.stringify(file), "utf-8");
    await rename(tmp, target);
  }

  async getWindow(caseId: string, rounds = MEMORY_SPEC.short_term.rounds): Promise<MemoryTurn[]> {
    const file = await this.readFile(caseId);
    return file.turns.slice(-Math.max(0, rounds) * 2);
  }

  async appendTurn(caseId: string, turn: MemoryTurnInput): Promise<void> {
    if (!caseId) return;
    await this.locked(caseId, async () => {
      const file = await this.readFile(caseId);
      const at = turn.at || new Date().toISOString();
      const base = file.turns.length ? Math.max(...file.turns.map((t) => t.seq)) : 0;
      // 落盘前替换身份字段：文件本身可作为提示词/切片来源，姓名/手机/证件不得留存。
      const userContent = redactIdentity(String(turn.user ?? ""));
      const assistantContent = redactIdentity(String(turn.assistant ?? ""));
      const userTurn: MemoryTurn = { seq: base + 1, role: "user", content: userContent, at };
      const assistantTurn: MemoryTurn = {
        seq: base + 2,
        role: "assistant",
        content: assistantContent,
        at,
      };
      file.turns.push(userTurn, assistantTurn);
      file.turns = file.turns.slice(-MEMORY_SPEC.short_term.rounds * 2);

      // 每轮问答成对切片（病例级）；身份字段在落盘前替换，避免经提示词外发。
      const text = redactIdentity(`医生：${userContent}\n助手：${assistantContent}`);
      if (text.trim()) {
        file.episodic.push({
          id: hashChunk([caseId, String(assistantTurn.seq), text]),
          scope: "case",
          title: clip(userContent, 40),
          text: clip(text, MEMORY_SPEC.episodic.chunk_max_chars),
          created_at: at,
          turn_seq: assistantTurn.seq,
        });
        file.episodic = file.episodic.slice(-MAX_EPISODIC);
      }
      await this.writeFile(file);
    });
  }

  async getProfile(caseId: string): Promise<MemoryProfile> {
    const file = await this.readFile(caseId);
    const facts = file.facts.filter((f) => (f.status ?? "active") === "active");
    return { facts, text: renderProfile(facts), degraded: false };
  }

  async importFacts(caseId: string, facts: MemoryFact[], source = "case_import"): Promise<void> {
    if (!caseId || !facts.length) return;
    await this.locked(caseId, async () => {
      const file = await this.readFile(caseId);
      let nextId = file.facts.reduce((max, f) => Math.max(max, Number(f.fact_id) || 0), 0) + 1;
      for (const fact of facts) {
        const category = String(fact.category ?? "");
        const field = String(fact.field ?? "");
        const value = String(fact.value ?? "").trim();
        if (!isAllowedFact(category, field, value)) continue;
        const norm = normalizeValue(value);
        const same = file.facts.filter(
          (f) => f.category === category && f.field === field && (f.status ?? "active") === "active"
        );
        if (same.some((f) => normalizeValue(f.value) === norm)) continue;
        if (!isMultiValued(category)) {
          for (const old of same) old.status = "superseded";
        }
        file.facts.push({
          // 事实表会被裁剪，用 max+1 而不是 length+1，避免裁剪后 fact_id 重复
          fact_id: nextId++,
          category,
          field,
          value: clip(value, MEMORY_SPEC.profile.max_value_chars),
          source: fact.source || source,
          status: "active",
        });
      }
      file.facts = file.facts.slice(-MAX_FACTS);
      await this.writeFile(file);
    });
  }

  async recall(caseId: string, query: string, topK?: number): Promise<MemoryRecall> {
    const file = await this.readFile(caseId);
    if (!file.episodic.length || !String(query ?? "").trim()) {
      return { hits: [], text: "", degraded: true, detail: "lexical" };
    }
    const q = bigrams(query);
    const assistantSeqs = file.turns.filter((t) => t.role === "assistant").map((t) => t.seq);
    const latest = assistantSeqs.length ? Math.max(...assistantSeqs) : 0;
    const excludeBelow = latest - (MEMORY_SPEC.episodic.exclude_recent_turns - 1) * 2;
    const scored: MemoryHit[] = [];
    for (const entry of file.episodic) {
      if (entry.turn_seq >= excludeBelow) continue; // 排除最近回合（当轮尚未写入，仍防重）
      const score = lexicalScore(q, bigrams(`${entry.title ?? ""}\n${entry.text}`));
      if (score < LEXICAL_THRESHOLD) continue;
      scored.push({
        scope: entry.scope,
        title: entry.title,
        text: entry.text,
        score: Number(score.toFixed(4)),
        created_at: entry.created_at,
      });
    }
    scored.sort((a, b) => b.score - a.score);
    const limit = Math.max(1, Math.trunc(topK ?? MEMORY_SPEC.episodic.max_injected));
    const hits = scored.slice(0, limit);
    return { hits, text: renderEpisodic(hits), degraded: true, detail: "lexical" };
  }

  async deleteCase(caseId: string): Promise<void> {
    if (!caseId) return;
    await this.locked(caseId, async () => {
      try {
        await unlink(this.pathOf(caseId));
      } catch {
        /* 文件不存在即视为已删除 */
      }
    });
  }

  async health(): Promise<MemoryHealth> {
    try {
      await mkdir(this.dir, { recursive: true });
      return {
        ok: true,
        backend: "file",
        detail: this.dir,
        degraded: { short_term: false, profile: false, episodic: true, worker: true },
      };
    } catch (err) {
      return {
        ok: false,
        backend: "file",
        detail: String(err),
        degraded: { short_term: true, profile: true, episodic: true, worker: true },
      };
    }
  }
}
