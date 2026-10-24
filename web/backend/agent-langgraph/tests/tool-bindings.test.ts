import { describe, expect, it } from "vitest";
import type { StructuredTool } from "@langchain/core/tools";
import { pickTools } from "../src/app/tool-bindings.js";

function fakeTool(name: string): StructuredTool {
  return { name } as unknown as StructuredTool;
}

describe("pickTools", () => {
  it("names 为空时返回全部工具（向后兼容：节点未声明工具名 = 用全部）", () => {
    const tools = [fakeTool("echo"), fakeTool("search")];
    expect(pickTools(tools, [])).toEqual(tools);
    // 节点未声明工具名（names 为 undefined）时等价于用全部
    expect(pickTools(tools, undefined as unknown as string[])).toEqual(tools);
  });

  it("按工具名选取子集", () => {
    const tools = [fakeTool("echo"), fakeTool("search"), fakeTool("quote_price")];
    expect(pickTools(tools, ["search", "quote_price"]).map((t) => t.name)).toEqual([
      "search",
      "quote_price",
    ]);
  });

  it("忽略 allTools 里不存在的工具名", () => {
    const tools = [fakeTool("echo"), fakeTool("search")];
    expect(pickTools(tools, ["search", "nope"]).map((t) => t.name)).toEqual(["search"]);
  });

  it("保持 allTools 原始顺序，不按 names 顺序重排", () => {
    const tools = [fakeTool("a"), fakeTool("b"), fakeTool("c")];
    expect(pickTools(tools, ["c", "a"]).map((t) => t.name)).toEqual(["a", "c"]);
  });
});
