/**
 * 记忆层端口（TS 侧）。
 *
 * 三端（服务层 memory 包 / 桌面 memory_local / 本包）遵循同一语义：
 *  - 短期：最近 N 轮窗口（只读，供 intake 参考）；
 *  - 画像：结构化患者事实（临床字段，身份字段不进入提示词）；
 *  - 情景：话题切片（模糊召回，仅供参考，不改变当前病例事实）。
 *
 * 任何方法失败都不得阻断问诊流程：调用方负责降级为 `degraded` 标记。
 */

export type MemoryRole = "user" | "assistant";
export type MemoryScope = "case" | "doctor";
export type MemoryBackend = "file" | "http" | "none";

export interface MemoryTurn {
  seq: number;
  role: MemoryRole;
  content: string;
  at: string;
}

export interface MemoryFact {
  fact_id?: string | number;
  category: string;
  field: string;
  value: string;
  source?: string;
  status?: string;
}

export interface MemoryHit {
  scope: MemoryScope;
  title?: string;
  text: string;
  score: number;
  created_at?: string;
}

export interface MemoryDegraded {
  short_term?: boolean;
  profile?: boolean;
  episodic?: boolean;
  worker?: boolean;
}

export interface MemoryProfile {
  patient_id?: string;
  facts: MemoryFact[];
  /** 已按预算裁剪的提示词片段；空字符串表示没有可注入内容。 */
  text: string;
  degraded: boolean;
  detail?: string;
}

export interface MemoryRecall {
  hits: MemoryHit[];
  text: string;
  degraded: boolean;
  detail?: string;
}

export interface MemoryHealth {
  ok: boolean;
  backend: MemoryBackend;
  detail?: string;
  degraded: MemoryDegraded;
}

export interface MemoryTurnInput {
  user: string;
  assistant: string;
  taskType?: string;
  at?: string;
}

/** 记忆层统一端口（file / http / none 三种实现）。 */
export interface MemoryPort {
  readonly backend: MemoryBackend;
  getWindow(caseId: string, rounds?: number): Promise<MemoryTurn[]>;
  appendTurn(caseId: string, turn: MemoryTurnInput): Promise<void>;
  getProfile(caseId: string): Promise<MemoryProfile>;
  recall(caseId: string, query: string, topK?: number): Promise<MemoryRecall>;
  /** 本地实现可导入病例字段作为画像事实；服务端模式下由服务端负责写入。 */
  importFacts?(caseId: string, facts: MemoryFact[], source?: string): Promise<void>;
  deleteCase?(caseId: string): Promise<void>;
  health(): Promise<MemoryHealth>;
}

/** 本轮记忆装配结果（只读标注，写入 FlowState.memoryMeta）。 */
export interface MemoryMeta {
  enabled: boolean;
  source: MemoryBackend;
  profile_facts: number;
  episodic_hits: number;
  latency_ms: number;
  degraded: MemoryDegraded;
  /** 以下两段只给 LLM 提示词使用，不入日志/审计原文。 */
  profile_text: string;
  episodic_text: string;
}
