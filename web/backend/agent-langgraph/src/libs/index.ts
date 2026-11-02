/**
 * src/libs —— 可复用构建件(节点 factory + 通用工具)一站式 barrel。
 *
 * 框架级构建件层:`core → runtime → libs → app → surfaces`。libs 只依赖 runtime+core。
 * - nodes/ :LangGraph 节点 factory(createLlmNode/createToolExecNode/...) + 构建原语(emit/runTool/extractText/...)。
 * - tools/ :模板内置通用工具(bash/fs/search/demo/http/json/...)。MCP 工具经 mcp-access + runtime-context 加载为 native。
 * nodes/ 与 tools/ 互不引用。
 */

export * from "./nodes/index.js";
export * from "./tools/index.js";
export * from "./messages/index.js";
