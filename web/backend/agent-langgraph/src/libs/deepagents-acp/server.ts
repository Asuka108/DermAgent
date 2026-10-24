// 搬入自维护的 ACP 协议层（deepagents-acp 0.1.3）。它与 npm @agentclientprotocol/sdk 0.24.0
// 的协议类型演进差异（SessionNotification / *Response）已收窄为少量带说明的局部
// `@ts-expect-error`（搜本文件 "演进差异" 即可定位）；不再整文件 @ts-nocheck，恢复类型检查防回归。
/**
 * DeepAgents ACP Server Implementation
 *
 * This module provides an ACP (Agent Client Protocol) server that wraps
 * DeepAgents, enabling integration with ACP platform hosts (e.g. NuwaClaw).
 *
 * @see https://agentclientprotocol.com
 * @see https://github.com/agentclientprotocol/typescript-sdk
 */

import {
  AgentSideConnection,
  ndJsonStream,
  type Agent,
  type ContentBlock,
  type SetSessionConfigOptionResponse,
} from "@agentclientprotocol/sdk";
import { randomUUID } from "node:crypto";

import {
  createDeepAgent,
  FilesystemBackend,
  type BackendProtocolV2,
  type BackendFactory,
} from "deepagents";

import { ACPFilesystemBackend } from "./acp-filesystem-backend.js";
import { applySessionConfigOptionPatch, buildSessionConfigOptionsSnapshot, resolveEnvModelId } from "./session-config-option.js";

import {
  type BaseMessage,
  HumanMessage,
  AIMessage,
  ToolMessage,
} from "@langchain/core/messages";
import type { InteropZodObject } from "@langchain/core/utils/types";
import { MemorySaver } from "@langchain/langgraph-checkpoint";

import type {
  DeepAgentConfig,
  DeepAgentsServerOptions,
  DeepAgentsServerHooks,
  SessionConfigurePatch,
  SessionState,
  ToolCallInfo,
  StopReason,
  ACPCapabilities,
  ACPAuthMethod,
} from "./types.js";

import { Logger, createLogger } from "./logger.js";

import {
  acpPromptToHumanMessage,
  langChainMessageToACP,
  extractToolCalls,
  todosToPlanEntries,
  generateSessionId,
} from "./adapter.js";
import {
  toolInfoFromToolEvent,
  toolUpdateFromToolResult,
  buildPermissionToolCall,
} from "./acp-tool-presentation.js";

// Type definitions for ACP requests/responses (SDK uses generic types)
type InitializeRequest = Record<string, unknown>;
type InitializeResponse = Record<string, unknown>;
type NewSessionRequest = Record<string, unknown>;
type NewSessionResponse = Record<string, unknown>;
type LoadSessionRequest = Record<string, unknown>;
type LoadSessionResponse = Record<string, unknown>;
type PromptRequest = Record<string, unknown>;
type PromptResponse = Record<string, unknown>;
type CancelNotification = Record<string, unknown>;
type SetSessionModeRequest = Record<string, unknown>;
type SetSessionModeResponse = Record<string, unknown>;
type SetSessionConfigOptionRequest = Record<string, unknown>;
type AuthenticateRequest = Record<string, unknown>;
type AuthenticateResponse = Record<string, unknown>;
type SessionNotification = Record<string, unknown>;

const DEFAULT_AUTH_METHODS: ACPAuthMethod[] = [
  {
    id: "anthropic",
    name: "Anthropic API Key",
    type: "env_var",
    vars: [{ name: "ANTHROPIC_API_KEY" }],
    link: "https://console.anthropic.com/settings/keys",
  },
  {
    id: "openai",
    name: "OpenAI API Key",
    type: "env_var",
    vars: [{ name: "OPENAI_API_KEY" }],
    link: "https://platform.openai.com/api-keys",
  },
  {
    id: "deepagents-setup",
    name: "DeepAgents Setup",
    description: "Configure LLM provider credentials via environment variables",
  },
];

const AVAILABLE_MODES = [
  { id: "agent", name: "Agent Mode", description: "Full autonomous agent" },
  { id: "plan", name: "Plan Mode", description: "Planning and discussion" },
  { id: "ask", name: "Ask Mode", description: "Q&A without file changes" },
];

const DEFAULT_COMMANDS = [
  { name: "plan", description: "Switch to plan mode (read-only planning)" },
  { name: "agent", description: "Switch to agent mode (full autonomous)" },
  { name: "ask", description: "Switch to ask mode (Q&A, no file changes)" },
  { name: "clear", description: "Clear conversation context and start fresh" },
  {
    name: "status",
    description: "Show current session status and loaded skills",
  },
];

/**
 * DeepAgents ACP Server
 *
 * Wraps DeepAgents with the Agent Client Protocol, enabling communication
 * with ACP-compatible platform hosts.
 *
 * @example
 * ```typescript
 * import { DeepAgentsServer } from "deepagents-acp";
 *
 * const server = new DeepAgentsServer({
 *   agents: {
 *     name: "coding-assistant",
 *     description: "AI coding assistant with filesystem access",
 *   },
 *   workspaceRoot: process.cwd(),
 * });
 *
 * await server.start();
 * ```
 */
export class DeepAgentsServer {
  private connection: AgentSideConnection | null = null;
  private agents: Map<string, ReturnType<typeof createDeepAgent>> = new Map();
  private agentConfigs: Map<string, DeepAgentConfig> = new Map();
  private sessions: Map<string, SessionState> = new Map();
  private checkpointer: MemorySaver;
  private clientCapabilities: ACPCapabilities = {};
  private isRunning = false;
  /**
   * Per-session abort controllers for in-flight prompts, keyed by sessionId.
   * `session/cancel` targets only the cancelling session, so concurrent prompts
   * on different sessions no longer clobber each other's controller. ACP
   * guarantees one prompt at a time per session, so a single entry per id suffices.
   */
  private readonly promptAbortControllers = new Map<string, AbortController>();
  private acpBackends: Map<string, ACPFilesystemBackend> = new Map();

  private readonly serverName: string;
  private readonly serverVersion: string;
  private readonly debug: boolean;
  // Not readonly: a configureSession hook may switch the workspace root per session.
  private workspaceRoot: string;
  private readonly authMethods: ACPAuthMethod[];
  private readonly hooks?: DeepAgentsServerHooks;
  private readonly logger: Logger;

