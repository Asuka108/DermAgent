/**
 * Config merge primitives.
 *
 * Layered merge with array-concat semantics for the fields where a later layer
 * should add to (not replace) the earlier one. Extracted from config-loader.ts.
 */
import type { AppConfig } from "./config-schema.js";
import { deepMerge } from "./deep-merge.js";

/** Set a nested value in an object using a dot-separated path */
export function setNestedValue(obj: Record<string, unknown>, path: string, value: unknown): void {
  const keys = path.split(".");
  let current = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    const key = keys[i]!;
    if (!(key in current) || current[key] === null || typeof current[key] !== "object") {
      current[key] = {};
    }
    current = current[key] as Record<string, unknown>;
  }
  current[keys[keys.length - 1]!] = value;
}

export function concatUnique(a: string[], b: string[]): string[] {
  return Array.from(new Set([...a, ...b]));
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

export function mergeConfigLayer(config: AppConfig, layer: Partial<AppConfig>): AppConfig {
  const previousSkills = config.skills.directories;
  const previousSubagents = config.subagents.directories;
  const previousAgents = config.agentsDirectories;
  const previousMcpPaths = config.mcp.configPaths;
  const previousMcpServers = config.mcp.servers;
  const previousPluginDirs = config.plugins.directories;
  const merged = deepMerge(config, layer);

  if (layer.skills?.directories) {
    merged.skills.directories = concatUnique(previousSkills, layer.skills.directories);
  }
  if (layer.subagents?.directories) {
    merged.subagents.directories = concatUnique(previousSubagents, layer.subagents.directories);
  }
  if (layer.agentsDirectories) {
    merged.agentsDirectories = concatUnique(previousAgents, layer.agentsDirectories);
  }
  if (layer.mcp?.configPaths) {
    merged.mcp.configPaths = concatUnique(previousMcpPaths, layer.mcp.configPaths);
  }
  if (layer.mcp?.servers) {
    merged.mcp.servers = { ...previousMcpServers, ...layer.mcp.servers };
  }
  if (layer.plugins?.directories) {
    merged.plugins.directories = concatUnique(previousPluginDirs, layer.plugins.directories);
  }

  return merged;
}
