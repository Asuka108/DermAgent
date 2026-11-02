/**
 * Config sources.
 *
 * Load and shape overlays from the inputs that feed the loader — JSON config
 * files, plugin manifests, and environment variables. Extracted from
 * config-loader.ts; the orchestration that combines these lives there.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { logger } from "../logger.js";
import type { AppConfig } from "./config-schema.js";
import { resolvePath, resolveConfigResourcePath } from "./config-paths.js";
import { setNestedValue, isRecord } from "./config-merge.js";

/** Load JSON config from file path */
export function loadJsonFile(filePath: string, baseDir = process.cwd()): Record<string, unknown> | null {
  const resolved = resolvePath(filePath, baseDir);
  if (!existsSync(resolved)) {
    return null;
  }
  try {
    const content = readFileSync(resolved, "utf-8");
    return JSON.parse(content) as Record<string, unknown>;
  } catch (err) {
    logger.warn(`Failed to parse config file: ${resolved}`, {
      error: err instanceof Error ? err.message : String(err),
    });
    return null;
  }
}

export function normalizeConfigResourcePaths(config: Record<string, unknown>, baseDir: string): void {
  const mcp = config.mcp;
  if (isRecord(mcp)) {
    if (typeof mcp.configPath === "string") {
      mcp.configPath = resolveConfigResourcePath(mcp.configPath, baseDir);
    }
    if (Array.isArray(mcp.configPaths)) {
      mcp.configPaths = mcp.configPaths.map((entry: unknown) =>
        typeof entry === "string" ? resolveConfigResourcePath(entry, baseDir) : entry
      );
    }
  }

  const skills = config.skills;
  if (isRecord(skills) && Array.isArray(skills.directories)) {
    skills.directories = skills.directories.map((entry: unknown) =>
      typeof entry === "string" ? resolveConfigResourcePath(entry, baseDir) : entry
    );
  }

  if (Array.isArray(config.agentsDirectories)) {
    config.agentsDirectories = config.agentsDirectories.map((entry: unknown) =>
      typeof entry === "string" ? resolveConfigResourcePath(entry, baseDir) : entry
    );
  }

  const memory = config.memory;
  if (isRecord(memory) && typeof memory.dir === "string") {
    memory.dir = resolveConfigResourcePath(memory.dir, baseDir);
  }

  const agent = config.agent;
  if (isRecord(agent) && typeof agent.systemPromptPath === "string") {
    agent.systemPromptPath = resolveConfigResourcePath(agent.systemPromptPath, baseDir);
  }
}

export function modelsOverlayFromFile(filePath: string): Partial<AppConfig> | null {
  const raw = loadJsonFile(filePath);
  if (!raw) {
    return null;
  }
  if (raw.model && typeof raw.model === "object") {
    return raw as Partial<AppConfig>;
  }
  if (raw.default && typeof raw.default === "object") {
    return { model: raw.default as AppConfig["model"] } as Partial<AppConfig>;
  }
  return { model: raw as AppConfig["model"] } as Partial<AppConfig>;
}

export function mcpOverlayFromFile(filePath: string): Partial<AppConfig> | null {
  if (!existsSync(resolvePath(filePath))) {
    return null;
  }
  return { mcp: { configPaths: [resolvePath(filePath)] } as AppConfig["mcp"] } as Partial<AppConfig>;
}

interface PluginManifest {
  id?: string;
  name?: string;
  enabled?: boolean;
  skillsDirectories?: string[];
  agentsDirectories?: string[];
  hooks?: AppConfig["hooks"];
  mcp?: {
    configPath?: string;
    configPaths?: string[];
    servers?: Record<string, unknown>;
  };
  mcpServers?: Record<string, unknown>;
}

