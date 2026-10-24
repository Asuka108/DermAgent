/**
 * Runtime 底层运行时 —— Barrel Export。
 *
 * 模板自有、自包含的底层运行时门面:config 加载、模型解析、日志、runtime-context。
 * MCP 经 @langchain/mcp-adapters 的 MultiServerMCPClient 由 runtime-context 内部自管，不从此处导出。
 * app / surfaces / compose 统一从这里取底层能力。
 */

export {
  loadConfig,
  resolveConfiguredWorkspaceRoot,
  AppConfigSchema,
  type AppConfig,
  type ModelConfig,
  type PermissionsConfig,
  type ACPSessionConfig,
} from "./config/config-loader.js";
export {
  createRuntimeContext,
  createRuntimeContextAsync,
  hydrateRuntimeContext,
  destroyRuntimeContext,
  isDefaultMcpEnabled,
  resolveModelString,
  resolveModel,
  resolveApiKey,
  resolveCliSystemPrompt,
  resolveSystemPrompt,
  discoverMemoryFiles,
  resolveSkillsPaths,
  resolveSubagentPaths,
  discoverSubAgents,
  discoverSkills,
  renderSkillsSection,
  renderSubagentsSection,
  renderMcpServersSection,
  type DiscoveredSubAgent,
  type DiscoveredSkill,
  type RuntimeContext,
} from "./context/helpers.js";
export {
  Logger,
  logger,
  setLogAgent,
  setLogSession,
  getSessionLogPath,
  getEffectiveLogLevel,
  truncateForLog,
  formatMessagesForLog,
  formatPayloadForLog,
  resolveTraceMaxChars,
  type LogLevel,
} from "./logger.js";
export { traceFlowCallbacks, traceFlowRun, type SessionTraceContext, type TraceFlowRunMeta } from "./session-trace.js";
export {
  isInAcpPromptCycle,
  getAcpPromptCycle,
  acpPromptLogFields,
  runInAcpPromptCycle,
  logAcpPromptStart,
  logAcpPromptEnd,
  logPromptComplete,
  type AcpPromptCycle,
} from "./session-trace.js";
export {
  isPerfTraceEnabled,
  markStart,
  markEnd,
  recordPhase,
  timePhase,
  beginPerfSession,
  endPerfSession,
  type PerfMark,
} from "./perf-trace.js";
export { getPackageVersion } from "./version.js";
export type { PlatformToolRef, PlatformToolDescriptor } from "./platform-tools/types.js";
export { createPlatformToolDescriptors } from "./platform-tools/descriptor.js";
export { schemaToZodInput } from "./platform-tools/schema-to-zod.js";
export { createPlatformStructuredTool } from "./platform-tools/create-platform-structured-tool.js";
