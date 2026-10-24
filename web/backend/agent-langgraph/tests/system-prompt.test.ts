/**
 * resolveSystemPromptMeta —— ACP session 提示词语义。
 *
 * 关键契约：平台经 ACP 下发 systemPrompt 时，它是 agent 的权威身份，**不再前置**本地
 * flow.base.md（那是模板通用基座，会污染业务身份并与 PLATFORM_CONVENTIONS 工具优先级冲突）；
 * 仅在末尾补 PLATFORM_CONVENTIONS。flow.base.md 只用于「无平台 session」的本地/CLI 路径。
 */

import { describe, it, expect } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { AppConfigSchema } from "../src/runtime/config/config-schema.js";
import { resolveSystemPromptMeta } from "../src/runtime/context/prompt.js";
import { PLATFORM_CONVENTIONS } from "../src/runtime/context/harness-profile.js";
import { resolvePackageRoot } from "../src/runtime/package-root.js";

describe("resolveSystemPromptMeta — ACP session append", () => {
  const pkgRoot = resolvePackageRoot(import.meta.url);

  it("有 session prompt（即便本地存在 flow.base.md）→ 平台人设为主，不前置 flow.base.md，末尾补 PLATFORM_CONVENTIONS", () => {
    const config = AppConfigSchema.parse({});
    const meta = resolveSystemPromptMeta(
      config,
      { systemPrompt: "平台业务人设" },
      pkgRoot
    );

    expect(meta.source).toBe("acp-session");
    // flow.base.md 的身份句不得混入（防身份污染 + 工具优先级冲突）
    expect(meta.prompt).not.toContain("LangGraph 工作流图");
    // 平台人设在前，PLATFORM_CONVENTIONS 在后
    expect(meta.prompt).toBe(`平台业务人设\n\n${PLATFORM_CONVENTIONS}`);
    expect(meta.prompt.indexOf("平台业务人设")).toBeLessThan(
      meta.prompt.indexOf("Tool Selection Priority")
    );
  });

  it("有 session prompt + 提示词文件不存在 → 仅 session + PLATFORM_CONVENTIONS", () => {
    const workspace = mkdtempSync(join(tmpdir(), "flow-prompt-test-"));
    const config = AppConfigSchema.parse({
      agent: { systemPromptPath: "prompts/missing-flow.base.md" },
    });

    const meta = resolveSystemPromptMeta(
      config,
      { systemPrompt: "仅平台指令" },
      workspace
    );

    expect(meta.source).toBe("acp-session");
    expect(meta.prompt).toBe(`仅平台指令\n\n${PLATFORM_CONVENTIONS}`);
  });

  it("无 session prompt → 从 flow.base.md 加载（config-file）", () => {
    const config = AppConfigSchema.parse({});
    const meta = resolveSystemPromptMeta(config, undefined, pkgRoot);

    expect(meta.source).toBe("config-file");
    expect(meta.prompt).toContain("LangGraph 工作流图");
    expect(meta.prompt).not.toContain(PLATFORM_CONVENTIONS);
  });

  it("ACP session 下即便配了自定义 systemPromptPath，也不加载为 base（平台人设为主）", () => {
    const workspace = mkdtempSync(join(tmpdir(), "flow-prompt-custom-"));
    mkdirSync(join(workspace, "prompts"), { recursive: true });
    writeFileSync(
      join(workspace, "prompts/custom.md"),
      "# Title\n\n本地脚手架身份\n",
      "utf-8"
    );
    const config = AppConfigSchema.parse({
      agent: { systemPromptPath: "prompts/custom.md" },
    });

    const meta = resolveSystemPromptMeta(
      config,
      { systemPrompt: "平台人设" },
      workspace
    );

    expect(meta.source).toBe("acp-session");
    // 本地文件不再被前置
    expect(meta.prompt).not.toContain("本地脚手架身份");
    expect(meta.prompt).toBe(`平台人设\n\n${PLATFORM_CONVENTIONS}`);
  });
});
