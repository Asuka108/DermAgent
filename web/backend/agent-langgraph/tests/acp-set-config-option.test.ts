/**
 * ACP session/set_config_option — nuwaclaw prompt 前 model 同步兼容。
 */
import { describe, it, expect } from "vitest";
import type { SessionState } from "../src/libs/deepagents-acp/types.js";
import {
  applySessionConfigOptionPatch,
  buildSessionConfigOptionsSnapshot,
  resolveEnvModelId,
} from "../src/libs/deepagents-acp/session-config-option.js";

function stubSession(): Pick<SessionState, "mode" | "modelId"> {
  return {};
}

describe("applySessionConfigOptionPatch", () => {
  it("configId=model 时写入 session.modelId", () => {
    const session = stubSession();
    const result = applySessionConfigOptionPatch(
      session,
      { configId: "model", value: "glm-5.2" },
      { envModelId: "glm-5.2" },
    );
    expect(result.kind).toBe("model");
    expect(result.runtimeModelMismatch).toBeUndefined();
    expect(session.modelId).toBe("glm-5.2");
  });

  it("model 与 env 不一致时返回 runtimeModelMismatch", () => {
    const session = stubSession();
    const result = applySessionConfigOptionPatch(
      session,
      { configId: "model", value: "gpt-4o" },
      { envModelId: "glm-5.2" },
    );
    expect(result.kind).toBe("model");
    expect(result.runtimeModelMismatch).toEqual({
      requested: "gpt-4o",
      envModel: "glm-5.2",
    });
    expect(session.modelId).toBe("gpt-4o");
  });

  it("configId=mode 时写入 session.mode", () => {
    const session = stubSession();
    const result = applySessionConfigOptionPatch(session, {
      configId: "mode",
      value: "plan",
    });
    expect(result.kind).toBe("mode");
    expect(session.mode).toBe("plan");
  });

  it("未知 configId 忽略且不抛错", () => {
    const session = stubSession();
    const result = applySessionConfigOptionPatch(session, {
      configId: "theme",
      value: "dark",
    });
    expect(result.kind).toBe("ignored");
    expect(session.modelId).toBeUndefined();
  });
});

describe("buildSessionConfigOptionsSnapshot", () => {
  it("含 model 与 mode，且 env fallback 可注入", () => {
    const session: Pick<SessionState, "mode" | "modelId"> = {
      modelId: "glm-5.2",
      mode: "plan",
    };
    const options = buildSessionConfigOptionsSnapshot(session);
    expect(options.map((o) => o.id).sort()).toEqual(["mode", "model"]);
    expect(options.find((o) => o.id === "model")?.currentValue).toBe("glm-5.2");
    expect(options.find((o) => o.id === "mode")?.currentValue).toBe("plan");
    expect(options.every((o) => o.type === "select")).toBe(true);
  });

  it("resolveEnvModelId 优先级 ANTHROPIC > OPENCODE > OPENAI", () => {
    expect(
      resolveEnvModelId({
        ANTHROPIC_MODEL: "a",
        OPENCODE_MODEL: "b",
        OPENAI_MODEL: "c",
      }),
    ).toBe("a");
    expect(
      resolveEnvModelId({
        OPENCODE_MODEL: "b",
        OPENAI_MODEL: "c",
      }),
    ).toBe("b");
  });
});