  constructor(options: DeepAgentsServerOptions) {
    this.serverName = options.serverName ?? "deepagents-acp";
    this.serverVersion = options.serverVersion ?? "0.0.1";
    this.debug = options.debug ?? false;
    this.workspaceRoot = options.workspaceRoot ?? process.cwd();
    this.authMethods = options.authMethods ?? DEFAULT_AUTH_METHODS;
    this.hooks = options.hooks;

    // Initialize logger with debug and/or file logging
    this.logger = createLogger({
      debug: this.debug,
      logFile: options.logFile,
      prefix: "[deepagents-acp]",
    });

    // Shared checkpointer for session persistence
    this.checkpointer = new MemorySaver();

    // Initialize agent configurations
    const agentConfigs = Array.isArray(options.agents)
      ? options.agents
      : [options.agents];

    for (const config of agentConfigs) {
      this.agentConfigs.set(config.name, config);
    }

    this.log("Initialized with agents:", [...this.agentConfigs.keys()]);

    if (options.logFile) {
      this.log("Logging to file:", options.logFile);
    }
  }

  /**
   * Start the ACP server and listen for connections
   *
   * Uses stdio transport by default (stdin/stdout)
   */
  async start(): Promise<void> {
    if (this.isRunning) {
      throw new Error("Server is already running");
    }

    // Set up process signal handlers for graceful shutdown
    const handleSignal = (signal: string) => {
      this.log(`Received ${signal}, shutting down...`);
      this.stop();
      process.exit(0);
    };

    process.on("SIGINT", () => handleSignal("SIGINT"));
    process.on("SIGTERM", () => handleSignal("SIGTERM"));

    // Handle uncaught errors
    process.on("uncaughtException", (err) => {
      this.log("Uncaught exception:", err);
      process.exit(1);
    });

    process.on("unhandledRejection", (reason) => {
      this.log("Unhandled rejection:", reason);
      // Don't exit - try to keep running
    });

    try {
      // Create the stdio stream for ACP communication
      // ndJsonStream signature: (output: WritableStream, input: ReadableStream)
      // output = where we write responses (stdout)
      // input = where we read requests from (stdin)
      const input = new ReadableStream<Uint8Array>({
        start: (controller) => {
          // Keep stdin open in raw mode for continuous reading
          if (process.stdin.isTTY) {
            process.stdin.setRawMode(true);
          }
          process.stdin.resume();

          process.stdin.on("data", (chunk: Buffer) => {
            try {
              controller.enqueue(new Uint8Array(chunk));
            } catch (err) {
              this.log("Error enqueueing data:", err);
            }
          });

          process.stdin.on("end", () => {
            this.log("stdin ended");
            try {
              controller.close();
            } catch {
              // Controller may already be closed
            }
          });

          process.stdin.on("error", (err) => {
            this.log("stdin error:", err);
            try {
              controller.error(err);
            } catch {
              // Controller may already be errored
            }
          });

          process.stdin.on("close", () => {
            this.log("stdin closed");
          });
        },
      });

      const output = new WritableStream<Uint8Array>({
        write: (chunk) => {
          return new Promise((resolve, reject) => {
            if (!process.stdout.writable) {
              this.log("stdout not writable, dropping message");
              resolve();
              return;
            }
            process.stdout.write(chunk, (err) => {
              if (err) {
                this.log("stdout write error:", err);
                reject(err);
              } else {
                resolve();
              }
            });
          });
        },
        close: () => {
          this.log("output stream closed");
        },
        abort: (reason) => {
          this.log("output stream aborted:", reason);
        },
      });

      // ndJsonStream(output, input) - output first, then input
      const stream = ndJsonStream(output, input);

      // Create the agent-side connection with our Agent implementation
      this.connection = new AgentSideConnection(
        (conn) => this.createAgentHandler(conn),
        stream,
      );

      this.isRunning = true;
      this.log("Server started, waiting for connections...");

      // Wait for the connection to close
      await this.connection.closed;

      this.isRunning = false;
      this.log("Server stopped");
    } catch (err) {
      this.log("Server error:", err);
      this.isRunning = false;
      throw err;
    }
  }

  /**
   * Stop the ACP server
   */
  async stop(): Promise<void> {
    if (!this.isRunning) {
      return;
    }

    this.isRunning = false;
    this.connection = null;
    // 先逐个 closeSession（触发 onSessionClosed hook → host dispose → MCP client.close()），
    // 再清 Map —— 避免 SIGINT 时 MCP stdio 子进程仅靠 OS 回收、host 的 per-session 清理
    // （日志 flush / 状态持久化）丢失。单个 session 关闭失败不影响其余。
    for (const sessionId of [...this.sessions.keys()]) {
      try {
        await this.closeSession(sessionId);
      } catch (err) {
        this.log("Failed to close session on stop:", sessionId, err);
      }
    }
    this.sessions.clear();
    this.log("Server stopped");

    // Close the logger to flush any pending writes
    await this.logger.close();
  }

  /**
   * Create the Agent handler for ACP
   */
  private createAgentHandler(conn: AgentSideConnection): Agent {
    // 对象按 Agent 接口检查（params 由接口推断）。仅 initialize/newSession/prompt 的
    // *Response 类型与 @agentclientprotocol/sdk@0.24 有演进差异，逐个局部抑制。
    return {
      // @ts-expect-error vendored *Response 与 @agentclientprotocol/sdk@0.24 协议类型演进差异
      initialize: (params) => this.handleInitialize(params as InitializeRequest),
      authenticate: (params) =>
        this.handleAuthenticate(params as AuthenticateRequest),
      // @ts-expect-error vendored *Response 与 @agentclientprotocol/sdk@0.24 协议类型演进差异
      newSession: (params) => this.handleNewSession(params as NewSessionRequest, conn),
      loadSession: (params) =>
        this.handleLoadSession(params as LoadSessionRequest, conn),
      // @ts-expect-error vendored *Response 与 @agentclientprotocol/sdk@0.24 协议类型演进差异
      prompt: (params) => this.handlePrompt(params as PromptRequest, conn),
      cancel: (params) => this.handleCancel(params as CancelNotification),
      setSessionMode: (params) =>
        this.handleSetSessionMode(params as SetSessionModeRequest),
      // nuwaclaw / ACP SDK：newSession 后同步 model（env 已注入时此处为兼容 ack）
      setSessionConfigOption: (params) =>
        this.handleSetSessionConfigOption(params as SetSessionConfigOptionRequest),
    };
  }

