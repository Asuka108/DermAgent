/**
 * Flow ACP 会话配置解析与合并（surface 层）。
 *
 * 将 ACP `session/new|load` 的 raw params + 启动 env 转为 `ACPSessionConfig`，
 * 供 `configureSession` → `createFlowRuntime` 使用。
 *
 * nuwaclaw 经 `_meta.systemPrompt = { append: "..." }` 下发平台 system_prompt（见 acpNewSessionParams.ts）。
 */

import type { ACPSessionConfig } from "../../runtime/config/config-schema.js";
import { logger } from "../../runtime/logger.js";
import { sanitizeMcpServerRecord } from "../../runtime/mcp/sanitize-mcp-name.js";

const log = logger.child("acp-session-config");
const flowAcpLog = logger.child("flow-acp");

/** systemPrompt 在 params / env 中的来源（诊断用）。 */
export type SystemPromptFieldSource =
  | "params-top-level"
  | "params-configOptions"
  | "params-meta"
  | "acp-env-json"
  | "env-system-prompt"
  | "merged-env"
  | "none";

const PLAIN_SYSTEM_PROMPT_ENV_KEYS = [
  "SYSTEM_PROMPT",
  "AGENT_SYSTEM_PROMPT",
  "PLATFORM_SYSTEM_PROMPT",
] as const;

/** ACP session/new 的 mcpServers（数组 [{name,...}] 或 record）→ Record<name, cfg>。 */
export function acpMcpToRecord(raw: unknown): Record<string, unknown> | undefined {
  if (!raw) return undefined;

  let rec: Record<string, unknown> | undefined;

  if (Array.isArray(raw)) {
    const built: Record<string, unknown> = {};
    for (const s of raw) {
      if (s && typeof s === "object" && typeof (s as { name?: unknown }).name === "string") {
        const { name, ...rest } = s as { name: string } & Record<string, unknown>;
        built[name] = rest;
      }
    }
    rec = Object.keys(built).length ? built : undefined;
  } else if (typeof raw === "object") {
    rec = raw as Record<string, unknown>;
  }

  if (!rec) return undefined;

  // 规范 server 键名（中文等 → `_`），与 runtime-context 合并逻辑一致。
  const { servers, renames } = sanitizeMcpServerRecord(rec);
  if (Object.keys(renames).length > 0) {
    log.info("ACP mcpServers 名称已规范化", { renames });
  }
  return servers;
}

/** 解析 ACP_SESSION_CONFIG_JSON + 平台常用 SYSTEM_PROMPT 环境变量。 */
export function loadSessionConfigFromEnv(): ACPSessionConfig | undefined {
  let config: ACPSessionConfig | undefined;

  const raw = process.env.ACP_SESSION_CONFIG_JSON;
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as ACPSessionConfig;
      if (!parsed || typeof parsed !== "object") {
        log.warn("ACP_SESSION_CONFIG_JSON 解析结果非对象", { rawChars: raw.length });
      } else {
        config = { ...parsed };
        const coalesced = coalesceSystemPromptValue(parsed.systemPrompt);
        if (coalesced) {
          config.systemPrompt = coalesced;
        }
      }
    } catch (err) {
      log.warn("ACP_SESSION_CONFIG_JSON 解析失败", {
        rawChars: raw.length,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  for (const key of PLAIN_SYSTEM_PROMPT_ENV_KEYS) {
    const fromPlain = coalesceSystemPromptValue(process.env[key]);
    if (fromPlain) {
      config = { ...(config ?? {}), systemPrompt: fromPlain };
      log.info("从环境变量加载 systemPrompt", { envKey: key, chars: fromPlain.length });
      break;
    }
  }

  if (!config || Object.keys(config).length === 0) {
    return undefined;
  }
  return config;
}

export function readSystemPromptString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

/**
 * 将多种 ACP / Claude Code / 平台形态的 systemPrompt 归一为纯文本。
 * nuwaclaw: `_meta.systemPrompt = { append: systemPromptTrimmed }`
 */
export function coalesceSystemPromptValue(value: unknown): string | undefined {
  const direct = readSystemPromptString(value);
  if (direct) return direct;

  if (Array.isArray(value)) {
    const parts: string[] = [];
    for (const item of value) {
      const piece = coalesceSystemPromptValue(item);
      if (piece) parts.push(piece);
    }
    return parts.length ? parts.join("\n\n") : undefined;
  }

  if (!value || typeof value !== "object") {
    return undefined;
  }

  const obj = value as Record<string, unknown>;
  const append = readSystemPromptString(obj.append);
  if (append) return append;

  for (const key of ["content", "text", "value", "prompt", "body", "systemPrompt"]) {
    const nested = readSystemPromptString(obj[key]);
    if (nested) return nested;
  }

  const blockText = readSystemPromptString(obj.text);
  if (blockText && (obj.type === "text" || obj.type === "content")) {
    return blockText;
  }

  return undefined;
}

export function describeSystemPromptValue(value: unknown): Record<string, unknown> {
  if (value === undefined) return { kind: "undefined" };
  if (value === null) return { kind: "null" };
  if (typeof value === "string") {
    return { kind: "string", chars: value.trim().length, empty: !value.trim() };
  }
  if (Array.isArray(value)) {
    return { kind: "array", length: value.length };
  }
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    return {
      kind: "object",
      keys: Object.keys(obj),
      type: obj.type,
      preset: obj.preset,
      appendChars: readSystemPromptString(obj.append)?.length ?? 0,
      coalescedChars: coalesceSystemPromptValue(value)?.length ?? 0,
    };
  }
  return { kind: typeof value };
}

export function readAcpParamsMeta(
  params: Record<string, unknown>
): Record<string, unknown> | undefined {
  const meta = params._meta;
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) {
    return undefined;
  }
  return meta as Record<string, unknown>;
}

