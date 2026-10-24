/**
 * Flow 工具集装配（app 层）。
 *
 * 通用工具来自框架 toolkit/；task 委派工具是 app 专属（依赖默认图 createFlowGraph）。
 * createFlowTools 与 task.tool 共享 buildTools（按工作目录重建工具集,不含 task 防递归）。
 * 放 app 层（而非 toolkit）:它要同时 import toolkit（通用工具,下行）+ app/task.tool
 * （默认图专属,同层）+ app/graph（task 委派的目标图,同层）。
 */

import type { StructuredTool } from "@langchain/core/tools";
import type {
  RuntimeContext,
  DiscoveredSkill,
  DiscoveredSubAgent,
} from "../runtime/index.js";
import type { FlowSandboxPolicy } from "../runtime/fs/sandbox.js";
import {
  httpRequestTool,
  jsonUtilsTool,
  createBashTool,
  createFsTools,
  createSearchTools,
  createDemoTools,
  createSkillTool,
  writeTodosTool,
} from "../libs/tools/index.js";
import { createTaskTool } from "./task.tool.js";

export function createFlowTools(
  ctx: RuntimeContext,
  opts: {
    workspaceRoot: string;
    policy: FlowSandboxPolicy;
    /** 已发现的 skills → 暴露 load_skill 工具 + 提示词清单。 */
    skills?: DiscoveredSkill[];
    /** 已发现的声明式 subagent → 暴露 task 委派工具。 */
    subAgents?: DiscoveredSubAgent[];
    /** 基于 `platformToolRefs`（schema）动态创建的平台 StructuredTool。 */
    platformTools?: StructuredTool[];
  }
): StructuredTool[] {
  const { workspaceRoot, policy, skills = [], subAgents = [], platformTools = [] } = opts;

  // 与 cwd 无关的通用工具（无状态，主 agent 与子智能体共享同实例）。
  const reused: StructuredTool[] = [httpRequestTool, jsonUtilsTool, writeTodosTool];
  const skillTools = skills.length ? [createSkillTool(skills)] : [];

  const platformToolNameSet = new Set(platformTools.map((tool) => tool.name));
  const mcpTools = ctx.mcpTools.filter((tool) => !platformToolNameSet.has(tool.name));

  // 按工作目录构建一套工具（bash/fs/search 沙箱受限于该 cwd）——**不含 task，防递归**。
  // 主 agent 用 workspaceRoot；子智能体（task.tool）按各自 workdir 重建。
  const buildTools = (wsRoot: string): StructuredTool[] => [
    ...reused,
    createBashTool({ workspaceRoot: wsRoot, policy }),
    ...createFsTools({ workspaceRoot: wsRoot, policy }),
    ...createSearchTools({ workspaceRoot: wsRoot, policy }),
    ...createDemoTools(),
    ...mcpTools,
    ...platformTools,
    ...skillTools,
  ];

  const tools = buildTools(workspaceRoot);
  if (subAgents.length) {
    tools.push(
      createTaskTool({
        subAgents,
        config: ctx.config,
        parentWorkspaceRoot: workspaceRoot,
        buildTools,
      })
    );
  }
  return tools;
}
