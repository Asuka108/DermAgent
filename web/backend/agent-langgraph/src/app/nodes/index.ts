/**
 * 默认图节点 barrel —— 仅 re-export 默认图**专属**节点（think / respond）。
 *
 * prepare / tools 已泛化为框架级 factory 上移到 src/libs/nodes（createPrepareNode /
 * createToolExecNode），graph.ts 直接从 src/libs/nodes 取。think（自管 model + bindTools +
 * 无凭证 fallback）与 respond（取最后 AIMessage 文本流式透出，不调 LLM）行为特定，
 * 暂留为默认图专属；后续可再上移。
 *
 *   prepare（src/libs/nodes）→ think（本目录）─┬─ tools（src/libs/nodes）→ think
 *                                         └─ respond（本目录）→ END
 */

export { createThinkNode, type ThinkNodeDeps } from "./think.js";
export { createRespondNode, type RespondNodeDeps } from "./respond.js";
