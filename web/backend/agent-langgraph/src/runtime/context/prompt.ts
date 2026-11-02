/**
 * System Prompt & Output Style Resolution
 *
 * Resolves the agent's system prompt across the ACP/config/file priority chain
 * and appends configured output styles. `withRuntimeContextPrompt` is exported
 * for the agent-config builder, which wraps the resolved prompt with runtime
 * workspace context.
 */

import { readFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { type AppConfig, type ACPSessionConfig } from "../config/config-loader.js";
import { logger } from "../logger.js";
import { previewText } from "../preview-text.js";
import { PLATFORM_CONVENTIONS } from "./harness-profile.js";

const log = logger.child("system-prompt");

/** resolveSystemPrompt 分支来源（诊断日志用）。 */
type SystemPromptResolveSource =
  | "acp-session"
  | "config-inline"
  | "config-file"
  | "inline-fallback"
  | "none";

/**
 * Resolve system prompt with priority chain:
 *   ACP session: local prompt file + session append + PLATFORM_CONVENTIONS
 *   No session: config.agent.systemPrompt > config.agent.systemPromptPath > inline fallback
 */
export interface SystemPromptResolveMeta {
  prompt: string;
  source: SystemPromptResolveSource;
  /** 文件来源时的绝对路径；inline-fallback 时为尝试过的路径。 */
  promptPath?: string;
  promptChars: number;
  promptPreview?: string;
}

/** 解析系统提示词并返回来源元数据（供诊断日志与 resolveSystemPrompt 共用）。 */
export function resolveSystemPromptMeta(
  config: AppConfig,
  sessionConfig: ACPSessionConfig | undefined,
  workspaceRoot: string
): SystemPromptResolveMeta {
  // ACP session prompt is authoritative identity — when the platform delivers a
  // session `systemPrompt` it IS the agent's business persona (configured on the
  // platform). We do NOT prepend the local `prompts/flow.base.md`: that file is the
  // template's generic scaffolding ("通用基座"), and prepending it pollutes the
  // business identity and duplicates/conflicts with the tool-priority ordering in
  // PLATFORM_CONVENTIONS. Only append the harness-level tool/secret conventions the
  // model still needs. `flow.base.md` remains the identity for the no-session path
  // (local CLI / config-file branch below).
  if (sessionConfig?.systemPrompt) {
    const prompt = `${sessionConfig.systemPrompt.trimEnd()}\n\n${PLATFORM_CONVENTIONS}`;
    return {
      prompt,
      source: "acp-session",
      promptChars: prompt.length,
      promptPreview: previewText(prompt),
    };
  }

  if (config.agent.systemPrompt) {
    const prompt = withOutputStyle(config.agent.systemPrompt, config, workspaceRoot);
    return {
      prompt,
      source: "config-inline",
      promptChars: prompt.length,
      promptPreview: previewText(prompt),
    };
  }

  // Try loading from the configured prompt path.
  const promptPath = resolvePromptPath(config.agent.systemPromptPath, workspaceRoot);
  let basePrompt: string;
  let source: SystemPromptResolveSource;
  if (existsSync(promptPath)) {
    const content = readFileSync(promptPath, "utf-8");
    // Strip the H1 title line (metadata, not prompt content)
    basePrompt = content.replace(/^# .*\r?\n/, "").trim();
    source = "config-file";
  } else {
    // Inline fallback
    source = "inline-fallback";
    basePrompt = `You are ${config.agent.name} — an AI application agent.

## Workflow
1. Research — understand the task and check available tools
2. Plan — break down into steps using write_todos
3. Implement — execute each step
4. Verify — test and validate results

## Tool Priority (MANDATORY)
1. MCP tools (native, bound from configured MCP servers) — check first
2. Built-in tools (bash, filesystem, search, http_request, json_utils)
3. deepagents built-in tools (read_file, write_file, edit_file, execute, task)
4. Write custom code (last resort only)

## Rules
- Never hardcode secrets — use environment variables
- Target agent prompts come from ACP — never hardcode them
`;
  }

  // Append output style if configured
  const prompt = withOutputStyle(basePrompt, config, workspaceRoot);
  return {
    prompt,
    source,
    promptPath,
    promptChars: prompt.length,
    promptPreview: previewText(prompt),
  };
}

export function resolveSystemPrompt(
  config: AppConfig,
  sessionConfig: ACPSessionConfig | undefined,
  workspaceRoot: string
): string {
  const meta = resolveSystemPromptMeta(config, sessionConfig, workspaceRoot);
  log.info("resolveSystemPrompt", {
    source: meta.source,
    promptPath: meta.promptPath,
    promptChars: meta.promptChars,
    promptPreview: meta.promptPreview,
    sessionHasSystemPrompt: Boolean(sessionConfig?.systemPrompt?.trim()),
    configInlinePromptChars: config.agent.systemPrompt?.trim().length ?? 0,
    configuredPromptPath: config.agent.systemPromptPath,
    workspaceRoot,
  });
  return meta.prompt;
}

/**
 * Resolve system prompt for CLI modes (REPL / 单次调用).
 * Priority: explicit text > custom file > default prompt file > generic fallback.
 */
export function resolveCliSystemPrompt(options: {
  systemPrompt?: string;
  promptPath?: string;
  workspaceRoot?: string;
  config?: AppConfig;
}): string {
  if (options.systemPrompt) {
    return options.systemPrompt;
  }

  const workspaceRoot = options.workspaceRoot || process.cwd();
  if (options.promptPath) {
    const fullPath = resolvePromptPath(options.promptPath, workspaceRoot);
    if (existsSync(fullPath)) {
      return readFileSync(fullPath, "utf-8").replace(/^# .*\r?\n/, "").trim();
    }
  }

  if (options.config?.agent.systemPrompt) {
    return options.config.agent.systemPrompt;
  }

  const configuredPath = options.config?.agent.systemPromptPath;
  const defaultPath = resolvePromptPath(configuredPath || "prompts/flow.base.md", workspaceRoot);
  if (existsSync(defaultPath)) {
    return readFileSync(defaultPath, "utf-8").replace(/^# .*\r?\n/, "").trim();
  }

  return "You are a helpful DeepAgent assistant. Be concise and action-oriented.";
}

/**
 * Load an output style file from prompts/styles/{name}.md.
 * Returns the style content (without frontmatter) or empty string if not found.
 */
export function resolveOutputStyle(styleName: string, workspaceRoot: string): string {
  const stylePath = resolve(workspaceRoot, "prompts/styles", `${styleName}.md`);
  if (!existsSync(stylePath)) {
    return "";
  }
  const content = readFileSync(stylePath, "utf-8");
  // Strip YAML frontmatter
  return content.replace(/^---\n[\s\S]*?\n---\n?/, "").trim();
}

function withOutputStyle(basePrompt: string, config: AppConfig, workspaceRoot: string): string {
  const style = resolveOutputStyle(config.agent.outputStyle, workspaceRoot);
  return style ? `${basePrompt}\n\n${style}` : basePrompt;
}

export function withRuntimeContextPrompt(basePrompt: string, workspaceRoot: string): string {
  return `${basePrompt}

## Runtime Context
- Effective workspace root: ${workspaceRoot}
- If the user asks for the current workspace directory, project root, cwd, runtime directory, or session location, use the \`runtime_info\` tool or answer from this Runtime Context.
- Do not infer the workspace by listing \`/\`, \`/Users\`, or parent directories.`;
}

function resolvePromptPath(path: string, workspaceRoot: string): string {
  if (path.startsWith("~/")) {
    return resolve(process.env.HOME || "", path.slice(2));
  }
  return path.startsWith("/") ? path : resolve(workspaceRoot, path);
}
