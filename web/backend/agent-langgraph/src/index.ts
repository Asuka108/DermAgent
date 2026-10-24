#!/usr/bin/env node

/**
 * 通用工作流编排模板入口
 *
 * 模式：
 *   (默认) / acp        启动 ACP 服务（stdio）—— 供 NuwaClaw / 平台宿主
 *   flow "<输入>"       命令行跑一次默认 flow（测试用）
 *   flow -i             交互模式
 *   graph               导出默认图拓扑（JSON；加 --mermaid 出 Mermaid 源）
 *   capabilities        输出能力分层 + 可用工具/MCP/skills（无凭证，静态查询）
 *   sessions            列出已持久化的会话（thread id）
 *
 * 默认图是标准 LangGraph ReAct（prepare → think ↔ tools → respond）。
 * 工具/会话/压缩/Skills/Subagent 经 FlowRuntime（框架原生能力 + 能力分层配置）驱动。
 * 选项：--config <path>  --debug  -h/--help
 */

import { config as loadDotenv } from "dotenv";
import { basename, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { loadFlowConfig } from "./runtime/flow-config.js";
import { destroyRuntimeContext, setLogAgent } from "./runtime/index.js";
import {
  createRuntimeContextAsync,
  resolveSystemPrompt,
  resolveSkillsPaths,
  discoverSkills,
  discoverSubAgents,
  renderSkillsSection,
  renderSubagentsSection,
  renderMcpServersSection,
  beginPerfSession,
  endPerfSession,
  timePhase,
  markStart,
  markEnd,
  type AppConfig,
  type ACPSessionConfig,
} from "./runtime/index.js";
import { createFlowTools } from "./app/flow-tools.js";
import { getFlowSandboxPolicy } from "./runtime/fs/sandbox.js";
import { createFileCheckpointer } from "./runtime/services/file-checkpoint-saver.js";
import { logRuntimeSystemPromptDiagnostics } from "./surfaces/acp/session-diagnostics.js";
import type { FlowRuntime } from "./runtime/flow-runtime.js";
import { bootstrapFlowAcp } from "./surfaces/acp/server.js";
import { loadSessionConfigFromEnv } from "./surfaces/acp/session-config.js";
import { runFlowCli } from "./surfaces/cli/run.js";
import { runCapabilities } from "./surfaces/cli/capabilities.js";
import { runSessions } from "./surfaces/cli/sessions.js";
import {
  listFlowProfiles,
  recommendFlows,
  resolveFlow,
  resolveFlowSelection,
  type FlowDef,
} from "./app/flows/index.js";
import { createStatefulFlow } from "./surfaces/stateful-flow.js";
import type { StatefulFlow } from "./core/flow-types.js";
import {
  createPlatformStructuredTool,
  createPlatformToolDescriptors,
  type PlatformToolRef,
} from "./runtime/index.js";

/**
 * createFlowRuntime —— 组合根(原 `compose/flow-runtime.ts` 折入入口)。
 * 装配 runtime 基础设施 + app 工具(createFlowTools)→ FlowRuntime,注入图节点。
 * 唯一跨层向下装配点(取 app/flow-tools);故在 root(index)而非 runtime,免 runtime→app 倒挂。
 */
export async function createFlowRuntime(
  appConfig: AppConfig,
  options: {
    sessionConfig?: ACPSessionConfig;
    workspaceRoot?: string;
    platformToolRefs?: PlatformToolRef[];
    /** ACP sessionId（MCP 工具 schema 缓存主键）；CLI 无 session 时省略。 */
    sessionId?: string;
    /** ACP 装配 phase：`new`（清缓存不读）/ `load`（读缓存）；省略按 load 处理。 */
    sessionPhase?: string;
  } = {}
): Promise<FlowRuntime> {
  const workspaceRoot = options.workspaceRoot ?? process.cwd();
  // 全流程加载耗时追踪（全局 env 开关 PERF_TRACE，默认开）：各阶段单独计时 + 收尾汇总，定位启动瓶颈。
  const perf = beginPerfSession("runtime.load");

  // runtime.context 含 MCP 加载（getTools 通常是启动大头；命中工具 schema 缓存时近乎零）。
  const ctx = await timePhase("runtime.context", () =>
    createRuntimeContextAsync(
      appConfig,
      options.sessionConfig,
      workspaceRoot,
      options.sessionId,
      options.sessionPhase
    )
  );
  const sandbox = getFlowSandboxPolicy(appConfig);

  const discoverMark = markStart("discover.skills+subagents");
  const skillsPaths = resolveSkillsPaths(appConfig);
  const skills = discoverSkills(appConfig, workspaceRoot);
  const subAgents = discoverSubAgents(appConfig, workspaceRoot);
  markEnd(discoverMark, { skills: skills.length, subAgents: subAgents.length });
  const progressiveSkills = appConfig.skills.progressiveLoading;

  const platformToolRefs = options.platformToolRefs ?? [];
  const platformToolDescriptors = createPlatformToolDescriptors(platformToolRefs);
  const platformTools = platformToolDescriptors.map((descriptor) =>
    createPlatformStructuredTool({ descriptor })
  );

  // skills → load_skill 工具;subAgents → task 委派工具(沙箱按 workspaceRoot / 各自 workdir)。
  const toolsMark = markStart("tools.build");
  const allTools = createFlowTools(ctx, {
    workspaceRoot,
    policy: sandbox,
    skills: progressiveSkills ? skills : [],
    subAgents,
    platformTools,
  });
  markEnd(toolsMark, { toolCount: allTools.length });

  // 系统提示词追加「Available Skills」「Subagents」「MCP Servers」清单。
  const promptMark = markStart("systemPrompt.resolve");
  const baseSystemPrompt = resolveSystemPrompt(appConfig, options.sessionConfig, workspaceRoot);
  const mcpServersSection = renderMcpServersSection(ctx.mcpServerToolLists);
  const skillsSection = renderSkillsSection(skills, progressiveSkills);
  const subagentsSection = renderSubagentsSection(subAgents);
  const sections = [
    mcpServersSection,
    skillsSection,
    subagentsSection,
  ].filter(Boolean);
  const systemPrompt = sections.length
    ? `${baseSystemPrompt}\n\n${sections.join("\n\n")}`
    : baseSystemPrompt;
  markEnd(promptMark, { chars: systemPrompt.length });

  logRuntimeSystemPromptDiagnostics({
    sessionConfig: options.sessionConfig,
    configInlinePrompt: appConfig.agent.systemPrompt,
    systemPromptPath: appConfig.agent.systemPromptPath,
    workspaceRoot,
    finalSystemPromptChars: systemPrompt.length,
    skillsSectionChars: skillsSection?.length,
    subagentsSectionChars: subagentsSection?.length,
  });

  // 文件后端 checkpointer(跨重启恢复 + interrupt/resume 持久化)。
  const checkpointerMark = markStart("checkpointer.build");
  const checkpointer = createFileCheckpointer(appConfig, workspaceRoot);
  markEnd(checkpointerMark);

  endPerfSession(perf, { workspaceRoot });

  return {
    config: appConfig,
    ctx,
    allTools,
    platformToolRefs,
    platformToolDescriptors,
    systemPrompt,
    skillsPaths,
    skills,
    subAgents,
    sandbox,
    workspaceRoot,
    checkpointer,
  };
}

/**
 * materializeFlow —— 把 FlowDef 物化成 surface 能用的 StatefulFlow。
 * createStatefulFlow 全工程仅此一处调用。
 */
function materializeFlow(def: FlowDef, runtime: FlowRuntime): StatefulFlow {
  return createStatefulFlow({
    ...def.recipe(runtime),
    checkpointer: runtime.checkpointer,
    appConfig: runtime.config,
    conversational: def.conversational ?? def.profile.interaction === "chat",
    mcpClient: runtime.ctx.mcpClient ?? undefined,
  });
}

interface ParsedArgs {
  command: "acp" | "flow" | "graph" | "capabilities" | "sessions" | "flows";
  query?: string;
  configPath?: string;
  debug: boolean;
  interactive: boolean;
  help: boolean;
  /** graph 命令:输出 Mermaid 源而非 JSON */
  mermaid: boolean;
  /** sessions 子命令：list（默认）/ delete */
  sessionsAction?: "list" | "delete";
  /** sessions delete 的目标 thread id */
  sessionsId?: string;
  /** flows 子命令：list（默认）/ recommend */
  flowsAction?: "list" | "recommend";
  /** flows --json */
  flowsJson?: boolean;
  /** flows recommend --kind */
  flowsKind?: "chat" | "pipeline" | "approval";
}

function parseArgs(argv: string[]): ParsedArgs {
  const args: ParsedArgs = {
    command: "acp",
    debug: false,
    interactive: false,
    help: false,
    mermaid: false,
  };
  const positional: string[] = [];

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]!;
    if (a === "--debug") args.debug = true;
    else if (a === "--help" || a === "-h") args.help = true;
    else if (a === "--interactive" || a === "-i") args.interactive = true;
    else if (a === "--mermaid") args.mermaid = true;
    else if (a === "--json") args.flowsJson = true;
    else if (a === "--kind" && argv[i + 1]) {
      const kind = argv[++i];
      if (kind === "chat" || kind === "pipeline" || kind === "approval") {
        args.flowsKind = kind;
      } else {
        console.error(`Unknown flow kind: ${kind}`);
        process.exit(1);
      }
    }
    else if (a === "--config" && argv[i + 1]) args.configPath = argv[++i];
    else if (a.startsWith("--")) {
      console.error(`Unknown flag: ${a}`);
      process.exit(1);
    } else positional.push(a);
  }

  const first = positional[0];
  if (first === "graph") {
    args.command = "graph";
  } else if (first === "capabilities") {
    args.command = "capabilities";
  } else if (first === "sessions") {
    args.command = "sessions";
    if (positional[1] === "delete") args.sessionsAction = "delete";
    else if (positional[1] === "list") args.sessionsAction = "list";
    args.sessionsId = positional[2];
  } else if (first === "flows") {
    args.command = "flows";
    if (positional[1] === "recommend") args.flowsAction = "recommend";
    else args.flowsAction = "list";
  } else if (first === "flow") {
    args.command = "flow";
    args.query = positional.slice(1).join(" ") || undefined;
  } else if (first && first !== "acp") {
    args.command = "flow";
    args.query = positional.join(" ");
  }

  return args;
}

