/**
 * createPrepareNode —— 把用户输入转成首条 HumanMessage 加入 messages（可选拼系统提示）。
 *
 * 默认图 prepare 的泛化版：`createPrepareNode()` 等价于原 prepareNode（input → HumanMessage）。
 * 泛型于 S（只要含 messages 通道；可选 input）。可选 systemPrompt 在首条非 system 时前置注入。
 */

import {
  HumanMessage,
  SystemMessage,
  type BaseMessage,
} from "@langchain/core/messages";

export interface PrepareNodeOptions<S> {
  /** 系统提示词（首条非 system 时前置）。 */
  systemPrompt?: string;
  /** 自定义从 state 取输入文本（默认 state.input）。 */
  input?: (state: S) => string;
  /** 把构造好的消息写回 state（默认 { messages }）。 */
  write?: (messages: BaseMessage[], state: S) => Partial<S>;
}

export function createPrepareNode<
  S extends { messages: BaseMessage[] }
>(opts: PrepareNodeOptions<S> = {}) {
  const { systemPrompt, input: inputFn, write } = opts;
  return async (state: S): Promise<Partial<S>> => {
    const text = inputFn ? inputFn(state) : ((state as { input?: string }).input ?? "");
    if (!text) return {};
    const msgs: BaseMessage[] = systemPrompt
      ? [new SystemMessage(systemPrompt), new HumanMessage(text)]
      : [new HumanMessage(text)];
    if (write) return write(msgs, state);
    return { messages: msgs } as Partial<S>;
  };
}
