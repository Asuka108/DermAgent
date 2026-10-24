import { describe, it, expect } from "vitest";
import { INTERRUPT } from "@langchain/langgraph";
import { mapStreamChunk } from "../src/surfaces/map-stream-chunk.js";

describe("mapStreamChunk", () => {
  it("messages → text（string 与 content block）", () => {
    expect(mapStreamChunk("messages", [{ content: "你好" }, {}])).toEqual([
      { type: "text", text: "你好" },
    ]);
    expect(
      mapStreamChunk("messages", [
        { content: [{ type: "text", text: "foo" }, { type: "text", text: "bar" }] },
        {},
      ])
    ).toEqual([{ type: "text", text: "foobar" }]);
    expect(mapStreamChunk("messages", [{ content: "" }, {}])).toEqual([]);
  });

  it("messages → text / thought 分流（reasoning 不得并入 text）", () => {
    expect(
      mapStreamChunk("messages", [
        {
          content: "",
          additional_kwargs: {
            reasoning_content: "用户只是打招呼，先内部推理一下",
          },
        },
        {},
      ])
    ).toEqual([{ type: "thought", text: "用户只是打招呼，先内部推理一下" }]);

    // content + reasoning 同时存在：正文走 text，思考走 thought
    expect(
      mapStreamChunk("messages", [
        {
          content: "可见回复",
          additional_kwargs: { reasoning_content: "内部推理" },
        },
        {},
      ])
    ).toEqual([
      { type: "text", text: "可见回复" },
      { type: "thought", text: "内部推理" },
    ]);
  });

  it("messages → Anthropic thinking content block 走 thought 通道", () => {
    expect(
      mapStreamChunk("messages", [
        {
          content: [
            { type: "thinking", thinking: "先分析问题" },
            { type: "text", text: "可见回复" },
          ],
        },
        {},
      ])
    ).toEqual([
      { type: "text", text: "可见回复" },
      { type: "thought", text: "先分析问题" },
    ]);
  });

  it("custom → stage / plan / tool 三态", () => {
    expect(
      mapStreamChunk("custom", { type: "stage", stage: "调研", index: 1, total: 3 })
    ).toEqual([{ type: "stage", stage: "调研", index: 1, total: 3, detail: undefined }]);

    const entries = [{ content: "step1", priority: "high", status: "pending" }] as const;
    expect(mapStreamChunk("custom", { type: "plan", entries })).toEqual([
      { type: "plan", entries },
    ]);

    expect(
      mapStreamChunk("custom", {
        type: "tool",
        id: "t1",
        name: "search",
        status: "in_progress",
        input: { q: "x" },
      })
    ).toEqual([{ type: "tool_start", id: "t1", name: "search", input: { q: "x" } }]);

    expect(
      mapStreamChunk("custom", { type: "tool", id: "t1", status: "completed", output: "ok" })
    ).toEqual([{ type: "tool_update", id: "t1", status: "completed", output: "ok" }]);
  });

  it("updates → __interrupt__", () => {
    expect(
      mapStreamChunk("updates", {
        reviewNode: { [INTERRUPT]: [{ value: { question: "确认？" } }] },
      })
    ).toEqual([{ type: "interrupt", question: "确认？" }]);
    expect(mapStreamChunk("updates", { thinkNode: { output: "x" } })).toEqual([]);
  });

  it("tools → on_tool_start / on_tool_end", () => {
    expect(
      mapStreamChunk("tools", {
        event: "on_tool_start",
        toolCallId: "c1",
        name: "echo",
        input: '{"x":"hi"}',
      })
    ).toEqual([{ type: "tool_start", id: "c1", name: "echo", input: { x: "hi" } }]);

    expect(
      mapStreamChunk("tools", {
        event: "on_tool_end",
        toolCallId: "c1",
        name: "echo",
        output: { kwargs: { content: "echo:hi", status: "success" } },
      })
    ).toEqual([
      { type: "tool_update", id: "c1", name: "echo", status: "completed", output: "echo:hi" },
    ]);
  });

  it("未知 mode / 缺字段 → 空", () => {
    expect(mapStreamChunk("values", { x: 1 })).toEqual([]);
    expect(mapStreamChunk("tools", { name: "echo" })).toEqual([]);
  });
});
