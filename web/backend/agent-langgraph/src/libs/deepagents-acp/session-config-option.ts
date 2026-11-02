/**
 * ACP `session/set_config_option` 补丁逻辑（纯函数，便于单测）。
 *
 * flow-ts 运行时模型以 configureSession / set_config_option 触发的 per-session 重建为准；
 * 此处记录 host 同步期望并返回 configOptions。
 */
import type { SessionConfigOption } from "@agentclientprotocol/sdk";
import type { SessionState } from "./types.js";

export type SessionConfigOptionKind = "model" | "mode" | "ignored";

export type SessionConfigOptionPatchResult = {
  kind: SessionConfigOptionKind;
  /** host 请求的 model 与进程 env 不一致（runtime 仍用 env） */
  runtimeModelMismatch?: { requested: string; envModel: string };
};

/** 从 env 解析当前进程生效的 model id（与 configureSession / platformModelEnv 一致）。 */
export function resolveEnvModelId(env: NodeJS.ProcessEnv = process.env): string {
  return (
    env.ANTHROPIC_MODEL?.trim() ||
    env.OPENCODE_MODEL?.trim() ||
    env.OPENAI_MODEL?.trim() ||
    ""
  );
}

export function applySessionConfigOptionPatch(
  session: Pick<SessionState, "mode" | "modelId">,
  params: { configId: string; value: string },
  opts?: { envModelId?: string },
): SessionConfigOptionPatchResult {
  const configId = params.configId.trim();
  const value = params.value.trim();
  if (!configId) return { kind: "ignored" };

  if (configId === "model") {
    session.modelId = value;
    const envModel = (opts?.envModelId ?? resolveEnvModelId()).trim();
    if (value && envModel && value !== envModel) {
      return {
        kind: "model",
        runtimeModelMismatch: { requested: value, envModel },
      };
    }
    return { kind: "model" };
  }

  if (configId === "mode") {
    session.mode = value;
    return { kind: "mode" };
  }

  return { kind: "ignored" };
}

function selectOption(
  id: string,
  name: string,
  category: "model" | "mode",
  description: string,
  currentValue: string,
  optionValues: Array<{ value: string; name: string; description?: string }>,
): SessionConfigOption {
  return {
    id,
    name,
    description,
    category,
    type: "select",
    currentValue,
    options: optionValues.map((o) => ({
      value: o.value,
      name: o.name,
      description: o.description ?? null,
    })),
  };
}

/** 根据 session 当前状态构造 set_config_option 响应中的 configOptions（ACP SDK 类型）。 */
export function buildSessionConfigOptionsSnapshot(
  session: Pick<SessionState, "mode" | "modelId">,
  opts?: { envModelFallback?: string },
): SessionConfigOption[] {
  const options: SessionConfigOption[] = [];

  const modelId =
    session.modelId?.trim() || (opts?.envModelFallback ?? resolveEnvModelId()).trim();
  if (modelId) {
    options.push(
      selectOption(
        "model",
        "Model",
        "model",
        "LLM model for this session",
        modelId,
        [{ value: modelId, name: modelId }],
      ),
    );
  }

  const mode = session.mode?.trim() || "agent";
  options.push(
    selectOption(
      "mode",
      "Mode",
      "mode",
      "Session permission / behavior mode",
      mode,
      [
        { value: "agent", name: "Agent Mode", description: "Full autonomous agent" },
        { value: "plan", name: "Plan Mode", description: "Planning and discussion" },
        { value: "ask", name: "Ask Mode", description: "Q&A without file changes" },
      ],
    ),
  );

  return options;
}
