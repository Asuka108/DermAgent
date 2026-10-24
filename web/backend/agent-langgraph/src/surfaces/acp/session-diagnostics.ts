/**
 * Flow ACP 会话配置诊断日志（surface 层）。
 */

import { existsSync } from "node:fs";
import { resolve } from "node:path";
import type { ACPSessionConfig } from "../../runtime/config/config-schema.js";
import { logger } from "../../runtime/logger.js";
import { previewText } from "../../runtime/preview-text.js";
import {
  coalesceSystemPromptValue,
  describeSystemPromptValue,
  extractSystemPromptFromParams,
  loadSessionConfigFromEnv,
  readAcpParamsMeta,
  resolveMergedSystemPromptSource,
  type SystemPromptFieldSource,
} from "./session-config.js";

export type { SystemPromptFieldSource };
export {
  loadSessionConfigFromEnv,
  mergeAcpSessionConfig,
} from "./session-config.js";

export type SystemPromptResolveSource =
  | "acp-session"
  | "config-inline"
  | "config-file"
  | "inline-fallback"
  | "none";

const log = logger.child("acp-session-diag");

/** 敏感键名（key/token/secret/password/auth/bearer/credential，词边界匹配）。 */
const SECRET_KEY_RE = /(?:^|[-_])(key|token|secret|password|passwd|auth|bearer|credential)(?:[-_]|$)/i;

/** `key=value` / `--flag=value` 形态的 value 脱敏（仅敏感键名），其余原样。 */
function maskSensitiveAssignee(s: string): string {
  const eq = s.indexOf("=");
  if (eq <= 0) return s;
  return SECRET_KEY_RE.test(s.slice(0, eq)) ? `${s.slice(0, eq)}=***` : s;
}

/** URL query 中敏感参数的值脱敏；非 URL 原样返回。 */
function maskUrlSecret(url: string): string {
  try {
    const u = new URL(url);
    let masked = false;
    for (const k of Array.from(u.searchParams.keys())) {
      if (SECRET_KEY_RE.test(k)) {
        u.searchParams.set(k, "***");
        masked = true;
      }
    }
    return masked ? u.toString() : url;
  } catch {
    return url;
  }
}

/**
 * 单个 MCP server 配置摘要（诊断用）：command/args/url/transport + env 键名列表。
 * env 只记键名（值是密钥重灾区）；args/url 的敏感项脱敏。用于排查平台下发命令在
 * Windows 下 spawn 失败时"实际下发了什么 command/args"（session-diagnostics 此前只记 names）。
 */
export function summarizeMcpServerEntry(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== "object") return { kind: typeof raw };
  const e = raw as Record<string, unknown>;
  const command = typeof e.command === "string" ? e.command : undefined;
  const url = typeof e.url === "string" ? e.url : undefined;
  const args = Array.isArray(e.args)
    ? e.args.filter((a): a is string => typeof a === "string")
    : undefined;
  const env =
    e.env && typeof e.env === "object" && !Array.isArray(e.env)
      ? (e.env as Record<string, unknown>)
      : undefined;
  return {
    ...(command ? { command } : {}),
    ...(args ? { args: args.map(maskSensitiveAssignee) } : {}),
    ...(url ? { url: maskUrlSecret(url) } : {}),
    ...(typeof e.transport === "string" ? { transport: e.transport } : {}),
    ...(typeof e.type === "string" ? { type: e.type } : {}),
    ...(env ? { envKeys: Object.keys(env) } : {}),
  };
}

export function summarizeSessionConfig(
  label: string,
  config: ACPSessionConfig | undefined
): Record<string, unknown> {
  if (!config) {
    return { label, present: false };
  }
  return {
    label,
    present: true,
    cwd: config.cwd,
    model: config.model,
    mcpServerNames: config.mcpServers ? Object.keys(config.mcpServers) : [],
    // 平台下发的 command/args 快照（env 值不记、敏感项脱敏）：排查 stdio spawn 失败时
    // 实际下发了什么命令，不再只有 server 名单。
    mcpServers: config.mcpServers
      ? Object.fromEntries(
          Object.entries(config.mcpServers).map(([name, cfg]) => [
            name,
            summarizeMcpServerEntry(cfg),
          ])
        )
      : {},
    systemPromptChars: config.systemPrompt?.trim().length ?? 0,
    systemPromptPreview: previewText(config.systemPrompt),
  };
}

export function summarizeAcpSessionParams(params: Record<string, unknown>): Record<string, unknown> {
  const configOptions =
    params.configOptions && typeof params.configOptions === "object"
      ? (params.configOptions as Record<string, unknown>)
      : undefined;
  const meta = readAcpParamsMeta(params);
  const topPrompt =
    coalesceSystemPromptValue(params.systemPrompt) ??
    coalesceSystemPromptValue(params.system_prompt);
  const optsPrompt =
    coalesceSystemPromptValue(configOptions?.systemPrompt) ??
    coalesceSystemPromptValue(configOptions?.system_prompt);
  const metaCoalesced = meta
    ? (coalesceSystemPromptValue(meta.systemPrompt) ?? coalesceSystemPromptValue(meta.system_prompt))
    : undefined;
  return {
    paramKeys: Object.keys(params),
    cwd: typeof params.cwd === "string" ? params.cwd : undefined,
    hasCwd: typeof params.cwd === "string" && Boolean(params.cwd),
    model: typeof params.model === "string" ? params.model : undefined,
    hasMcpServers: Boolean(params.mcpServers),
    mcpServersType:
      params.mcpServers == null ? "none" : Array.isArray(params.mcpServers) ? "array" : "object",
    hasTopLevelSystemPrompt: Boolean(topPrompt),
    topLevelSystemPromptChars: topPrompt?.length ?? 0,
    hasTopLevelSystemPromptSnake: Boolean(coalesceSystemPromptValue(params.system_prompt)),
    hasConfigOptionsSystemPrompt: Boolean(optsPrompt),
    configOptionsSystemPromptChars: optsPrompt?.length ?? 0,
    configOptionsKeys: configOptions ? Object.keys(configOptions) : [],
    hasMeta: Boolean(meta),
    metaKeys: meta ? Object.keys(meta) : [],
    hasMetaSystemPrompt: Boolean(metaCoalesced),
    metaSystemPromptChars: metaCoalesced?.length ?? 0,
    metaSystemPromptShape: meta ? describeSystemPromptValue(meta.systemPrompt) : undefined,
    envSystemPromptChars:
      coalesceSystemPromptValue(process.env.SYSTEM_PROMPT)?.length ??
      coalesceSystemPromptValue(process.env.AGENT_SYSTEM_PROMPT)?.length ??
      0,
  };
}

