/**
 * core —— 分层架构的契约层（L1）。
 *
 * 纯类型、零运行时依赖：graph（app）与 surface 共享的执行契约都在这里，
 * 因而 app 与 surfaces **互不直接依赖**。import 方向规则见 ../../README.md「分层」。
 *
 * 注意：`FlowRuntime` 接口**不在**此层 —— 它引用 runtime 层类型
 * （FileCheckpointSaver / FlowSandboxPolicy），故定义在 `runtime/flow-runtime.ts`
 * （app→runtime 为合法下行）。
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
} from "./flow-types.js";
