/**
 * DeepAgentsServer handleLoadSession —— 进程重启后从磁盘 hydrate SessionState。
 *
 * 回归：内存 sessions Map 为空时 session/load 不应抛 Session not found，
 * 而应重建 SessionState 并触发 configureSession(phase=load)。
 */

import { describe, it, expect, afterAll } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  StateGraph,
  START,
  END,
  Annotation,
  interrupt,
  type BaseCheckpointSaver,
} from "@langchain/langgraph";
import { DeepAgentsServer } from "../src/libs/deepagents-acp/server.js";
import { createStatefulFlow } from "../src/surfaces/stateful-flow.js";
import { FileCheckpointSaver } from "../src/runtime/services/file-checkpoint-saver.js";
import { createFlowHooks, type SessionExecutor } from "../src/surfaces/acp/server.js";
import type { AppConfig } from "../src/runtime/index.js";

const ToyState = Annotation.Root({
  query: Annotation<string>,
  reply: Annotation<string>,
  output: Annotation<string>,
});
type ToyStateType = typeof ToyState.State;

function buildToyGraph(checkpointer: BaseCheckpointSaver) {
  return new StateGraph(ToyState)
    .addNode("ask", () => {
      const fb = interrupt({ question: "确认吗？" });
      return { reply: String(fb ?? "") };
    })
    .addNode("finish", (s: ToyStateType) => ({ output: `done:${s.reply}` }))
    .addEdge(START, "ask")
    .addEdge("ask", "finish")
    .addEdge("finish", END)
    .compile({ checkpointer });
}

const makeFlow = (cp: BaseCheckpointSaver) =>
  createStatefulFlow<ToyStateType>({
    buildGraph: (saver) => buildToyGraph(saver),
    toInput: (query) => ({ query }),
    toResult: (v) => ({ answer: v.output ?? "" }),
    checkpointer: cp,
  });

const tmpDirs: string[] = [];
function freshDir(): string {
  const d = mkdtempSync(join(tmpdir(), "flow-acp-load-"));
  tmpDirs.push(d);
  return d;
}
afterAll(() => {
  for (const d of tmpDirs) rmSync(d, { recursive: true, force: true });
});

const fakeAppConfig = {
  agent: { name: "test-flow", version: "0.0.0", description: "test" },
} as unknown as AppConfig;

const AGENT_NAME = "test-flow";

function makeFakeConn() {
  return {
    async sessionUpdate() {
      /* no-op */
    },
  };
}

/** 访问 DeepAgentsServer 私有 handleLoadSession（in-process 回归专用）。 */
type LoadSessionServer = DeepAgentsServer & {
  handleLoadSession(
    params: Record<string, unknown>,
    conn: ReturnType<typeof makeFakeConn>
  ): Promise<{ modes: { currentModeId: string } }>;
};

describe("handleLoadSession hydrate（跨进程 load）", () => {
  it("sessions Map 为空时 load 重建 SessionState 并触发 configureSession(phase=load)", async () => {
    const dir = freshDir();
    const sessionId = "sess_hydrate_acp_01";
    const configurePhases: string[] = [];

    let current = makeFlow(new FileCheckpointSaver({ dir }));
    const flowByDir = new Map([[dir, current]]);
    const hooks = createFlowHooks({
      appConfig: fakeAppConfig,
      createExecutor: async (args): Promise<SessionExecutor> => ({
        executor: flowByDir.get(args.sessionConfig.cwd)!,
      }),
    });

    const wrappedHooks = {
      ...hooks,
      async configureSession(
        ctx: Parameters<NonNullable<typeof hooks.configureSession>>[0]
      ) {
        configurePhases.push(ctx.phase);
        return hooks.configureSession!(ctx);
      },
    };

    const server = new DeepAgentsServer({
      agents: {
        name: AGENT_NAME,
        description: "test agent",
        tools: [],
      },
      serverName: AGENT_NAME,
      hooks: wrappedHooks,
    });

    // initializeSession 会 createAgent；注入占位避免真实 LLM / deepagents 依赖。
    const internals = server as unknown as {
      agents: Map<string, object>;
    };
    internals.agents.set(AGENT_NAME, {} as object);

    const conn = makeFakeConn();
    const response = await (server as LoadSessionServer).handleLoadSession(
      { sessionId, cwd: dir },
      conn
    );

    expect(response.modes.currentModeId).toBe("agent");
    expect(configurePhases).toContain("load");
  });

  it("无注册 agent 时仍抛 Session not found", async () => {
    const server = new DeepAgentsServer({
      agents: {
        name: AGENT_NAME,
        description: "test agent",
        tools: [],
      },
      hooks: createFlowHooks({ appConfig: fakeAppConfig }),
    });

    const internals = server as unknown as { agents: Map<string, object> };
    internals.agents.set(AGENT_NAME, {} as object);

    await expect(
      (server as LoadSessionServer).handleLoadSession(
        {
          sessionId: "sess_missing_agent",
          configOptions: { agent: "nonexistent-agent" },
        },
        makeFakeConn()
      )
    ).rejects.toThrow("Session not found");
  });
});
