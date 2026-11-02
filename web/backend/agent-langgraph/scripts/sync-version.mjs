#!/usr/bin/env node
/**
 * 版本号同步脚本（跨平台 / 纯 Node ESM）。
 *
 * 以当前包的 package.json 顶层 `version` 为权威源，把它同步到发布相关的派生元数据，
 * 避免手动改一处忘一处导致打包/发布制品版本错位。
 *
 * 同步目标（按需，文件缺失则跳过）：
 *   - .nuwax-agent/agent-package.json  — 顶层 version / source.version / source.prefix
 *                                        / alternativeSources[].version|path|ref
 *   - config/flow-agent.config.json    — agent.version
 *
 * 不触碰：依赖版本（package.json dependencies）、引擎要求（nodeVersion / engines）、
 * 框架版本（metadata.*Version）——这些与产品版本无关，全局替换会误伤。
 *
 * 字符串字段（prefix / path / ref）采用「精确 oldV 替换 + 正则兜底」：先按当前版本
 * 字串替换，匹配不到则把串中任意 `\d+.\d+.\d+` 形态的旧版本号替换为 newV，覆盖
 * 「文件顶层 version 已对齐、但内部路径残留旧 semver」的漂移场景。
 *
 * 用法：
 *   node scripts/sync-version.mjs              # 执行同步（写盘）
 *   node scripts/sync-version.mjs --dry-run    # 只预览将要发生的变更，不写盘
 *   node scripts/sync-version.mjs --check      # CI 守卫：不一致则退出 1，不写盘
 *   node scripts/sync-version.mjs --pkg DIR    # 指定包目录（默认当前工作目录）
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SEMVER = /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/;
const SEMVER_ANYWHERE = /\b\d+\.\d+\.\d+\b/g;

function parseArgs(argv) {
  let mode = "apply"; // apply | dry-run | check
  let pkgDir = process.cwd();
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--dry-run") mode = "dry-run";
    else if (arg === "--check") mode = "check";
    else if (arg === "--pkg") pkgDir = path.resolve(argv[++i] ?? ".");
    else if (arg === "-h" || arg === "--help") {
      console.log(`Usage: node scripts/sync-version.mjs [options]

Options:
  --dry-run    Preview changes without writing
  --check      Exit non-zero if out of sync (no writes)
  --pkg DIR    Target package directory (default: cwd)
  -h, --help   Show this help`);
      process.exit(0);
    } else throw new Error(`Unknown option: ${arg}`);
  }
  return { mode, pkgDir };
}

function readJson(file) {
  return JSON.parse(readFileSync(file, "utf8"));
}

/** 精确子串替换：把 oldV 出现处换成 newV，不含则原样返回。 */
function bumpExact(str, oldV, newV) {
  if (typeof str !== "string" || !oldV || !str.includes(oldV)) return str;
  return str.split(oldV).join(newV);
}

/**
 * 由字符串旧值派生新值：先精确 oldV 替换；匹配不到则正则兜底，把串中任意
 * `\d+.\d+.\d+` 形态的版本号替换为 newV。
 */
function deriveString(raw, oldV, newV) {
  if (typeof raw !== "string") return raw;
  const exact = bumpExact(raw, oldV, newV);
  return exact === raw ? raw.replace(SEMVER_ANYWHERE, newV) : exact;
}

/**
 * 收集对单个 JSON 文件的版本同步动作。返回 { changes, doc }，doc 为更新后的对象。
 *
 * 每个可同步字段用 { label, get, set, derive } 描述：
 *   - get(doc) 读取当前值（undefined/null 则跳过）
 *   - set(doc, value) 写入新值（apply 阶段调用）
 *   - derive(raw, oldV, newV) 由旧值派生新值
 */
