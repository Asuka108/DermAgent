/**
 * task 工具 —— 把子任务委派给一个声明式 subagent（.agents/agents/<name>/AGENT.md）。
 *
 * 框架原生实现：每个子智能体（subagent）复用默认 ReAct 图（createFlowGraph）单次 invoke——
 * 自带 systemPrompt（AGENT.md 正文）、工具子集、独立工作目录、可选独立模型。
 * LangGraph 无「声明式子智能体（subagent）」原生概念，故参考 deepagents 的 `task` 委派语义
 *（对齐 LangChain 文档 Subagents-as-tools / Supervisor 模式）。
 *
 * 防递归：子智能体拿到的工具集**不含 `task` 本身**（由 createFlowTools 的 buildTools 保证）。
 * 工作目录：默认 = 父级 workspaceRoot（启动 agent 的当前目录）；AGENT.md `workdir` 指定相对子目录。
 * 临时态：用图默认 MemorySaver，不落父会话 checkpointer。
 */

import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import type { StructuredTool } from "@langchain/core/tools";
import type { AppConfig, DiscoveredSubAgent } from "../runtime/index.js";
import { createFlowGraph } from "./graph.js";
import type { FlowState } from "./state.js";
import {
  extractText,
  extractThoughtTextFromMessage,
  STREAM_TEXT_NODES,
  foldStreamTextChunk,
} from "../libs/nodes/index.js";
import type { FlowCallbacks } from "../core/flow-types.js";

/** 兼容 LangChain 实例与 checkpoint 反序列化后的 plain object。 */
function messageRole(msg: unknown): string | undefined {
  if (!msg || typeof msg !== "object") return undefined;
  const m = msg as { _getType?: () => string; type?: string };
  return m._getType?.() ?? m.type;
}

/** 框架级委派约定：追加到 AGENT.md 正文后，无需逐个改 builtin/agents。 */
function subagentDelegationSuffix(tools: StructuredTool[]): string {
  const todoInstruction = tools.some((tool) => tool.name === "write_todos")
    ? "复杂多步骤任务应使用 write_todos 维护完整待办快照并及时更新状态，简单任务不要创建待办。"
    : "";
  return `\n\n完成任务后必须用自然语言给出非空最终结论；如任务需要联网或实时资料，可调用当前工具列表中已授权的搜索 MCP 工具，不得臆造或调用未提供的工具。${todoInstruction}`;
}

/**
 * 从子图终态提取 task 返回值。
 * 优先级：respond 写入的 output → 倒序扫描所有 AIMessage 末条非空文本 → streamBuffer。
 */
export function extractSubagentTaskOutput(
  state: Pick<FlowState, "output" | "messages">,
  streamBuffer?: string
): string {
  if (state.output?.trim()) return state.output.trim();

  const msgs = state.messages ?? [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (messageRole(msgs[i]) !== "ai") continue;
    const text = extractText((msgs[i] as { content?: unknown }).content).trim();
    if (text) return text;
  }

  return streamBuffer?.trim() ?? "";
}

/** 从 LangGraph tool invoke 的 runConfig 解析当前 tool_call_id（供 ACP messageId 分桶）。 */
function resolveTaskToolCallId(
  runConfig: unknown,
  fallbackId: string
): string {
  const cfg = runConfig as
    | {
        configurable?: Record<string, unknown>;
        metadata?: Record<string, unknown>;
        toolCallId?: unknown;
        toolCall?: { id?: unknown };
      }
    | undefined;
  const fromRuntime = cfg?.toolCallId ?? cfg?.toolCall?.id;
  if (typeof fromRuntime === "string" && fromRuntime) {
    return fromRuntime;
  }
  const fromConfigurable = cfg?.configurable?.langgraph_tool_call_id;
  if (typeof fromConfigurable === "string" && fromConfigurable) {
    return fromConfigurable;
  }
  const fromMetadata = cfg?.metadata?.tool_call_id;
  if (typeof fromMetadata === "string" && fromMetadata) {
    return fromMetadata;
  }
  // 并行多 task 或 standalone invoke：用本轮生成的完整 UUID，勿解析 threadId（子 agent 名可含连字符）。
  return fallbackId;
}

