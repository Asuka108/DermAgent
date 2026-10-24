/**
 * Shared Runtime Helpers — Barrel
 *
 * Historically a single module; the implementation now lives in focused
 * modules (model, prompt, discovery, permissions, runtime-context,
 * agent-config). This barrel preserves the original import surface so existing
 * callers and the runtime barrel (`index.ts`) keep working unchanged.
 */

export {
  createRuntimeContext,
  createRuntimeContextAsync,
  hydrateRuntimeContext,
  destroyRuntimeContext,
  isDefaultMcpEnabled,
  type RuntimeContext,
} from "./runtime-context.js";
export { resolveModelString, resolveModel, resolveApiKey } from "./model.js";
export { resolveSystemPrompt, resolveCliSystemPrompt, resolveOutputStyle } from "./prompt.js";
export {
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
} from "./discovery.js";
