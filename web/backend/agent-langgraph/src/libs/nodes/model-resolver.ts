/**
 * requireModel —— 「无 demo fallback」模型凭证策略。
 *
 * 通用节点 factory（createLlmNode 等）不强制凭证；本函数给「真实接入」的拓扑/示例用：
 * 必须有模型凭证，否则直接报错（不降级 demo）。各拓扑节点统一经此取模型，错误信息一致。
 *
 * runtime 的 resolveModel 是框架默认图的模型解析；requireModel 是其「硬凭证」包装。
 */
import { resolveModel, resolveApiKey, logger, type AppConfig } from "../../runtime/index.js";

const log = logger.child("model-resolver");

/**
 * 真实接入：必须有模型，否则直接报错（不降级 demo fallback）。
 */
export function requireModel(appConfig?: AppConfig, exampleName = "本示例") {
  if (!appConfig || !resolveApiKey(appConfig)) {
    log.warn("无模型凭证（provider-aware 校验失败）→ 不降级 demo，直接报错");
    throw new Error(
      `${exampleName}需要模型凭证（无 demo fallback）：为 provider=${appConfig?.model?.provider ?? "?"} 设对应 env（OPENAI_API_KEY / ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，或 config 指定的 apiKeyEnv/authTokenEnv）`
    );
  }

  const resolved = resolveModel(appConfig);
  if (!resolved || typeof resolved === "string") {
    throw new Error(
      `${exampleName}需要模型凭证（无 demo fallback）：在 env / .env 设 ANTHROPIC_API_KEY（或 ANTHROPIC_AUTH_TOKEN / OPENAI_API_KEY）`
    );
  }
  return resolved;
}
