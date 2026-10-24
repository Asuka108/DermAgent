import { describe, expect, it, vi } from "vitest";
import { AIMessage, AIMessageChunk, HumanMessage } from "@langchain/core/messages";
import { FakeStreamingChatModel } from "@langchain/core/utils/testing";
import {
  Annotation,
  END,
  MemorySaver,
  START,
  StateGraph,
} from "@langchain/langgraph";
import type { StructuredTool } from "@langchain/core/tools";
import { AppConfigSchema } from "../src/runtime/index.js";
import { FlowStateAnnotation, type FlowState } from "../src/app/state.js";
import { createFlowGraph } from "../src/app/graph.js";
import {
  createTaskTool,
  extractSubagentTaskOutput,
  parseSubagentModelOverride,
} from "../src/app/task.tool.js";
import { createToolExecNode } from "../src/libs/nodes/tools.js";
import type { FlowCallbacks, PlanEvent } from "../src/core/flow-types.js";

vi.mock("../src/app/graph.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/app/graph.js")>();
  return {
    ...actual,
    createFlowGraph: vi.fn(actual.createFlowGraph),
  };
});

const baseConfig = AppConfigSchema.parse({
  model: { provider: "openai", name: "deepseek-chat" },
});

describe("parseSubagentModelOverride", () => {
  it("plain model name inherits parent provider", () => {
    const result = parseSubagentModelOverride("gpt-4o-mini", baseConfig);
    expect("error" in result).toBe(false);
    if ("config" in result) {
      expect(result.config.model.provider).toBe("openai");
      expect(result.config.model.name).toBe("gpt-4o-mini");
    }
  });

  it("provider/model overrides provider and model together", () => {
    const result = parseSubagentModelOverride("anthropic/claude-sonnet-4-6", baseConfig);
    expect("error" in result).toBe(false);
    if ("config" in result) {
      expect(result.config.model.provider).toBe("anthropic");
      expect(result.config.model.name).toBe("claude-sonnet-4-6");
      expect(result.config.model.apiKeyEnv).toBe("ANTHROPIC_API_KEY");
    }
  });

  it("rejects unsupported provider prefixes", () => {
    const result = parseSubagentModelOverride("google/gemini-pro", baseConfig);
    expect(result).toEqual({
      error: "不支持的 model provider，当前仅支持 anthropic/openai: google/gemini-pro",
    });
  });
});

describe("extractSubagentTaskOutput", () => {
  it("prefers output then scans all AI messages then streamBuffer", () => {
    expect(
      extractSubagentTaskOutput({
        output: "from respond",
        messages: [new AIMessage({ content: "ignored" })],
      })
    ).toBe("from respond");

    expect(
      extractSubagentTaskOutput({
        output: "",
        messages: [
          new AIMessage({ content: "first" }),
          new AIMessage({ content: "", tool_calls: [{ id: "c1", name: "bash", args: {} }] }),
        ],
      })
    ).toBe("first");

    expect(
      extractSubagentTaskOutput(
        { output: "", messages: [new AIMessage({ content: "", tool_calls: [{ id: "c1", name: "bash", args: {} }] })] },
        "streamed text"
      )
    ).toBe("streamed text");
  });
});