export interface TaskToolDeps {
  subAgents: DiscoveredSubAgent[];
  config: AppConfig;
  /** 父 agent 的工作目录（子智能体未指定 workdir 时继承）。 */
  parentWorkspaceRoot: string;
  /** 按工作目录重建子智能体工具集（不含 task，防递归）。 */
  buildTools: (workspaceRoot: string) => StructuredTool[];
}

export function parseSubagentModelOverride(
  spec: string | undefined,
  config: AppConfig
): { config: AppConfig } | { error: string } {
  if (!spec) return { config };

  // 兼容简单模型名；也支持 "provider/model" 或 "provider:model" 显式覆盖 provider。
  const match = spec.match(/^(anthropic|openai)[/:](.+)$/);
  if (!match) {
    if (/^[a-zA-Z]+[/:]/.test(spec)) {
      return {
        error: `不支持的 model provider，当前仅支持 anthropic/openai: ${spec}`,
      };
    }
    return { config: { ...config, model: { ...config.model, name: spec } } };
  }

  const provider = match[1] as AppConfig["model"]["provider"];
  const name = match[2]?.trim();
  if (!name) return { error: `model 覆盖缺少模型名: ${spec}` };

  const inherited =
    provider === config.model.provider
      ? config.model
      : {
          ...config.model,
          provider,
          apiKeyEnv: provider === "openai" ? "OPENAI_API_KEY" : "ANTHROPIC_API_KEY",
          authTokenEnv:
            provider === "anthropic" ? config.model.authTokenEnv : "ANTHROPIC_AUTH_TOKEN",
          baseUrl: undefined,
        };

  return { config: { ...config, model: { ...inherited, provider, name } } };
}