  /**
   * Handle ACP initialize request
   */
  private async handleInitialize(
    params: InitializeRequest,
  ): Promise<InitializeResponse> {
    // Extract client info from either new format (clientInfo) or legacy format
    const clientInfo = params.clientInfo as
      | { name?: string; version?: string }
      | undefined;
    const clientName =
      clientInfo?.name ?? (params.clientName as string) ?? "unknown";
    const clientVersion =
      clientInfo?.version ?? (params.clientVersion as string) ?? "unknown";

    this.log("Client connected:", clientName, clientVersion);

    // Store client capabilities
    const clientCaps = params.clientCapabilities as
      | Record<string, unknown>
      | undefined;
    if (clientCaps) {
      const fs = clientCaps.fs as Record<string, boolean> | undefined;
      this.clientCapabilities = {
        fsReadTextFile: fs?.readTextFile ?? false,
        fsWriteTextFile: fs?.writeTextFile ?? false,
        terminal: clientCaps.terminal !== undefined,
      };
    }

    // Protocol version - ensure it's a number (ACP spec requires number)
    const requestedVersion =
      typeof params.protocolVersion === "number"
        ? params.protocolVersion
        : parseInt(String(params.protocolVersion), 10) || 1;

    const response = {
      // Required: protocol version as number per ACP spec
      protocolVersion: requestedVersion,
      // ACP spec: agentInfo contains name and version
      agentInfo: {
        name: this.serverName,
        version: this.serverVersion,
      },
      // ACP spec: agentCapabilities with correct structure
      agentCapabilities: {
        // Whether we support session/load - must be boolean
        loadSession: true,
        // Prompt capabilities - what content types we accept
        promptCapabilities: {
          image: true,
          audio: false,
          embeddedContext: true,
        },
        // MCP capabilities
        mcpCapabilities: {
          http: false,
          sse: false,
        },
        // Session capabilities (modes, commands, etc.)
        sessionCapabilities: {
          modes: true,
          commands: true,
        },
      },
      authMethods: this.authMethods,
    };

    this.log("Initialize response:", JSON.stringify(response));
    return response;
  }

  /**
   * Handle ACP authenticate request (no-op for now)
   */
  private async handleAuthenticate(
    _params: AuthenticateRequest,
  ): Promise<AuthenticateResponse | void> {
    // No authentication required
    return;
  }

  /**
   * Handle ACP session/new request
   */
  private async handleNewSession(
    params: NewSessionRequest,
    conn: AgentSideConnection,
  ): Promise<NewSessionResponse> {
    const sessionId = generateSessionId();
    const threadId = randomUUID();

    const configOptions = params.configOptions as
      | Record<string, unknown>
      | undefined;
    const agentName =
      (configOptions?.agent as string) ?? [...this.agentConfigs.keys()][0];

    if (!agentName || !this.agentConfigs.has(agentName)) {
      throw new Error(`Unknown agent: ${agentName}`);
    }

    const session: SessionState = {
      id: sessionId,
      agentName,
      threadId,
      messages: [],
      createdAt: new Date(),
      lastActivityAt: new Date(),
      mode: params.mode as string | undefined,
    };

    this.sessions.set(sessionId, session);

    this.log("Created session:", sessionId, "for agent:", agentName);

    const response: NewSessionResponse = {
      sessionId,
      modes: {
        availableModes: AVAILABLE_MODES,
        currentModeId: session.mode ?? "agent",
      },
    };

    this.log("New session response:", JSON.stringify(response));

    // Initialize session: apply host hook, create agent, send commands
    await this.initializeSession(session, conn, "new", params);

    return response;
  }

  /**
   * Handle ACP session/load request
   */
  private async handleLoadSession(
    params: LoadSessionRequest,
    conn: AgentSideConnection,
  ): Promise<LoadSessionResponse> {
    const sessionId = params.sessionId as string;
    let session = this.sessions.get(sessionId);

    // 进程重启后内存 Map 为空，但 FileCheckpointSaver 可能已在 cwd/isolated HOME 落盘。
    // 为 load 重建 SessionState，让 configureSession(phase=load) 从 checkpointer 恢复上下文。
    if (!session) {
      const configOptions = params.configOptions as
        | Record<string, unknown>
        | undefined;
      const agentName =
        (configOptions?.agent as string) ?? [...this.agentConfigs.keys()][0];

      if (!agentName || !this.agentConfigs.has(agentName)) {
        this.log("Load session failed: unknown agent for:", sessionId);
        throw new Error(`Session not found: ${sessionId}`);
      }

      session = {
        id: sessionId,
        agentName,
        // Flow 层 checkpoint 以 sessionId 为 thread_id（见 surfaces/acp onPrompt）。
        threadId: sessionId,
        messages: [],
        createdAt: new Date(),
        lastActivityAt: new Date(),
        mode: params.mode as string | undefined,
      };
      this.sessions.set(sessionId, session);
      this.log("Hydrated session for load (disk checkpoint):", sessionId);
    }

    this.log("Loading session:", {
      sessionId,
      agent: session.agentName,
      mode: session.mode,
    });
    session.lastActivityAt = new Date();

    // Initialize session: apply host hook, create agent, send commands
    await this.initializeSession(session, conn, "load", params);

    await this.replaySessionHistory(session, conn, params);

    const response: LoadSessionResponse = {
      modes: {
        availableModes: AVAILABLE_MODES,
        currentModeId: session.mode ?? "agent",
      },
    };

    this.log("Load session response:", JSON.stringify(response));
    return response;
  }