export function loadPluginOverlay(config: AppConfig, workspaceRoot: string): Partial<AppConfig> | null {
  const overlay: Partial<AppConfig> = {};
  const enabled = new Set(config.plugins.enabled);
  const disabled = new Set(config.plugins.disabled);

  for (const pluginDir of config.plugins.directories) {
    const resolvedDir = resolvePath(pluginDir, workspaceRoot);
    for (const manifestPath of discoverPluginManifests(resolvedDir)) {
      const manifest = loadJsonFile(manifestPath) as PluginManifest | null;
      if (!manifest) {
        continue;
      }
      const pluginId = manifest.id || manifest.name || manifestPath;
      if (manifest.enabled === false || disabled.has(pluginId)) {
        continue;
      }
      if (enabled.size > 0 && !enabled.has(pluginId)) {
        continue;
      }

      const baseDir = dirname(manifestPath);
      if (manifest.skillsDirectories?.length) {
        overlay.skills = {
          ...overlay.skills,
          directories: [
            ...(overlay.skills?.directories ?? []),
            ...manifest.skillsDirectories.map((path) => resolvePath(path, baseDir)),
          ],
        } as Partial<AppConfig["skills"]> as AppConfig["skills"];
      }
      if (manifest.agentsDirectories?.length) {
        overlay.agentsDirectories = [
          ...(overlay.agentsDirectories ?? []),
          ...manifest.agentsDirectories.map((path) => resolvePath(path, baseDir)),
        ];
      }
      if (manifest.hooks?.length) {
        overlay.hooks = [...(overlay.hooks ?? []), ...manifest.hooks];
      }

      const configPaths = [
        ...(manifest.mcp?.configPath ? [manifest.mcp.configPath] : []),
        ...(manifest.mcp?.configPaths ?? []),
      ].map((path) => resolvePath(path, baseDir));
      const servers = {
        ...(manifest.mcp?.servers ?? {}),
        ...(manifest.mcpServers ?? {}),
      };
      if (configPaths.length > 0 || Object.keys(servers).length > 0) {
        overlay.mcp = {
          ...overlay.mcp,
          configPaths: [...(overlay.mcp?.configPaths ?? []), ...configPaths],
          servers: { ...(overlay.mcp?.servers ?? {}), ...servers },
        } as AppConfig["mcp"];
      }
    }
  }

  return Object.keys(overlay).length > 0 ? overlay : null;
}

function discoverPluginManifests(pluginRoot: string): string[] {
  if (!existsSync(pluginRoot)) {
    return [];
  }
  const rootManifest = resolve(pluginRoot, "plugin.json");
  if (existsSync(rootManifest)) {
    return [rootManifest];
  }
  return readdirSync(pluginRoot)
    .map((entry) => resolve(pluginRoot, entry))
    .filter((entryPath) => {
      try {
        return statSync(entryPath).isDirectory();
      } catch {
        return false;
      }
    })
    .map((entryPath) => resolve(entryPath, "plugin.json"))
    .filter((manifestPath) => existsSync(manifestPath));
}

// ─── Environment Variable Mapping ───────────────────────

const ENV_MAP: Record<string, string> = {
  ACP_AGENT_NAME: "agent.name",
  ACP_AGENT_DESCRIPTION: "agent.description",
  AGENT_SYSTEM_PROMPT: "agent.systemPrompt",
  AGENT_SYSTEM_PROMPT_PATH: "agent.systemPromptPath",
  DEEPAGENTS_WORKING_DIR: "workspace.workingDir",
  AGENT_WORKING_DIR: "workspace.workingDir",
  DEFAULT_MODEL: "model.name",
  ANTHROPIC_MODEL: "model.name",
  ANTHROPIC_BASE_URL: "model.baseUrl",
  OPENAI_MODEL: "model.name",
  OPENAI_BASE_URL: "model.baseUrl",
  LLM_PROVIDER: "model.provider",
  /** 平台 model_provider.api_protocol 经 Electron agent env 下发，与 LLM_PROVIDER 同义 */
  API_PROTOCOL: "model.provider",
  MAX_TOKENS: "model.settings.maxTokens",
  LLM_TIMEOUT_MS: "model.settings.invokeTimeoutMs",
  LLM_LONG_TIMEOUT_MS: "model.settings.invokeLongTimeoutMs",
  LLM_MAX_CONCURRENT: "model.settings.maxConcurrentInvokes",
  MCP_CONFIG_PATH: "mcp.configPath",
  LOG_LEVEL: "logging.level",
  DEEPAGENTS_PERMISSIONS_MODE: "permissions.mode",
  DEEPAGENTS_SANDBOX_PROFILE: "sandbox.profile",
};

