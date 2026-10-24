/**
 * createFanout —— Send map-reduce 扇出。返回一个**条件边函数**:对每个 item emit 一个
 * `Send` 到目标节点(parallel 实例),结果经 state 的 array-reducer 通道归约。
 *
 * 用法:`.addConditionalEdges("src", createFanout({ items, target, input }), [target])`
 * 泛型于 S(state)与 T(item)。
 */

import { Send } from "@langchain/langgraph";

export interface FanoutOptions<T, S> {
  /** 从 state 取待扇出的 item 列表。 */
  items: (state: S) => T[];
  /** 扇出目标节点名(所有并行实例都进这个节点)。 */
  target: string;
  /** 每个 item → Send 载荷(注入该并行实例的 state 切片)。 */
  input: (item: T, state: S) => Record<string, unknown>;
}

/** 构造一个 emit `Send[]` 的条件边函数(map-reduce 扇出)。 */
export function createFanout<T, S>(opts: FanoutOptions<T, S>): (state: S) => Send[] {
  const { items, target, input } = opts;
  return (state) => items(state).map((item) => new Send(target, input(item, state)));
}