  /**
   * Common session initialization logic shared by handleNewSession and handleLoadSession.
   * Applies host hook, creates agent if needed, sets ACP backend session ID, and sends commands.
   */
  private async initializeSession(
    session: SessionState,
    conn: AgentSideConnection,
    phase: "new" | "load",
    params: Record<string, unknown>,
  ): Promise<void> {
    const { id: sessionId, agentName } = session;

    // Host hook: configure the session before the agent is built.
    await this.applySessionPatch(
      agentName,
      await this.hooks?.configureSession?.({
        sessionId,
        agentName,
        mode: session.mode,
        phase,
        params,
      }),
    );

    // A configureSession patch may have replaced the agent config, which
    // invalidates the cached agent + backend (applySessionPatch deletes them).
    // Rebuild before use — otherwise the agent is missing and the next prompt
    // throws "Agent not found".
    if (!this.agents.has(agentName)) {
      this.createAgent(agentName);
    }

    const acpBackend = this.acpBackends.get(agentName);
    if (acpBackend) {
      acpBackend.setSessionId(sessionId);
    }

    // Send available commands to the client
    const agentConfig = this.agentConfigs.get(agentName);
    const customCommands = agentConfig?.commands ?? [];
    const allCommands = [...DEFAULT_COMMANDS, ...customCommands];
    conn
      // @ts-expect-error vendored SessionNotification 与 @agentclientprotocol/sdk@0.24 协议类型演进差异
      .sessionUpdate({
        sessionId,
        update: {
          sessionUpdate: "available_commands_update",
          availableCommands: allCommands,
        },
      } as SessionNotification)
      .catch((err) => {
        this.log("Failed to send commands update:", err);
      });
  }

  /**
   * Handle ACP session/prompt request
   *
   * This is the main entry point for agent interactions
   */
  private async handlePrompt(
    params: PromptRequest,
    conn: AgentSideConnection,
  ): Promise<PromptResponse> {
    const sessionId = params.sessionId as string;
    const session = this.sessions.get(sessionId);

    if (!session) {
      this.log("Prompt failed: session not found:", sessionId);
      throw new Error(`Session not found: ${sessionId}`);
    }

    const agent = this.agents.get(session.agentName);

    if (!agent) {
      this.log("Prompt failed: agent not found:", session.agentName);
      throw new Error(`Agent not found: ${session.agentName}`);
    }

    session.lastActivityAt = new Date();

    // Create a per-session abort controller for this turn (replacing any stale
    // entry left by a turn that never reached `finally`).
    this.promptAbortControllers.set(sessionId, new AbortController());

    // Extract prompt text for logging
    const prompt = params.prompt as ContentBlock[];
    const promptPreview = this.getPromptPreview(prompt);
    this.log("Prompt received:", {
      sessionId,
      agent: session.agentName,
      preview: promptPreview,
    });

    try {
      const commandResult = await this.handleSlashCommand(
        session,
        prompt,
        conn,
      );
      if (commandResult) {
        return commandResult;
      }

      // Host hook: intercept the prompt (e.g. custom slash commands) before the
      // agent runs. A returned response short-circuits the turn.
      const hookResult = await this.hooks?.onPrompt?.({
        sessionId,
        promptText: this.getPromptText(prompt),
        params,
        conn,
        signal: this.promptAbortControllers.get(sessionId)?.signal,
      });
      if (hookResult) {
        await this.hooks?.onPromptComplete?.({
          sessionId,
          stopReason: hookResult.stopReason,
        });
        return hookResult;
      }

      const humanMessage = acpPromptToHumanMessage(prompt);

      // Stream the agent response
      const stopReason = await this.streamAgentResponse(
        session,
        agent,
        humanMessage,
        conn,
      );

      this.log("Prompt completed:", { sessionId, stopReason });
      await this.hooks?.onPromptComplete?.({ sessionId, stopReason });
      return { stopReason };
    } catch (error) {
      if ((error as Error).name === "AbortError") {
        this.log("Prompt cancelled:", sessionId);
        await this.hooks?.onPromptComplete?.({
          sessionId,
          stopReason: "cancelled",
        });
        return { stopReason: "cancelled" };
      }
      this.log("Prompt error:", { sessionId, error: (error as Error).message });
      await this.hooks?.onPromptError?.({ sessionId, error });
      throw error;
    } finally {
      this.promptAbortControllers.delete(sessionId);
    }
  }

  /**
   * Get a preview of the prompt for logging (truncated)
   */
  private getPromptPreview(prompt: ContentBlock[]): string {
    const textBlocks = prompt.filter((b) => b.type === "text");
    if (textBlocks.length === 0) {
      return `[${prompt.length} non-text blocks]`;
    }
    const text = (textBlocks[0] as { text: string }).text;
    return text.length > 100 ? text.slice(0, 100) + "..." : text;
  }

  /**
   * Extract the trimmed text of the first text block (for hook context), or
   * null when the prompt carries no text.
   */
  private getPromptText(prompt: ContentBlock[]): string | null {
    const textBlock = prompt.find((b) => b.type === "text") as
      | { text?: string }
      | undefined;
    return textBlock?.text?.trim() ?? null;
  }

  /**
   * Handle ACP session/cancel notification
   */
  private async handleCancel(params: CancelNotification): Promise<void> {
    this.log("Cancelling session:", params.sessionId);

    // Abort only the cancelling session's in-flight prompt — not whatever
    // happens to be globally current. No-op if that session has no prompt running.
    this.promptAbortControllers.get(params.sessionId as string)?.abort();
  }

  /**
   * Handle ACP session/set_mode request
   */
  private async handleSetSessionMode(
    params: SetSessionModeRequest,
  ): Promise<SetSessionModeResponse | void> {
    const sessionId = params.sessionId as string;
    const session = this.sessions.get(sessionId);

    if (!session) {
      throw new Error(`Session not found: ${sessionId}`);
    }

    // SDK 0.24.0: SetSessionModeRequest has modeId (required)
    const mode = params.modeId as string;
    session.mode = mode;
    this.log("Set mode for session:", sessionId, "to:", mode);

    return;
  }

