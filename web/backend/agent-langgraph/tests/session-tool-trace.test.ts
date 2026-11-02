import { afterEach, describe, expect, it, vi } from "vitest";
import { logPlatformModelEnvDiagnostics } from "../src/runtime/config/config-sources.js";
import { traceFlowCallbacks } from "../src/runtime/session-trace.js";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

function captureStderr(): { output: () => string } {
  const chunks: string[] = [];
  vi.spyOn(process.stderr, "write").mockImplementation((chunk) => {
    chunks.push(String(chunk));
    return true;
  });
  return { output: () => chunks.join("") };
}

describe("SMOKE_TOOL_TRACE", () => {
  it("不会像全局 debug 一样导致启动诊断泄露模型凭证", () => {
    vi.stubEnv("LOG_LEVEL", "info");
    vi.stubEnv("SMOKE_TOOL_TRACE", "1");
    vi.stubEnv("API_PROTOCOL", "OpenAI");
    vi.stubEnv("OPENAI_API_KEY", "sk-top-secret-value");
    const captured = captureStderr();

    logPlatformModelEnvDiagnostics();

    expect(captured.output()).toContain("platformModelEnv 收到真实值");
    expect(captured.output()).not.toContain("sk-top-secret-value");
  });

  it("在 info 级别输出可供 smoke 断言的安全工具摘要", async () => {
    vi.stubEnv("LOG_LEVEL", "info");
    vi.stubEnv("SMOKE_TOOL_TRACE", "1");
    const captured = captureStderr();
    const callbacks = traceFlowCallbacks({}, { sessionId: "sess-smoke" });

    await callbacks.onToolCall?.({
      toolCallId: "call-1",
      toolName: "search__web_search",
      args: { token: "secret-in-args" },
      status: "in_progress",
    });
    await callbacks.onToolCall?.({
      toolCallId: "call-1",
      toolName: "search__web_search",
      args: {},
      status: "completed",
      result: "search result",
    });

    expect(captured.output()).toContain("[info] [runtime:session-trace] tool invoke start");
    expect(captured.output()).toContain("tool invoke done");
    expect(captured.output()).toContain("resultChars=13");
    expect(captured.output()).not.toContain("secret-in-args");
    expect(captured.output()).not.toContain("search result");
  });

  it("失败摘要不泄露底层错误正文", async () => {
    vi.stubEnv("LOG_LEVEL", "info");
    vi.stubEnv("SMOKE_TOOL_TRACE", "1");
    const captured = captureStderr();
    const callbacks = traceFlowCallbacks({}, { sessionId: "sess-smoke" });

    await callbacks.onToolCall?.({
      toolCallId: "call-2",
      toolName: "search__web_search",
      args: {},
      status: "failed",
      error: "request failed with api_key=top-secret",
    });

    expect(captured.output()).toContain("tool invoke failed");
    expect(captured.output()).not.toContain("top-secret");
  });
});
