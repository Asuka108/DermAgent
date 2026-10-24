/**
 * createSubgraphNode —— 把一组节点 + 边编译成一个子图(可作节点 `.addNode(name, subgraph)` 进父图)。
 *
 * 薄封装 LangGraph 原生 idiom(`new StateGraph + addNode + addEdge + compile`),
 * 收口「子图作节点」复用模式(独立 state,经共享 channel 与父图映射)。
 * 边用 `[from, to]` 元组数组;from/to 可是节点名或 START/END 常量。
 */

import { StateGraph } from "@langchain/langgraph";

export interface SubgraphNodeOptions<S> {
  /** state:StateGraph 构造器首参(Annotation.Root spec)。 */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  state: any;
  /** 节点名 → 节点函数。 */
  nodes: Record<string, (state: S) => unknown>;
  /** 边:[from, to];from/to 为节点名或 START/END 常量。 */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  edges: ReadonlyArray<[from: any, to: any]>;
}

/** 编译一个子图(CompiledGraph),可直接 addNode 进父图。 */
export function createSubgraphNode<S = unknown>(opts: SubgraphNodeOptions<S>) {
  const builder = new StateGraph(opts.state);
  for (const [name, fn] of Object.entries(opts.nodes)) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    builder.addNode(name, fn as any);
  }
  for (const [from, to] of opts.edges) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    builder.addEdge(from as any, to as any);
  }
  return builder.compile();
}