  /**
   * Handle ACP session/set_config_option request（nuwaclaw prompt 前 model 对齐）。
   * flow-ts 模型主要来自进程 env；此处记录 host 期望值并返回成功，避免 Method not found 阻断 chat。
   */
  private async handleSetSessionConfigOption(
    params: SetSessionConfigOptionRequest,
  ): Promise<SetSessionConfigOptionResponse> {
    const sessionId = params.sessionId as string;
    const session = this.sessions.get(sessionId);

    if (!session) {
      throw new Error(`Session not found: ${sessionId}`);
    }

    const configId = typeof params.configId === "string" ? params.configId : "";
    const value =
      typeof params.value === "string" ? params.value : String(params.value ?? "");

    const kind = applySessionConfigOptionPatch(session, { configId, value }, {
      envModelId: resolveEnvModelId(),
    });
    if (kind.kind === "model") {
      this.log("Set model for session:", sessionId, "to:", value);
      if (kind.runtimeModelMismatch) {
        this.log(
          "session/set_config_option model differs from process env; host may hot-reload runtime",
          {
            sessionId,
            requested: kind.runtimeModelMismatch.requested,
            envModel: kind.runtimeModelMismatch.envModel,
          },
        );
      }
    } else if (kind.kind === "mode") {
      this.log("Set mode via config option for session:", sessionId, "to:", value);
    } else {
      this.log("Ignored unknown session config option:", {
        sessionId,
        configId,
        value,
      });
    }

    await this.hooks?.onSessionConfigOption?.({
      sessionId,
      configId,
      value,
    });

    return {
      configOptions: buildSessionConfigOptionsSnapshot(session, {
        envModelFallback: resolveEnvModelId(),
      }),
    };
  }

  /**
   * Stream agent response and send ACP updates
   */
  private async streamAgentResponse(
    session: SessionState,
    agent: ReturnType<typeof createDeepAgent>,
    humanMessage: HumanMessage,
    conn: AgentSideConnection,
  ): Promise<StopReason> {
    const config = {
      configurable: { thread_id: session.threadId },
      signal: this.promptAbortControllers.get(session.id)?.signal,
    };

    // Track active tool calls
    const activeToolCalls = new Map<string, ToolCallInfo>();
    let eventCount = 0;

    this.log("Starting agent stream:", {
      sessionId: session.id,
      threadId: session.threadId,
    });

    session.messages.push(humanMessage);

    const stream = await agent.stream({ messages: [humanMessage] }, config);

    for await (const event of stream) {
      eventCount++;

      // Log event structure for debugging
      const eventKeys = Object.keys(event);

      // Check for cancellation
      if (this.promptAbortControllers.get(session.id)?.signal.aborted) {
        this.log(
          "Stream cancelled, cleaning up tool calls:",
          activeToolCalls.size,
        );
        // Cancel all active tool calls
        for (const toolCall of activeToolCalls.values()) {
          await this.sendToolCallUpdate(session.id, conn, {
            ...toolCall,
            status: "cancelled",
          });
        }
        return "cancelled";
      }

      // Extract messages from the event structure
      // LangGraph stream events have node names as keys (e.g., "model_request", "tools")
      // Messages are nested inside these node updates
      let messages: BaseMessage[] = [];

      // Check for direct messages property
      if (event.messages && Array.isArray(event.messages)) {
        messages = event.messages;
      }
      // Check for model_request node which contains messages
      else if (event.model_request && typeof event.model_request === "object") {
        const modelReq = event.model_request as { messages?: BaseMessage[] };
        if (modelReq.messages && Array.isArray(modelReq.messages)) {
          messages = modelReq.messages;
        }
      }
      // Check for tools node which may contain tool messages
      else if (event.tools && typeof event.tools === "object") {
        const toolsUpdate = event.tools as { messages?: BaseMessage[] };
        if (toolsUpdate.messages && Array.isArray(toolsUpdate.messages)) {
          messages = toolsUpdate.messages;
        }
      }

      this.log("Stream event:", {
        sessionId: session.id,
        eventNum: eventCount,
        keys: eventKeys,
        messagesFound: messages.length,
      });

      // Process any messages found
      for (const message of messages) {
        const messageType = message.constructor?.name ?? typeof message;
        this.log("Processing message:", {
          sessionId: session.id,
          type: messageType,
          isAI: AIMessage.isInstance(message),
          isTool: ToolMessage.isInstance(message),
          contentType: typeof message.content,
          contentPreview:
            typeof message.content === "string"
              ? message.content.slice(0, 100)
              : "[complex]",
        });

        if (AIMessage.isInstance(message)) {
          await this.handleAIMessage(
            session,
            message as AIMessage,
            activeToolCalls,
            conn,
          );
        } else if (ToolMessage.isInstance(message)) {
          // Handle tool completion
          await this.handleToolMessage(
            session,
            message as ToolMessage,
            activeToolCalls,
            conn,
          );
        }
      }

      // Handle todo list updates (plan entries)
      if (event.todos && Array.isArray(event.todos)) {
        this.log("Plan updated:", {
          sessionId: session.id,
          entries: event.todos.length,
        });
        const planEntries = todosToPlanEntries(event.todos);
        await this.sendPlanUpdate(session.id, conn, planEntries);
      }
    }

    this.log("Agent stream completed:", {
      sessionId: session.id,
      eventCount,
      toolCalls: activeToolCalls.size,
    });
    return "end_turn";
  }

  /**
   * Handle an AI message from the agent
   */
  private async handleAIMessage(
    session: SessionState,
    message: AIMessage,
    activeToolCalls: Map<string, ToolCallInfo>,
    conn: AgentSideConnection,
  ): Promise<void> {
    if (message.content && typeof message.content === "string") {
      const contentBlocks = langChainMessageToACP(message);
      const preview =
        message.content.slice(0, 50) +
        (message.content.length > 50 ? "..." : "");
      this.log("Agent message:", { sessionId: session.id, preview });
      await this.sendMessageChunk(session.id, conn, "agent", contentBlocks);
    } else if (Array.isArray(message.content)) {
      for (const block of message.content) {
        const b = block as Record<string, unknown>;
        if (b.type === "thinking" && b.thinking) {
          this.log("Thought message:", { sessionId: session.id });
          await this.sendMessageChunk(session.id, conn, "thought", [
            { type: "text", text: b.thinking as string } as ContentBlock,
          ]);
        } else if (b.type === "text" && b.text) {
          await this.sendMessageChunk(session.id, conn, "agent", [
            { type: "text", text: b.text as string } as ContentBlock,
          ]);
        }
      }
    }

    // Handle tool calls
    const toolCalls = extractToolCalls(message);

    for (const toolCall of toolCalls) {
      this.log("Tool call started:", {
        sessionId: session.id,
        toolId: toolCall.id,
        tool: toolCall.name,
        args: Object.keys(toolCall.args),
      });

      // Send tool call notification
      await this.sendToolCall(session.id, conn, toolCall);
      activeToolCalls.set(toolCall.id, toolCall);

      // Update to in_progress
      toolCall.status = "in_progress";
      await this.sendToolCallUpdate(session.id, conn, toolCall);
    }
  }

