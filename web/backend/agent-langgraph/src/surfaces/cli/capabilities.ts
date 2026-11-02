/**
 * flow capabilities —— 输出能力分层 + 可用工具/MCP/skills（静态解析，无凭证）。
 *
 * 静态解析（不加载 MCP server、不需要凭证、不 spawn）：读 config + mcp.default.json
 * + .nuwax-agent/capability-sources.json，输出结构化 JSON。
 */

import { readFileSync, existsSync } from "node:fs";
import { dirname, resolve, join } from "node:path";
import { loadFlowConfig } from "../../runtime/flow-config.js";
import {
  discoverSkills,
  discoverSubAgents,
  isDefaultMcpEnabled,
  resolveSystemPrompt,
} from "../../runtime/index.js";
import { resolveFlowHome } from "../../runtime/services/file-checkpoint-saver.js";

const BUILTIN_TOOLS = [
  { name: "bash", layer: "agent-builtin", desc: "shell 执行（cwd=workspace，受 sandbox 约束）" },
  { name: "read_file", layer: "agent-builtin", desc: "读取文件（限 workspace）" },
  { name: "write_file", layer: "agent-builtin", desc: "写入文件（受 sandbox 写权限）" },
  { name: "edit_file", layer: "agent-builtin", desc: "查找替换编辑" },
  { name: "grep", layer: "agent-builtin", desc: "正则搜索文件内容" },
  { name: "glob", layer: "agent-builtin", desc: "按 glob 列文件" },
  { name: "http_request", layer: "agent-builtin", desc: "HTTP 调用" },
  { name: "json_utils", layer: "agent-builtin", desc: "JSON 解析/校验/合并" },
  { name: "load_skill", layer: "agent-builtin", desc: "按需读取 skill 的 SKILL.md 或 references/ 子文件（part 参数）" },
  { name: "task", layer: "agent-builtin", desc: "委派任务给已发现的声明式子智能体（builtin/agents 或 .agents/agents）" },
  { name: "echo", layer: "agent-builtin", desc: "demo：回显（无凭证 fallback）" },
  { name: "calculate", layer: "agent-builtin", desc: "demo：算术求值" },
  { name: "time", layer: "agent-builtin", desc: "demo：当前时间" },
];

function readJson<T>(path: string): T | null {
  try {
    if (existsSync(path)) return JSON.parse(readFileSync(path, "utf-8")) as T;
  } catch {
    /* ignore */
  }
  return null;
}

export async function runCapabilities(): Promise<void> {
  const { appConfig, configPath } = loadFlowConfig();
  const pkgRoot = dirname(dirname(configPath));

  // 与 runtime-context 一致：DEEPAGENTS_DEFAULT_MCP=disabled 时不展示默认 MCP。
  let mcpServers: Record<string, unknown> = {};
  if (isDefaultMcpEnabled()) {
    const mcpPath = resolve(pkgRoot, appConfig.mcp.configPath);
    const mcpDefault = readJson<{ servers?: Record<string, unknown> }>(mcpPath);
    mcpServers = mcpDefault?.servers ?? {};
  }

  const capabilitySources = readJson<unknown>(
    join(pkgRoot, ".nuwax-agent", "capability-sources.json")
  );

  // 静态发现（只读文件，不加载 MCP / 不需凭证）：实际可用的 skills / subagents。
  const skills = discoverSkills(appConfig, process.cwd()).map((s) => ({
    name: s.name,
    description: s.description,
  }));
  const subagents = discoverSubAgents(appConfig, process.cwd()).map((a) => ({
    name: a.name,
    description: a.description,
    ...(a.model ? { model: a.model } : {}),
    ...(a.workdir ? { workdir: a.workdir } : {}),
  }));

  const workspaceRoot = process.cwd();
  const systemPrompt = resolveSystemPrompt(appConfig, undefined, workspaceRoot);
  const contextBudget = {
    systemPromptChars: systemPrompt.length,
    skillsCatalogChars: skills.reduce((n, s) => n + s.name.length + s.description.length + 4, 0),
    skillsCount: skills.length,
    progressiveLoading: appConfig.skills.progressiveLoading,
    note: "工具 schema 经 bindTools 注入，通常大于 systemPrompt；skills 正文经 load_skill 按需加载",
  };

  const result = {
    agent: appConfig.agent.name,
    description: appConfig.agent.description,
    model: { provider: appConfig.model.provider, name: appConfig.model.name },
    builtinTools: BUILTIN_TOOLS,
    mcpServers,
    skillsDirectories: appConfig.skills.directories,
    skills,
    subagentsDirectories: appConfig.subagents.directories,
    agentsDirectories: appConfig.agentsDirectories,
    subagents,
    sandbox: { profile: appConfig.sandbox.profile, writablePaths: appConfig.sandbox.writablePaths },
    compaction: {
      enabled: appConfig.compaction.enabled,
      contextWindow: appConfig.compaction.contextWindow,
      triggerThreshold: appConfig.compaction.triggerThreshold,
    },
    memoryDir: resolveFlowHome(appConfig, process.cwd()),
    contextBudget,
    capabilitySources,
  };

  process.stdout.write(JSON.stringify(result, null, 2) + "\n");
}
