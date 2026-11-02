/**
 * 图拓扑导出 ——【对接「编排可视化」目标】。
 *
 * 把默认 flow 的显式 StateGraph 反射成结构化 `{ nodes, edges }`(+ Mermaid 源),
 * 供 inspector / 文档 / 调试器直接消费:**不运行图、不需要模型凭证**。
 *
 * 用法:
 *   import { getFlowTopology } from "./topology.js"; // 工作区内
 *   const { nodes, edges, mermaid } = await getFlowTopology();
 * 或命令行: `pnpm graph`（JSON）/ `pnpm graph --mermaid`；平台安装包为 `nuwax-flow-ts graph`。
 */

import { createFlowGraph, type CreateFlowGraphConfig } from "./graph.js";
import type {
  FlowTopology,
  FlowTopologyNode,
  FlowTopologyEdge,
} from "../core/flow-types.js";

// re-export 维持公开 npm 子路径 `/topology`（见 package.json exports）。
export type { FlowTopology, FlowTopologyNode, FlowTopologyEdge } from "../core/flow-types.js";

/** 可反射的编译图最小结构（LangGraph `.compile()` 产物的结构子集）。 */
interface ReflectableGraph {
  getGraphAsync(opts?: unknown): Promise<ReflectableGraphViz>;
}
interface ReflectableGraphViz {
  /** reid() 用可读节点名替代内部 id（prepare/think/…，外加 __start__/__end__）。 */
  reid(): ReflectableGraphViz;
  nodes: Record<string, { id: string; name?: string }>;
  edges: Array<{
    source: string;
    target: string;
    conditional?: boolean;
    /** 条件分支标签（路由目标名等）。 */
    data?: string;
  }>;
  drawMermaid(): string;
}

/**
 * 反射编译图拓扑。数据来自 LangGraph `getGraphAsync()`：静态边总是准确；返回 Command 的路由节点
 * 须在 addNode 第三参声明 ends（列出 goto 目标），否则这些条件边反射不出——漏 ends 的节点会丢边。
 */
export async function reflectTopology(
  compiled: ReflectableGraph
): Promise<FlowTopology> {
  const g = (await compiled.getGraphAsync({})).reid();

  const nodes: FlowTopologyNode[] = Object.values(g.nodes).map((n) => ({
    id: n.id,
    label: n.name || n.id,
  }));
  const edges: FlowTopologyEdge[] = g.edges.map((e) => ({
    source: e.source,
    target: e.target,
    conditional: e.conditional ?? false,
    ...(e.data ? { label: e.data } : {}),
  }));

  return { nodes, edges, mermaid: g.drawMermaid() };
}

/**
 * 提取默认 flow 图的拓扑(静态:只构建图结构,不执行节点,无需凭证)。
 * 传入与 createFlowGraph 相同的 config 即可(通常无需 —— 拓扑不依赖 appConfig)。
 */
export async function getFlowTopology(
  config: CreateFlowGraphConfig = {}
): Promise<FlowTopology> {
  return reflectTopology(createFlowGraph(config));
}
