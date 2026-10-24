/**
 * Config Schema
 *
 * Declarative layer for the agent configuration: Zod schemas, their inferred
 * types, the built-in template config presets, and the ACP session config
 * shape. The loading/merging logic lives in `config-loader.ts`; this module is
 * pure declaration (depends only on zod).
 */

import { z } from "zod";
import { getPackageVersion } from "../version.js";
import { FLOWAGENTS_DIRNAME } from "../paths.js";

export const BUILTIN_TEMPLATE_CONFIGS = {
  flowAgent: {
    path: "config/flow-agent.config.json",
    resourceBase: ".",
  },
  /** @deprecated 别名；与 flowAgent 同路径，兼容 DEEPAGENTS_BUILTIN_CONFIG=appAgent */
  appAgent: {
    path: "config/flow-agent.config.json",
    resourceBase: ".",
  },
} as const;

export type BuiltinTemplateConfigName = keyof typeof BUILTIN_TEMPLATE_CONFIGS;

export const DEFAULT_BUILTIN_TEMPLATE_CONFIG: BuiltinTemplateConfigName = "flowAgent";

// ─── Config Schema ──────────────────────────────────────

export const ModelConfigSchema = z
  .object({
    provider: z.enum(["anthropic", "openai"]).default("anthropic"),
    name: z.string().default("claude-sonnet-4-6"),
    baseUrl: z.string().url().optional(),
    apiKeyEnv: z.string().default("ANTHROPIC_API_KEY"),
    authTokenEnv: z.string().default("ANTHROPIC_AUTH_TOKEN"),
    settings: z
      .object({
        temperature: z.number().min(0).max(2).default(0),
        maxTokens: z.number().optional(),
        /** Anthropic extended thinking token 预算；必须严格小于 maxTokens。 */
        thinkingBudgetTokens: z.number().int().min(1024).default(1024),
        /** 短 LLM 调用超时（毫秒）。可被 LLM_TIMEOUT_MS 覆盖。 */
        invokeTimeoutMs: z.number().min(1000).optional(),
        /** 长 LLM 调用超时（毫秒）。可被 LLM_LONG_TIMEOUT_MS 覆盖。 */
        invokeLongTimeoutMs: z.number().min(1000).optional(),
        /** 并行/Send 扇出时 LLM 最大并发。可被 LLM_MAX_CONCURRENT 覆盖。 */
        maxConcurrentInvokes: z.number().min(1).optional(),
        /** 模型是否支持 vision content blocks；开启后保留 image_url 等多模态消息。 */
        supportsVision: z.boolean().optional(),
      })
      .default({}),
  })
  .superRefine((model, ctx) => {
    if (
      model.provider === "anthropic" &&
      !model.name.startsWith("claude-opus-4-7") &&
      model.settings.maxTokens !== undefined &&
      model.settings.maxTokens <= model.settings.thinkingBudgetTokens
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["settings", "maxTokens"],
        message: `Anthropic maxTokens 必须大于 thinkingBudgetTokens (${model.settings.thinkingBudgetTokens})`,
      });
    }
  });

export const MCPConfigSchema = z.object({
  configPath: z.string().default("./config/mcp.default.json"),
  configPaths: z.array(z.string()).default([]),
  servers: z.record(z.unknown()).default({}),
  mergeStrategy: z.enum(["session-wins", "defaults-wins"]).default("session-wins"),
});

export const PermissionsConfigSchema = z.object({
  /**
   * Permission mode controlling HITL (human-in-the-loop) behavior:
   * - "yolo": No approvals needed. Agent writes/edits/executes freely.
   * - "ask":  HITL on write/edit/execute. User must approve each operation.
   * - "plan": Agent presents a plan first, user approves, then executes with HITL.
   */
  mode: z.enum(["yolo", "ask", "plan"]).default("ask"),
  interruptOn: z
    .array(z.string())
    .default(["write_file", "edit_file", "bash", "http_request"]),
  allowedPaths: z.array(z.string()).default(["src/app/", "prompts/", "skills/", "config/"]),
  deniedPaths: z.array(z.string()).default(["src/runtime/", "src/surfaces/"]),
});

