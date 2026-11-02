import { describe, expect, it } from "vitest";
import { createPlatformToolDescriptors } from "../src/runtime/platform-tools/descriptor.js";
import { schemaToZodInput } from "../src/runtime/platform-tools/schema-to-zod.js";

describe("platform schema-driven tooling", () => {
  it("将 platformToolRefs 展开为 descriptor（toolName = targetType_targetId，从 schema 解析）", () => {
    const descriptors = createPlatformToolDescriptors([
      {
        targetType: "Plugin",
        targetId: 309,
        name: "联网搜索",
        schema: {
          method: "POST",
          url: "${PLATFORM_BASE_URL}/api/v1/4sandbox/agent/plugin/execute",
          authorization: "Bearer ${SANDBOX_ACCESS_KEY}",
          requestBody: {
            params: {
              type: "object",
              properties: { query: { type: "string" } },
              required: ["query"],
            },
          },
        },
      },
    ]);
    expect(descriptors).toHaveLength(1);
    expect(descriptors[0]?.toolName).toBe("Plugin_309");
    expect(descriptors[0]?.targetId).toBe(309);
    expect(descriptors[0]?.method).toBe("POST");
    expect(descriptors[0]?.url).toBe("${PLATFORM_BASE_URL}/api/v1/4sandbox/agent/plugin/execute");
    expect(descriptors[0]?.auth).toBe("Bearer ${SANDBOX_ACCESS_KEY}");
    // 参数 zod 从 requestBody.params 建
    const zod = schemaToZodInput(descriptors[0]?.inputSchema);
    expect(() => zod.parse({ query: "hello" })).not.toThrow();
  });

  it("schemaToZodInput 支持 object 必填与 optional 字段", () => {
    const schema = schemaToZodInput({
      type: "object",
      properties: {
        query: { type: "string" },
        limit: { type: "integer" },
      },
      required: ["query"],
    });

    expect(() => schema.parse({ query: "hello" })).not.toThrow();
    expect(() => schema.parse({ limit: 3 })).toThrow();
  });

  it("ref.toolName 优先于 targetType_targetId 自动拼", () => {
    const descriptors = createPlatformToolDescriptors([
      {
        targetType: "Plugin",
        targetId: 309,
        toolName: "web_search",
        schema: {
          method: "POST",
          url: "${PLATFORM_BASE_URL}/api/v1/4sandbox/agent/plugin/execute",
          authorization: "Bearer ${SANDBOX_ACCESS_KEY}",
          requestBody: { params: { type: "object", properties: { query: { type: "string" } } } },
        },
      },
    ]);
    expect(descriptors[0]?.toolName).toBe("web_search");
  });
});