/**
 * 从 ACP `session/prompt` params._meta 提取透传的业务 requestId。
 * 宿主约定：`_meta.requestId` / `_meta.request_id`（见 nuwaclaw acpEngine.buildPromptMeta）。
 * 非官方 PromptRequest 字段，缺省时返回 undefined（CLI / 其他宿主仍正常）。
 */
export function extractRequestIdFromAcpParams(
  params: Record<string, unknown> | undefined
): string | undefined {
  if (!params) return undefined;
  const meta = readAcpParamsMeta(params);
  if (!meta) return undefined;
  const id = meta.requestId ?? meta.request_id;
  return typeof id === "string" && id.trim() ? id.trim() : undefined;
}

function extractFromMeta(meta: Record<string, unknown>): string | undefined {
  const direct =
    coalesceSystemPromptValue(meta.systemPrompt) ?? coalesceSystemPromptValue(meta.system_prompt);
  if (direct) return direct;

  const sessionConfig =
    meta.sessionConfig && typeof meta.sessionConfig === "object"
      ? (meta.sessionConfig as Record<string, unknown>)
      : undefined;
  const fromMetaSession = coalesceSystemPromptValue(sessionConfig?.systemPrompt);
  if (fromMetaSession) return fromMetaSession;

  const agentConfig =
    meta.agentConfig && typeof meta.agentConfig === "object"
      ? (meta.agentConfig as Record<string, unknown>)
      : undefined;
  const fromMetaAgent = coalesceSystemPromptValue(agentConfig?.systemPrompt);
  if (fromMetaAgent) return fromMetaAgent;

  const claudeCode =
    meta.claudeCode && typeof meta.claudeCode === "object"
      ? (meta.claudeCode as Record<string, unknown>)
      : undefined;
  const options =
    claudeCode?.options && typeof claudeCode.options === "object"
      ? (claudeCode.options as Record<string, unknown>)
      : undefined;
  return coalesceSystemPromptValue(options?.systemPrompt);
}

export function extractSystemPromptFromParams(params: Record<string, unknown>): {
  systemPrompt?: string;
  source: SystemPromptFieldSource;
} {
  const top =
    coalesceSystemPromptValue(params.systemPrompt) ??
    coalesceSystemPromptValue(params.system_prompt);
  if (top) {
    return { systemPrompt: top, source: "params-top-level" };
  }

  const configOptions =
    params.configOptions && typeof params.configOptions === "object"
      ? (params.configOptions as Record<string, unknown>)
      : undefined;
  const fromOptions =
    coalesceSystemPromptValue(configOptions?.systemPrompt) ??
    coalesceSystemPromptValue(configOptions?.system_prompt);
  if (fromOptions) {
    return { systemPrompt: fromOptions, source: "params-configOptions" };
  }

  const meta = readAcpParamsMeta(params);
  if (meta) {
    const fromMeta = extractFromMeta(meta);
    if (fromMeta) {
      return { systemPrompt: fromMeta, source: "params-meta" };
    }
    if ("systemPrompt" in meta || "system_prompt" in meta) {
      log.warn("_meta 含 systemPrompt 字段但无法解析为文本", {
        systemPromptShape: describeSystemPromptValue(meta.systemPrompt),
        system_promptShape: describeSystemPromptValue(meta.system_prompt),
        hint:
          "平台应将 HTTP body.system_prompt 注入为 session/new 顶层字符串、ACP_SESSION_CONFIG_JSON、env SYSTEM_PROMPT，或 _meta.systemPrompt.append",
      });
    }
  }

  return { source: "none" };
}