export function systemPromptParamSource(
  params: Record<string, unknown>
): SystemPromptFieldSource {
  return extractSystemPromptFromParams(params).source;
}

export function predictSystemPromptSource(args: {
  sessionConfig?: ACPSessionConfig;
  configInlinePrompt?: string;
  systemPromptPath?: string;
  workspaceRoot: string;
}): { source: SystemPromptResolveSource; resolvedPath?: string; pathExists?: boolean } {
  if (args.sessionConfig?.systemPrompt?.trim()) {
    return { source: "acp-session" };
  }
  if (args.configInlinePrompt?.trim()) {
    return { source: "config-inline" };
  }
  const path = resolvePromptPathForDiag(
    args.systemPromptPath ?? "prompts/flow.base.md",
    args.workspaceRoot
  );
  const pathExists = existsSync(path);
  if (pathExists) {
    return { source: "config-file", resolvedPath: path, pathExists: true };
  }
  return { source: "inline-fallback", resolvedPath: path, pathExists: false };
}

function resolvePromptPathForDiag(path: string, workspaceRoot: string): string {
  if (path.startsWith("~/")) {
    return resolve(process.env.HOME || "", path.slice(2));
  }
  return path.startsWith("/") ? path : resolve(workspaceRoot, path);
}

export function logStartupAcpEnvDiagnostics(): void {
  const raw = process.env.ACP_SESSION_CONFIG_JSON;
  const envConfig = loadSessionConfigFromEnv();
  log.info("ACP 启动环境诊断", {
    hasAcpSessionConfigJson: Boolean(raw),
    acpSessionConfigJsonChars: raw?.length ?? 0,
    ...summarizeSessionConfig("ACP_SESSION_CONFIG_JSON", envConfig),
  });
}

export function logConfigureSessionDiagnostics(args: {
  sessionId: string;
  phase: string;
  params: Record<string, unknown>;
  fromParams: ACPSessionConfig;
  merged: ACPSessionConfig;
  workspaceRoot: string;
}): void {
  const envConfig = loadSessionConfigFromEnv();
  const paramSource = systemPromptParamSource(args.params);
  const mergedSource = resolveMergedSystemPromptSource({
    params: args.params,
    fromParams: args.fromParams,
    fromEnv: envConfig,
    merged: args.merged,
  });
  log.info("ACP configureSession 配置诊断", {
    sessionId: args.sessionId,
    phase: args.phase,
    workspaceRoot: args.workspaceRoot,
    params: summarizeAcpSessionParams(args.params),
    systemPromptParamSource: paramSource,
    env: summarizeSessionConfig("ACP_SESSION_CONFIG_JSON", envConfig),
    fromParams: summarizeSessionConfig("sessionConfigFromParams", args.fromParams),
    merged: summarizeSessionConfig("mergedSessionConfig", args.merged),
    mergedSystemPromptSource: mergedSource,
    envHasSystemPromptButParamsMissing:
      Boolean(envConfig?.systemPrompt?.trim()) && !args.fromParams.systemPrompt?.trim(),
    mergedHasSystemPrompt: Boolean(args.merged.systemPrompt?.trim()),
    predictResolveSource: predictSystemPromptSource({
      sessionConfig: args.merged,
      workspaceRoot: args.workspaceRoot,
    }),
  });
}

export function logRuntimeSystemPromptDiagnostics(args: {
  sessionConfig?: ACPSessionConfig;
  configInlinePrompt?: string;
  systemPromptPath?: string;
  workspaceRoot: string;
  finalSystemPromptChars: number;
  skillsSectionChars?: number;
  subagentsSectionChars?: number;
}): void {
  const prediction = predictSystemPromptSource({
    sessionConfig: args.sessionConfig,
    configInlinePrompt: args.configInlinePrompt,
    systemPromptPath: args.systemPromptPath,
    workspaceRoot: args.workspaceRoot,
  });
  log.info("FlowRuntime systemPrompt 诊断", {
    workspaceRoot: args.workspaceRoot,
    session: summarizeSessionConfig("sessionConfig", args.sessionConfig),
    env: summarizeSessionConfig("ACP_SESSION_CONFIG_JSON", loadSessionConfigFromEnv()),
    resolveSource: prediction.source,
    resolvePath: prediction.resolvedPath,
    resolvePathExists: prediction.pathExists,
    configInlinePromptChars: args.configInlinePrompt?.trim().length ?? 0,
    finalSystemPromptChars: args.finalSystemPromptChars,
    skillsSectionChars: args.skillsSectionChars ?? 0,
    subagentsSectionChars: args.subagentsSectionChars ?? 0,
  });
}