/** Numeric env vars that need parsing */
const NUMERIC_ENV_KEYS = new Set([
  "MAX_TOKENS",
  "LLM_TIMEOUT_MS",
  "LLM_LONG_TIMEOUT_MS",
  "LLM_MAX_CONCURRENT",
]);

/** 协议 env 需归一化为 schema 枚举（平台可能下发 "Anthropic"） */
const PROTOCOL_ENV_KEYS = new Set(["API_PROTOCOL", "LLM_PROVIDER"]);

export type ModelProviderName = AppConfig["model"]["provider"];

/** 归一化 api_protocol / LLM_PROVIDER 为 anthropic | openai；非法值返回 null */
export function normalizeModelProvider(value: string): ModelProviderName | null {
  const normalized = value.trim().toLowerCase();
  if (normalized === "anthropic" || normalized === "openai") {
    return normalized;
  }
  return null;
}

/**
 * 读取显式协议 env（API_PROTOCOL 优先，与平台 model_provider.api_protocol 对齐）。
 * 未设置或非法时返回 null；API_PROTOCOL 非法时仍尝试 LLM_PROVIDER。
 */
export function resolveExplicitModelProviderFromEnv(): ModelProviderName | null {
  const apiProtocolRaw = process.env.API_PROTOCOL?.trim();
  const llmProviderRaw = process.env.LLM_PROVIDER?.trim();
  const explicitFromApi = apiProtocolRaw ? normalizeModelProvider(apiProtocolRaw) : null;
  const explicitFromLlm = llmProviderRaw ? normalizeModelProvider(llmProviderRaw) : null;
  return explicitFromApi ?? explicitFromLlm;
}

/** 按 provider 补齐 apiKeyEnv，确保 resolveApiKey 走正确凭证族 */
export function withModelProvider(
  config: AppConfig,
  provider: ModelProviderName
): AppConfig {
  if (provider === "openai") {
    return {
      ...config,
      model: {
        ...config.model,
        provider,
        apiKeyEnv: "OPENAI_API_KEY",
      },
    };
  }
  return {
    ...config,
    model: {
      ...config.model,
      provider,
      apiKeyEnv: "ANTHROPIC_API_KEY",
      authTokenEnv: "ANTHROPIC_AUTH_TOKEN",
    },
  };
}

/** Build config overlay from environment variables */
export function loadFromEnv(): Partial<AppConfig> {
  const overlay: Record<string, unknown> = {};
  for (const [envKey, configPath] of Object.entries(ENV_MAP)) {
    const value = process.env[envKey];
    if (value !== undefined && value !== "") {
      // Parse numeric env vars
      if (NUMERIC_ENV_KEYS.has(envKey)) {
        const num = Number(value);
        if (!isNaN(num)) {
          setNestedValue(overlay, configPath, num);
        }
      } else if (PROTOCOL_ENV_KEYS.has(envKey)) {
        const provider = normalizeModelProvider(value);
        if (provider) {
          setNestedValue(overlay, configPath, provider);
        } else {
          logger.warn(`忽略非法协议 env ${envKey}=${value}（仅支持 anthropic | openai）`);
        }
      } else {
        setNestedValue(overlay, configPath, value);
      }
    }
  }
  // ACP_DEBUG is a boolean flag that maps to logging.level = "debug"
  const acpDebug = process.env.ACP_DEBUG;
  if (acpDebug === "true" || acpDebug === "1") {
    setNestedValue(overlay, "logging.level", "debug");
  }
  return overlay as Partial<AppConfig>;
}

export type ModelProviderResolveSource =
  | "API_PROTOCOL"
  | "LLM_PROVIDER"
  | "env-infer"
  | "config-file";

/**
 * 解析 model.provider 并写诊断日志。
 *
 * 优先级：API_PROTOCOL > LLM_PROVIDER > 凭证启发式 > config 文件已有值。
 * 显式协议存在时不会被双凭证启发式覆盖（对齐平台 model_provider.api_protocol）。
 */
