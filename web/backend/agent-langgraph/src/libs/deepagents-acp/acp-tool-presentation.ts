/**
 * ACP 工具展示层 —— 对齐 nuwax-ai/claude-code-acp-ts 的 tools.ts 模式。
 *
 * Flow 与 Legacy deepagents-acp 共用：tool_call 的 title/kind/locations/content/diff，
 * tool_call_update 的 content/rawOutput。不依赖 libs/nodes（分层：deepagents-acp 自闭环）。
 */

import type { ToolCallUpdate } from "@agentclientprotocol/sdk";
import {
  extractToolCallLocations,
  formatToolCallTitle,
  getToolCallKind,
} from "./adapter.js";

/** ACP ToolCallContent 子集（与 @agentclientprotocol/sdk 对齐）。 */
export type AcpToolCallContent =
  | { type: "content"; content: { type: "text"; text: string } }
  | {
      type: "diff";
      path: string;
      oldText: string | null;
      newText: string;
    };

/** tool_call 首包展示字段（合并进 session/update）。 */
export interface ToolPresentationInfo {
  title: string;
  kind: ReturnType<typeof getToolCallKind>;
  content?: AcpToolCallContent[];
  locations?: Array<{ path: string; line?: number }>;
}

/** tool_call_update 完成时的展示字段。 */
export interface ToolResultPresentation {
  content?: AcpToolCallContent[];
  locations?: Array<{ path: string; line?: number }>;
  /** 写入 ACP rawOutput；MCP 优先 structuredContent */
  rawOutput?: unknown;
  /** 无专用 content 时的兜底纯文本 */
  displayText?: string;
}

/**
 * 代码块安全包裹（对齐 claude-code-acp-ts markdownEscape）。
 * 若正文已含 ```，自动加长围栏避免截断。
 */
export function markdownEscape(text: string): string {
  let fence = "```";
  for (const [m] of text.matchAll(/^```+/gm)) {
    while (m.length >= fence.length) {
      fence += "`";
    }
  }
  const suffix = text.endsWith("\n") ? "" : "\n";
  return `${fence}\n${text}${suffix}${fence}`;
}

/**
 * 保留工具返回值原貌写入 rawOutput（对齐参考实现 rawOutput: chunk.content）。
 * MCP ask-question：有 structuredContent 时 rawOutput 为结构化对象本身。
 */
export function preserveRawOutput(result: unknown): unknown {
  return unwrapStructured(unwrapPayload(result));
}

/**
 * 从 ToolCallEvent 参数生成 tool_call 展示字段（含 locations / diff）。
 * @param cwd session workspaceRoot，用于 locations 绝对路径
 */
export function toolInfoFromToolEvent(
  toolName: string,
  args: Record<string, unknown>,
  cwd?: string
): ToolPresentationInfo {
  const base: ToolPresentationInfo = {
    title: formatToolCallTitle(toolName, args),
    kind: getToolCallKind(toolName),
  };

  const locations = extractToolCallLocations(toolName, args, cwd);
  if (locations?.length) {
    base.locations = locations;
  }

  const filePath = resolveFilePath(args, cwd);

  switch (toolName) {
    case "write_file": {
      const content = typeof args.content === "string" ? args.content : "";
      if (filePath && content) {
        base.content = [
          {
            type: "diff",
            path: filePath,
            oldText: null,
            newText: content,
          },
        ];
      }
      break;
    }
    case "edit_file": {
      const find = typeof args.find === "string" ? args.find : "";
      const replace = typeof args.replace === "string" ? args.replace : "";
      if (filePath && (find || replace)) {
        base.content = [
          {
            type: "diff",
            path: filePath,
            oldText: find || null,
            newText: replace,
          },
        ];
      }
      break;
    }
    default:
      break;
  }

  return base;
}

/**
 * 从工具执行结果生成 tool_call_update 的 content / rawOutput。
 * write/edit 完成时不重复 diff（与参考 Write/Edit 分支一致返回空 content）。
 */
