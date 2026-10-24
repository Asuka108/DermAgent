/**
 * ACP session/set_config_option(model) → per-session runtime 热切换。
 */
import { describe, it, expect } from "vitest";
import { createFlowHooks } from "../src/surfaces/acp/server.js";
import type { AppConfig } from "../src/runtime/config/config-schema.js";
import type { FlowExecutor } from "../src/core/flow-types.js";

const stubAppConfig = {
  agent: { name: "nuwax-flow-ts", description: "test" },
  model: { provider: "anthropic", name: "glm-5.2" },
} as AppConfig;

const stubExecutor: FlowExecutor = async (_query, _opts) => ({ answer: "ok" });

describe("onSessionConfigOption model hot reload", () => {
  it("model 变更时 dispose 旧 executor 并重建", async () => {
    const buildModels: Array<string | undefined> = [];
    let disposeCount = 0;

    const hooks = createFlowHooks({
      appConfig: stubAppConfig,
      createExecutor: async ({ sessionConfig }) => {
        buildModels.push(sessionConfig.model);
        return {
          executor: stubExecutor,
          dispose: async () => {
            disposeCount += 1;
          },
        };
      },
    });

    const sessionId = "sess-hot-reload";
    await hooks.configureSession?.({
      sessionId,
      agentName: "nuwax-flow-ts",
      phase: "new",
      params: { cwd: "/tmp/ws-hot" },
    });
    expect(buildModels).toEqual([undefined]);
    expect(disposeCount).toBe(0);

    await hooks.onSessionConfigOption?.({
      sessionId,
      configId: "model",
      value: "gpt-4o-mini",
    });
    expect(buildModels).toEqual([undefined, "gpt-4o-mini"]);
    expect(disposeCount).toBe(1);

    await hooks.onSessionConfigOption?.({
      sessionId,
      configId: "model",
      value: "gpt-4o-mini",
    });
    expect(buildModels).toEqual([undefined, "gpt-4o-mini"]);
    expect(disposeCount).toBe(1);
  });

  it("非 model configId 不触发重建", async () => {
    let buildCount = 0;
    const hooks = createFlowHooks({
      appConfig: stubAppConfig,
      createExecutor: async () => {
        buildCount += 1;
        return { executor: stubExecutor };
      },
    });

    const sessionId = "sess-mode-only";
    await hooks.configureSession?.({
      sessionId,
      agentName: "nuwax-flow-ts",
      phase: "new",
      params: { cwd: "/tmp/ws-mode" },
    });
    expect(buildCount).toBe(1);

    await hooks.onSessionConfigOption?.({
      sessionId,
      configId: "mode",
      value: "plan",
    });
    expect(buildCount).toBe(1);
  });
});
