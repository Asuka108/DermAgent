/**
 * model.provider 解析：API_PROTOCOL / LLM_PROVIDER 优先级与凭证启发式。
 */

import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { AppConfigSchema } from "../src/runtime/config/config-schema.js";
import {
  normalizeModelProvider,
  resolveExplicitModelProviderFromEnv,
  inferModelProviderIfUnset,
  loadFromEnv,
  hasUnresolvedModelProviderPlaceholder,
  collectPlatformModelEnvRaw,
} from "../src/runtime/config/config-sources.js";

describe("hasUnresolvedModelProviderPlaceholder", () => {
  it("识别未替换的平台占位符", () => {
    expect(hasUnresolvedModelProviderPlaceholder("{MODEL_PROVIDER_API_KEY}")).toBe(true);
    expect(hasUnresolvedModelProviderPlaceholder("https://x.com/{MODEL_PROVIDER_BASE_URL}/v1")).toBe(
      true
    );
    expect(hasUnresolvedModelProviderPlaceholder("ak-eb7d7a9ef2be4e33a0bb4f0612de6e29")).toBe(false);
  });
});

describe("normalizeModelProvider", () => {
  it("归一化大小写", () => {
    expect(normalizeModelProvider("Anthropic")).toBe("anthropic");
    expect(normalizeModelProvider("OPENAI")).toBe("openai");
  });

  it("非法值返回 null", () => {
    expect(normalizeModelProvider("glm")).toBeNull();
  });
});

describe("AppConfigSchema model settings", () => {
  it("保留 supportsVision 配置，供消息 content coerce 判定使用", () => {
    const parsed = AppConfigSchema.parse({
      model: { settings: { temperature: 0, supportsVision: true } },
    });

    expect(parsed.model.settings.supportsVision).toBe(true);
  });
});

describe("inferModelProviderIfUnset", () => {
  const protocolEnvKeys = ["API_PROTOCOL", "LLM_PROVIDER"] as const;
  const credEnvKeys = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
  ] as const;
  const saved: Record<string, string | undefined> = {};

  beforeEach(() => {
    for (const key of [...protocolEnvKeys, ...credEnvKeys]) {
      saved[key] = process.env[key];
      delete process.env[key];
    }
  });

  afterEach(() => {
    for (const key of [...protocolEnvKeys, ...credEnvKeys]) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key]!;
    }
  });

  const base = AppConfigSchema.parse({
    model: { provider: "openai", name: "deepseek-chat" },
  });

  it("API_PROTOCOL=anthropic 优先于双凭证启发式 openai", () => {
    process.env.API_PROTOCOL = "anthropic";
    process.env.ANTHROPIC_API_KEY = "ak-test";
    process.env.OPENAI_API_KEY = "sk-test";
    const result = inferModelProviderIfUnset(base);
    expect(result.model.provider).toBe("anthropic");
    expect(result.model.apiKeyEnv).toBe("ANTHROPIC_API_KEY");
  });

  it("API_PROTOCOL 大小写不敏感", () => {
    process.env.API_PROTOCOL = "Anthropic";
    const result = inferModelProviderIfUnset(base);
    expect(result.model.provider).toBe("anthropic");
  });

  it("API_PROTOCOL 优先于 LLM_PROVIDER", () => {
    process.env.API_PROTOCOL = "anthropic";
    process.env.LLM_PROVIDER = "openai";
    expect(resolveExplicitModelProviderFromEnv()).toBe("anthropic");
    const result = inferModelProviderIfUnset(base);
    expect(result.model.provider).toBe("anthropic");
  });

  it("无显式协议时双凭证 + OPENAI_API_KEY → openai", () => {
    process.env.ANTHROPIC_API_KEY = "ak-test";
    process.env.OPENAI_API_KEY = "sk-test";
    const result = inferModelProviderIfUnset({
      ...base,
      model: { ...base.model, provider: "anthropic" },
    });
    expect(result.model.provider).toBe("openai");
    expect(result.model.apiKeyEnv).toBe("OPENAI_API_KEY");
  });

  it("无显式协议时仅 ANTHROPIC_* → anthropic", () => {
    process.env.ANTHROPIC_API_KEY = "ak-test";
    const result = inferModelProviderIfUnset(base);
    expect(result.model.provider).toBe("anthropic");
  });

  it("loadFromEnv 归一化 API_PROTOCOL", () => {
    process.env.API_PROTOCOL = "openai";
    const overlay = loadFromEnv();
    expect(overlay.model?.provider).toBe("openai");
  });

  it("collectPlatformModelEnvRaw 收集真实 env", () => {
    process.env.API_PROTOCOL = "anthropic";
    process.env.ANTHROPIC_MODEL = "glm-5.1";
    process.env.ANTHROPIC_BASE_URL = "https://proxy.example/v1";
    process.env.ANTHROPIC_API_KEY = "ak-test-key";
    const raw = collectPlatformModelEnvRaw();
    expect(raw.API_PROTOCOL).toBe("anthropic");
    expect(raw.ANTHROPIC_MODEL).toBe("glm-5.1");
    expect(raw.ANTHROPIC_BASE_URL).toBe("https://proxy.example/v1");
    expect(raw.ANTHROPIC_API_KEY).toBe("ak-test-key");
  });
});