/** 从 ACP session/new|load params 提取 cwd / mcpServers / model / systemPrompt。 */
export function sessionConfigFromParams(params: Record<string, unknown>): {
  sessionConfig: ACPSessionConfig;
  workspaceRoot: string;
} {
  const cwd = typeof params.cwd === "string" && params.cwd ? params.cwd : process.cwd();
  const mcpServers = acpMcpToRecord(params.mcpServers);
  const model = typeof params.model === "string" ? params.model : undefined;
  const { systemPrompt } = extractSystemPromptFromParams(params);
  const sessionConfig: ACPSessionConfig = {
    cwd,
    ...(mcpServers ? { mcpServers } : {}),
    ...(model ? { model } : {}),
    ...(systemPrompt ? { systemPrompt } : {}),
  };

  void (async () => {
    try {
      const fs = await import("node:fs");
      const path = await import("node:path");
      const hasProject =
        fs.existsSync(path.join(cwd, "package.json")) &&
        fs.existsSync(path.join(cwd, "config", "mcp.default.json"));
      if (!hasProject) {
        flowAcpLog.warn(
          "ACP session cwd 不是 flow-ts 项目根（缺 package.json / config/mcp.default.json），MCP 默认配置与项目代码将读不到",
          { cwd }
        );
      }
    } catch {
      /* 诊断失败不影响主流程 */
    }
  })();

  return { sessionConfig, workspaceRoot: cwd };
}

export function mergeAcpSessionConfig(
  base: ACPSessionConfig | undefined,
  override: ACPSessionConfig
): ACPSessionConfig {
  return {
    ...base,
    ...override,
    cwd: override.cwd,
    mcpServers: override.mcpServers ?? base?.mcpServers,
    model: override.model ?? base?.model,
    systemPrompt: override.systemPrompt?.trim()
      ? override.systemPrompt
      : base?.systemPrompt?.trim()
        ? base.systemPrompt
        : undefined,
  };
}

/** 合并 env + session/new params（params 优先）。 */
export function resolveAcpSessionConfig(params: Record<string, unknown>): {
  sessionConfig: ACPSessionConfig;
  workspaceRoot: string;
  fromParams: ACPSessionConfig;
  fromEnv?: ACPSessionConfig;
} {
  const { sessionConfig: fromParams, workspaceRoot } = sessionConfigFromParams(params);
  const fromEnv = loadSessionConfigFromEnv();
  const sessionConfig = mergeAcpSessionConfig(fromEnv, fromParams);
  return { sessionConfig, workspaceRoot, fromParams, fromEnv };
}

export function resolveMergedSystemPromptSource(args: {
  params: Record<string, unknown>;
  fromParams: ACPSessionConfig;
  fromEnv?: ACPSessionConfig;
  merged: ACPSessionConfig;
}): SystemPromptFieldSource {
  if (!args.merged.systemPrompt?.trim()) {
    return "none";
  }
  const paramExtract = extractSystemPromptFromParams(args.params);
  if (paramExtract.systemPrompt) {
    return paramExtract.source;
  }
  if (args.fromEnv?.systemPrompt?.trim()) {
    const fromJson = process.env.ACP_SESSION_CONFIG_JSON;
    if (fromJson) {
      try {
        const parsed = JSON.parse(fromJson) as { systemPrompt?: unknown };
        if (coalesceSystemPromptValue(parsed?.systemPrompt)) {
          return "acp-env-json";
        }
      } catch {
        /* ignore */
      }
    }
    for (const key of PLAIN_SYSTEM_PROMPT_ENV_KEYS) {
      if (coalesceSystemPromptValue(process.env[key])) {
        return "env-system-prompt";
      }
    }
    return "acp-env-json";
  }
  return "merged-env";
}
