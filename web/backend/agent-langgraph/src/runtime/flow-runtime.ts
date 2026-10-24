/**
 * FlowRuntime —— 图节点启动时拿到的「装配好的运行时」契约。
 *
 * 本文件**只定义接口**（纯类型，仅引用 runtime 层类型 → app→runtime 合法下行）。
 * 装配逻辑 `createFlowRuntime`（要 import app 层的 createFlowTools）在 `src/index.ts`（组合根/入口），
 * 以免 runtime→app 依赖倒挂;compose 层已并入 index,本文件不再 re-export 它。
 *
 * 图节点经 FlowRuntime 拿到 allTools（bindTools）/ systemPrompt / checkpointer，
 * 不再各自裸调 resolveModel / appConfig。
 */

import type { StructuredTool } from "@langchain/core/tools";
import type {
  AppConfig,
  RuntimeContext,
  DiscoveredSubAgent,
  DiscoveredSkill,
} from "./index.js";
import type { FlowSandboxPolicy } from "./fs/sandbox.js";
import type { FileCheckpointSaver } from "./services/file-checkpoint-saver.js";
import type { PlatformToolDescriptor, PlatformToolRef } from "./platform-tools/types.js";

export interface FlowRuntime {
  config: AppConfig;
  /** runtime context（含 mcpServerConfigs/mcpTools + MCP hydration）。 */
  ctx: RuntimeContext;
  /** 全部工具（内置通用 + flow 自补 + native MCP）—— 供 think 节点 bindTools。 */
  allTools: StructuredTool[];
  /**
   * 平台工具引用（schema 源）：来自 `FlowDef.platformToolRefs` /
   * `createFlowRuntime({ platformToolRefs })`。须固化平台已登记工具的真实配置；
   * **不是**旧 flow.json `spec.tools`。运行期也可由宿主注入等价工具。
   */
  platformToolRefs: PlatformToolRef[];
  /** 由 platformToolRefs 展开后的可执行 descriptor（一 toolName 一条）。 */
  platformToolDescriptors: PlatformToolDescriptor[];
  /** 解析后的系统提示词（ACP > config > prompts/ 文件 > fallback）。 */
  systemPrompt: string;
  /** 已发现的 skills 目录（deepagents progressive skills）。 */
  skillsPaths: string[];
  /** 已发现的 skills 清单（name/description/path）→ load_skill 工具 + 提示词清单。 */
  skills: DiscoveredSkill[];
  /** 已发现的声明式 subagent（.agents/agents/&lt;name&gt;/AGENT.md）。 */
  subAgents: DiscoveredSubAgent[];
  /** 工具沙箱策略（bash/fs 执行前校验）。 */
  sandbox: FlowSandboxPolicy;
  workspaceRoot: string;
  /** 文件后端 checkpointer（跨重启恢复 + interrupt/resume 持久化）。 */
  checkpointer: FileCheckpointSaver;
}
