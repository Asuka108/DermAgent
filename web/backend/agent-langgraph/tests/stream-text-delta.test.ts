import { describe, expect, it } from "vitest";
import { foldStreamTextChunk } from "../src/libs/nodes/stream-text-delta.js";

describe("foldStreamTextChunk", () => {
  it("增量 chunk → delta 为 chunk 本身", () => {
    expect(foldStreamTextChunk("", "你")).toEqual({ full: "你", delta: "你" });
    expect(foldStreamTextChunk("你", "好")).toEqual({ full: "你好", delta: "好" });
  });

  it("累积全文 chunk → 只返回新增后缀", () => {
    let full = "";
    const chunks = ["你", "你好", "你好世界"];
    const deltas: string[] = [];
    for (const chunk of chunks) {
      const folded = foldStreamTextChunk(full, chunk);
      full = folded.full;
      if (folded.delta) deltas.push(folded.delta);
    }
    expect(full).toBe("你好世界");
    expect(deltas).toEqual(["你", "好", "世界"]);
  });

  it("与前缀完全相同 → delta null", () => {
    expect(foldStreamTextChunk("你好", "你好")).toEqual({ full: "你好", delta: null });
  });

  it("空 chunk → delta null", () => {
    expect(foldStreamTextChunk("已有", "")).toEqual({ full: "已有", delta: null });
  });
});
