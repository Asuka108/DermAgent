import type { PlatformToolDescriptor, PlatformToolRef } from "./types.js";
import { asObject } from "./schema-to-zod.js";

function toObject(value: unknown): Record<string, unknown> | undefined {
  if (!value) return undefined;
  if (typeof value === "string") {
    try {
      return asObject(JSON.parse(value));
    } catch {
      return undefined;
    }
  }
  return asObject(value);
}

/**
 * 平台工具的运行时工具名。优先 ref.toolName（平台返回的工具名，若有）；
 * 否则按 `${targetType}_${targetId}` 自动推导（如 `Plugin_309`、`Workflow_1309`，保留 targetType 原大小写）。
 */
export function platformToolName(ref: {
  targetType: string;
  targetId: number | string;
  toolName?: string;
}): string {
  return ref.toolName ?? `${ref.targetType}_${ref.targetId}`;
}

/**
 * 把 `platformToolRefs` 固化的平台工具引用（平台已登记工具的真实配置，
 * **不是**旧 scaffold `spec.tools` / `*.flow.json`）展开为可执行 descriptor。
 * 一个 ref（targetType+targetId）→ 一个 descriptor；工具名自动拼；
 * `method` / `url` / `auth` / 参数 schema 从 `schema` 解析（支持
 * `{ method, url, authorization, requestBody.params }` 这种平台接口定义结构）。
 */
export function createPlatformToolDescriptors(refs: PlatformToolRef[]): PlatformToolDescriptor[] {
  const descriptors: PlatformToolDescriptor[] = [];
  for (const ref of refs) {
    const parsedSchema = toObject(ref.schema);
    const requestBody = toObject(parsedSchema?.requestBody);
    const inputSchema =
      toObject(requestBody?.params) ??
      toObject(ref.inputSchema) ??
      toObject(parsedSchema?.inputSchema) ??
      toObject(parsedSchema?.input);
    descriptors.push({
      toolName: platformToolName(ref),
      targetType: ref.targetType,
      targetId: ref.targetId,
      displayName: ref.name,
      description: ref.description,
      rawSchema: ref.schema,
      inputSchema,
      method: typeof parsedSchema?.method === "string" ? parsedSchema.method : ref.method,
      url: typeof parsedSchema?.url === "string" ? parsedSchema.url : ref.url,
      auth:
        typeof parsedSchema?.authorization === "string" ? parsedSchema.authorization : ref.auth,
      contentType:
        typeof parsedSchema?.contentType === "string" ? parsedSchema.contentType : undefined,
    });
  }
  return descriptors;
}
