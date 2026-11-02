#!/usr/bin/env node
/**
 * 跨平台按平台打包（纯 Node，无需 bash）。
 * 生成 {agentName}-{os}-{arch}-{version}.{tar.gz|zip} 及 platforms.json。
 */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, mkdir, rm, copyFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { bundleAgent } from "./lib/bundle.mjs";
import {
  copyPackageTree,
  createTarGz,
  createZipArchive,
  pruneReleaseResidue,
} from "./lib/staging.mjs";

const PKG_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const PLATFORMS = [
  { key: "linux-x86_64", ext: "tar.gz" },
  { key: "linux-arm64", ext: "tar.gz" },
  { key: "darwin-arm64", ext: "tar.gz" },
  { key: "darwin-x86_64", ext: "tar.gz" },
  { key: "windows-x86_64", ext: "zip" },
];

function readJson(rel) {
  return JSON.parse(readFileSync(path.join(PKG_DIR, rel), "utf8"));
}

function err(msg) {
  console.error(`ERROR: ${msg}`);
}

function parseArgs(argv) {
  let verbose = false;
  let printArtifacts = false;
  const positional = [];

  for (const arg of argv) {
    if (arg === "-v" || arg === "--verbose") verbose = true;
    else if (arg === "-q" || arg === "--quiet") verbose = false;
    else if (arg === "--print-artifacts") printArtifacts = true;
    else if (arg === "-h" || arg === "--help") return { help: true, verbose, printArtifacts, positional };
    else if (arg.startsWith("-")) throw new Error(`Unknown option: ${arg}`);
    else positional.push(arg);
  }
  return { help: false, verbose, printArtifacts, positional };
}

function usage() {
  console.log(`Usage: node scripts/package-platforms.mjs [agentName] [version] [outDir] [options]

Options:
  -v, --verbose          Stream progress to stderr (default: quiet)
  -q, --quiet            Quiet mode (default)
      --print-artifacts  Print produced artifact paths to stdout (one per line)
  -h, --help             Show this help`);
}

async function ensureEsbuild() {
  try {
    await import("esbuild");
  } catch {
    const { spawnSync } = await import("node:child_process");
    const pm = spawnSync("pnpm", ["install"], { cwd: PKG_DIR, stdio: "inherit", shell: true });
    if (pm.status !== 0) {
      spawnSync("npm", ["install"], { cwd: PKG_DIR, stdio: "inherit", shell: true });
    }
  }
}

function writePackageMetadata(stageRoot, { version, packageName, agentName }) {
  const generatedAt = new Date().toISOString();
  const versionJson = {
    schema: "nuwax.agent.version.v1",
    packageName,
    agentName,
    version,
    generatedAt,
    bundleStrategy: "esbuild-bundle",
  };
  const platformJson = {
    schema: "nuwax.agent.platform.v1",
    packageName,
    agentName,
    version,
    entrypoints: { server: "dist/bundle.mjs", graph: "dist/bundle.mjs graph" },
    dependencies: {
      strategy: "esbuild-bundle",
      nodeModules: "none",
      installCommand: null,
    },
    config: {
      placeholders: ".nuwax-agent/placeholders.json",
      package: ".nuwax-agent/package.config.json",
    },
    platforms: [
      { os: "darwin", arch: "arm64" },
      { os: "darwin", arch: "x64" },
      { os: "linux", arch: "x64" },
      { os: "linux", arch: "arm64" },
    ],
  };
  writeFileSync(path.join(stageRoot, ".version.json"), `${JSON.stringify(versionJson, null, 2)}\n`);
  writeFileSync(path.join(stageRoot, ".platform.json"), `${JSON.stringify(platformJson, null, 2)}\n`);
}

function writePlatformsJson(outPath, { agentName, version, pairs }) {
  const base = (process.env.NUWAX_ARTIFACT_BASE_URL || "").replace(/\/+$/, "");
  const platforms = {};
  for (const { key, file } of pairs) {
    const buf = readFileSync(file);
    const record = {
      file: path.basename(file),
      sha256: createHash("sha256").update(buf).digest("hex"),
      size: buf.length,
    };
    if (base) record.url = `${base}/${path.basename(file)}`;
    platforms[key] = record;
  }
  const doc = { schema: "nuwax.agent.platforms.v1", agentName, version, platforms };
  writeFileSync(outPath, `${JSON.stringify(doc, null, 2)}\n`);
}