export function toolUpdateFromToolResult(
  toolName: string,
  result: unknown,
  options?: { workspaceRoot?: string; isError?: boolean }
): ToolResultPresentation {
  const parsed = unwrapPayload(result);
  const rawOutput = unwrapStructured(parsed);
  const isError = options?.isError ?? false;

  if (parsed === null || parsed === undefined) {
    return { rawOutput };
  }

  switch (toolName) {
    case "read_file": {
      // 文件内容原样展示：字符串结果直接保留字节，不经 unwrap/extract，避免把 JSON
      // 文件解析成对象后丢失原始格式（甚至退化成 "[object Object]"）。
      if (typeof result === "string") {
        if (!result) return { rawOutput: result };
        return {
          rawOutput: result,
          content: [
            { type: "content", content: { type: "text", text: markdownEscape(result) } },
          ],
        };
      }
      const text = extractDisplayText(parsed);
      if (!text) return { rawOutput };
      return {
        rawOutput,
        content: [
          { type: "content", content: { type: "text", text: markdownEscape(text) } },
        ],
      };
    }
    case "write_file":
    case "edit_file":
      // 创建时的 diff 已在 tool_call；完成时不重复（参考 tools.ts Write/Edit）
      return { rawOutput };

    default:
      return toGenericContentUpdate(parsed, rawOutput, isError);
  }
}

/** requestPermission / Legacy 与 Flow 共用的 toolCall 载荷片段。 */
export function buildPermissionToolCall(
  toolCallId: string,
  toolName: string,
  args: Record<string, unknown>,
  workspaceRoot?: string
): ToolCallUpdate {
  const info = toolInfoFromToolEvent(toolName, args, workspaceRoot);
  return {
    toolCallId,
    status: "pending",
    rawInput: args,
    title: info.title,
    kind: info.kind,
    ...(info.locations?.length ? { locations: info.locations } : {}),
    ...(info.content?.length ? { content: info.content } : {}),
  };
}

// —— 内部：结果解析 ——

function unwrapPayload(result: unknown, depth = 0): unknown {
  if (depth > 3) return result;
  if (typeof result === "string") {
    const trimmed = result.trim();
    if (
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))
    ) {
      try {
        return unwrapPayload(JSON.parse(trimmed) as unknown, depth + 1);
      } catch {
        return result;
      }
    }
    return result;
  }
  return result;
}

/** structuredContent 优先：对象含 structuredContent 时取其本身作 rawOutput，否则原值。 */
function unwrapStructured(parsed: unknown): unknown {
  if (parsed === null || parsed === undefined) return parsed;
  if (typeof parsed === "object" && !Array.isArray(parsed)) {
    const obj = parsed as Record<string, unknown>;
    if (obj.structuredContent !== undefined) {
      return obj.structuredContent;
    }
  }
  return parsed;
}

function extractDisplayText(parsed: unknown): string {
  if (typeof parsed === "string") return parsed;
  if (parsed === null || parsed === undefined) return "";

  if (typeof parsed === "object") {
    const obj = parsed as Record<string, unknown>;

    if (obj.structuredContent !== undefined) {
      const structured = obj.structuredContent;
      if (typeof obj.text === "string" && obj.text.length > 0) return obj.text;
      if (
        typeof structured === "object" &&
        structured !== null &&
        typeof (structured as { message?: unknown }).message === "string"
      ) {
        return (structured as { message: string }).message;
      }
      return JSON.stringify(structured, null, 2);
    }

    if (obj.type === "text" && typeof obj.text === "string") {
      return obj.text;
    }

    if (Array.isArray(obj.content)) {
      const text = (obj.content as Array<{ type?: string; text?: string }>)
        .filter((b) => b.type === "text" && typeof b.text === "string")
        .map((b) => b.text as string)
        .join("\n");
      if (text) return text;
    }

    if (typeof obj.text === "string" && obj.text.length > 0) {
      return obj.text;
    }
  }

  if (Array.isArray(parsed)) {
    const text = (parsed as Array<{ type?: string; text?: string }>)
      .filter((b) => b && b.type === "text" && typeof b.text === "string")
      .map((b) => b.text as string)
      .join("\n");
    if (text) return text;
    return JSON.stringify(parsed, null, 2);
  }

  // 普通对象（无 text/content/structuredContent 等已知键）：序列化为 JSON，
  // 而非 String(obj) 退化成 "[object Object]"（与 normalizeToolResult 的兜底一致）。
  if (typeof parsed === "object") {
    return JSON.stringify(parsed, null, 2);
  }

  return String(parsed);
}

function toGenericContentUpdate(
  parsed: unknown,
  rawOutput: unknown,
  isError: boolean
): ToolResultPresentation {
  const text = extractDisplayText(parsed);
  if (!text) return { rawOutput };

  return {
    rawOutput,
    displayText: text,
    content: [
      {
        type: "content",
        content: {
          type: "text",
          text: isError ? markdownEscape(text) : text,
        },
      },
    ],
  };
}

function resolveFilePath(
  args: Record<string, unknown>,
  cwd?: string
): string | undefined {
  const path = args.path as string | undefined;
  if (!path) return undefined;
  if (path.startsWith("/")) return path;
  if (cwd) return `${cwd.replace(/\/$/, "")}/${path}`;
  return path;
}