  /**
   * Handle a tool message (tool result) from the agent
   */
  private async handleToolMessage(
    session: SessionState,
    message: ToolMessage,
    activeToolCalls: Map<string, ToolCallInfo>,
    conn: AgentSideConnection,
  ): Promise<void> {
    // Get the tool call ID from the message
    const toolCallId = message.tool_call_id;
    const toolCall = activeToolCalls.get(toolCallId);

    if (!toolCall) {
      this.log("Tool message for unknown tool call:", {
        sessionId: session.id,
        toolCallId,
      });
      return;
    }

    const resultContent =
      typeof message.content === "string"
        ? message.content
        : JSON.stringify(message.content);

    const isError =
      message.status === "error" ||
      (typeof message.content === "string" &&
        message.content.toLowerCase().includes("error"));

    const resultPreview =
      resultContent.slice(0, 100) + (resultContent.length > 100 ? "..." : "");
    this.log("Tool completed:", {
      sessionId: session.id,
      toolId: toolCallId,
      tool: toolCall.name,
      status: isError ? "error" : "completed",
      resultPreview,
    });

    toolCall.status = isError ? "error" : "completed";
    toolCall.result = resultContent;

    await this.sendToolCallUpdate(session.id, conn, toolCall);
    activeToolCalls.delete(toolCallId);
  }

  async executeWithTerminal(
    session: SessionState,
    conn: AgentSideConnection,
    toolCall: ToolCallInfo,
  ): Promise<{ output: string; exitCode: number | null }> {
    const command = toolCall.args.command as string;
    if (!command) {
      return { output: "Error: No command specified", exitCode: 1 };
    }

    try {
      const terminal = await conn.createTerminal({
        sessionId: session.id,
        command: "/bin/bash",
        args: ["-c", command],
        cwd: this.workspaceRoot,
      } as any);

      await this.sendToolCallUpdate(session.id, conn, {
        ...toolCall,
        status: "in_progress",
      });

      const exitResult = await terminal.waitForExit();
      const outputResult = await terminal.currentOutput();
      await terminal.release();

      return {
        output: outputResult.output ?? "",
        exitCode: exitResult.exitCode ?? null,
      };
    } catch (err) {
      this.log("Terminal execution failed, falling back:", err);
      return {
        output: `Terminal error: ${(err as Error).message}`,
        exitCode: 1,
      };
    }
  }

  async requestToolPermission(
    session: SessionState,
    conn: AgentSideConnection,
    toolCall: ToolCallInfo,
  ): Promise<"allow" | "reject" | "cancelled"> {
    if (!session.permissionDecisions) {
      session.permissionDecisions = new Map();
    }

    const cached = session.permissionDecisions.get(toolCall.name);
    if (cached === "allow_always") return "allow";
    if (cached === "reject_always") return "reject";

    try {
      const result = await conn.requestPermission({
        sessionId: session.id,
        toolCall: buildPermissionToolCall(
          toolCall.id,
          toolCall.name,
          toolCall.args,
          this.workspaceRoot,
        ),
        options: [
          {
            optionId: "allow-once",
            name: "Allow once",
            kind: "allow_once" as const,
          },
          {
            optionId: "allow-always",
            name: "Always allow",
            kind: "allow_always" as const,
          },
          {
            optionId: "reject-once",
            name: "Reject",
            kind: "reject_once" as const,
          },
          {
            optionId: "reject-always",
            name: "Always reject",
            kind: "reject_always" as const,
          },
        ],
      } as any);

      const outcome = result?.outcome as
        | { outcome: string; optionId?: string }
        | undefined;
      if (!outcome || outcome.outcome === "cancelled") {
        return "cancelled";
      }

      const optionId = outcome.optionId;
      if (optionId === "allow-always") {
        session.permissionDecisions.set(toolCall.name, "allow_always");
        return "allow";
      }
      if (optionId === "reject-always") {
        session.permissionDecisions.set(toolCall.name, "reject_always");
        return "reject";
      }
      if (optionId === "reject-once") {
        return "reject";
      }
      return "allow";
    } catch (err) {
      this.log("Permission request failed:", err);
      return "allow";
    }
  }

  private async handleSlashCommand(
    session: SessionState,
    prompt: ContentBlock[],
    conn: AgentSideConnection,
  ): Promise<PromptResponse | null> {
    const textBlocks = prompt.filter((b) => b.type === "text");
    if (textBlocks.length === 0) return null;

    const text = (textBlocks[0] as { text: string }).text.trim();
    if (!text.startsWith("/")) return null;

    const [command] = text.split(/\s+/, 1);
    const commandName = command.slice(1).toLowerCase();

    switch (commandName) {
      case "plan":
      case "agent":
      case "ask": {
        session.mode = commandName;
        this.log("Slash command mode switch:", commandName);
        await this.sendMessageChunk(session.id, conn, "agent", [
          {
            type: "text",
            text: `Switched to ${commandName} mode.`,
          } as ContentBlock,
        ]);
        return { stopReason: "end_turn" };
      }
      case "clear": {
        session.messages = [];
        session.threadId = randomUUID();
        this.log("Slash command clear:", session.id);
        await this.sendMessageChunk(session.id, conn, "agent", [
          {
            type: "text",
            text: "Conversation cleared. Starting fresh.",
          } as ContentBlock,
        ]);
        return { stopReason: "end_turn" };
      }
      case "status": {
        const config = this.agentConfigs.get(session.agentName);
        const status = [
          `**Agent:** ${session.agentName}`,
          `**Mode:** ${session.mode ?? "agent"}`,
          `**Model:** ${config?.model ?? "default"}`,
          `**Skills:** ${config?.skills?.length ?? 0} loaded`,
          `**Memory:** ${config?.memory?.length ?? 0} sources`,
          `**Session:** ${session.id}`,
        ].join("\n");
        await this.sendMessageChunk(session.id, conn, "agent", [
          { type: "text", text: status } as ContentBlock,
        ]);
        return { stopReason: "end_turn" };
      }
      default:
        return null;
    }
  }

