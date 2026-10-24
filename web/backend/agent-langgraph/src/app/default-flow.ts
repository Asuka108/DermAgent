/**
 * 默认 flow —— 把肤智（DermAgent） 图（src/app/graph.ts 的问诊审核 HITL 管道）包成 surface 能用的
 * StatefulFlow recipe。
 *
 * 由组合根 index.ts 的 materializeFlow 调 createStatefulFlow({...recipe, checkpointer,
 * appConfig}) 物化。interaction=approval（flows/index.ts profile）→ 不传 conversational，
 * surface 暴露 hasStarted：首条 query 开题（intake → screen → …），interrupt（ask_more /
 * doctor_review）后每条用户消息走 resume 续跑同一病例。
 *
 * toInput 把 query 写入 state.input；toResult 从终态 output 取回答（正式报告 / 拒绝收尾）。
 *
 * **ACP MCP**：本管道 RAG 使用内置合成演示知识库（纯函数检索，见 src/app/knowledge），
 * 不依赖 MCP 工具；allTools 保持接口兼容注入但不参与图内连线。
 *
 * 调试 trace 由 surface 层负责（ACP server / CLI），本文件不注入日志。
 */

import type { FlowRuntime } from "../runtime/flow-runtime.js";
import type { StatefulTopologyRecipe } from "../core/flow-types.js";
import { createFlowGraph } from "./graph.js";
import type { FlowState } from "./state.js";

/**
 * 肤智（DermAgent） HITL recipe —— 首条消息开题、interrupt/resume 审核闭环。
 * buildGraph 用注入的 checkpointer 编译问诊审核图；toInput 把 query 交给 intake
 * （合成案例指令或脱敏病例描述）；toResult 输出正式报告（或拒绝收尾文案）。
 */
export function recipe(runtime: FlowRuntime): StatefulTopologyRecipe {
  return {
    buildGraph: (checkpointer) =>
      createFlowGraph({
        allTools: runtime.allTools,
        checkpointer,
        config: runtime.config,
        systemPrompt: runtime.systemPrompt,
      }),
    toInput: (query) => ({ input: query }),
    toResult: (values) => ({ answer: String((values as FlowState).output ?? "") }),
  };
}