describe("createTaskTool", () => {
  it("子智能体 token 仅经 source 回调发送一次，不再向父图 messages 重复冒泡", async () => {
    const text = "好的，。";
    const model = new FakeStreamingChatModel({
      sleep: 0,
      chunks: [...text].map((content) => new AIMessageChunk({ content })),
    });
    const childGraph = new StateGraph(FlowStateAnnotation)
      .addNode("think", async (state: FlowState) => {
        const message = await model.invoke([new HumanMessage(state.input)]);
        return { messages: [message], output: text };
      })
      .addEdge(START, "think")
      .addEdge("think", END)
      .compile({ checkpointer: new MemorySaver() });
    vi.mocked(createFlowGraph).mockReturnValueOnce(
      childGraph as ReturnType<typeof createFlowGraph>
    );

    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [],
      subAgents: [
        {
          name: "视觉设计师",
          description: "视觉设计",
          systemPrompt: "负责视觉设计。",
        },
      ],
    });

    const ParentState = Annotation.Root({
      output: Annotation<string>,
    });
    const delivered: Array<{ route: "source" | "parent"; text: string; source?: string }> = [];
    const parentGraph = new StateGraph(ParentState)
      .addNode("task", async (_state, config) => {
        const output = await task.invoke(
          { subagent_type: "视觉设计师", description: "设计海报" },
          config
        );
        return { output: String(output) };
      })
      .addEdge(START, "task")
      .addEdge("task", END)
      .compile();

    const stream = await parentGraph.stream(
      {},
      {
        configurable: {
          onToken: (token: string, source?: string) => {
            delivered.push({ route: "source", text: token, source });
          },
        },
        streamMode: ["messages", "updates"],
      }
    );
    for await (const raw of stream) {
      if (!Array.isArray(raw) || raw[0] !== "messages") continue;
      const token = raw[1]?.[0]?.content;
      if (typeof token === "string" && token) {
        delivered.push({ route: "parent", text: token });
      }
    }

    expect(delivered.map((event) => event.text).join("")).toBe(text);
    expect(delivered.filter((event) => event.route === "parent")).toEqual([]);
    expect(
      new Set(
        delivered
          .filter((event) => event.route === "source")
          .map((event) => event.source)
      )
    ).toEqual(new Set(["视觉设计师"]));
  });

  it("rejects subagent tools that are not available", async () => {
    const tool = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [{ name: "read_file" } as StructuredTool],
      subAgents: [
        {
          name: "researcher",
          description: "Researcher",
          systemPrompt: "You research.",
          tools: ["missing_tool"],
        },
      ],
    });

    const result = await tool.invoke({
      subagent_type: "researcher",
      description: "test",
    });

    expect(result).toContain('Error: subagent "researcher" 配置了未知工具: missing_tool');
  });

  it("streamBuffer 兜底：末条 AI 仅 tool_calls 时仍返回已流式文本", async () => {
    const streamed = "品牌策略结论";
    const model = new FakeStreamingChatModel({
      sleep: 0,
      chunks: [...streamed].map((content) => new AIMessageChunk({ content })),
    });
    const childGraph = new StateGraph(FlowStateAnnotation)
      .addNode("think", async (state: FlowState) => {
        const message = await model.invoke([new HumanMessage(state.input)]);
        return {
          messages: [
            message,
            new AIMessage({
              content: "",
              tool_calls: [{ id: "c1", name: "bash", args: { command: "echo hi" } }],
            }),
          ],
          output: "",
        };
      })
      .addEdge(START, "think")
      .addEdge("think", END)
      .compile({ checkpointer: new MemorySaver() });
    vi.mocked(createFlowGraph).mockReturnValueOnce(
      childGraph as ReturnType<typeof createFlowGraph>
    );

    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [],
      subAgents: [
        {
          name: "brand-strategist",
          description: "品牌",
          systemPrompt: "你是品牌策略师。",
        },
      ],
    });

    const result = await task.invoke({
      subagent_type: "brand-strategist",
      description: "写策略",
    });

    expect(result).toBe(streamed);
    expect(result).not.toContain("(subagent 无输出)");
  });

  it("默认继承聚合 MCP 工具并注入搜索委派约定", async () => {
    let capturedSystemPrompt = "";
    let capturedToolNames: string[] = [];
    const childGraph = new StateGraph(FlowStateAnnotation)
      .addNode("think", async (state: FlowState) => ({
        messages: [new AIMessage({ content: `done:${state.input}` })],
        output: `done:${state.input}`,
      }))
      .addEdge(START, "think")
      .addEdge("think", END)
      .compile({ checkpointer: new MemorySaver() });

    vi.mocked(createFlowGraph).mockImplementationOnce((cfg) => {
      capturedSystemPrompt = cfg.systemPrompt ?? "";
      capturedToolNames = (cfg.allTools ?? []).map((t) => t.name);
      return childGraph as ReturnType<typeof createFlowGraph>;
    });

    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () =>
        [
          { name: "read_file" },
          { name: "platform__web_search" },
          { name: "write_todos" },
        ] as StructuredTool[],
      subAgents: [
        {
          name: "copywriter",
          description: "文案",
          systemPrompt: "你是文案专家。",
        },
      ],
    });

    const result = await task.invoke({
      subagent_type: "copywriter",
      description: "写标题",
    });

    expect(result).toBe("done:写标题");
    expect(capturedToolNames).toEqual([
      "read_file",
      "platform__web_search",
      "write_todos",
    ]);
    expect(capturedSystemPrompt).toContain("你是文案专家。");
    expect(capturedSystemPrompt).toContain("非空最终结论");
    expect(capturedSystemPrompt).toContain("搜索 MCP");
    expect(capturedSystemPrompt).toContain("write_todos");
    expect(capturedSystemPrompt).not.toContain("不要调用联网搜索或 MCP");
  });

  it("onToken 透传 toolCallId（来自 configurable.langgraph_tool_call_id）", async () => {
    const text = "ok";
    const model = new FakeStreamingChatModel({
      sleep: 0,
      chunks: [...text].map((content) => new AIMessageChunk({ content })),
    });
    const childGraph = new StateGraph(FlowStateAnnotation)
      .addNode("think", async (state: FlowState) => {
        const message = await model.invoke([new HumanMessage(state.input)]);
        return { messages: [message], output: text };
      })
      .addEdge(START, "think")
      .addEdge("think", END)
      .compile({ checkpointer: new MemorySaver() });
    vi.mocked(createFlowGraph).mockReturnValueOnce(
      childGraph as ReturnType<typeof createFlowGraph>
    );

    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [],
      subAgents: [
        {
          name: "analyst",
          description: "分析",
          systemPrompt: "分析。",
        },
      ],
    });

    const tokens: Array<{ source?: string; toolCallId?: string }> = [];
    await task.invoke(
      { subagent_type: "analyst", description: "分析数据" },
      { configurable: { langgraph_tool_call_id: "call_abc123", onToken: (_t, source, toolCallId) => {
        tokens.push({ source, toolCallId });
      } } }
    );

    expect(tokens.length).toBeGreaterThan(0);
    expect(tokens[0]?.source).toBe("analyst");
    expect(tokens[0]?.toolCallId).toBe("call_abc123");
  });

  it("standalone invoke 无 langgraph_tool_call_id 时回退为完整 UUID", async () => {
    const text = "ok";
    const model = new FakeStreamingChatModel({
      sleep: 0,
      chunks: [...text].map((content) => new AIMessageChunk({ content })),
    });
    const childGraph = new StateGraph(FlowStateAnnotation)
      .addNode("think", async (state: FlowState) => {
        const message = await model.invoke([new HumanMessage(state.input)]);
        return { messages: [message], output: text };
      })
      .addEdge(START, "think")
      .addEdge("think", END)
      .compile({ checkpointer: new MemorySaver() });
    vi.mocked(createFlowGraph).mockReturnValue(childGraph as ReturnType<typeof createFlowGraph>);

    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [],
      subAgents: [
        {
          name: "brand-strategist",
          description: "品牌",
          systemPrompt: "品牌。",
        },
      ],
    });

    const uuidRe =
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    const toolCallIds: string[] = [];
    const onToken = (_t: string, _source?: string, toolCallId?: string) => {
      if (toolCallId) toolCallIds.push(toolCallId);
    };

    await task.invoke(
      { subagent_type: "brand-strategist", description: "任务 A" },
      { configurable: { onToken } }
    );
    await task.invoke(
      { subagent_type: "brand-strategist", description: "任务 B" },
      { configurable: { onToken } }
    );

    expect(new Set(toolCallIds)).toHaveLength(2);
    for (const id of new Set(toolCallIds)) {
      expect(id).toMatch(uuidRe);
    }
  });

  it("write_todos 的 plan 更新附带 subagent 来源和父 task id", async () => {
    const childGraph = new StateGraph(FlowStateAnnotation)
      .addNode("think", async (state: FlowState, config) => {
        const onPlan = config?.configurable?.onPlan as
          | FlowCallbacks["onPlan"]
          | undefined;
        await onPlan?.({
          entries: [{ content: "检索资料", status: "in_progress" }],
        });
        return {
          messages: [new AIMessage({ content: "done" })],
          output: `done:${state.input}`,
        };
      })
      .addEdge(START, "think")
      .addEdge("think", END)
      .compile({ checkpointer: new MemorySaver() });
    vi.mocked(createFlowGraph).mockReturnValueOnce(
      childGraph as ReturnType<typeof createFlowGraph>
    );

    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [],
      subAgents: [
        {
          name: "researcher",
          description: "研究",
          systemPrompt: "研究。",
        },
      ],
    });
    const plans: PlanEvent[] = [];

    await task.invoke(
      { subagent_type: "researcher", description: "调研" },
      {
        configurable: {
          langgraph_tool_call_id: "task-plan-1",
          onPlan: (event: PlanEvent) => plans.push(event),
        },
      }
    );

    expect(plans).toEqual([
      {
        entries: [{ content: "检索资料", status: "in_progress" }],
        source: "researcher",
        toolCallId: "task-plan-1",
      },
    ]);
  });

  it("向子图透传父级工具审批回调", async () => {
    const childGraph = new StateGraph(FlowStateAnnotation)
      .addNode("think", async () => ({
        messages: [new AIMessage({ content: "done" })],
        output: "done",
      }))
      .addEdge(START, "think")
      .addEdge("think", END)
      .compile({ checkpointer: new MemorySaver() });
    let capturedPermission: FlowCallbacks["onPermissionRequest"];
    vi.mocked(createFlowGraph).mockImplementationOnce((cfg) => {
      capturedPermission = cfg.callbacks?.onPermissionRequest;
      return childGraph as ReturnType<typeof createFlowGraph>;
    });
    const onPermissionRequest: NonNullable<FlowCallbacks["onPermissionRequest"]> =
      async () => "allow";
    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [],
      subAgents: [
        {
          name: "worker",
          description: "执行",
          systemPrompt: "执行。",
        },
      ],
    });

    await task.invoke(
      { subagent_type: "worker", description: "执行任务" },
      { configurable: { onPermissionRequest } }
    );

    expect(capturedPermission).toBe(onPermissionRequest);
  });

  it("并行 task 从 ToolRuntime 读取各自真实 toolCallId", async () => {
    const childGraph = new StateGraph(FlowStateAnnotation)
      .addNode("think", async (state: FlowState) => ({
        messages: [new AIMessage({ content: `done:${state.input}` })],
        output: `done:${state.input}`,
      }))
      .addEdge(START, "think")
      .addEdge("think", END)
      .compile({ checkpointer: new MemorySaver() });
    vi.mocked(createFlowGraph).mockImplementation(
      () => childGraph as ReturnType<typeof createFlowGraph>
    );
    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [],
      subAgents: [
        {
          name: "researcher",
          description: "研究",
          systemPrompt: "研究。",
        },
      ],
    });
    const exec = createToolExecNode<{ messages: AIMessage[] }>({ tools: [task] });
    const tokens: Array<{ text: string; toolCallId?: string }> = [];

    await exec(
      {
        messages: [
          new AIMessage({
            content: "",
            tool_calls: [
              {
                id: "task-call-a",
                name: "task",
                args: { subagent_type: "researcher", description: "A" },
              },
              {
                id: "task-call-b",
                name: "task",
                args: { subagent_type: "researcher", description: "B" },
              },
            ],
          }),
        ],
      },
      {
        configurable: {
          onToken: (text: string, _source?: string, toolCallId?: string) => {
            tokens.push({ text, toolCallId });
          },
        },
      }
    );

    expect(new Set(tokens.map((token) => token.toolCallId))).toEqual(
      new Set(["task-call-a", "task-call-b"])
    );
  });

  it("messages 模式累积 chunk → onToken 折叠无叠词", async () => {
    const childGraph = {
      stream: async function* () {
        yield ["messages", [{ content: "你" }, { langgraph_node: "think" }]];
        yield ["messages", [{ content: "你好" }, { langgraph_node: "think" }]];
        yield ["messages", [{ content: "你好世界" }, { langgraph_node: "think" }]];
      },
      getState: async () => ({
        values: {
          messages: [new AIMessage({ content: "你好世界" })],
          output: "你好世界",
        },
      }),
    };
    vi.mocked(createFlowGraph).mockReturnValueOnce(
      childGraph as ReturnType<typeof createFlowGraph>
    );

    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [],
      subAgents: [
        {
          name: "researcher",
          description: "研究",
          systemPrompt: "研究。",
        },
      ],
    });
    const tokens: string[] = [];
    await task.invoke(
      { subagent_type: "researcher", description: "test" },
      {
        configurable: {
          langgraph_tool_call_id: "call_fold",
          onToken: (text: string) => tokens.push(text),
        },
      }
    );

    expect(tokens).toEqual(["你", "好", "世界"]);
  });

  it("Anthropic thinking 走 onThought 并透传 subagent 来源与父 task id", async () => {
    const childGraph = {
      stream: async function* () {
        yield [
          "messages",
          [
            { content: [{ type: "thinking", thinking: "先" }] },
            { langgraph_node: "think" },
          ],
        ];
        yield [
          "messages",
          [
            { content: [{ type: "thinking", thinking: "先分析" }] },
            { langgraph_node: "think" },
          ],
        ];
        yield ["messages", [{ content: "完成" }, { langgraph_node: "think" }]];
      },
      getState: async () => ({
        values: {
          messages: [new AIMessage({ content: "完成" })],
          output: "完成",
        },
      }),
    };
    vi.mocked(createFlowGraph).mockReturnValueOnce(
      childGraph as ReturnType<typeof createFlowGraph>
    );

    const task = createTaskTool({
      config: baseConfig,
      parentWorkspaceRoot: process.cwd(),
      buildTools: () => [],
      subAgents: [
        {
          name: "analyst",
          description: "分析",
          systemPrompt: "分析。",
        },
      ],
    });
    const thoughts: Array<{ text: string; source?: string; toolCallId?: string }> = [];

    await task.invoke(
      { subagent_type: "analyst", description: "分析数据" },
      {
        configurable: {
          langgraph_tool_call_id: "task-thought-1",
          onThought: (text: string, source?: string, toolCallId?: string) => {
            thoughts.push({ text, source, toolCallId });
          },
        },
      }
    );

    expect(thoughts).toEqual([
      { text: "先", source: "analyst", toolCallId: "task-thought-1" },
      { text: "分析", source: "analyst", toolCallId: "task-thought-1" },
    ]);
  });
});