/**
 * 各平台制品内容相同，仅扩展名/平台名不同：tar.gz 只压缩一次再复制，zip 与 tar.gz 并行生成。
 */
async function createPlatformArchives({ outDir, agentName, version, stagingDir, stageFolder, log }) {
  const artifactPath = (key, ext) => path.join(outDir, `${agentName}-${key}-${version}.${ext}`);
  const tarPlatforms = PLATFORMS.filter((p) => p.ext === "tar.gz");
  const zipPlatforms = PLATFORMS.filter((p) => p.ext === "zip");
  const pairByKey = new Map();

  const [{ key: primaryTarKey, ext: primaryTarExt }] = tarPlatforms;
  const primaryTarPath = artifactPath(primaryTarKey, primaryTarExt);

  await Promise.all([
    (async () => {
      await rm(primaryTarPath, { force: true });
      log(`Creating ${path.basename(primaryTarPath)}`);
      await createTarGz(primaryTarPath, stagingDir, stageFolder);
      pairByKey.set(primaryTarKey, { key: primaryTarKey, file: primaryTarPath });
    })(),
    ...zipPlatforms.map(async ({ key, ext }) => {
      const artifact = artifactPath(key, ext);
      await rm(artifact, { force: true });
      log(`Creating ${path.basename(artifact)}`);
      await createZipArchive(artifact, stagingDir, stageFolder);
      pairByKey.set(key, { key, file: artifact });
    }),
  ]);

  await Promise.all(
    tarPlatforms.slice(1).map(async ({ key, ext }) => {
      const artifact = artifactPath(key, ext);
      await rm(artifact, { force: true });
      log(`Creating ${path.basename(artifact)}`);
      await copyFile(primaryTarPath, artifact);
      pairByKey.set(key, { key, file: artifact });
    }),
  );

  const pairs = PLATFORMS.map(({ key }) => pairByKey.get(key));
  return { artifacts: pairs.map((p) => p.file), pairs };
}

async function main() {
  const { help, verbose, printArtifacts, positional } = parseArgs(process.argv.slice(2));
  if (help) {
    usage();
    return;
  }

  const log = verbose ? (...a) => console.error(...a) : () => {};

  let agentName = positional[0] ?? readJson(".nuwax-agent/agent-package.json").name;
  let version = positional[1] ?? readJson("package.json").version;
  let outDir = positional[2] ?? "dist-packages";
  const packageName = readJson("package.json").name;

  if (!/^[A-Za-z0-9._-]+$/.test(agentName)) {
    err(`invalid agentName: '${agentName}'`);
    process.exit(1);
  }
  if (!/^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$/.test(version)) {
    err(`invalid version: '${version}'`);
    process.exit(1);
  }

  await ensureEsbuild();

  outDir = path.resolve(PKG_DIR, outDir);
  await mkdir(outDir, { recursive: true });

  const stagingDir = await mkdtemp(path.join(tmpdir(), "nuwax-platforms-"));
  const stageRoot = path.join(stagingDir, `${agentName}-${version}`);

  try {
    log(`Packaging ${agentName} v${version} -> ${outDir}`);
    await mkdir(stageRoot, { recursive: true });

    log("Staging runnable content");
    await copyPackageTree(PKG_DIR, stageRoot, { excludeDist: true });
    await pruneReleaseResidue(stageRoot);

    log("Bundling esbuild -> dist/bundle.mjs");
    await bundleAgent({
      entry: "src/index.ts",
      outfile: path.join(stageRoot, "dist/bundle.mjs"),
      cwd: PKG_DIR,
      quiet: !verbose,
    });

    log("Writing in-package .version.json / .platform.json");
    writePackageMetadata(stageRoot, { version, packageName, agentName });

    const { artifacts, pairs } = await createPlatformArchives({
      outDir,
      agentName,
      version,
      stagingDir,
      stageFolder: `${agentName}-${version}`,
      log,
    });

    const platformsJson = path.join(outDir, `${agentName}-${version}.platforms.json`);
    log(`Writing ${path.basename(platformsJson)}`);
    writePlatformsJson(platformsJson, { agentName, version, pairs });

    if (printArtifacts) {
      for (const a of artifacts) console.log(a);
      console.log(platformsJson);
    }
    log(`Done: ${artifacts.length} archives + ${path.basename(platformsJson)} in ${outDir}`);
  } finally {
    await rm(stagingDir, { recursive: true, force: true });
  }
}

main().catch((e) => {
  err(e.stack ?? e.message ?? String(e));
  process.exit(1);
});
