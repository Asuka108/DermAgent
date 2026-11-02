/**
 * Anthropic baseUrl 归一化：避免 LangChain SDK 打出 /v1/v1/messages。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { AppConfigSchema } from "../src/runtime/config/config-schema.js";
import { resolveModel } from "../src/runtime/context/model.js";

describe("resolveModel anthropic baseUrl", () => {
  const saved = process.env.ANTHROPIC_API_KEY;
  const fetchUrls: string[] = [];
  const requestBodies: Array<Record<string, unknown>> = [];

  beforeEach(() => {
    process.env.ANTHROPIC_API_KEY = "test-key";
    fetchUrls.length = 0;
    requestBodies.length = 0;
    vi.stubGlobal(
      "fetch",
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        fetchUrls.push(url);
        if (typeof init?.body === "string") {
          requestBodies.push(JSON.parse(init.body) as Record<string, unknown>);
        }
        return new Response(JSON.stringify({ error: { message: "probe" } }), {
          status: 400,
          headers: { "content-type": "application/json" },
        });
      }
    );
  });

  afterEach(() => {
    if (saved === undefined) delete process.env.ANTHROPIC_API_KEY;
    else process.env.ANTHROPIC_API_KEY = saved;
    vi.unstubAllGlobals();
  });

  it("baseUrl 不含 /v1 时请求 .../model/v1/messages（无双 v1）", async () => {
    const config = AppConfigSchema.parse({
      model: {
        provider: "anthropic",
        name: "glm-5.1",
        baseUrl: "https://test-llm-proxy.nuwax.com/api/proxy/model",
      },
    });
    const model = resolveModel(config);
    if (!model || typeof model === "string") throw new Error("expected instance");
    try {
      await model.invoke([{ role: "user", content: "hi" }]);
    } catch {
      /* 探针请求，状态码不重要 */
    }
    expect(fetchUrls.some((u) => u.includes("/api/proxy/model/v1/messages"))).toBe(true);
    expect(fetchUrls.some((u) => u.includes("/v1/v1/messages"))).toBe(false);
  });

  it("baseUrl 已含 /v1 时去掉尾部版本段，避免双 v1", async () => {
    const config = AppConfigSchema.parse({
      model: {
        provider: "anthropic",
        name: "glm-5.1",
        baseUrl: "https://test-llm-proxy.nuwax.com/api/proxy/model/v1",
      },
    });
    const model = resolveModel(config);
    if (!model || typeof model === "string") throw new Error("expected instance");
    try {
      await model.invoke([{ role: "user", content: "hi" }]);
    } catch {
      /* 探针请求 */
    }
    expect(fetchUrls.some((u) => u.includes("/api/proxy/model/v1/messages"))).toBe(true);
    expect(fetchUrls.some((u) => u.includes("/v1/v1/messages"))).toBe(false);
  });

  it("Anthropic 协议默认开启 extended thinking", async () => {
    const config = AppConfigSchema.parse({
      model: {
        provider: "anthropic",
        name: "glm-5.1",
        baseUrl: "https://test-llm-proxy.nuwax.com/api/proxy/model",
      },
    });
    const model = resolveModel(config);
    if (!model || typeof model === "string") throw new Error("expected instance");
    try {
      await model.invoke([{ role: "user", content: "hi" }]);
    } catch {
      /* 探针请求 */
    }
    expect(requestBodies.at(-1)?.thinking).toEqual({
      type: "enabled",
      budget_tokens: 1024,
    });
    expect(requestBodies.at(-1)).not.toHaveProperty("temperature");
  });

  it("官方 Claude Opus 4.7 默认使用 adaptive thinking", async () => {
    const config = AppConfigSchema.parse({
      model: {
        provider: "anthropic",
        name: "claude-opus-4-7",
        baseUrl: "https://api.anthropic.com",
      },
    });
    const model = resolveModel(config);
    if (!model || typeof model === "string") throw new Error("expected instance");
    try {
      await model.invoke([{ role: "user", content: "hi" }]);
    } catch {
      /* 探针请求 */
    }
    expect(requestBodies.at(-1)?.thinking).toEqual({ type: "adaptive" });
  });

  it("Anthropic 固定 thinking budget 必须严格小于 maxTokens", () => {
    const invalid = AppConfigSchema.safeParse({
      model: {
        provider: "anthropic",
        name: "glm-5.1",
        settings: { maxTokens: 1024 },
      },
    });
    expect(invalid.success).toBe(false);
    if (!invalid.success) {
      expect(invalid.error.issues[0]?.path).toEqual(["model", "settings", "maxTokens"]);
      expect(invalid.error.issues[0]?.message).toContain("thinkingBudgetTokens");
    }

    expect(
      AppConfigSchema.safeParse({
        model: {
          provider: "openai",
          name: "gpt-test",
          settings: { maxTokens: 1024 },
        },
      }).success
    ).toBe(true);
  });

  it("Anthropic thinking budget 可配置并写入请求体", async () => {
    const config = AppConfigSchema.parse({
      model: {
        provider: "anthropic",
        name: "glm-5.1",
        baseUrl: "https://test-llm-proxy.nuwax.com/api/proxy/model",
        settings: { maxTokens: 8192, thinkingBudgetTokens: 2048 },
      },
    });
    const model = resolveModel(config);
    if (!model || typeof model === "string") throw new Error("expected instance");
    try {
      await model.invoke([{ role: "user", content: "hi" }]);
    } catch {
      /* 探针请求 */
    }
    expect(requestBodies.at(-1)?.thinking).toEqual({
      type: "enabled",
      budget_tokens: 2048,
    });
  });
});
