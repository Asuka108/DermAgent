/**
 * Flow 契约 —— **canonical 定义已移至 `core/flow-types.ts`**（分层架构的契约层 L1）。
 *
 * 本文件保留为 surface 层的 re-export shim：surfaces 与对外消费者历史上从
 * `src/surfaces/flow-types.js` import 这些类型，此处原样转发，零破坏。
 * 新代码请直接 import `../core/flow-types.js`（或 npm 子路径 `/core`，见 package.json exports）。
 *
 * 注：surfaces→core 为合法下行依赖，无需 layering 守卫 allowlist。
 */

export type {
  FlowResult,
  ToolCallEvent,
  PlanEntry,
  PlanEvent,
  StageEvent,
  FlowCallbacks,
  FlowExecutor,
  FlowRunResult,
  SessionThreadHistory,
  StatefulFlow,
} from "../core/flow-types.js";
