/**
 * Workspace Resource Discovery
 *
 * Discovers memory files (AGENTS.md / CLAUDE.md), normalizes skills directory
 * paths, and parses subagent definitions from `agentsDirectories` (`<root>/agents/`)
 * and optional flat `subagents.directories`.
 * All resolution is workspace-root relative, matching how deepagents loads
 * these resources.
 */

import { readFileSync, existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { resolve, join } from "node:path";
import { parse as parseYaml } from "yaml";
import { type AppConfig } from "../config/config-loader.js";
import { logger } from "../logger.js";
import { FLOWAGENTS_DIRNAME } from "../paths.js";

// ─── Memory Files ───────────────────────────────────────

/**
 * Discover AGENTS.md and CLAUDE.md files in the workspace.
 * These are loaded by deepagents' memory system into the system prompt.
 */
export function discoverMemoryFiles(workspaceRoot: string, includeWorkspaceInstructions = true): string[] {
  if (!includeWorkspaceInstructions) {
    return [];
  }

  const candidates = [
    "AGENTS.md",
    "CLAUDE.md",
    `${FLOWAGENTS_DIRNAME}/AGENTS.md`,
  ];
  const found: string[] = [];
  for (const candidate of candidates) {
    if (existsSync(resolve(workspaceRoot, candidate))) {
      found.push(`./${candidate}`);
    }
  }
  return found;
}

// ─── Skills Paths ───────────────────────────────────────

/**
 * Normalize skills directory paths for deepagents.
 * deepagents expects POSIX paths relative to the backend root.
 *
 * Includes:
 * 1. Built-in skill directories from config.skills.directories
 * 2. Skills from each configured agentsDirectory (via <dir>/skills/)
 */
export function resolveSkillsPaths(config: AppConfig): string[] {
  const paths = config.skills.directories.map(normalizeResourcePath);

  // Append skills from .agents directories
  for (const agentsDir of config.agentsDirectories) {
    const normalized = normalizeResourcePath(agentsDir);
    const skillsDir = `${normalized}/skills`;
    paths.push(skillsDir);
  }

  return Array.from(new Set(paths));
}

function normalizeResourcePath(path: string): string {
  const flowagentsPrefix = `~/${FLOWAGENTS_DIRNAME}`;
  if (path === flowagentsPrefix) {
    return resolve(process.env.FLOWAGENTS_HOME || resolve(homedir(), FLOWAGENTS_DIRNAME));
  }
  if (path.startsWith(`${flowagentsPrefix}/`)) {
    return resolve(
      process.env.FLOWAGENTS_HOME || resolve(homedir(), FLOWAGENTS_DIRNAME),
      path.slice(`${flowagentsPrefix}/`.length)
    );
  }
  if (path.startsWith("~/")) {
    return resolve(homedir(), path.slice(2));
  }
  if (path.startsWith("/") || path.startsWith("./")) {
    return path;
  }
  return `./${path}`;
}

// ─── Subagent Discovery ─────────────────────────────────

/** Parsed subagent definition from an AGENT.md file */
export interface DiscoveredSubAgent {
  name: string;
  description: string;
  systemPrompt: string;
  /** 可选：覆盖模型名；可写 `model-name` 或 `provider/model-name`。frontmatter `model`。 */
  model?: string;
  /** 可选：工具名 allowlist（缺省继承父级全部工具，去掉 `task` 防递归）。frontmatter `tools`（逗号/空白分隔）。 */
  tools?: string[];
  /** 可选：相对工作目录（缺省 = 父 workspaceRoot，即启动 agent 的当前目录）。frontmatter `workdir`。 */
  workdir?: string;
}

/**
 * Parse YAML frontmatter from a markdown file.
 * Returns { frontmatter, body } where frontmatter is a plain object.
 * Supports simple string values and multi-line values (continuation lines starting with whitespace).
 */
export function parseFrontmatter(content: string): { frontmatter: Record<string, string>; body: string } {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!match) {
    return { frontmatter: {}, body: content };
  }

  let frontmatter: Record<string, string> = {};
  try {
    const parsed = parseYaml(match[1]) as Record<string, unknown> | null;
    if (parsed && typeof parsed === "object") {
      frontmatter = {};
      for (const [key, value] of Object.entries(parsed)) {
        frontmatter[key] = typeof value === "string" ? value : String(value ?? "");
      }
    }
  } catch {
    // YAML parse error — return empty frontmatter, keep body intact
  }

  return { frontmatter, body: match[2]!.trim() };
}

