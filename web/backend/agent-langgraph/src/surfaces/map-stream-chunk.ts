/**
 * mapStreamChunk —— 把 LangGraph 多模式 stream 的一个 `[mode, chunk]` 归一成
 * `SurfaceStreamEvent[]`。纯函数，供 surface（ACP/CLI）消费；surface 不再各自
 * 解析原生事件。
 *
 * 执行仍是 `graph.stream(input, { streamMode: ["messages","tools","custom","updates"] })`，
 * `for await (const [mode, chunk] of stream) yield* mapStreamChunk(mode, chunk)`。
 *
 * chunk 形状依 LangGraph 版本；本实现基于 @langchain/langgraph 多模式约定：
 *  - `messages`: `[AIMessageChunk, metadata]` —— 文本增量（按 metadata.langgraph_node
 *    过滤回答节点，避免把规划/工具前置 LLM 的 token 吐给用户；过滤由调用方做，
 *    这里只抽文本）。
 *  - `custom`: 节点 `config.writer(payload)` —— payload.type 区分 stage / tool。
 *  - `updates`: `{ nodeName: delta }` —— 检测 `__interrupt__`（INTERRUPT）。
 *  - `tools`: ToolNode 生命周期 —— 形状待 spike（surface 接入时按当前版本补全）。
 */

import { INTERRUPT } from "@langchain/langgraph";
import type { SurfaceStreamEvent } from "./stream-events.js";
import {
  extractThoughtTextFromMessage,
  extractText,
  extractToolEndOutput,
} from "../libs/nodes/index.js";

export function mapStreamChunk(mode: string, chunk: unknown): SurfaceStreamEvent[] {
  const events: SurfaceStreamEvent[] = [];

  if (mode === "messages") {
    const pair = chunk as
      | [{ content?: unknown; additional_kwargs?: Record<string, unknown> }, unknown]
      | undefined;
    // 流式路径必须分流：content → text（可见回复），reasoning_content → thought（思考）。
    // 禁止把 reasoning 兜底进 text，否则会出现「思考+正文」拼进同一条 agent_message。
    // 终态空 content 的兜底留在 think 出口提升 / respond extractVisibleTextFromMessage。
    if (Array.isArray(pair) && pair[0]) {
      const text = extractText(pair[0].content);
      if (text) events.push({ type: "text", text });
      const thought = extractThoughtTextFromMessage(pair[0]);
      if (thought) events.push({ type: "thought", text: thought });
    }
    return events;
  }

  if (mode === "custom") {
    const payload = chunk as { type?: string } | undefined;
    if (!payload || typeof payload !== "object") return events;
    if (payload.type === "stage") {
      const s = payload as {
        stage: string;
        index?: number;
        total?: number;
        detail?: string;
      };
      events.push({
        type: "stage",
        stage: s.stage,
        ...(s.index !== undefined ? { index: s.index } : {}),
        ...(s.total !== undefined ? { total: s.total } : {}),
        ...(s.detail !== undefined ? { detail: s.detail } : {}),
      });
    } else if (payload.type === "plan") {
      const p = payload as {
        entries?: Array<{
          content: string;
          priority?: "high" | "medium" | "low";
          status: "pending" | "in_progress" | "completed" | "skipped";
        }>;
      };
      if (Array.isArray(p.entries) && p.entries.length) {
        events.push({ type: "plan", entries: p.entries });
      }
    } else if (payload.type === "text") {
      const t = payload as { text?: string };
      if (t.text) events.push({ type: "text", text: t.text });
    } else if (payload.type === "tool") {
      const t = payload as {
        id?: string;
        name?: string;
        status?: string;
        input?: unknown;
        output?: unknown;
        error?: string;
      };
      const id = t.id ?? t.name ?? "";
      if (t.status === "in_progress" && t.name) {
        events.push({ type: "tool_start", id, name: t.name, ...(t.input !== undefined ? { input: t.input } : {}) });
      } else if (t.status === "completed" || t.status === "failed") {
        events.push({
          type: "tool_update",
          id,
          ...(t.name ? { name: t.name } : {}),
          status: t.status,
          ...(t.output !== undefined ? { output: t.output } : {}),
          ...(t.error !== undefined ? { error: t.error } : {}),
        });
      }
    }
    return events;
  }

  if (mode === "updates") {
    const updates = chunk as Record<string, Record<string, unknown>> | undefined;
    if (updates && typeof updates === "object") {
      const candidates =
        INTERRUPT in updates
          ? [updates as Record<string, unknown>]
          : Object.values(updates);
      for (const delta of candidates) {
        if (!delta || typeof delta !== "object") continue;
        const intr = delta[INTERRUPT] as
          | Array<{ value?: { question?: string } | string }>
          | undefined;
        if (intr && intr.length) {
          const v = intr[intr.length - 1]?.value;
          const question =
            typeof v === "string" ? v : v?.question ?? String(v ?? "");
          events.push({ type: "interrupt", question });
        }
      }
    }
    return events;
  }

  if (mode === "tools") {
    // ToolNode 生命周期（spike 确认 @langchain/langgraph 多模式）：
    //   { event: "on_tool_start"|"on_tool_end", toolCallId, name, input<string>, output?<ToolMessage 序列化> }
    //   output 为 LangChain 序列化 ToolMessage：{ kwargs: { content, status:"success"|"error", ... } }
    // output 为 LangChain 序列化 ToolMessage（{kwargs:{content,status,...}}）；content 多路径取
    //（kwargs.content 标准 + output.content 兜底），实测完整 ReAct 下正常（不再 resultChars=2）。
    const e = chunk as {
      event?: string;
      toolCallId?: string;
      name?: string;
      input?: string;
      output?: { kwargs?: { content?: unknown; status?: string }; content?: unknown };
    } | undefined;
    if (!e || !e.event) return events;
    const id = e.toolCallId ?? e.name ?? "";
    if (e.event === "on_tool_start" && e.name) {
      let parsed: unknown = e.input;
      try {
        parsed = e.input ? JSON.parse(e.input) : undefined;
      } catch {
        parsed = e.input;
      }
      events.push({
        type: "tool_start",
        id,
        name: e.name,
        ...(parsed !== undefined ? { input: parsed } : {}),
      });
    } else if (e.event === "on_tool_end") {
      const k = e.output?.kwargs;
      const status: "completed" | "failed" = k?.status === "error" ? "failed" : "completed";
      const content = extractToolEndOutput(e.output);
      events.push({
        type: "tool_update",
        id,
        ...(e.name ? { name: e.name } : {}),
        status,
        ...(content !== undefined ? { output: content } : {}),
      });
    }
    return events;
  }

  return events;
}
