/**
 * ripgrep（rg）可选加速层 —— 系统已安装时优先用于 grep / glob，否则由调用方回退纯 Node 实现。
 *
 * 设计：
 * - 进程内缓存 `rg` 可执行路径，避免每次工具调用都探测；
 * - 与 search.tool 相同的跳过目录（node_modules / .git / dist 等）；
 * - 输出统一为 POSIX 路径，与 path-utils 一致；
 * - rg 不可用或执行失败（exit 2 等）时返回 null，由上层 fallback。
 */

import { spawnSync } from "node:child_process";
import { toPosixPath } from "./path-utils.js";

/** 与 search.tool walk 跳过的目录对齐 */
const SKIP_GLOBS = [
  "!node_modules/**",
  "!.git/**",
  "!dist/**",
  "!.flow-sessions/**",
  "!.pnpm/**",
];

/** 未探测 / 已探测无 rg / 已解析路径 */
let rgResolved: string | null | undefined;

/**
 * 解析本机 ripgrep 可执行文件路径（跨平台）。
 * Windows: where.exe；Unix: command -v rg
 */
export function resolveRipgrepBinary(): string | null {
  if (rgResolved !== undefined) return rgResolved;

  if (process.platform === "win32") {
    const result = spawnSync("where.exe", ["rg"], { encoding: "utf8", windowsHide: true });
    if (result.status === 0) {
      const first = result.stdout.trim().split(/\r?\n/)[0]?.trim();
      rgResolved = first || null;
    } else {
      rgResolved = null;
    }
    return rgResolved;
  }

  const result = spawnSync("sh", ["-c", "command -v rg"], { encoding: "utf8" });
  if (result.status === 0) {
    const bin = result.stdout.trim().split("\n")[0]?.trim();
    rgResolved = bin || "rg";
  } else {
    rgResolved = null;
  }
  return rgResolved;
}

/** 是否已安装 rg（探测结果缓存） */
export function isRipgrepAvailable(): boolean {
  return resolveRipgrepBinary() !== null;
}

/** 单测用：重置 rg 路径缓存 */
export function resetRipgrepCache(): void {
  rgResolved = undefined;
}

export interface RipgrepGrepOptions {
  /** 搜索根目录（绝对路径，sandbox 已校验） */
  root: string;
  /** 正则表达式（传给 rg 的 pattern 参数） */
  pattern: string;
  /** 可选文件名 glob 过滤（传给 rg --glob） */
  glob?: string;
  maxMatches?: number;
}

/**
 * 用 rg 做内容搜索。成功返回格式化结果；不可用或失败返回 null（触发 Node fallback）。
 *
 * 输出格式与 Node 实现一致：`path:line: trimmed_content`（最多 200 字符）
 */
export function ripgrepGrep(opts: RipgrepGrepOptions): string | null {
  const rg = resolveRipgrepBinary();
  if (!rg) return null;

  const max = opts.maxMatches ?? 200;
  const args = [
    "--no-heading",
    "-n",
    "--color",
    "never",
    "--no-messages",
    "--path-separator",
    "/",
    ...SKIP_GLOBS.flatMap((g) => ["--glob", g]),
  ];
  if (opts.glob) args.push("--glob", opts.glob);
  args.push(opts.pattern, ".");

  const result = spawnSync(rg, args, {
    cwd: opts.root,
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
    windowsHide: true,
  });

  if (result.error) return null;
  // 0=有匹配 1=无匹配 2=错误（非法正则等）→ fallback
  if (result.status === 2) return null;
  if (result.status !== 0 && result.status !== 1) return null;

  const lines = result.stdout
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .slice(0, max);
  if (!lines.length) return "(no matches)";

  return lines
    .map((line) => formatRipgrepMatchLine(line))
    .join("\n");
}

export interface RipgrepGlobOptions {
  root: string;
  pattern: string;
  maxFiles?: number;
}

/** 用 rg --files 列文件；失败返回 null */
export function ripgrepGlob(opts: RipgrepGlobOptions): string | null {
  const rg = resolveRipgrepBinary();
  if (!rg) return null;

  const max = opts.maxFiles ?? 200;
  const args = [
    "--files",
    "--color",
    "never",
    "--no-messages",
    "--path-separator",
    "/",
    ...SKIP_GLOBS.flatMap((g) => ["--glob", g]),
    "--glob",
    opts.pattern,
    ".",
  ];

  const result = spawnSync(rg, args, {
    cwd: opts.root,
    encoding: "utf8",
    maxBuffer: 10 * 1024 * 1024,
    windowsHide: true,
  });

  if (result.error) return null;
  if (result.status !== 0 && result.status !== 1) return null;

  const files = result.stdout
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
    .slice(0, max);
  if (!files.length) return "(no matches)";
  return files.map((f) => toPosixPath(f)).join("\n");
}

/** 解析 rg 单行输出 path:line:content（content 中可能含冒号） */
function formatRipgrepMatchLine(line: string): string {
  const m = line.match(/^(.+?):(\d+):(.*)$/);
  if (!m) return toPosixPath(line);
  const [, file, lineNo, content] = m;
  return `${toPosixPath(file!)}:${lineNo}: ${content!.trim().slice(0, 200)}`;
}
