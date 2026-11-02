/**
 * demo 工具（StructuredTool 版）—— 无凭证 / 无 MCP 时也能跑图，演示工具调用闭环。
 *
 * 由原 src/app/nodes/tools.ts 的 DEMO_TOOLS 迁移为标准 StructuredTool，
 * 注册进工具集（供 bindTools），保证默认图始终可跑、可测。
 */

import { tool } from "@langchain/core/tools";
import { z } from "zod";

/** 安全算术求值：仅允许数字、加减乘除、()、小数点与空白；其余字符直接报错（绝不 eval 任意代码）。 */
export function safeCalc(expression: string): string {
  const expr = expression.replace(/\s+/g, "");
  if (!expr || !/^[-+/*().0-9]+$/.test(expr)) {
    throw new Error(`calculate: unsupported expression "${expression}"`);
  }
  const value = Function(`"use strict"; return (${expr});`)();
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`calculate: "${expression}" 未求出有限数值`);
  }
  return String(value);
}

export function createDemoTools() {
  const echo = tool(
    async ({ text }) => String(text ?? ""),
    {
      name: "echo",
      description: "原样回显文本（demo）。",
      schema: z.object({ text: z.string() }),
    }
  );
  const calculate = tool(
    async ({ expression }) => safeCalc(String(expression)),
    {
      name: "calculate",
      description: "算术求值，如 '2 + 3 * 4'（demo）。",
      schema: z.object({ expression: z.string() }),
    }
  );
  const time = tool(
    async () => new Date().toISOString(),
    {
      name: "time",
      description: "返回当前 ISO 时间（demo，无参）。",
      schema: z.object({}),
    }
  );
  return [echo, calculate, time];
}
