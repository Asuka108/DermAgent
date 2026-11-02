import { StructuredTool } from "@langchain/core/tools";
import type { PlatformToolDescriptor } from "./types.js";
import { schemaToZodInput } from "./schema-to-zod.js";

interface CreatePlatformStructuredToolOptions {
  descriptor: PlatformToolDescriptor;
}

interface SseFinalResult {
  data?: unknown;
  error?: { message?: string; code?: string; [key: string]: unknown } | null;
}

/** 从 SSE 流文本提取 FinalResult 事件的 data（Workflow 流式返回，取最后一个 FinalResult）。 */
function parseSseFinalResult(text: string): SseFinalResult | undefined {
  // SSE 行尾可能是 \r\n / \r / \n，先规范化再按空行分块
  const blocks = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split(/\n\n+/);
  // 先尝试标准 SSE（event: 行），无命中则回退检查 data 行中的 JSON 内嵌 type 字段
  for (let i = blocks.length - 1; i >= 0; i--) {
    const lines = blocks[i]!.split("\n");
    let event = "";
    const dataLines: string[] = [];
    for (const line of lines) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (event === "FinalResult" && dataLines.length) {
      try {
        return JSON.parse(dataLines.join("\n")) as SseFinalResult;
      } catch {
        return undefined;
      }
    }
  }
  // 回退：逐块检查 data 行 JSON 中是否包含 "type":"FinalResult"
  for (let i = blocks.length - 1; i >= 0; i--) {
    const lines = blocks[i]!.split("\n");
    const dataLines: string[] = [];
    for (const line of lines) {
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) continue;
    try {
      const parsed = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
      if (parsed.type === "FinalResult") {
        return parsed as unknown as SseFinalResult;
      }
    } catch {
      // skip unparseable blocks
    }
  }
  return undefined;
}

export function createPlatformStructuredTool({
  descriptor,
}: CreatePlatformStructuredToolOptions): StructuredTool {
  const schema = schemaToZodInput(descriptor.inputSchema ?? descriptor.rawSchema);

  return new (class extends StructuredTool {
    name = descriptor.toolName;
    description =
      descriptor.description ??
      descriptor.displayName ??
      `Execute platform ${descriptor.targetType} tool (${descriptor.targetId}) via schema-driven runtime`;
    schema = schema;

    async _call(input: Record<string, unknown>): Promise<string> {
      const baseUrl = process.env.PLATFORM_BASE_URL?.trim();
      const accessKey = process.env.SANDBOX_ACCESS_KEY?.trim();
      if (!baseUrl || !accessKey) {
        throw new Error(
          `[${descriptor.toolName}] missing env PLATFORM_BASE_URL or SANDBOX_ACCESS_KEY for platform execute`
        );
      }
      // url / method / authorization 从固化 schema 读，${...} 占位符由运行时 env 替换。
      // schema.url 必须随平台登记配置固化带来；缺 url 时拒绝执行（不猜测端点，避免 Knowledge 等未验证路由 404）。
      if (!descriptor.url) {
        throw new Error(
          `[${descriptor.toolName}] missing descriptor.url (platformToolRefs 未固化 url；请写入平台已登记工具的完整 schema)`
        );
      }
      const url = descriptor.url.replace(/\$\{PLATFORM_BASE_URL\}/g, baseUrl);
      const method = descriptor.method ?? "POST";
      const authTemplate =
        typeof descriptor.auth === "string" && descriptor.auth.length > 0
          ? descriptor.auth
          : "Bearer ${SANDBOX_ACCESS_KEY}";
      const authorization = authTemplate.replace(/\$\{SANDBOX_ACCESS_KEY\}/g, accessKey);
      // body 按 schema.requestBody 结构。devAgentId 由运行时 env 注入——执行接口需它定位 plugin
      // 上下文（不带会 "Error plugin id"）；schema 注释称"仅调试"但实测执行必需。
      const devAgentId = process.env.DEV_AGENT_ID?.trim();
      const payload: Record<string, unknown> = {
        targetType: descriptor.targetType,
        targetId: descriptor.targetId,
        params: input,
      };
      if (devAgentId) payload.devAgentId = devAgentId;
      const acceptType = descriptor.contentType ?? "application/json; charset=utf-8";
      const resp = await fetch(url, {
        method,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          Accept: acceptType,
          Authorization: authorization,
        },
        body: JSON.stringify(payload),
      });
      const respText = await resp.text();
      const contentType = resp.headers.get("content-type") ?? "";
      if (!resp.ok) {
        throw new Error(`[${descriptor.toolName}] http ${resp.status}: ${respText || contentType}`);
      }
      // Workflow 多为 SSE(text/event-stream) → 取 FinalResult；Plugin 为 JSON。
      if (contentType.includes("text/event-stream")) {
        const finalResult = parseSseFinalResult(respText);
        if (!finalResult) {
          throw new Error(
            `[${descriptor.toolName}] SSE 缺 FinalResult 事件: ${respText.slice(0, 200)}`
          );
        }
        const errObj = finalResult.error;
        if (errObj) {
          throw new Error(
            `[${descriptor.toolName}] ${errObj.message ?? errObj.code ?? JSON.stringify(errObj)}`
          );
        }
        return JSON.stringify(finalResult.data ?? {});
      }
      // 非事件流：先 text 再 try JSON.parse
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(respText) as Record<string, unknown>;
      } catch {
        throw new Error(
          `[${descriptor.toolName}] 非 JSON 响应 (${contentType}): ${respText.slice(0, 200)}`
        );
      }
      // 平台 Plugin 响应格式多样：{success,data} / {code:"200",data} / 裸数据
      // 先检查 error 字段，再检查 success/code，最后视为成功数据
      const errVal = parsed.error;
      if (errVal && typeof errVal === "object" && Object.keys(errVal as object).length > 0) {
        const errObj = errVal as { message?: string; code?: string };
        throw new Error(
          `[${descriptor.toolName}] ${errObj.message ?? errObj.code ?? JSON.stringify(errObj)}`
        );
      }
      if (typeof errVal === "string" && errVal.length > 0) {
        throw new Error(`[${descriptor.toolName}] ${errVal}`);
      }
      if (parsed.code !== undefined && String(parsed.code) !== "200" && String(parsed.code) !== "0000") {
        throw new Error(`[${descriptor.toolName}] code=${parsed.code}`);
      }
      if (parsed.success !== undefined && !parsed.success) {
        const msg = (typeof parsed.message === "string" && parsed.message) || "platform execute failed";
        throw new Error(`[${descriptor.toolName}] ${msg}`);
      }
      return JSON.stringify(parsed.data ?? parsed);
    }
  })();
}
