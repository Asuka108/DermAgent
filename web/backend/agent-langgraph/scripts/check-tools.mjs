#!/usr/bin/env node
/** 报告打包 CLI 工具（rsync、zip、gzip、tar）是否可用。 */
import { detectPackagingTools, formatToolReport } from "./lib/tools.mjs";

const tools = detectPackagingTools();
console.log(formatToolReport(tools));
const missing = Object.entries(tools).filter(([, ok]) => !ok);
process.exit(missing.length > 0 ? 1 : 0);
