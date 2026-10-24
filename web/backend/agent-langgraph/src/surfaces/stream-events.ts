/**
 * Surface Stream Events —— LangGraph 多模式 stream chunk 归一后的 surface 内部事件。
 *
 * 用途：ACP / CLI 不再各自解析原生 stream 事件，统一消费这组归一结构。
 * 这是 surface 内部薄契约，不是新的执行抽象（执行仍是 graph.stream）。
 */

/** ACP plan entry —— 与 deepagents-acp PlanEntry 对齐。 */
export interface SurfacePlanEntry {
  content: string;
  priority?: "high" | "medium" | "low";
  status: "pending" | "in_progress" | "completed" | "skipped";
}

export type SurfaceStreamEvent =
  /** 模型可见回答增量（messages mode 的 content，或 custom writer）。 */
  | { type: "text"; text: string }
  /**
   * 模型思考增量（Anthropic content thinking block 或 additional_kwargs.reasoning_content）。
   * 必须与 text 分流：ACP 走 agent_thought_chunk，不得并入 agent_message_chunk。
   */
  | { type: "thought"; text: string }
  /** 研究大纲 / 任务清单（custom writer → ACP sessionUpdate: plan）。 */
  | { type: "plan"; entries: SurfacePlanEntry[] }
  /** 工具开始（tools mode 的 ToolNode，或 custom mode 自定义检索）。 */
  | { type: "tool_start"; id: string; name: string; input?: unknown }
  /** 工具完成/失败。name 可选：tools mode 的 on_tool_end 通常带工具名；缺失时下游回退 id。 */
  | {
      type: "tool_update";
      id: string;
      name?: string;
      status: "completed" | "failed";
      output?: unknown;
      error?: string;
    }
  /** durable stateful flow 阶段进度（custom mode 的 config.writer({type:"stage"})）。 */
  | { type: "stage"; stage: string; index?: number; total?: number; detail?: string }
  /** HITL 暂停（updates mode 的 __interrupt__）。 */
  | { type: "interrupt"; question: string };