/** CLI 展示名：全局 bin 用实际命令名；tsx/node 直跑入口时展示产品名 nuwax-flow-ts。 */
function cliDisplayName(): string {
  const base = basename(process.argv[1] ?? "");
  if (base === "index.ts" || base === "index.js") return "nuwax-flow-ts";
  return base || "nuwax-flow-ts";
}

const HELP = `工作流编排模板（nuwax-flow-ts）

用法:
  ${cliDisplayName()}                启动 ACP 服务（默认，stdio）
  ${cliDisplayName()} acp            同上
  ${cliDisplayName()} flow "<输入>"  命令行跑一次默认 flow
  ${cliDisplayName()} flow -i        交互模式
  ${cliDisplayName()} graph          导出默认图拓扑（JSON；加 --mermaid 出 Mermaid 源）
  ${cliDisplayName()} capabilities   输出能力分层 + 可用工具/MCP/skills（无凭证）
  ${cliDisplayName()} sessions       列出已持久化的会话（thread id）
  ${cliDisplayName()} sessions delete <id>  删除某个已持久化会话
  ${cliDisplayName()} flows --json   输出注册 flow 的交互形态画像
  ${cliDisplayName()} flows recommend --kind chat|pipeline|approval

默认图经 StatefulFlow 运行（checkpointer 多轮记忆 + 可选 HITL interrupt/resume）。

选项:
  --config <path>   指定配置文件（默认 config/flow-agent.config.json）
  --debug           调试日志
  -h, --help        显示帮助
`;

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write(HELP);
    return;
  }

  // graph：导出图拓扑（静态，不运行图、不需要凭证）——对接可视化 / 文档。
  if (args.command === "graph") {
    const { raw } = loadFlowConfig({ configPath: args.configPath });
    const selection = resolveFlowSelection(raw);
    const { nodes, edges, mermaid } = await resolveFlow(selection.active, {
      unknownActivePolicy: selection.unknownActivePolicy,
    }).getTopology();
    process.stdout.write(
      args.mermaid ? mermaid + "\n" : JSON.stringify({ nodes, edges }, null, 2) + "\n"
    );
    return;
  }

  // capabilities / sessions / flows：静态查询（不加载 MCP、不需凭证）。
  if (args.command === "capabilities") {
    await runCapabilities();
    return;
  }
  if (args.command === "sessions") {
    await runSessions({ action: args.sessionsAction, id: args.sessionsId });
    return;
  }
  if (args.command === "flows") {
    const rows = args.flowsAction === "recommend"
      ? recommendFlows(args.flowsKind ?? "chat")
      : listFlowProfiles();
    if (args.flowsJson || args.flowsAction === "recommend") {
      process.stdout.write(JSON.stringify(rows, null, 2) + "\n");
    } else {
      for (const row of rows) {
        const flags = [
          row.isDefault ? "default" : "",
          row.profile.defaultForAmbiguous ? "ambiguous-default" : "",
          row.profile.requiresGraphReason ? "requires-graph-reason" : "",
        ].filter(Boolean).join(", ");
        process.stdout.write(`${row.name}\t${row.profile.interaction}\t${row.profile.userLabel}\t${flags}\n`);
      }
    }
    return;
  }

  // ACP 模式下凭证由平台宿主注入；dotenv 仅作本地兜底。
  loadDotenv();

  // server 身份用一次轻量配置解析（loadFlowConfig 不触 MCP；MCP 在 createFlowRuntime
  // 内按 session 加载）。
  const baseConfig = loadFlowConfig({ configPath: args.configPath });
  // 设全局 agent 名（log 文件名前缀），早于任何 session log。
  setLogAgent(baseConfig.appConfig.agent.name);

  if (args.command === "flow") {
    // CLI：单 runtime；可用 ACP_SESSION_CONFIG_JSON 模拟 host 下发的 mcpServers（与 ACP 会话合并逻辑一致）。
    const sessionConfig = loadSessionConfigFromEnv();
    const workspaceRoot = sessionConfig?.cwd ?? process.cwd();
    const { appConfig, raw } = loadFlowConfig({
      configPath: args.configPath,
      workspaceRoot,
      sessionConfig,
    });
    const selection = resolveFlowSelection(raw);
    const flowDef = resolveFlow(selection.active, {
      unknownActivePolicy: selection.unknownActivePolicy,
    });
    const runtime = await createFlowRuntime(appConfig, {
      sessionConfig,
      workspaceRoot,
      platformToolRefs: flowDef.platformToolRefs,
    });
    const executor = materializeFlow(flowDef, runtime);
    await runFlowCli(executor, {
      query: args.query,
      interactive: args.interactive,
    });
    await destroyRuntimeContext(runtime.ctx).catch(() => {});
    return;
  }

  // ACP（默认）模式：per-session 工厂。每个 ACP session 按 session/new 下发的
  // cwd / mcpServers / model 装配**独立** runtime（ACP 最高优先级，见 loadConfig 第 6 层）；
  // onSessionClosed 时经 dispose 释放该 session 的 MCP stdio 子进程。
  await bootstrapFlowAcp({
    appConfig: baseConfig.appConfig,
    debug: args.debug,
    createExecutor: async ({ sessionConfig, workspaceRoot, sessionId, sessionPhase }) => {
      const { appConfig, raw } = loadFlowConfig({
        configPath: args.configPath,
        workspaceRoot,
        sessionConfig,
      });
      const selection = resolveFlowSelection(raw);
      const flowDef = resolveFlow(selection.active, {
        unknownActivePolicy: selection.unknownActivePolicy,
      });
      const runtime = await createFlowRuntime(appConfig, {
        sessionConfig,
        workspaceRoot,
        platformToolRefs: flowDef.platformToolRefs,
        sessionId,
        sessionPhase,
      });
      return {
        executor: materializeFlow(flowDef, runtime),
        dispose: async () => {
          await destroyRuntimeContext(runtime.ctx).catch(() => {
            /* best-effort teardown of MCP stdio procs */
          });
        },
      };
    },
  });
}

const isMainModule =
  typeof process.argv[1] === "string" &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href;

if (isMainModule) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
