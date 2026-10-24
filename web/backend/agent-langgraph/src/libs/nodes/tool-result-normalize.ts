/**
 * 工具结果归一化 —— ToolMessage / MCP tools/call 响应 → ACP 可展示的 text + structured rawOutput。
 *
 * **标准栈**（本包不自研 MCP JSON-RPC）：
 *
 * 1. **LangGraph** — `MultiServerMCPClient.getTools()` 注入 `ToolNode` / `bindTools`；
 *    `streamMode: "tools"` 产生 `on_tool_*`；`createToolExecNode` 透出 `onToolCall` → ACP。
 * 2. **@langchain/mcp-adapters** — MCP server 配置 → LangChain `StructuredTool[]`。
 * 3. **@modelcontextprotocol/sdk** — `CallToolResult`（`content` + `structuredContent`）。
 *
 * **ACP 桥接**（completed）：`rawOutput` ← `structuredContent`；`rawInput` ← `structuredContent.input`
 *（交互式 MCP server 回传的 ACP 载荷；按 structuredContent 形状识别，非工具名硬编码）。
 *
 * @see https://docs.langchain.com/oss/javascript/langchain/mcp
 * @see https://github.com/langchain-ai/langchain-mcp-adapters
 * @see https://github.com/modelcontextprotocol/typescript-sdk
 */

/** normalizeToolResult 的返回值：展示文本 + 可选结构化 rawOutput。 */
export interface NormalizedToolResult {
  /** 写入 content[0].content.text 与 output 的纯文本 */
  text: string;
  /** 写入 rawOutput 的结构化对象（MCP CallToolResult.structuredContent） */
  rawOutput?: unknown;
}

/**
 * 从 ToolMessage.content 或任意工具返回值提取可展示文本。
 * content block 数组只拼接 type==="text" 的块。
 */
export function normalizeToolMessageContent(content: unknown): string {
  return normalizeToolResult(content).text;
}

/**
 * 从工具返回值提取展示文本与结构化 rawOutput。
 * 支持：纯字符串、MCP content block 数组、含 structuredContent 的 JSON 字符串、嵌套 JSON。
 */
export function normalizeToolResult(result: unknown): NormalizedToolResult {
  const parsed = unwrapToolResultPayload(result);

  if (parsed === null || parsed === undefined) {
    return { text: "" };
  }

  if (typeof parsed === "string") {
    return { text: parsed };
  }

  if (typeof parsed !== "object") {
    return { text: String(parsed) };
  }

  const obj = parsed as Record<string, unknown>;

  // MCP CallToolResult 顶层：{ content: ContentBlock[], structuredContent?: object }
  const fromCallToolResult = extractCallToolResultFields(obj);
  if (fromCallToolResult) {
    return fromCallToolResult;
  }

  // LangChain / 序列化层：structuredContent 嵌在同一对象（如 ToolMessage content block）
  if (obj.structuredContent !== undefined) {
    return normalizeStructuredContentEnvelope(obj.structuredContent, obj.text);
  }

  // 单层 MCP text block：{ type: "text", text: "..." }
  if (obj.type === "text" && typeof obj.text === "string") {
    return { text: obj.text };
  }

  // MCP content 数组：[{ type: "text", text: "..." }, ...]
  if (Array.isArray(obj.content)) {
    const blocks = obj.content as Array<{ type?: string; text?: string }>;
    const text = blocks
      .filter((b) => b.type === "text" && typeof b.text === "string")
      .map((b) => b.text as string)
      .join("\n");
    if (text) return { text };
  }

  if (Array.isArray(parsed)) {
    const text = (parsed as Array<{ type?: string; text?: string }>)
      .filter((b) => b && b.type === "text" && typeof b.text === "string")
      .map((b) => b.text as string)
      .join("\n");
    if (text) return { text };
    return { text: JSON.stringify(parsed, null, 2) };
  }

  if (typeof obj.text === "string" && obj.text.length > 0) {
    return { text: obj.text };
  }

  return { text: JSON.stringify(parsed, null, 2) };
}