/**
 * Subagent scan roots (POSIX paths relative to workspace unless absolute):
 * Subagent scan roots:
 * 1. `config.subagents.directories` — flat `<dir>/<name>/AGENT.md`（高级，默认空）
 * 2. Each `agentsDirectories` entry — `<dir>/agents/<name>/AGENT.md`（如 `./builtin/agents/`）
 */
export function resolveSubagentPaths(config: AppConfig): string[] {
  const paths = config.subagents.directories.map(normalizeResourcePath);

  for (const agentsDir of config.agentsDirectories) {
    const normalized = normalizeResourcePath(agentsDir);
    paths.push(`${normalized}/agents`);
  }

  return Array.from(new Set(paths));
}

function discoverSubAgentsInDirectory(
  agentsPath: string,
  subagents: DiscoveredSubAgent[],
  seen: Set<string>,
  log: ReturnType<typeof logger.child>,
): void {
  if (!existsSync(agentsPath)) {
    log.debug("No subagents directory found", { path: agentsPath });
    return;
  }

  let entries: string[];
  try {
    entries = readdirSync(agentsPath, { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .map((e) => e.name);
  } catch {
    log.warn("Failed to read subagents directory", { path: agentsPath });
    return;
  }

  for (const entry of entries) {
    const agentMdPath = join(agentsPath, entry, "AGENT.md");
    if (!existsSync(agentMdPath)) {
      log.debug("No AGENT.md found in agent directory", { dir: entry });
      continue;
    }

    try {
      const content = readFileSync(agentMdPath, "utf-8");
      const { frontmatter, body } = parseFrontmatter(content);

      const name = frontmatter.name || entry;
      if (seen.has(name)) {
        log.debug("Skipping duplicate subagent name", { name, path: agentMdPath });
        continue;
      }

      const description = frontmatter.description || `Subagent: ${name}`;

      if (!body) {
        log.warn("AGENT.md has no body (systemPrompt)", { path: agentMdPath });
        continue;
      }

      const model = frontmatter.model || undefined;
      const workdir = frontmatter.workdir || undefined;
      const tools = frontmatter.tools
        ? frontmatter.tools.split(/[\s,]+/).map((t) => t.trim()).filter(Boolean)
        : undefined;

      seen.add(name);
      subagents.push({ name, description, systemPrompt: body, model, tools, workdir });
      log.info("Discovered subagent", { name, source: agentMdPath });
    } catch (err) {
      log.warn("Failed to parse AGENT.md", {
        path: agentMdPath,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }
}

/**
 * Discover subagents from `agentsDirectories` (`<root>/agents/`) and optional flat `subagents.directories`.
 *
 * @example builtin/agents/researcher/AGENT.md
 * @example .agents/agents/researcher/AGENT.md
 */
export function discoverSubAgents(config: AppConfig, workspaceRoot?: string): DiscoveredSubAgent[] {
  const subagents: DiscoveredSubAgent[] = [];
  const seen = new Set<string>();
  const log = logger.child("subagent-discovery");
  const root = workspaceRoot || process.cwd();

  for (const dirPath of resolveSubagentPaths(config)) {
    discoverSubAgentsInDirectory(resolve(root, dirPath), subagents, seen, log);
  }

  return subagents;
}

// ─── Skill Discovery ────────────────────────────────────

/** Parsed skill manifest from a SKILL.md file (frontmatter only; body loaded on demand). */
export interface DiscoveredSkill {
  name: string;
  description: string;
  /** SKILL.md 绝对路径（`load_skill` 工具读正文用）。 */
  path: string;
}

/**
 * Discover skills from every directory in `resolveSkillsPaths(config)`.
 *
 * Convention: each skill is a subdirectory containing a SKILL.md with YAML
 * frontmatter (name, description) and a body (full instructions). Only the
 * frontmatter is parsed here — the body is loaded on demand via `load_skill`
 * (progressive disclosure, deepagents-style). First occurrence of a name wins
 * (matches `resolveSkillsPaths` precedence order).
 */
export function discoverSkills(config: AppConfig, workspaceRoot?: string): DiscoveredSkill[] {
  const log = logger.child("skill-discovery");
  const root = workspaceRoot || process.cwd();
  const skills: DiscoveredSkill[] = [];
  const seen = new Set<string>();

  for (const dirPath of resolveSkillsPaths(config)) {
    const skillsDir = resolve(root, dirPath);
    if (!existsSync(skillsDir)) {
      log.debug("No skills directory found", { path: skillsDir });
      continue;
    }

    let entries: string[];
    try {
      entries = readdirSync(skillsDir, { withFileTypes: true })
        .filter((e) => e.isDirectory())
        .map((e) => e.name);
    } catch {
      log.warn("Failed to read skills directory", { path: skillsDir });
      continue;
    }

    for (const entry of entries) {
      const skillMd = join(skillsDir, entry, "SKILL.md");
      if (!existsSync(skillMd)) continue;
      try {
        const { frontmatter } = parseFrontmatter(readFileSync(skillMd, "utf-8"));
        const name = frontmatter.name || entry;
        if (seen.has(name)) continue;
        seen.add(name);
        skills.push({
          name,
          description: frontmatter.description || `Skill: ${name}`,
          path: skillMd,
        });
        log.debug("Discovered skill", { name, source: skillMd });
      } catch (err) {
        log.warn("Failed to parse SKILL.md", {
          path: skillMd,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }
  }

  return skills;
}

// ─── Prompt Section Renderers ───────────────────────────

/**
 * Render the "Available Skills" prompt section (name + description only).
 * Empty when no skills — caller appends to the system prompt.
 */
export function renderSkillsSection(skills: DiscoveredSkill[], progressiveLoading = true): string {
  if (!skills.length) return "";
  if (!progressiveLoading) {
    const blocks = skills.map((s) => {
      try {
        const { body } = parseFrontmatter(readFileSync(s.path, "utf-8"));
        return `### ${s.name}\n${body}`;
      } catch (err) {
        return `### ${s.name}\n读取失败: ${err instanceof Error ? err.message : String(err)}`;
      }
    });
    return `## Available Skills

以下 skill 已完整加载到系统提示词中，直接按对应说明执行：
${blocks.join("\n\n")}`;
  }
  const lines = skills.map((s) => `- **${s.name}** — ${s.description}`);
  return `## Available Skills

需要专门流程/知识时，先调用 \`load_skill(name)\` 读取该 skill 的完整说明（SKILL.md 正文），再据此执行：
${lines.join("\n")}`;
}

/**
 * Render the "Subagents" prompt section. Empty when no subagents.
 * Tells the model it can delegate via the `task` tool.
 *
 * 术语：中文用户文案统一称「子智能体」，英文 API/标识保留 subagent（如 subagent_type）。
 */
export function renderSubagentsSection(subAgents: DiscoveredSubAgent[]): string {
  if (!subAgents.length) return "";
  const lines = subAgents.map((a) => `- **${a.name}** — ${a.description}`);
  return `## Subagents（用 task 委派）

需要专门能力时，调用 \`task({ subagent_type, description })\` 把子任务委派给下列子智能体（subagent，各自独立 prompt/工具/工作目录），拿回结果后继续：
${lines.join("\n")}`;
}

/**
 * Render the "Available MCP Servers" prompt section.
 * 仅包含 tools/list 验证成功的 server 及其实际工具名（不做 URL/名称特征推断）。
 */
export function renderMcpServersSection(
  serverToolLists: Record<string, string[]>
): string {
  const entries = Object.entries(serverToolLists);
  if (!entries.length) return "";
  const lines = entries.map(([name, tools]) => {
    const toolPart =
      tools.length > 0
        ? tools.map((t) => `\`${t}\``).join(", ")
        : "（已连接，tools/list 为空）";
    return `- **${name}** — ${toolPart}`;
  });
  return `## Available MCP Servers

本会话经 tools/list 确认已连接的 MCP server（工具经 mcp-adapters 以 \`<server>__<tool>\` 前缀注入 bindTools）：
${lines.join("\n")}

用户询问「有哪些 MCP / 可用工具」时，直接根据上表回答，无需加载无关 skill 或全盘搜索文件。`;
}
