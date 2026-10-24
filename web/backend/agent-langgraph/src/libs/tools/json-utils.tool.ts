/**
 * JSON Utils Tool
 *
 * JSON parsing, validation, extraction, and merging utilities.
 * Built with @langchain/core/tools.
 */

import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { deepMerge } from "../../runtime/config/config-loader.js";

export const jsonUtilsTool = tool(
  async ({ operation, input, path, second }) => {
    try {
      switch (operation) {
        case "parse": {
          const parsed = JSON.parse(input);
          return JSON.stringify({ result: parsed, type: typeof parsed });
        }

        case "stringify": {
          const obj = JSON.parse(input);
          return JSON.stringify(obj, null, 2);
        }

        case "validate": {
          try {
            JSON.parse(input);
            return JSON.stringify({ valid: true });
          } catch (err) {
            return JSON.stringify({
              valid: false,
              error: err instanceof Error ? err.message : String(err),
            });
          }
        }

        case "extract": {
          if (!path) return "Error: 'path' is required for extract";
          const obj = JSON.parse(input);
          const value = getNestedValue(obj, path);
          return JSON.stringify({ path, value, found: value !== undefined });
        }

        case "merge": {
          if (!second) return "Error: 'second' is required for merge";
          const obj1 = JSON.parse(input);
          const obj2 = JSON.parse(second);
          return JSON.stringify({ result: deepMerge(obj1, obj2 as Partial<typeof obj1>) });
        }

        default:
          return `Unknown operation: ${operation}`;
      }
    } catch (err) {
      return `JSON operation failed: ${err instanceof Error ? err.message : String(err)}`;
    }
  },
  {
    name: "json_utils",
    description: `JSON parsing, stringification, validation, extraction, and merging utilities.

Operations:
- parse: Parse a JSON string (params: input)
- stringify: Pretty-print a JSON string (params: input)
- validate: Check if a string is valid JSON (params: input)
- extract: Extract a value using dot-notation path like "data.items[0].name" (params: input, path)
- merge: Deep merge two JSON objects (params: input, second)`,
    schema: z.object({
      operation: z
        .enum(["parse", "stringify", "validate", "extract", "merge"])
        .describe("JSON utility operation"),
      input: z.string().describe("Input JSON string"),
      path: z
        .string()
        .optional()
        .describe("Dot-notation path for extract (e.g., 'data.items[0].name')"),
      second: z
        .string()
        .optional()
        .describe("Second JSON string (for merge operation)"),
    }),
  }
);

// ─── Helpers ────────────────────────────────────────────

function getNestedValue(obj: unknown, path: string): unknown {
  const keys = path.split(".");
  let current: unknown = obj;
  for (const key of keys) {
    if (current === null || current === undefined) return undefined;
    const arrayMatch = key.match(/^(\w+)\[(\d+)\]$/);
    if (arrayMatch) {
      current = (current as Record<string, unknown>)[arrayMatch[1]!];
      if (Array.isArray(current)) {
        current = current[parseInt(arrayMatch[2]!, 10)];
      } else {
        return undefined;
      }
    } else {
      current = (current as Record<string, unknown>)[key];
    }
  }
  return current;
}