/**
 * 从 MCP CallToolResult.structuredContent 提取 ACP `tool_call.rawInput`。
 *
 * 识别规则（通用，不绑定工具名）：
 * 1. `structuredContent.input` —— 交互式 MCP server 在 outputSchema 内嵌 ACP rawInput（平台扩展）
 * 2. `structuredContent` 本身 —— 若顶层已含 `ui` 或 `schemaVersion`，视为已是 ACP rawInput
 */
export function extractMcpStructuredRawInput(
  result: unknown
): Record<string, unknown> | undefined {
  const structured = normalizeToolResult(result).rawOutput;
  if (!structured || typeof structured !== "object" || Array.isArray(structured)) {
    return undefined;
  }
  const envelope = structured as Record<string, unknown>;

  const nested = envelope.input;
  if (nested && typeof nested === "object" && !Array.isArray(nested)) {
    const candidate = nested as Record<string, unknown>;
    if (looksLikeAcpToolRawInput(candidate)) {
      return candidate;
    }
  }

  if (looksLikeAcpToolRawInput(envelope)) {
    return envelope;
  }

  return undefined;
}

/** MCP CallToolResult.content 数组 → 展示用纯文本。 */
function textFromContentBlocks(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .filter(
      (b): b is { type?: string; text?: string } =>
        !!b && typeof b === "object" && (b as { type?: string }).type === "text"
    )
    .map((b) => b.text as string)
    .filter(Boolean)
    .join("\n");
}

/** 是否为 MCP CallToolResult 顶层形状并提取 text + structuredContent。 */
function extractCallToolResultFields(
  obj: Record<string, unknown>
): NormalizedToolResult | undefined {
  if (!("content" in obj) || obj.structuredContent === undefined) {
    return undefined;
  }
  const textFromBlocks = textFromContentBlocks(obj.content);
  return normalizeStructuredContentEnvelope(
    obj.structuredContent,
    textFromBlocks || undefined
  );
}

/** structuredContent → NormalizedToolResult（text 优先 content，其次 message 字段）。 */
function normalizeStructuredContentEnvelope(
  structured: unknown,
  preferredText?: unknown
): NormalizedToolResult {
  const innerText =
    typeof preferredText === "string" && preferredText.length > 0
      ? preferredText
      : typeof structured === "object" &&
          structured !== null &&
          typeof (structured as { message?: unknown }).message === "string"
        ? ((structured as { message: string }).message as string)
        : JSON.stringify(structured, null, 2);
  return { text: innerText, rawOutput: structured };
}

/** ACP 交互式 tool_call.rawInput 最小识别（ui 表单 或 声明 schemaVersion）。 */
function looksLikeAcpToolRawInput(rawInput: Record<string, unknown>): boolean {
  const hasUi = !!rawInput.ui && typeof rawInput.ui === "object";
  const hasSchemaVersion = typeof rawInput.schemaVersion === "string";
  return hasUi || hasSchemaVersion;
}

/**
 * LangGraph on_tool_end 的 output 多路径提取 ToolMessage content。
 * kwargs.content（标准）→ output.content（兜底）→ 整体 fallback。
 */
export function extractToolEndOutput(output: unknown): unknown {
  if (output === null || output === undefined) return undefined;
  if (typeof output !== "object") return output;

  const o = output as {
    kwargs?: { content?: unknown };
    content?: unknown;
  };
  if (o.kwargs?.content !== undefined) return o.kwargs.content;
  if (o.content !== undefined) return o.content;
  return output;
}

/** 反复 JSON.parse 直到不再是「JSON 字符串包 JSON 对象」为止（最多 3 层）。 */
function unwrapToolResultPayload(result: unknown, depth = 0): unknown {
  if (depth > 3) return result;

  if (typeof result === "string") {
    const trimmed = result.trim();
    if (
      (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
      (trimmed.startsWith("[") && trimmed.endsWith("]"))
    ) {
      try {
        return unwrapToolResultPayload(JSON.parse(trimmed) as unknown, depth + 1);
      } catch {
        return result;
      }
    }
    return result;
  }

  return result;
}