export function createTaskTool(deps: TaskToolDeps) {
  const { subAgents, config, parentWorkspaceRoot, buildTools } = deps;
  const byName = new Map(subAgents.map((a) => [a.name, a]));
  const names = subAgents.map((a) => a.name).join(", ") || "(none)";

  return tool(
    async ({ subagent_type, description }, runConfig) => {
      const agent = byName.get(subagent_type);
      if (!agent) return `Error: 未找到 subagent "${subagent_type}"。可用: ${names}`;

      // 工作目录：默认继承父级；AGENT.md workdir 覆盖为相对子目录。
      const subRoot = agent.workdir
        ? resolve(parentWorkspaceRoot, agent.workdir)
        : parentWorkspaceRoot;

      // 工具：按子智能体工作目录重建（沙箱受限于 subRoot）；AGENT.md tools 进一步收窄为 allowlist。
      let tools = buildTools(subRoot);
      if (agent.tools?.length) {
        const allow = new Set(agent.tools);
        const available = new Set(tools.map((t) => t.name));
        const unknown = agent.tools.filter((name) => !available.has(name));
        if (unknown.length) {
          return `Error: subagent "${subagent_type}" 配置了未知工具: ${unknown.join(", ")}。`;
        }
        tools = tools.filter((t) => allow.has(t.name));
        if (!tools.length) {
          return `Error: subagent "${subagent_type}" 的 tools allowlist 为空，无法执行任务。`;
        }
      }

      // 模型：缺省继承父 agent；AGENT.md model 可写模型名或 provider/model。
      const modelOverride = parseSubagentModelOverride(agent.model, config);
      if ("error" in modelOverride) return `Error: ${modelOverride.error}`;
      const subConfig = modelOverride.config;

      const subagentSystemPrompt =
        `${agent.systemPrompt.trimEnd()}${subagentDelegationSuffix(tools)}`;

      // 流式委派：subagent 的工具调用经 callbacks.onToolCall（带 [subagent] 前缀）实时透出；
      // LLM token 经 graph.stream messages 模式逐个 onToken；最终 output 取终态（替代原 invoke）。
      // parentCallbacks 提到 try 外：finally（subagent 结束边界 onStage）也引用它，
      // const 块作用域否则在 finally 失效（tsc TS2304 + 运行时 ReferenceError）。
      const parentCallbacks = ((runConfig as { configurable?: Record<string, unknown> } | undefined)
        ?.configurable ?? {}) as Partial<FlowCallbacks>;
      const subagentRunId = randomUUID();
      const threadId = `subagent-${subagent_type}-${subagentRunId}`;
      const toolCallId = resolveTaskToolCallId(runConfig, subagentRunId);

      try {
        const wrapToolCall: FlowCallbacks["onToolCall"] = async (e) => {
          await parentCallbacks.onToolCall?.({
            ...e,
            toolName: `[${subagent_type}] ${e.toolName}`,
          });
        };
        const wrapPlan: FlowCallbacks["onPlan"] = async (e) => {
          await parentCallbacks.onPlan?.({
            entries: e.entries,
            source: subagent_type,
            toolCallId,
          });
        };
        const graph = createFlowGraph({
          allTools: tools,
          config: subConfig,
          systemPrompt: subagentSystemPrompt,
          callbacks: {
            onToolCall: wrapToolCall,
            onPlan: wrapPlan,
            onPermissionRequest: parentCallbacks.onPermissionRequest,
          },
        });
        // 透传父级 cancel signal（ACP cancel）给 subagent，避免父级取消时 subagent 仍跑完整轮。
        const parentSignal = (runConfig as { signal?: AbortSignal } | undefined)?.signal;
        // subagent 开始边界（surface 经此知道 subagent 生命周期；后续 token 带 source + toolCallId）。
        await parentCallbacks.onStage?.({
          stage: `委派 subagent: ${subagent_type}`,
          index: 1,
          total: 1,
          detail: description.slice(0, 100),
        });
        let streamBuffer = "";
        let thoughtStreamBuffer = "";
        const stream = await graph.stream(
          { input: description, messages: [] } as unknown as FlowState,
          {
            // 子图自己会为 messages stream 安装监听器；显式隔离父图 callbacks，避免同一
            // token 同时经父图 messages 冒泡和下方 parentCallbacks.onToken 重复发送。
            callbacks: [],
            configurable: {
              thread_id: threadId,
              // write_todos 从 ToolRuntime.configurable 读取 onPlan；附带父 task id 由 wrapPlan 分桶。
              onPlan: wrapPlan,
            },
            recursionLimit: 50,
            streamMode: ["messages"],
            ...(parentSignal ? { signal: parentSignal } : {}),
          }
        );
        for await (const raw of stream) {
          // 多模式 chunk = [mode, payload]；messages 模式 payload = [messageChunk, metadata]
          if (!Array.isArray(raw) || raw[0] !== "messages") continue;
          const pair = raw[1] as
            | [{ content?: unknown }, { langgraph_node?: string }]
            | undefined;
          if (!Array.isArray(pair)) continue;
          const [msg, meta] = pair;
          const node = meta?.langgraph_node;
          if (node && STREAM_TEXT_NODES.has(node)) {
            const thought = extractThoughtTextFromMessage(msg);
            if (thought) {
              const folded = foldStreamTextChunk(thoughtStreamBuffer, thought);
              thoughtStreamBuffer = folded.full;
              if (folded.delta) {
                await parentCallbacks.onThought?.(
                  folded.delta,
                  subagent_type,
                  toolCallId
                );
              }
            }
            const text = extractText(msg?.content);
            if (text) {
              const folded = foldStreamTextChunk(streamBuffer, text);
              if (!folded.delta) continue;
              streamBuffer = folded.full;
              await parentCallbacks.onToken?.(folded.delta, subagent_type, toolCallId);
            }
          }
        }
        const finalState = (await graph.getState({ configurable: { thread_id: threadId } }))
          .values as FlowState;
        return extractSubagentTaskOutput(finalState, streamBuffer) || "(subagent 无输出)";
      } catch (err) {
        return `Error: subagent "${subagent_type}" 执行失败: ${err instanceof Error ? err.message : String(err)}`;
      } finally {
        // subagent 结束边界（成功/失败均发，与开始 onStage 对称，避免客户端「进行中」悬挂）。
        await parentCallbacks.onStage?.({
          stage: `subagent ${subagent_type} 完成`,
          index: 1,
          total: 1,
        });
      }
    },
    {
      name: "task",
      description: `把子任务委派给一个声明式子智能体（subagent，各自独立 prompt/工具/工作目录），返回其最终结果。可用 subagent_type: ${names}。`,
      schema: z.object({
        subagent_type: z.string().describe("子智能体名 subagent_type（见系统提示词 Subagents 列表）"),
        description: z.string().describe("交给子智能体（subagent）执行的完整任务说明（自包含，子智能体看不到主对话历史）"),
      }),
    }
  );
}