  /**
   * Replay conversation history to the client via session update notifications
   */
  private async replaySessionHistory(
    session: SessionState,
    conn: AgentSideConnection,
    loadParams?: Record<string, unknown>,
  ): Promise<void> {
    let messages: BaseMessage[] = [];

    // 宿主 hook 优先（Flow 磁盘 checkpointer）；未实现时回退下方 MemorySaver。
    try {
      const fromHost = await this.hooks?.getSessionHistory?.({
        sessionId: session.id,
        threadId: session.threadId,
        phase: "load",
        params: loadParams ?? {},
      });
      if (Array.isArray(fromHost) && fromHost.length > 0) {
        messages = fromHost as BaseMessage[];
      }
    } catch {
      // Host hook may not have checkpoint data yet
    }

    if (messages.length === 0) {
      try {
        const checkpoint = await this.checkpointer.getTuple({
          configurable: { thread_id: session.threadId },
        });
        if (checkpoint?.checkpoint?.channel_values) {
          const channelValues = checkpoint.checkpoint.channel_values as Record<
            string,
            unknown
          >;
          if (Array.isArray(channelValues.messages)) {
            messages = channelValues.messages as BaseMessage[];
          }
        }
      } catch {
        // Checkpointer may not have data yet
      }
    }

    if (messages.length === 0) {
      messages = session.messages as BaseMessage[];
    }

    if (messages.length === 0) {
      this.log("No history to replay for session:", session.id);
      return;
    }

    this.log("Replaying history:", {
      sessionId: session.id,
      messageCount: messages.length,
    });

    for (const message of messages) {
      if (HumanMessage.isInstance(message)) {
        const contentBlocks = langChainMessageToACP(message);
        await this.sendMessageChunk(session.id, conn, "user", contentBlocks);
      } else if (AIMessage.isInstance(message)) {
        if (message.content && typeof message.content === "string") {
          const contentBlocks = langChainMessageToACP(message);
          await this.sendMessageChunk(session.id, conn, "agent", contentBlocks);
        }

        const toolCalls = extractToolCalls(message as AIMessage);
        for (const toolCall of toolCalls) {
          await this.sendToolCall(session.id, conn, toolCall);
          await this.sendToolCallUpdate(session.id, conn, {
            ...toolCall,
            status: "completed",
          });
        }
      } else if (ToolMessage.isInstance(message)) {
        // Tool results are already covered by the tool call updates above
      }
    }
  }

  private async sendMessageChunk(
    sessionId: string,
    conn: AgentSideConnection,
    messageType: "agent" | "user" | "thought",
    content: ContentBlock[],
  ): Promise<void> {
    const sessionUpdate =
      messageType === "thought"
        ? "agent_thought_chunk"
        : messageType === "user"
          ? "user_message_chunk"
          : "agent_message_chunk";

    const notification = {
      sessionId,
      update: {
        sessionUpdate,
        content: content[0], // ACP expects single content block per chunk
      },
    } as SessionNotification;

    this.log("Sending message chunk:", {
      sessionId,
      type: sessionUpdate,
      contentType: content[0]?.type,
      preview:
        content[0]?.type === "text"
          ? (content[0] as { text: string }).text?.slice(0, 50)
          : "[non-text]",
    });

    // @ts-expect-error vendored SessionNotification 与 @agentclientprotocol/sdk@0.24 协议类型演进差异
    await conn.sessionUpdate(notification);
  }

  /**
   * Send a tool call notification to the client
   */
  private async sendToolCall(
    sessionId: string,
    conn: AgentSideConnection,
    toolCall: ToolCallInfo,
  ): Promise<void> {
    const info = toolInfoFromToolEvent(
      toolCall.name,
      toolCall.args,
      this.workspaceRoot,
    );

    // @ts-expect-error vendored SessionNotification 与 @agentclientprotocol/sdk@0.24 协议类型演进差异
    await conn.sessionUpdate({
      sessionId,
      update: {
        sessionUpdate: "tool_call",
        toolCallId: toolCall.id,
        title: info.title,
        kind: info.kind,
        status: toolCall.status,
        rawInput: toolCall.args,
        ...(info.locations?.length ? { locations: info.locations } : {}),
        ...(info.content?.length ? { content: info.content } : {}),
      },
    } as SessionNotification);
  }

  /**
   * Send a tool call update to the client
   */
  private async sendToolCallUpdate(
    sessionId: string,
    conn: AgentSideConnection,
    toolCall: ToolCallInfo,
  ): Promise<void> {
    const update: Record<string, unknown> = {
      sessionUpdate: "tool_call_update",
      toolCallId: toolCall.id,
      status: toolCall.status,
    };

    if (toolCall.args && Object.keys(toolCall.args).length > 0) {
      update.rawInput = toolCall.args;
    }

    if (toolCall.status === "completed" && toolCall.result != null) {
      const presentation = toolUpdateFromToolResult(
        toolCall.name,
        toolCall.result,
        { workspaceRoot: this.workspaceRoot },
      );
      update.rawOutput = presentation.rawOutput;
      if (presentation.content?.length) {
        update.content = presentation.content;
      } else if (presentation.displayText) {
        update.content = [
          {
            type: "content",
            content: { type: "text", text: presentation.displayText },
          },
        ];
      }
    }

    // @ts-expect-error vendored SessionNotification 与 @agentclientprotocol/sdk@0.24 协议类型演进差异
    await conn.sessionUpdate({
      sessionId,
      update,
    } as SessionNotification);
  }

  /**
   * Send a plan update to the client
   */
  private async sendPlanUpdate(
    sessionId: string,
    conn: AgentSideConnection,
    entries: Array<{
      content: string;
      priority?: "high" | "medium" | "low";
      status: "pending" | "in_progress" | "completed" | "skipped";
    }>,
  ): Promise<void> {
    // @ts-expect-error vendored SessionNotification 与 @agentclientprotocol/sdk@0.24 协议类型演进差异
    await conn.sessionUpdate({
      sessionId,
      update: {
        sessionUpdate: "plan",
        entries,
      },
    } as SessionNotification);
  }

