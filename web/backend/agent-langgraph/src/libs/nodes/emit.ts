/**
 * surface 事件生产端 —— 在 LangGraph 节点内把阶段进度 / 结构化 Plan / 流式文本 token
 * 经 custom writer（streamMode:"custom"）发出去，或退回 configurable callback。
 *
 * 与消费端成对：
 *  - 生产端（本模块）：节点内 `config.writer({type:"stage"|...})`，或 `configurable.onStage/onPlan`；
 *  - 消费端：surfaces/map-stream-chunk 把 custom chunk 归一成 SurfaceStreamEvent，
 *    surfaces/dispatch-surface-event 再分发到 FlowCallbacks。
 * writer payload 形状与 map-stream-chunk 解析完全一致，改本模块不改消费行为。
 */

import type { StageEvent, PlanEvent } from "../../core/flow-types.js";

type StreamWriter = (payload: Record<string, unknown>) => void;

function getWriter(
  config: { writer?: StreamWriter } | undefined
): StreamWriter | undefined {
  return config?.writer;
}

/** 阶段进度：writer + onStage 双发。 */
export async function emitStage(
  config:
    | {
        writer?: StreamWriter;
        configurable?: { onStage?: (e: StageEvent) => void | Promise<void> };
      }
    | undefined,
  e: StageEvent
): Promise<void> {
  const writer = getWriter(config);
  if (writer) {
    writer({ type: "stage", ...e });
    return;
  }
  const onStage = config?.configurable?.onStage;
  if (onStage) await onStage(e);
}

/** 结构化 Plan：writer + onPlan 双发。 */
export async function emitPlan(
  config:
    | {
        writer?: StreamWriter;
        configurable?: { onPlan?: (e: PlanEvent) => void | Promise<void> };
      }
    | undefined,
  entries: PlanEvent["entries"]
): Promise<void> {
  if (!entries.length) return;
  const writer = getWriter(config);
  if (writer) {
    writer({ type: "plan", entries });
    return;
  }
  const onPlan = config?.configurable?.onPlan;
  if (onPlan) await onPlan({ entries });
}

/** 流式文本 token：优先 custom writer（streamMode:"custom"），否则退回 configurable.onToken（与 emitStage/emitPlan 一致）。 */
export async function emitTextToken(
  config:
    | {
        writer?: StreamWriter;
        configurable?: { onToken?: (text: string) => void | Promise<void> };
      }
    | undefined,
  text: string
): Promise<void> {
  if (!text) return;
  const writer = getWriter(config);
  if (writer) {
    writer({ type: "text", text });
    return;
  }
  const onToken = config?.configurable?.onToken;
  if (onToken) await onToken(text);
}

/**
 * 流式文本放行节点白名单：仅这些节点的 messages-mode 文本 token 透出给用户
 * （避免 RAG rewrite/grade/route 等中间决策节点的 token 泄漏）。默认 ReAct 的最终回答在
 * think 产生（respond 仅转存 output）——故 think 放行；工具决策轮 think 的 content 通常为空。
 *
 * 单一来源：surfaces/dispatch-surface-event（主图 messages 过滤）与 app/task.tool
 * （subagent messages 过滤）共用，避免两处副本漂移。
 */
export const STREAM_TEXT_NODES = new Set(["write_draft", "respond", "respondNode", "think"]);
