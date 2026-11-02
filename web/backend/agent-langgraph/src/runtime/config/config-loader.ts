/**
 * Configuration Loader
 *
 * Implements the config priority chain:
 *   ACP/session meta > Environment variables > template config > project .flowagents > user ~/.flowagents > Defaults
 *
 * Pattern borrowed from pydantic-deepagents CLI configuration system.
 *
 * This module is the orchestration entry point only. Path resolution lives in
 * `config-paths.ts`, merge primitives in `config-merge.ts`, and overlay sources
 * (files / plugins / env) in `config-sources.ts`. The declarative schema lives
 * in `config-schema.ts` and the generic merge util in `deep-merge.ts`; both are
 * re-exported here so the original import surface (`./config-loader.js`) is
 * unchanged.
 */

import { join, resolve } from "node:path";
import {
  AppConfigSchema,
  type AppConfig,
  type BuiltinTemplateConfigName,
  type ACPSessionConfig,
} from "./config-schema.js";
import { logger } from "../logger.js";
import { FLOWAGENTS_DIRNAME } from "../paths.js";
import {
  resolveBuiltinTemplateConfig,
  readBuiltinTemplateConfigNameFromEnv,
  flowAgentsHome,
  resolvePath,
} from "./config-paths.js";
import { mergeConfigLayer, setNestedValue } from "./config-merge.js";
import {
  loadJsonFile,
  normalizeConfigResourcePaths,
  modelsOverlayFromFile,
  mcpOverlayFromFile,
  loadPluginOverlay,
  loadFromEnv,
  inferModelProviderIfUnset,
  logPlatformModelEnvDiagnostics,
} from "./config-sources.js";

export * from "./config-schema.js";
export { deepMerge } from "./deep-merge.js";

const log = logger.child("config-loader");

// ─── Defaults ───────────────────────────────────────────

const DEFAULTS: AppConfig = AppConfigSchema.parse({});

// ─── Main Loader ────────────────────────────────────────

export interface LoadConfigOptions {
  /** Path to config file (default: template package config/app-agent.config.json) */
  configPath?: string;
  /** Built-in template config preset used when configPath is not provided */
  builtinConfig?: BuiltinTemplateConfigName;
  /** Base directory for relative paths inside the template config file */
  configBaseDir?: string;
  /** Workspace root used for project-level .flowagents config discovery */
  workspaceRoot?: string;
  /** ACP session-level overrides (highest priority) */
  sessionConfig?: ACPSessionConfig;
}

/**
 * Load configuration with priority chain:
 *   defaults < user ~/.flowagents < project .flowagents < template config < env vars < ACP/session meta
 */
export function loadConfig(options: LoadConfigOptions = {}): AppConfig {
  const builtinConfig = resolveBuiltinTemplateConfig(
    options.builtinConfig ?? readBuiltinTemplateConfigNameFromEnv()
  );
  const envConfigPath =
    process.env.DEEPAGENTS_CONFIG_PATH ||
    process.env.APP_AGENT_CONFIG_PATH ||
    undefined;
  const configPath = options.configPath ?? envConfigPath ?? builtinConfig.path;
  const globalDir = flowAgentsHome();
  const envWorkspaceRoot =
    process.env.DEEPAGENTS_WORKING_DIR ||
    process.env.AGENT_WORKING_DIR ||
    undefined;

  // Layer 1: Defaults
  let config: AppConfig = { ...DEFAULTS };

  // Layer 2: 全局用户级配置（~/.flowagents/config.json / models.json / mcp.json）
  const userConfig = loadJsonFile(join(globalDir, "config.json"));
  if (userConfig) {
    config = mergeConfigLayer(config, userConfig as Partial<AppConfig>);
  }
  const userModels = modelsOverlayFromFile(join(globalDir, "models.json"));
  if (userModels) {
    config = mergeConfigLayer(config, userModels);
  }
  const userMcp = mcpOverlayFromFile(join(globalDir, "mcp.json"));
  if (userMcp) {
    config = mergeConfigLayer(config, userMcp);
  }

  const workspaceRoot = resolveConfiguredWorkspaceRoot(
    config,
    options.workspaceRoot ?? options.sessionConfig?.cwd ?? envWorkspaceRoot ?? process.cwd()
  );
  const resolvedConfigPath = resolvePath(configPath, workspaceRoot);
  const usingBuiltinConfig = resolvedConfigPath === builtinConfig.path;
  const configBaseDir = options.configBaseDir ?? (usingBuiltinConfig ? builtinConfig.resourceBase : workspaceRoot);
  const projectDir = resolve(workspaceRoot, FLOWAGENTS_DIRNAME);

  // Layer 3: Project-level <workspace>/.flowagents config
  const projectConfig = loadJsonFile(join(projectDir, "config.json"));
  if (projectConfig) {
    config = mergeConfigLayer(config, projectConfig as Partial<AppConfig>);
  }
  const projectMcp = mcpOverlayFromFile(join(projectDir, "mcp.json"));
  if (projectMcp) {
    config = mergeConfigLayer(config, projectMcp);
  }

  // Layer 4: Existing template config (backward compatible)
  const fileConfig = loadJsonFile(configPath, workspaceRoot);
  if (fileConfig) {
    if (usingBuiltinConfig || options.configBaseDir) {
      normalizeConfigResourcePaths(fileConfig, configBaseDir);
    }
    config = mergeConfigLayer(config, fileConfig as Partial<AppConfig>);
  }

  // Layer 4b: Plugin manifests contributed by user/project plugin directories.
  const pluginOverlay = loadPluginOverlay(config, workspaceRoot);
  if (pluginOverlay) {
    config = mergeConfigLayer(config, pluginOverlay);
  }

  // Layer 5: Environment variables（平台 agent_server.env 占位符此时应已被 Electron 替换）
  const envConfig = loadFromEnv();
  config = mergeConfigLayer(config, envConfig);
  logPlatformModelEnvDiagnostics();
  config = inferModelProviderIfUnset(config);

  // Layer 6: ACP session overrides (highest priority)
  if (options.sessionConfig) {
    const sessionOverlay: Record<string, unknown> = {};
    const sc = options.sessionConfig;
    if (sc.model) setNestedValue(sessionOverlay, "model.name", sc.model);
    if (sc.systemPrompt) setNestedValue(sessionOverlay, "agent.systemPrompt", sc.systemPrompt);
    log.info("loadConfig layer-6 ACP session overlay", {
      workspaceRoot,
      cwd: sc.cwd,
      model: sc.model,
      mcpServerNames: sc.mcpServers ? Object.keys(sc.mcpServers) : [],
      systemPromptChars: sc.systemPrompt?.trim().length ?? 0,
      overlayModel: Boolean(sc.model),
      overlaySystemPrompt: Boolean(sc.systemPrompt?.trim()),
    });
    config = mergeConfigLayer(config, sessionOverlay as Partial<AppConfig>);
  }

  // Validate final config
  return AppConfigSchema.parse(config);
}

export function resolveConfiguredWorkspaceRoot(config: AppConfig, fallback = process.cwd()): string {
  return resolvePath(config.workspace.workingDir || fallback);
}
