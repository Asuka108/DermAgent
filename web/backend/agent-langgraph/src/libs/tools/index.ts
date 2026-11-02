/**
 * src/libs/tools —— 模板内置**通用**工具集（框架级,只依赖 runtime）。
 *
 * 通用能力工具:bash / fs / search / demo / http-request / json-utils / skill。
 * MCP 工具不经此（由 runtime-context 经 @langchain/mcp-adapters 加载为 native StructuredTool，
 * 见 ctx.mcpTools）。默认图与示例共享。
 *
 * 注意:`task`(子智能体 subagent 委派)依赖默认图(createFlowGraph + FlowState),属 app 专属,
 * 与 `createFlowTools`(工具集装配,共享 buildTools)同住在 src/app/flow-tools.ts。
 * nodekit 与 toolkit 是兄弟层,互不 import。
 */

export { httpRequestTool } from "./http-request.tool.js";
export { jsonUtilsTool } from "./json-utils.tool.js";
export { createBashTool } from "./bash.tool.js";
export { createFsTools } from "./fs.tool.js";
export { createSearchTools } from "./search.tool.js";
export { createDemoTools } from "./demo.tool.js";
export { createSkillTool } from "./skill.tool.js";
export { writeTodosTool } from "./todo.tool.js";