export const SandboxConfigSchema = z.object({
  /**
   * Sandbox profile:
   * - "custom": preserve `permissions.deniedPaths` behavior.
   * - "workspace-write": allow normal workspace edits while protecting runtime paths.
   * - "read-only": deny all writes.
   * - "open": no sandbox deny rules; useful for trusted local debugging only.
   */
  profile: z.enum(["custom", "workspace-write", "read-only", "open"]).default("custom"),
  writablePaths: z.array(z.string()).default(["src/app/", "prompts/", "skills/", "config/"]),
  deniedWritePaths: z.array(z.string()).default(["src/runtime/", "src/surfaces/"]),
  environment: z.object({
    allowedEnv: z.array(z.string()).default([
      "API_PROTOCOL",
      "LLM_PROVIDER",
      "OPENAI_MODEL",
      "OPENAI_BASE_URL",
      "ANTHROPIC_MODEL",
      "ANTHROPIC_BASE_URL",
      "MAX_TOKENS",
      "LLM_TIMEOUT_MS",
      "LLM_LONG_TIMEOUT_MS",
      "LLM_MAX_CONCURRENT",
      "LOG_LEVEL",
      "LOG_DIR",
    ]),
    secretEnv: z.array(z.string()).default([
      "OPENAI_API_KEY",
      "ANTHROPIC_API_KEY",
      "ANTHROPIC_AUTH_TOKEN",
    ]),
  }).default({}),
});

export const SkillsConfigSchema = z.object({
  directories: z.array(z.string()).default([
    `~/${FLOWAGENTS_DIRNAME}/skills`,   // 全局用户 skills（跨项目共享）
    `./${FLOWAGENTS_DIRNAME}/skills`,   // 项目级 .flowagents 目录
    "./skills/",                        // 项目直接 skills 目录
  ]),
  progressiveLoading: z.boolean().default(true),
});

/** 可选 flat 子智能体根（`<dir>/<name>/AGENT.md`）；默认用 agentsDirectories 的 `<root>/agents/` */
export const SubagentsConfigSchema = z.object({
  directories: z.array(z.string()).default([]),
});

export const WorkspaceConfigSchema = z.object({
  workingDir: z.string().optional(),
});

export const MemoryConfigSchema = z.object({
  enabled: z.boolean().default(true),
  /**
   * 会话/checkpoint 存储目录。默认 `~/.flowagents` → 由 resolveSessionDir 追加
   * `<workspace 散列>` 子目录（按 workspace 隔离）。设为相对路径（如 `./.flow-sessions`）
   * 可 opt-out 回项目内存储。
   */
  dir: z.string().default(`~/${FLOWAGENTS_DIRNAME}`),
  addCacheControl: z.boolean().default(true),
});

export const HookConfigSchema = z.object({
  event: z.enum([
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "before_model_request",
    "after_model_request",
    "before_run",
    "after_run",
  ]),
  matcher: z.string().optional(),
  command: z.string(),
  timeoutMs: z.number().min(1).default(30_000),
  priority: z.number().default(0),
  scope: z.enum(["user", "project"]).optional(),
});

export const PluginsConfigSchema = z.object({
  directories: z.array(z.string()).default([`~/${FLOWAGENTS_DIRNAME}/plugins`, `./${FLOWAGENTS_DIRNAME}/plugins`]),
  enabled: z.array(z.string()).default([]),
  disabled: z.array(z.string()).default([]),
});

export const FlowSelectionConfigSchema = z.object({
  active: z.string().default("default"),
  defaultInteraction: z.enum(["chat", "pipeline", "approval"]).default("chat"),
  unknownActivePolicy: z.enum(["warn-default", "default"]).default("warn-default"),
});

export const LoggingConfigSchema = z.object({
  level: z.enum(["debug", "info", "warn", "error"]).default("info"),
  structured: z.boolean().default(true),
});

export const CompactionConfigSchema = z.object({
  enabled: z.boolean().default(true),
  /** Context window size in tokens. Default: 200000 (Claude 3.5 Sonnet) */
  contextWindow: z.number().min(1000).default(200_000),
  /** Trigger compaction when context exceeds this ratio of contextWindow. Default: 0.8 */
  triggerThreshold: z.number().min(0.1).max(0.95).default(0.8),
  /** Tokens reserved for summary generation. Default: 16384 */
  reserveTokens: z.number().min(1000).default(16_384),
  /** Approximate recent context tokens to keep uncompressed. Default: 20000 */
  keepRecentTokens: z.number().min(1000).default(20_000),
  /**
   * Model name to use for LLM-based summarization. Same provider / credentials /
   * baseUrl as the agent's model. Defaults to the agent's model name when unset,
   * which is fine for small workloads but expensive for long sessions — set
   * to a cheaper model (e.g. "claude-haiku-4-5" or "gpt-4o-mini") for production.
   */
  summarizerModel: z.string().optional(),
});

