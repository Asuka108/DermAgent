/**
 * 节点类型目录一致性守卫 —— factory 惯用名须在 docs/node-catalog.md 有记录。
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const catalog = readFileSync(resolve(here, "../docs/node-catalog.md"), "utf-8");

/** 手写图 / catalog 惯用名（与 node-catalog.md 一览一致）。 */
const FACTORY_TYPE_ALIASES = [
  "llm",
  "llm-stream",
  "llm-router",
  "approval",
  "approval-finalize",
  "platform-tool",
  "tool-exec",
  "mcp-retrieval",
  "prepare",
  "passthrough",
];

describe("节点类型目录一致性", () => {
  it("catalog 文档记录了所有 factory 惯用名（防 catalog 漂移）", () => {
    for (const t of FACTORY_TYPE_ALIASES) {
      expect(catalog, `catalog 缺少惯用名 "${t}"`).toContain(`\`${t}\``);
    }
  });
});
