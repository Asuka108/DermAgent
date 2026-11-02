/**
 * 多模态消息 → 纯文本。
 *
 * chrome-devtools 等 MCP 会把截图以 `image_url` / base64 写入 ToolMessage；
 * 智谱等 OpenAI 兼容端点只接受 `content.type = text`，一旦进 checkpoint，
 * 后续每轮 think 都会 400，会话不可恢复。
 *
 * 本模块在写路径 / think 入口 / checkpoint 修复三处共用。
 */

import {
  AIMessage,
  HumanMessage,
  SystemMessage,
  ToolMessage,
  type BaseMessage,
} from "@langchain/core/messages";
import { logger } from "../../runtime/logger.js";
import { messageId, msgToolCallId, msgToolCalls, msgType } from "./sanitize-tool-calls.js";

const log = logger.child("coerce-text-content");

/** 需要压成纯文本的 content block type（视觉 / 二进制类）。 */
const NON_TEXT_BLOCK_TYPES = new Set([
  "image",
  "image_url",
  "input_image",
  "media",
  "file",
  "audio",
  "video",
  "document",
]);

/**
 * 是否应对该 config/env 强制 text-only（默认 true）。
 * 显式开启 vision 时跳过剥离：`FLOW_SUPPORTS_VISION=1` 或 `model.settings.supportsVision`。
 */
export function shouldCoerceToTextOnly(config?: {
  model?: { provider?: string; settings?: Record<string, unknown> };
}): boolean {
  const env = process.env.FLOW_SUPPORTS_VISION?.trim().toLowerCase();
  if (env === "1" || env === "true" || env === "yes") return false;
  if (config?.model?.settings?.supportsVision === true) return false;
  return true;
}

export type CoerceMode = "images" | "all-non-string";

/**
 * 按 provider 选择默认 coerce 强度（须先通过 shouldCoerceToTextOnly）。
 * - anthropic：只剥图像，保留 text[] + cache_control / thinking
 * - 其余（openai 兼容，含智谱 GLM）：压扁任意非字符串 content（含纯 text block 数组）
 *   —— 智谱等端点常拒收数组 content，即便块类型全是 text
 */
export function resolveCoerceMode(config?: {
  model?: { provider?: string; settings?: Record<string, unknown> };
}): CoerceMode {
  if (config?.model?.provider === "anthropic") return "images";
  return "all-non-string";
}

/**
 * content 是否需要压成纯文本。
 * - `images`（默认）：只打 image / media / base64 等视觉块；保留 thinking / tool_use / cache_control text。
 * - `all-non-string`：任意非字符串 content 一律压扁（think 自愈重试用）。
 */
export function messageContentNeedsTextCoerce(
  content: unknown,
  mode: CoerceMode = "images"
): boolean {
  if (content == null) return false;
  if (typeof content === "string") return false;

  if (mode === "all-non-string") {
    return typeof content === "object";
  }

  if (!Array.isArray(content)) {
    // 单对象：仅图像 / 二进制类才剥
    return typeof content === "object" && isNonTextBlock(content);
  }
  return content.some((block) => isNonTextBlock(block));
}

/**
 * 把 message content 压成纯文本字符串。
 * image / image_url 等替换为简短占位，避免把巨型 base64 送进 LLM / checkpoint。
 * `mode: "all-non-string"` 时连 thinking 等非图像块一并压扁。
 */
export function coerceContentToText(
  content: unknown,
  mode: CoerceMode = "images"
): string {
  if (content == null) return "";
  if (typeof content === "string") return content;

  if (Array.isArray(content)) {
    const parts: string[] = [];
    for (const block of content) {
      const piece = coerceBlockToText(block, mode);
      if (piece) parts.push(piece);
    }
    return parts.join("\n");
  }

  if (typeof content === "object") {
    return coerceBlockToText(content, mode);
  }

  return String(content);
}

export interface CoerceMessagesOptions {
  /**
   * - `images`：默认，只剥视觉 / 二进制块
   * - `all-non-string`：强制压扁一切非字符串 content（content.type 400 自愈）
   */
  mode?: CoerceMode;
}

/**
 * 清洗消息列表：按 mode 把目标 content 压成纯字符串；无变更时返回原数组引用。
 * 兼容 checkpoint 反序列化 plain object（非 LangChain 类实例）。
 */
