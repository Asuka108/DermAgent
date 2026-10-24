import { defineConfig } from "vitest/config";

/**
 * 测试期把日志降到 `error`。
 *
 * 本包 `runtime/logger.ts` 按 LOG_LEVEL 过滤；默认 info/warn 会刷结构化日志，淹没断言与
 * 失败信息。这里默认静音，排障时用 `LOG_LEVEL=debug pnpm test` 覆盖（已有 LOG_LEVEL 优先）。
 *
 * 注：本文件已在 scripts/lib/staging.mjs 的 STAGING_EXCLUDES（vitest.config.*）中，
 * 不进入发布压缩包。
 */
export default defineConfig({
  test: {
    env: {
      LOG_LEVEL: process.env.LOG_LEVEL || "error",
    },
  },
});
