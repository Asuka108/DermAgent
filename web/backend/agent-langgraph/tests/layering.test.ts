/**
 * 分层守卫 —— 把「只能 import 左侧层」的架构规则变成可执行测试（+ 可读文档）。
 *
 * 分层（每层只能 import 它**左侧**的层）：
 *   core → runtime → libs → app → surfaces → index.ts
 *   (纯契约) (底层运行时) (可复用/vendored 库) (默认图) (适配器) (入口/组合根)
 *
 * - core 纯类型契约,零依赖,是所有层的共享词汇。
 * - runtime 是底层运行时（config / model / logger / platform / mcp / context + checkpoint /
 *   sandbox / ripgrep / llm-resilience）—— flow-ts 自有,自包含。
 * - libs 是可复用 / vendored 库:nodes/(节点 factory + 构建原语)+ tools/(内置通用工具)
 *   + deepagents-acp/(vendored ACP SDK,自包含、只引外部包)。三者互不引用;只依赖 runtime+core 或纯外部包。
 * - app 是默认 flow（graph/state/nodes/compaction/topology + flow-tools/task 工具装配）。
 * - surfaces 是 ACP/CLI 适配器,只向上 import。
 * - index.ts（入口）= 组合根:createFlowRuntime 折入此处（原 compose 层已并入）,装配 runtime+app→FlowRuntime。
 *
 * 扫描 src/ 全部 .ts 的相对 import / re-export，发现上行依赖即失败并列出违规。
 */

import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, resolve, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), "../src");

type Layer = "core" | "runtime" | "libs" | "app" | "surfaces" | "root";

/** 每层允许 import 的层集合（自身 + 所有左侧层）。 */
const ALLOWED: Record<Layer, ReadonlySet<Layer>> = {
  core: new Set(["core"]),
  runtime: new Set(["runtime", "core"]),
  libs: new Set(["libs", "runtime", "core"]),
  app: new Set(["app", "libs", "runtime", "core"]),
  surfaces: new Set(["surfaces", "app", "libs", "runtime", "core"]),
  root: new Set(["root", "surfaces", "app", "libs", "runtime", "core"]),
};

/**
 * 受控例外：{ 文件 src-相对路径 → 允许的额外目标层 }。
 * （compose 层已并入 index.ts;原 runtime/flow-runtime→compose 例外已移除。当前无例外。）
 */
const ALLOWLIST: Record<string, ReadonlySet<Layer>> = {};

/** 由 src-相对路径判定所属层。 */
function layerOf(srcRelPath: string): Layer {
  const top = srcRelPath.split(sep)[0]!;
  if (top === "core") return "core";
  if (top === "runtime") return "runtime";
  if (top === "libs") return "libs";
  if (top === "app") return "app";
  if (top === "surfaces") return "surfaces";
  return "root"; // src 顶层文件（index.ts 等）
}

/** 递归收集 src/ 下全部 .ts（排除 .d.ts）。 */
function collectTs(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const full = resolve(dir, name);
    if (statSync(full).isDirectory()) collectTs(full, out);
    else if (name.endsWith(".ts") && !name.endsWith(".d.ts")) out.push(full);
  }
  return out;
}

/** 抽取一个文件里所有相对 import / re-export / side-effect import 的目标路径。 */
function relativeImports(content: string): string[] {
  const specs: string[] = [];
  // `import ... from "."` / `export ... from "."`（含 import type / export type）
  const fromRe = /\bfrom\s+["'](\.[^"']+)["']/g;
  // 纯副作用 import "."
  const sideRe = /\bimport\s+["'](\.[^"']+)["']/g;
  for (const re of [fromRe, sideRe]) {
    let m: RegExpExecArray | null;
    while ((m = re.exec(content)) !== null) specs.push(m[1]!);
  }
  return specs;
}

describe("分层架构守卫（import 方向）", () => {
  const files = collectTs(SRC);

  it("src/ 下能扫到文件（自检，防 glob 失效误绿）", () => {
    expect(files.length).toBeGreaterThan(20);
  });

  it("每个文件只 import 其允许的层（core→runtime→kit→app→surfaces→index）", () => {
    const violations: string[] = [];

    for (const file of files) {
      const srcRel = relative(SRC, file);
      const fromLayer = layerOf(srcRel);
      const allowed = new Set(ALLOWED[fromLayer]);
      for (const extra of ALLOWLIST[srcRel] ?? []) allowed.add(extra);

      for (const spec of relativeImports(readFileSync(file, "utf-8"))) {
        const targetAbs = resolve(dirname(file), spec);
        const targetRel = relative(SRC, targetAbs);
        if (targetRel.startsWith("..")) continue; // 解析到 src 外（不应发生），跳过
        const toLayer = layerOf(targetRel);
        if (!allowed.has(toLayer)) {
          violations.push(`${srcRel} [${fromLayer}] → ${spec} [${toLayer}]`);
        }
      }
    }

    expect(violations, `发现跨层上行依赖:\n${violations.join("\n")}`).toEqual([]);
  });
});