export function coerceMessagesToTextContent(
  messages: BaseMessage[],
  opts?: CoerceMessagesOptions
): BaseMessage[] {
  const mode = opts?.mode ?? "images";
  let changed = false;
  let imageBlocks = 0;
  const out = messages.map((msg) => {
    const raw = msg as unknown as Record<string, unknown>;
    const content = raw.content;
    if (!messageContentNeedsTextCoerce(content, mode)) return msg;

    if (Array.isArray(content)) {
      imageBlocks += content.filter((b) => isNonTextBlock(b)).length;
    } else if (isNonTextBlock(content)) {
      imageBlocks += 1;
    }

    changed = true;
    return rebuildMessageWithTextContent(msg, coerceContentToText(content, mode));
  });

  if (!changed) return messages;

  log.info("已将多模态 message content 压成纯文本", {
    messageCount: messages.length,
    rewritten: out.filter((m, i) => m !== messages[i]).length,
    imageBlocks,
    mode,
  });
  return out;
}

/**
 * 识别「模型拒收非 text content.type」类错误（智谱：参数非法，取值范围 ['text']）。
 * 收紧：须命中 content.type / 取值范围 ['text']，避免无关文案误触发自愈。
 */
export function isIllegalContentTypeError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err);
  if (/参数非法.*\['text'\]/i.test(msg)) return true;
  const mentionsContentType =
    /content\.type/i.test(msg) || /messages\.content\.type/i.test(msg);
  if (!mentionsContentType) return false;
  return /非法|unsupported|not\s+(?:supported|allowed)|取值范围|invalid/i.test(msg);
}

function coerceBlockToText(block: unknown, mode: CoerceMode = "images"): string {
  if (block == null) return "";
  if (typeof block === "string") return block;
  if (typeof block !== "object") return String(block);

  const b = block as Record<string, unknown>;
  const type = typeof b.type === "string" ? b.type : "";

  if (type === "text" && typeof b.text === "string") {
    return b.text;
  }

  if (isNonTextBlock(b)) {
    return describeNonTextBlock(b);
  }

  // images 模式：非视觉块原样保留语义（thinking / tool_use 等）——
  // 仅在整条 message 因含图像而被压扁时，才把它们转成可读占位，避免丢光。
  if (mode === "images") {
    if (typeof b.text === "string" && b.text.length > 0) return b.text;
    if (typeof b.thinking === "string" && b.thinking.length > 0) {
      return `[thinking] ${b.thinking}`;
    }
    try {
      return JSON.stringify(sanitizeForLog(b));
    } catch {
      return `[${type || "content"} omitted]`;
    }
  }

  // all-non-string：aggressive 压扁
  if (typeof b.text === "string" && b.text.length > 0) {
    return b.text;
  }
  if (typeof b.thinking === "string" && b.thinking.length > 0) {
    return `[thinking] ${b.thinking}`;
  }
  if (looksLikeImagePayload(b)) {
    return describeNonTextBlock({ ...b, type: type || "image" });
  }
  try {
    return JSON.stringify(sanitizeForLog(b));
  } catch {
    return `[${type || "content"} omitted]`;
  }
}

function isNonTextBlock(block: unknown): boolean {
  if (!block || typeof block !== "object") return false;
  const b = block as Record<string, unknown>;
  const type = typeof b.type === "string" ? b.type : "";
  if (NON_TEXT_BLOCK_TYPES.has(type)) return true;
  // OpenAI 风格：{ type: "image_url", image_url: { url } } 已覆盖；兜底有 image_url 字段
  if (b.image_url != null) return true;
  if (typeof b.data === "string" && looksLikeBase64(b.data) && b.mimeType) return true;
  return false;
}

function describeNonTextBlock(b: Record<string, unknown>): string {
  const type = typeof b.type === "string" ? b.type : "binary";
  const mime = guessMime(b);
  const bytes = estimatePayloadBytes(b);
  if (bytes != null && bytes > 0) {
    return `[${type} omitted: ${mime}, ~${formatBytes(bytes)}]`;
  }
  return `[${type} omitted: ${mime}]`;
}