export function inferModelProviderIfUnset(config: AppConfig): AppConfig {
  const apiProtocolRaw = process.env.API_PROTOCOL?.trim();
  const llmProviderRaw = process.env.LLM_PROVIDER?.trim();
  const explicitFromApi = apiProtocolRaw ? normalizeModelProvider(apiProtocolRaw) : null;
  const explicitFromLlm = llmProviderRaw ? normalizeModelProvider(llmProviderRaw) : null;

  if (apiProtocolRaw && !explicitFromApi) {
    logger.warn(`忽略非法 API_PROTOCOL=${apiProtocolRaw}（仅支持 anthropic | openai）`);
  }
  if (llmProviderRaw && !explicitFromLlm) {
    logger.warn(`忽略非法 LLM_PROVIDER=${llmProviderRaw}（仅支持 anthropic | openai）`);
  }

  const explicit = explicitFromApi ?? explicitFromLlm;
  if (explicit) {
    const source: ModelProviderResolveSource = explicitFromApi ? "API_PROTOCOL" : "LLM_PROVIDER";
    const resolved = withModelProvider(config, explicit);
    logModelProviderResolved(resolved, source);
    return resolved;
  }

  const hasOpenAISignals = !!(
    process.env.OPENAI_API_KEY?.trim() ||
    process.env.OPENAI_BASE_URL?.trim()
  );
  const hasAnthropicSignals = !!(
    process.env.ANTHROPIC_API_KEY?.trim() ||
    process.env.ANTHROPIC_AUTH_TOKEN?.trim() ||
    process.env.ANTHROPIC_BASE_URL?.trim()
  );

  let inferred: ModelProviderName | null = null;

  if (hasOpenAISignals && !hasAnthropicSignals) {
    inferred = "openai";
  } else if (hasAnthropicSignals && !hasOpenAISignals) {
    inferred = "anthropic";
  } else if (hasOpenAISignals && hasAnthropicSignals && process.env.OPENAI_API_KEY?.trim()) {
    // 两套凭证同时存在且未指定协议 env：有 OPENAI_API_KEY 时走 OpenAI 兼容路径
    inferred = "openai";
  }

  if (inferred && inferred !== config.model.provider) {
    const resolved = withModelProvider(config, inferred);
    logModelProviderResolved(resolved, "env-infer");
    return resolved;
  }

  logModelProviderResolved(config, "config-file");
  return config;
}

/** 平台经 Electron agent_server.env 下发的模型相关 env（占位符由宿主替换后传入子进程） */
const PLATFORM_MODEL_ENV_KEYS = [
  "API_PROTOCOL",
  "LLM_PROVIDER",
  "ANTHROPIC_MODEL",
  "ANTHROPIC_BASE_URL",
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_AUTH_TOKEN",
  "OPENAI_MODEL",
  "OPENAI_BASE_URL",
  "OPENAI_API_KEY",
] as const;

/** agent_config.env 模板占位符，未替换时不应进入 flow-ts 子进程 */
const MODEL_PROVIDER_PLACEHOLDER_RE = /\{MODEL_PROVIDER_[A-Z_]+\}/;

/** 凭证类 env 日志脱敏（保留前4后2，与 logger 一致） */
function maskEnvSecretForLog(value: string): string {
  if (!value || value.length <= 8) return value ? "***" : "(unset)";
  return `${value.slice(0, 4)}…${value.slice(-2)} (${value.length} chars)`;
}

/** 检测 env 值是否仍为未替换的平台模板占位符 */
export function hasUnresolvedModelProviderPlaceholder(value: string): boolean {
  return MODEL_PROVIDER_PLACEHOLDER_RE.test(value);
}

/** 收集子进程 process.env 中平台下发的模型相关键值（原始字符串，未脱敏） */
export function collectPlatformModelEnvRaw(): Record<string, string> {
  const received: Record<string, string> = {};
  for (const key of PLATFORM_MODEL_ENV_KEYS) {
    const raw = process.env[key]?.trim();
    if (raw) received[key] = raw;
  }
  return received;
}