  /**
   * Apply a host-provided session patch (from a configureSession hook):
   * optionally switch the workspace root and/or replace this agent's config,
   * invalidating the cached agent + backend so the next createAgent picks up
   * the new configuration.
   */
  private async applySessionPatch(
    agentName: string,
    patch: SessionConfigurePatch | void,
  ): Promise<void> {
    if (!patch) {
      return;
    }
    if (patch.workspaceRoot) {
      this.workspaceRoot = patch.workspaceRoot;
    }
    if (patch.agentConfig) {
      this.agentConfigs.set(agentName, patch.agentConfig);
      this.agents.delete(agentName);
      this.acpBackends.delete(agentName);
    }
  }

  /**
   * Remove an active session. Returns `false` for an unknown session. Fires the
   * `onSessionClosed` hook so hosts can persist/clean up session state.
   */
  async closeSession(sessionId: string): Promise<boolean> {
    const session = this.sessions.get(sessionId);
    if (!session) {
      return false;
    }
    this.sessions.delete(sessionId);
    await this.hooks?.onSessionClosed?.({
      sessionId,
      agentName: session.agentName,
    });
    this.log("Session closed:", sessionId);
    return true;
  }

  /**
   * List active (in-memory) sessions.
   */
  listSessions(): Array<{
    sessionId: string;
    agentName: string;
    mode?: string;
    createdAt: Date;
    lastActivityAt: Date;
  }> {
    return Array.from(this.sessions.values()).map((s) => ({
      sessionId: s.id,
      agentName: s.agentName,
      mode: s.mode,
      createdAt: s.createdAt,
      lastActivityAt: s.lastActivityAt,
    }));
  }

  /**
   * Create a DeepAgent instance for the given configuration
   */
  private createAgent(agentName: string): void {
    const config = this.agentConfigs.get(agentName);

    if (!config) {
      this.log("Agent configuration not found:", agentName);
      throw new Error(`Agent configuration not found: ${agentName}`);
    }

    this.log("Creating agent:", {
      name: agentName,
      model: config.model ?? "default",
      skills: config.skills?.length ?? 0,
      memory: config.memory?.length ?? 0,
      tools: config.tools?.length ?? 0,
      subagents: config.subagents?.length ?? 0,
    });

    // Create backend - prefer ACP filesystem if client supports it
    const backend = this.createBackend(config);

    const agent = createDeepAgent({
      model: config.model,
      tools: config.tools,
      systemPrompt: config.systemPrompt,
      middleware: config.middleware,
      subagents: config.subagents,
      responseFormat: config.responseFormat,
      contextSchema: config.contextSchema as InteropZodObject | undefined,
      interruptOn: config.interruptOn,
      // Forward filesystem permissions so the built-in FilesystemMiddleware
      // enforces path access rules in ACP mode. Permission denials are handled
      // at the tool layer (not via LangGraph interrupts), so this is safe even
      // though ACP does not drive humanInTheLoop interrupts.
      permissions: config.permissions,
      store: config.store,
      backend: backend as NonNullable<Parameters<typeof createDeepAgent>[0]>["backend"],
      skills: config.skills,
      memory: config.memory,
      checkpointer: this.checkpointer,
      name: config.name,
    });

    this.agents.set(agentName, agent);
    this.log("Agent created successfully:", agentName);
  }

  private createBackend(
    config: DeepAgentConfig,
  ): BackendProtocolV2 | BackendFactory {
    if (config.backend) {
      this.log("Using custom backend for agent:", config.name);
      return config.backend as BackendProtocolV2 | BackendFactory;
    }

    if (
      this.clientCapabilities.fsReadTextFile &&
      this.clientCapabilities.fsWriteTextFile &&
      this.connection
    ) {
      this.log("Creating ACPFilesystemBackend for agent:", config.name);
      const backend = new ACPFilesystemBackend({
        conn: this.connection,
        rootDir: this.workspaceRoot,
      });
      this.acpBackends.set(config.name, backend);
      return backend;
    }

    this.log("Creating FilesystemBackend:", { rootDir: this.workspaceRoot });
    return new FilesystemBackend({
      rootDir: this.workspaceRoot,
    });
  }

  /**
   * Read a file through the ACP client (if supported)
   */
  async readFileViaClient(path: string): Promise<string | null> {
    if (!this.connection || !this.clientCapabilities.fsReadTextFile) {
      this.log("readFileViaClient: client does not support file read");
      return null;
    }

    this.log("Reading file via client:", path);
    try {
      const result = await this.connection.readTextFile({ sessionId: "", path });
      this.log("File read successful:", {
        path,
        length: result.content?.length ?? 0,
      });
      return result.content;
    } catch (err) {
      this.log("File read failed:", { path, error: (err as Error).message });
      return null;
    }
  }

  /**
   * Write a file through the ACP client (if supported)
   */
  async writeFileViaClient(path: string, content: string): Promise<boolean> {
    if (!this.connection || !this.clientCapabilities.fsWriteTextFile) {
      this.log("writeFileViaClient: client does not support file write");
      return false;
    }

    this.log("Writing file via client:", { path, length: content.length });
    try {
      await this.connection.writeTextFile({ sessionId: "", path, content });
      this.log("File write successful:", path);
      return true;
    } catch (err) {
      this.log("File write failed:", { path, error: (err as Error).message });
      return false;
    }
  }

  /**
   * Log a debug message
   */
  private log(...args: unknown[]): void {
    this.logger.log(...args);
  }

  /**
   * Get the logger instance (for external access if needed)
   */
  getLogger(): Logger {
    return this.logger;
  }
}

/**
 * Create and start a DeepAgents ACP server
 *
 * Convenience function for quick server setup
 *
 * @example
 * ```typescript
 * import { startServer } from "deepagents-acp";
 *
 * await startServer({
 *   agents: {
 *     name: "my-agent",
 *     description: "My coding assistant",
 *   },
 * });
 * ```
 */
export async function startServer(
  options: DeepAgentsServerOptions,
): Promise<DeepAgentsServer> {
  const server = new DeepAgentsServer(options);
  await server.start();
  return server;
}