function collectChanges(file, oldV, newV) {
  const changes = [];
  if (!existsSync(file)) return { changes, doc: null };
  const doc = readJson(file);

  // 数值型字段：直接设为 newV。
  const direct = () => (raw) => newV;
  // 字符串字段：精确替换 + 正则兜底。
  const derived = () => (raw) => deriveString(raw, oldV, newV);

  const fieldsFor = (name) => {
    if (name === "agent-package.json") {
      const alts = doc.alternativeSources ?? [];
      const altFields = [];
      alts.forEach((src, idx) => {
        if (src.version !== undefined)
          altFields.push({
            label: `alternativeSources[${idx}].version (${src.type})`,
            get: (d) => d.alternativeSources[idx].version,
            set: (d, v) => (d.alternativeSources[idx].version = v),
            derive: direct(),
          });
        if (typeof src.path === "string")
          altFields.push({
            label: `alternativeSources[${idx}].path (${src.type})`,
            get: (d) => d.alternativeSources[idx].path,
            set: (d, v) => (d.alternativeSources[idx].path = v),
            derive: derived(),
          });
        if (typeof src.ref === "string")
          altFields.push({
            label: `alternativeSources[${idx}].ref (${src.type})`,
            get: (d) => d.alternativeSources[idx].ref,
            set: (d, v) => (d.alternativeSources[idx].ref = v),
            derive: derived(),
          });
      });
      return [
        { label: "version", get: (d) => d.version, set: (d, v) => (d.version = v), derive: direct() },
        { label: "source.version", get: (d) => d.source?.version, set: (d, v) => (d.source.version = v), derive: direct() },
        { label: "source.prefix", get: (d) => d.source?.prefix, set: (d, v) => (d.source.prefix = v), derive: derived() },
        ...altFields,
      ];
    }
    if (name === "flow-agent.config.json") {
      return [
        { label: "agent.version", get: (d) => d.agent?.version, set: (d, v) => (d.agent.version = v), derive: direct() },
      ];
    }
    return [];
  };

  for (const f of fieldsFor(path.basename(file))) {
    const before = f.get(doc);
    if (before === undefined || before === null) continue;
    const after = f.derive(before);
    if (after !== before) {
      f.set(doc, after);
      changes.push({ file, label: f.label, before, after });
    }
  }
  return { changes, doc };
}

function main() {
  const { mode, pkgDir } = parseArgs(process.argv.slice(2));
  const pkgJsonPath = path.join(pkgDir, "package.json");
  const agentPkgPath = path.join(pkgDir, ".nuwax-agent", "agent-package.json");
  const agentConfigPath = path.join(pkgDir, "config", "flow-agent.config.json");

  if (!existsSync(pkgJsonPath)) {
    console.error(`[sync-version] package.json not found: ${pkgJsonPath}`);
    process.exit(1);
  }
  const newVersion = readJson(pkgJsonPath).version;
  if (!SEMVER.test(newVersion ?? "")) {
    console.error(`[sync-version] invalid version in package.json: ${newVersion}`);
    process.exit(1);
  }

  // oldVersion 取 agent-package.json 顶层 version（若存在）；否则取 flow-agent.config.json。
  let oldVersion = "";
  if (existsSync(agentPkgPath)) oldVersion = readJson(agentPkgPath).version ?? "";
  else if (existsSync(agentConfigPath)) oldVersion = readJson(agentConfigPath).agent?.version ?? "";

  console.log(`[sync-version] package: ${path.basename(pkgDir)}`);
  console.log(
    `[sync-version] target version: ${newVersion}${oldVersion ? ` (current derived: ${oldVersion})` : ""}`
  );

  if (oldVersion && !SEMVER.test(oldVersion)) {
    console.error(`[sync-version] current version not semver, aborting: ${oldVersion}`);
    process.exit(1);
  }

  const targets = [agentPkgPath, agentConfigPath];
  let allChanges = [];
  const docs = new Map();
  for (const file of targets) {
    const { changes, doc } = collectChanges(file, oldVersion, newVersion);
    if (doc) docs.set(file, doc);
    allChanges = allChanges.concat(changes);
  }

  if (allChanges.length === 0) {
    console.log("[sync-version] ✓ all metadata already in sync");
    process.exit(0);
  }

  for (const c of allChanges) {
    const rel = path.relative(pkgDir, c.file);
    console.log(`  ${rel} :: ${c.label}: ${c.before} → ${c.after}`);
  }

  if (mode === "check") {
    console.error(
      `[sync-version] ✗ ${allChanges.length} field(s) out of sync (run sync-version to fix)`
    );
    process.exit(1);
  }

  if (mode === "dry-run") {
    console.log(`[sync-version] dry-run: ${allChanges.length} change(s) previewed, no files written`);
    return;
  }

  // apply：按文件写回，保留原缩进风格（检测首个键的缩进，默认 2）。
  for (const [file, doc] of docs) {
    let indent = 2;
    try {
      const raw = readFileSync(file, "utf8");
      const m = raw.match(/^\{\n(\s+)"/);
      if (m) indent = m[1].length;
    } catch {
      /* keep default */
    }
    writeFileSync(file, `${JSON.stringify(doc, null, indent)}\n`);
  }
  console.log(
    `[sync-version] ✓ synced ${allChanges.length} field(s) across ${docs.size} file(s)`
  );
}

main();