export const EvictionConfigSchema = z.object({
  enabled: z.boolean().default(true),
  /** Token threshold before evicting tool output. Default: 20000 */
  tokenLimit: z.number().min(1000).default(20_000),
  /** Characters per token for estimation. Default: 4 */
  charPerToken: z.number().min(1).default(4),
  /** Lines to show from start in preview. Default: 5 */
  headLines: z.number().min(1).default(5),
  /** Lines to show from end in preview. Default: 5 */
  tailLines: z.number().min(1).default(5),
  /** Backend path for evicted files. Default: "/large_tool_results" */
  evictionPath: z.string().default("/large_tool_results"),
});

export const AppConfigSchema = z.object({
  agent: z.object({
    name: z.string().default("flow-agent"),
    description: z.string().default("AI application agent"),
    // 惰性默认值：仅当 config 未提供 version 时才读 package.json，避免每次启动都做一次文件读取。
    version: z.string().default(() => getPackageVersion() ?? "0.0.0"),
    outputStyle: z.string().default("concise"),
    systemPrompt: z.string().optional(),
    systemPromptPath: z.string().default("prompts/flow.base.md"),
    includeWorkspaceInstructions: z.boolean().default(true),
  }).default({}),
  model: ModelConfigSchema.default({}),
  mcp: MCPConfigSchema.default({}),
  permissions: PermissionsConfigSchema.default({}),
  sandbox: SandboxConfigSchema.default({}),
  skills: SkillsConfigSchema.default({}),
  subagents: SubagentsConfigSchema.default({}),
  /**
   * Paths to `.agents` directories that contain skills/ and agents/ subdirectories.
   * Each entry should point to a directory following the `.agents` convention:
   *   <dir>/skills/<skill-name>/SKILL.md
   *   <dir>/agents/<agent-name>/AGENT.md
   *
   * Built-in repo capabilities: `agentsDirectories` 含 `./builtin` → `builtin/skills/`、`builtin/agents/`。
   * Optional flat roots: `config.subagents.directories`（高级，默认空）。
   * Skills from agentsDirectories are merged with `config.skills.directories`.
   *
   * @example ["../shared-agents/.agents", "./my-custom-agents"]
   */
  agentsDirectories: z.array(z.string()).default([`~/${FLOWAGENTS_DIRNAME}`, `./${FLOWAGENTS_DIRNAME}`, "./.agents"]),
  memory: MemoryConfigSchema.default({}),
  hooks: z.array(HookConfigSchema).default([]),
  plugins: PluginsConfigSchema.default({}),
  flow: FlowSelectionConfigSchema.default({}),
  workspace: WorkspaceConfigSchema.default({}),
  logging: LoggingConfigSchema.default({}),
  compaction: CompactionConfigSchema.default({}),
  eviction: EvictionConfigSchema.default({}),
  middleware: z.object({
    stuckLoopDetection: z.object({
      enabled: z.boolean().default(true),
      threshold: z.number().min(2).max(10).default(3),
      mode: z.enum(["warn", "error"]).default("warn"),
    }).default({}),
    periodicReminder: z.object({
      enabled: z.boolean().default(true),
      firstAt: z.number().min(1).default(5),
      every: z.number().min(1).default(10),
    }).default({}),
    costTracking: z.object({
      enabled: z.boolean().default(true),
      warnAtTokens: z.number().min(1000).default(100_000),
    }).default({}),
  }).default({}),
});

export type AppConfig = z.infer<typeof AppConfigSchema>;
export type ModelConfig = z.infer<typeof ModelConfigSchema>;
export type PermissionsConfig = z.infer<typeof PermissionsConfigSchema>;
export type SandboxConfig = z.infer<typeof SandboxConfigSchema>;
export type CompactionConfig = z.infer<typeof CompactionConfigSchema>;
export type EvictionConfig = z.infer<typeof EvictionConfigSchema>;

// ─── ACP Session Config ────────────────────────────────

export interface ACPSessionConfig {
  model?: string;
  systemPrompt?: string;
  cwd?: string;
  mcpServers?: Record<string, unknown>;
}
