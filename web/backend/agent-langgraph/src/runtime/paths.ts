/**
 * flowagents 统一命名/路径常量。
 *
 * 数据目录、子目录、默认 agent 名的**同源维护点**——改这里的常量即可整体迁移
 * session/artifact/log（含默认 agent 名），避免多处硬编码 "flowagents"/".flowagents" 漂移。
 * 无依赖的纯常量模块，logger / file-checkpoint-saver 等都可 import（不引入循环）。
 */

/** 核心名：数据目录与默认 agent 名同源。 */
export const FLOWAGENTS_NAME = "flowagents";

/** 数据目录名（~ 下的 .flowagents）。 */
export const FLOWAGENTS_DIRNAME = `.${FLOWAGENTS_NAME}`;

/** 数据根（~/.flowagents）。用户可经 config.memory.dir 覆盖。 */
export const FLOWAGENTS_HOME = `~/${FLOWAGENTS_DIRNAME}`;

/** 数据根下的子目录名（按用途分组）。 */
export const SESSIONS_SUBDIR = "sessions";
export const ARTIFACTS_SUBDIR = "artifacts";
export const LOGS_SUBDIR = "logs";
/** 运行时缓存根（~/.flowagents/cache）；下再按用途分子目录（如 mcp-tools）。 */
export const CACHE_SUBDIR = "cache";