/**
 * 诊断平台下发的模型 env：确认 {MODEL_PROVIDER_*} 占位符已被 Electron 替换为真实值。
 * 在 loadConfig layer-5（env 合并）之后调用，便于对照 latest.log / session 日志排查。
 */
export function logPlatformModelEnvDiagnostics(): void {
  const received = collectPlatformModelEnvRaw();
  const presentCount = Object.keys(received).length;
  const unresolvedKeys: string[] = [];

  for (const [key, raw] of Object.entries(received)) {
    if (hasUnresolvedModelProviderPlaceholder(raw)) {
      unresolvedKeys.push(key);
    }
  }

  if (presentCount === 0) {
    logger.info("platformModelEnv 诊断", {
      present: false,
      hint: "未检测到 API_PROTOCOL / ANTHROPIC_* / OPENAI_*（CLI 或无 agent_server.env 时正常）",
    });
    return;
  }

  // 完整真实值（info）：便于核对 Electron resolveAgentEnv 替换结果；密钥仅 debug 打明文
  const receivedForLog: Record<string, string> = { ...received };
  const logLevel = (process.env.LOG_LEVEL || "info").toLowerCase();
  const secretsPlaintext = logLevel === "debug" || process.env.ACP_DEBUG === "1";
  if (!secretsPlaintext) {
    for (const key of ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"] as const) {
      if (receivedForLog[key]) {
        receivedForLog[key] = maskEnvSecretForLog(receivedForLog[key]);
      }
    }
  }
  logger.info("platformModelEnv 收到真实值", {
    present: true,
    keyCount: presentCount,
    secretsPlaintext,
    ...receivedForLog,
  });

  if (unresolvedKeys.length > 0) {
    logger.warn("platformModelEnv 占位符未替换", {
      present: true,
      unresolvedKeys,
      received,
      hint: "Electron resolveAgentEnv 应在 spawn 前把 {MODEL_PROVIDER_*} 替换为 model_provider 真实值",
    });
    return;
  }

  // 摘要行（密钥始终脱敏，避免非 debug 会话日志泄露）
  const snapshot: Record<string, string> = {};
  for (const [key, raw] of Object.entries(received)) {
    if (key === "ANTHROPIC_API_KEY" || key === "ANTHROPIC_AUTH_TOKEN" || key === "OPENAI_API_KEY") {
      snapshot[key] = maskEnvSecretForLog(raw);
      snapshot[`${key}_set`] = "true";
    } else {
      snapshot[key] = raw;
    }
  }
  logger.info("platformModelEnv 已注入（占位符已替换）", {
    present: true,
    keyCount: presentCount,
    ...snapshot,
  });
}

function logModelProviderResolved(config: AppConfig, source: ModelProviderResolveSource): void {
  const provider = config.model.provider;
  const credKey =
    provider === "openai" ? "OPENAI_API_KEY" : config.model.authTokenEnv || "ANTHROPIC_AUTH_TOKEN";
  const apiKeyRaw =
    provider === "openai"
      ? process.env.OPENAI_API_KEY?.trim()
      : process.env[credKey]?.trim() || process.env.ANTHROPIC_API_KEY?.trim();
  const baseUrlRaw =
    provider === "openai"
      ? process.env.OPENAI_BASE_URL?.trim() || config.model.baseUrl
      : process.env.ANTHROPIC_BASE_URL?.trim() || config.model.baseUrl;

  logger.info("resolveModelProvider", {
    source,
    provider,
    model: config.model.name,
    baseUrl: baseUrlRaw || undefined,
    apiKeyEnv: config.model.apiKeyEnv,
    apiKey_set: Boolean(apiKeyRaw && !hasUnresolvedModelProviderPlaceholder(apiKeyRaw)),
    baseUrl_set: Boolean(baseUrlRaw && !hasUnresolvedModelProviderPlaceholder(baseUrlRaw)),
    env_model:
      process.env.ANTHROPIC_MODEL?.trim() ||
      process.env.OPENAI_MODEL?.trim() ||
      undefined,
    // 合并后 config 中的真实字段（env 覆盖 config 文件后的最终值）
    config_model_name: config.model.name,
    config_model_baseUrl: config.model.baseUrl,
  });
}
