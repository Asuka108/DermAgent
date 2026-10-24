/**
 * createLlmRouterNode —— 收口「LLM 裁决 → Command goto 路由」bespoke 模式（deep-research ×4：
 * outline_review / quality_review / converse / outline_gate）。
 *
 * reflection / evaluator 模式：LLM 评审 → parse verdict → 节点内 `new Command({goto, update})` 跳转。
 * 与 createLlmNode 互补：后者写回 Partial<S>（无路由）；本 factory 返回 Command（节点内路由）。
 * 不做成 createLlmNode 的 option——返回类型从 Partial<S> 变 Command 会污染其签名。
 *
 * 与「外部纯函数条件边」(routeAfterXxx) 的关系：那些纯函数保留（已 export、可单测、可被外部条件边复用）；
 * 本 factory 的 `route` 回调内部可调它们：`route: (v, s) => { const update = ...; return { goto: routeAfterXxx({...s, ...update}), update }; }`。
 * factory 只消灭「调 LLM + parse + 包 Command + catch fallback」的样板。
 *
 * @example
 * const grade = createLlmRouterNode<MyState>({
 *   model: () => requireModel(appConfig, "grade"),
 *   prompt: (s) => [new SystemMessage("评审，输出 JSON {verdict,critique}"), new HumanMessage(s.draft)],
 *   parse: (t) => parseJson<{verdict?:string;critique?:string}>(t),
 *   route: (parsed, s) => {
 *     const v = parsed as { verdict?: string; critique?: string };
 *     const update = { verdict: v.verdict, critique: v.critique ?? "" };
 *     return { goto: routeAfterGrade({...s, ...update}), update };
 *   },
 *   routeFallback: (s) => ({ goto: "done", update: { verdict: "pass", critique: "(评审异常放行)" } }),
 *   config: appConfig, label: "grade",
 * });
 */
import { Command } from "@langchain/langgraph";
import type { BaseMessage } from "@langchain/core/messages";
import type { AppConfig } from "../../runtime/index.js";
import {
  invokeWithResilience,
  resolveLlmResilience,
} from "../../runtime/services/llm-resilience.js";
import { extractText, type ChatModelLike } from "./llm.js";

export interface LlmRouterNodeOptions<S> {
  /** 模型实例，或按 state 解析（返回 falsy 触发 routeFallback("no-model")）。 */
  model: ChatModelLike | ((state: S) => ChatModelLike | null | undefined);
  /** 由 state 构造消息（含 SystemMessage；本 factory 不另注 systemPrompt）。 */
  prompt: (state: S) => BaseMessage[];
  /** 结构化：把 content 文本 parse 成裁决对象（如 parseJson）。**实际必填**——缺省则直接走 routeFallback("error")（route 需结构化裁决，不接受原始字符串）。 */
  parse?: (text: string) => unknown;
  /** 成功：parsed + state → { goto, update }。goto 通常由 routeAfterXxx({...state, ...update}) 算。 */
  route: (parsed: unknown, state: S) => { goto: string; update: Partial<S> };
  /** 无模型 / 调用失败 / parse 失败 → 放行兜底（防死循环；如 goto 放行目标 + update 标记异常）。 */
  routeFallback: (
    state: S,
    reason: "no-model" | "error",
    err?: unknown
  ) => { goto: string; update: Partial<S> };
  /** 韧性 config + label。 */
  config?: AppConfig;
  label?: string;
  retryLabel?: string;
  timeoutMs?: number;
  attempts?: number;
}

/**
 * 造一个「LLM 裁决 → Command goto」节点。返回 `(state) => Promise<Command>`。
 * 内部复刻 createLlmNode 的韧性调用栈（resolveModel → invokeWithResilience → extractText → parse），
 * 但成功/失败分别走 route / routeFallback（返回 Command，而非 Partial<S>）。
 */
export function createLlmRouterNode<S>(
  opts: LlmRouterNodeOptions<S>
): (state: S) => Promise<Command> {
  const {
    model: modelOpt,
    prompt,
    parse,
    route,
    routeFallback,
    config,
    label = "llm-router",
    retryLabel,
    timeoutMs,
    attempts,
  } = opts;

  return async (state: S): Promise<Command> => {
    const model = typeof modelOpt === "function" ? modelOpt(state) : modelOpt;
    if (!model) {
      return new Command(routeFallback(state, "no-model"));
    }
    try {
      const { shortTimeoutMs } = resolveLlmResilience(config);
      const ai = (await invokeWithResilience(model, prompt(state), {
        timeoutMs: timeoutMs ?? shortTimeoutMs,
        label,
        retryLabel: retryLabel ?? label,
        useSharedLimiter: true,
        attempts,
        config,
      })) as { content: unknown };
      const content = extractText(ai.content);
      if (!parse) {
        return new Command(
          routeFallback(state, "error", new Error("createLlmRouterNode: parse 未提供，无法结构化裁决（route 需结构化输入）"))
        );
      }
      const parsed = parse(content);
      return new Command(route(parsed, state));
    } catch (err) {
      return new Command(routeFallback(state, "error", err));
    }
  };
}