function guessMime(b: Record<string, unknown>): string {
  if (typeof b.mimeType === "string" && b.mimeType) return b.mimeType;
  if (typeof b.media_type === "string" && b.media_type) return b.media_type;
  const url = extractImageUrl(b);
  const m = url?.match(/^data:([^;,]+)/);
  if (m?.[1]) return m[1];
  if (typeLooksLikeImage(b)) return "image/png";
  return "application/octet-stream";
}

function typeLooksLikeImage(b: Record<string, unknown>): boolean {
  const type = typeof b.type === "string" ? b.type : "";
  return type.includes("image") || b.image_url != null;
}

function extractImageUrl(b: Record<string, unknown>): string | undefined {
  const imageUrl = b.image_url;
  if (typeof imageUrl === "string") return imageUrl;
  if (imageUrl && typeof imageUrl === "object") {
    const url = (imageUrl as { url?: unknown }).url;
    if (typeof url === "string") return url;
  }
  if (typeof b.url === "string") return b.url;
  if (typeof b.data === "string" && looksLikeBase64(b.data)) {
    const mime = typeof b.mimeType === "string" ? b.mimeType : "image/png";
    return `data:${mime};base64,${b.data}`;
  }
  return undefined;
}

function estimatePayloadBytes(b: Record<string, unknown>): number | undefined {
  const url = extractImageUrl(b);
  if (url?.startsWith("data:")) {
    const comma = url.indexOf(",");
    if (comma >= 0) {
      const b64 = url.slice(comma + 1);
      // base64 → 约 3/4 原始字节
      return Math.floor((b64.length * 3) / 4);
    }
  }
  if (typeof b.data === "string") {
    return Math.floor((b.data.length * 3) / 4);
  }
  return undefined;
}

function looksLikeBase64(s: string): boolean {
  return s.length > 64 && /^[A-Za-z0-9+/=\s]+$/.test(s.slice(0, 200));
}

function looksLikeImagePayload(b: Record<string, unknown>): boolean {
  return (
    b.image_url != null ||
    (typeof b.data === "string" && looksLikeBase64(b.data)) ||
    (typeof b.url === "string" && b.url.startsWith("data:image"))
  );
}

/** 序列化未知 block 时裁掉超长 base64 字段，避免日志/占位爆炸。 */
function sanitizeForLog(b: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(b)) {
    if (typeof v === "string" && v.length > 200) {
      out[k] = `${v.slice(0, 40)}…(+${v.length - 40} chars)`;
    } else if (v && typeof v === "object" && !Array.isArray(v)) {
      out[k] = sanitizeForLog(v as Record<string, unknown>);
    } else {
      out[k] = v;
    }
  }
  return out;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n}B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)}KB`;
  return `${(n / (1024 * 1024)).toFixed(1)}MB`;
}

/**
 * 按消息类型重建，保留 id / tool_calls / tool_call_id /
 * response_metadata / usage_metadata 等关键字段（写路径与 repair 共用）。
 */
export function rebuildMessageWithTextContent(msg: BaseMessage, content: string): BaseMessage {
  const raw = msg as unknown as Record<string, unknown>;
  const id = messageId(msg);
  const name = typeof raw.name === "string" ? raw.name : undefined;
  const type = msgType(msg);
  const additional_kwargs = raw.additional_kwargs as Record<string, unknown> | undefined;
  const response_metadata = raw.response_metadata as Record<string, unknown> | undefined;
  const usage_metadata = raw.usage_metadata as AIMessage["usage_metadata"] | undefined;

  if (type === "tool") {
    return new ToolMessage({
      content,
      tool_call_id: msgToolCallId(msg) || "unknown",
      name,
      id,
      status: raw.status === "error" ? "error" : raw.status === "success" ? "success" : undefined,
      additional_kwargs,
      response_metadata,
    });
  }

  if (type === "ai") {
    const calls = msgToolCalls(msg);
    return new AIMessage({
      content,
      id,
      name,
      additional_kwargs,
      response_metadata,
      usage_metadata,
      tool_calls: calls.length > 0 ? (calls as AIMessage["tool_calls"]) : undefined,
    });
  }

  if (type === "system") {
    return new SystemMessage({ content, id, name, additional_kwargs, response_metadata });
  }

  // human / 未知类型一律按 HumanMessage 处理（含 plain object type:"human"）
  return new HumanMessage({ content, id, name, additional_kwargs, response_metadata });
}
