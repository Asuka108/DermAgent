/**
 * dispatchSurfaceEvent —— thought / text 分流回归。
 */

import { describe, expect, it, vi } from "vitest";
import { dispatchSurfaceEvent } from "../src/surfaces/dispatch-surface-event.js";

describe("dispatchSurfaceEvent thought vs text", () => {
  it("text → onToken；thought → onThought（互不串道）", async () => {
    const onToken = vi.fn();
    const onThought = vi.fn();
    const callbacks = { onToken, onThought };

    await dispatchSurfaceEvent(
      { type: "text", text: "可见正文" },
      callbacks,
      { langgraph_node: "think" }
    );
    await dispatchSurfaceEvent(
      { type: "thought", text: "内部思考" },
      callbacks,
      { langgraph_node: "think" }
    );

    expect(onToken).toHaveBeenCalledTimes(1);
    expect(onToken).toHaveBeenCalledWith("可见正文");
    expect(onThought).toHaveBeenCalledTimes(1);
    expect(onThought).toHaveBeenCalledWith("内部思考");
  });

  it("非 STREAM_TEXT_NODES 的节点不透出 thought/text", async () => {
    const onToken = vi.fn();
    const onThought = vi.fn();
    await dispatchSurfaceEvent(
      { type: "text", text: "泄漏" },
      { onToken, onThought },
      { langgraph_node: "prepare" }
    );
    await dispatchSurfaceEvent(
      { type: "thought", text: "泄漏思考" },
      { onToken, onThought },
      { langgraph_node: "prepare" }
    );
    expect(onToken).not.toHaveBeenCalled();
    expect(onThought).not.toHaveBeenCalled();
  });
});
