#!/usr/bin/env node
/**
 * 跨平台打包脚本（PowerShell / bash / macOS / Linux）。
 * 生成 npm tgz 及 Nuwax tar/zip 制品。
 */
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { copyFileSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
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

function readJson(rel) {
  return JSON.parse(readFileSync(path.join(PKG_DIR, rel), "utf8"));
}

function run(cmd, args, { cwd = PKG_DIR } = {}) {
  const result = spawnSync(cmd, args, { cwd, stdio: "inherit", shell: process.platform === "win32" });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function parseArgs(argv) {
  let format = "all";
  let outDir = "dist-packages";
  let skipTests = false;
  let bundleNodeModules = true;
  let version = "";

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--format") format = argv[++i] ?? "";
    else if (arg === "--out") outDir = argv[++i] ?? outDir;
    else if (arg === "--version") version = argv[++i] ?? "";
    else if (arg === "--skip-tests") skipTests = true;
    else if (arg === "--no-bundle-node-modules") bundleNodeModules = false;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else if (/^[0-9]+\.[0-9]+\.[0-9]+/.test(arg)) version = arg;
    else throw new Error(`Unknown option: ${arg}`);
  }
  return { help: false, format, outDir, skipTests, bundleNodeModules, version };
}

function usage() {
  console.log(`Usage: node scripts/package.mjs [options]

Options:
  --format all|npm-tgz|tgz|tar|zip   Artifact format (default: all)
  --out DIR                          Output directory (default: dist-packages)
  --version VERSION                  Override package version metadata
  --skip-tests                       Build without running tests
  --no-bundle-node-modules           Legacy vendored node_modules instead of esbuild bundle
  -h, --help                         Show this help`);
}

function writePackageMetadata(stageRoot, { version, packageName, agentName, bundleNodeModules }) {
  const generatedAt = new Date().toISOString();
  const esbuildBundle = bundleNodeModules;
  const bundleStrategy = esbuildBundle ? "esbuild-bundle" : "vendored-node-modules";
  writeFileSync(
    path.join(stageRoot, ".version.json"),
    `${JSON.stringify(
      {
        schema: "nuwax.agent.version.v1",
        packageName,
        agentName,
        version,
        generatedAt,
        bundleStrategy,
      },
      null,
      2,
    )}\n`,
  );
  writeFileSync(
    path.join(stageRoot, ".platform.json"),
    `${JSON.stringify(
      {
        schema: "nuwax.agent.platform.v1",
        packageName,
        agentName,
        version,
        entrypoints: {
          server: esbuildBundle ? "dist/bundle.mjs" : "dist/index.js",
          graph: esbuildBundle ? "dist/bundle.mjs graph" : "dist/index.js graph",
        },
        dependencies: {
          strategy: bundleStrategy,
          nodeModules: esbuildBundle ? "none" : "bundled",
          installCommand: esbuildBundle ? null : "npm install --omit=dev",
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
      },
      null,
      2,
    )}\n`,
  );
}

function sha256(file) {
  return createHash("sha256").update(readFileSync(file)).digest("hex");
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) {
    usage();
    return;
  }

  const { format, skipTests, bundleNodeModules } = opts;
  if (!["all", "npm-tgz", "tgz", "tar", "zip"].includes(format)) {
    console.error(`Invalid --format: ${format}`);
    process.exit(1);
  }

  const pkgName = readJson("package.json").name;
  const agentName = readJson(".nuwax-agent/agent-package.json").name;
  const version = opts.version || readJson("package.json").version;
  const agentVersion = readJson(".nuwax-agent/agent-package.json").version;
  const outDir = path.resolve(PKG_DIR, opts.outDir);
  const npmCache = process.env.NPM_CONFIG_CACHE || path.join(tmpdir(), "deepagents-flow-ts-npm-cache");

  if (version !== agentVersion) {
    console.error(`Version mismatch: package.json=${version} agent-package.json=${agentVersion}`);
    process.exit(1);
  }

  console.log(`Packaging ${pkgName} v${version}`);
  run("pnpm", ["run", "build"]);

  if (!skipTests) {
    console.log("\nRunning tests...");
    run("pnpm", ["test"]);
  } else {
    console.log("Skipping tests by request.");
  }

  const stagingDir = await mkdtemp(path.join(tmpdir(), "nuwax-agent-package-"));
  const stageRoot = path.join(stagingDir, `${pkgName}-${version}`);
  const artifacts = [];

  try {
    console.log("\nPreparing staging directory...");
    await mkdir(stageRoot, { recursive: true });
    await copyPackageTree(PKG_DIR, stageRoot, { excludeDist: false });
    await pruneReleaseResidue(stageRoot);

    if (bundleNodeModules) {
      console.log("\nBundling runnable agent into staging dist/bundle.mjs (esbuild)...");
      await rm(path.join(stageRoot, "dist"), { recursive: true, force: true });
      await bundleAgent({
        entry: "src/index.ts",
        outfile: path.join(stageRoot, "dist/bundle.mjs"),
        cwd: PKG_DIR,
      });
    } else {
      console.log("\nVendoring production node_modules (legacy --no-bundle-node-modules)...");
      run("npm", ["--cache", npmCache, "install", "--omit=dev", "--no-package-lock"], { cwd: stageRoot });
    }

    writePackageMetadata(stageRoot, { version, packageName: pkgName, agentName, bundleNodeModules });
    await mkdir(outDir, { recursive: true });
    copyFileSync(path.join(stageRoot, ".version.json"), path.join(outDir, `${agentName}-${version}.version.json`));
    copyFileSync(path.join(stageRoot, ".platform.json"), path.join(outDir, `${agentName}-${version}.platform.json`));

    if (format === "all" || format === "npm-tgz" || format === "tgz") {
      console.log("\nCreating npm tgz...");
      const pack = spawnSync("npm", ["--cache", npmCache, "pack", "--pack-destination", outDir], {
        cwd: PKG_DIR,
        encoding: "utf8",
        shell: process.platform === "win32",
      });
      if (pack.status !== 0) process.exit(pack.status ?? 1);
      const tarball = pack.stdout.trim().split(/\r?\n/).at(-1);
      const tarballPath = path.join(outDir, path.basename(tarball));
      artifacts.push(tarballPath);
      copyFileSync(tarballPath, path.join(PKG_DIR, path.basename(tarball)));
      console.log(`Created ${tarballPath}`);
    }

    if (format === "all" || format === "tar") {
      console.log("\nCreating Nuwax tar.gz...");
      const artifact = path.join(outDir, `${agentName}-${version}-nuwax.tar.gz`);
      await rm(artifact, { force: true });
      await createTarGz(artifact, stagingDir, `${pkgName}-${version}`);
      artifacts.push(artifact);
      console.log(`Created ${artifact}`);
    }

    if (format === "all" || format === "zip") {
      console.log("\nCreating Nuwax zip...");
      const artifact = path.join(outDir, `${agentName}-${version}-nuwax.zip`);
      await rm(artifact, { force: true });
      await createZipArchive(artifact, stagingDir, `${pkgName}-${version}`);
      artifacts.push(artifact);
      console.log(`Created ${artifact}`);
    }

    console.log("\nWriting release metadata...");
    const artifactRecords = artifacts.map((file) => ({
      file: path.basename(file),
      type: file.endsWith(".zip") ? "nuwax-zip" : file.endsWith(".tar.gz") ? "nuwax-tar" : "npm-tgz",
      sha256: sha256(file),
    }));
    const checksums = {
      schema: "nuwax.agent.package-checksums.v1",
      packageName: pkgName,
      version,
      artifacts: artifactRecords,
    };
    const baseManifest = readJson(".nuwax-agent/agent-package.json");
    const primary = artifactRecords[0];
    const release = {
      ...baseManifest,
      version,
      source: primary
        ? { type: primary.type, path: `./${primary.file}`, version }
        : baseManifest.source,
      checksum: primary ? { algorithm: "sha256", value: primary.sha256 } : baseManifest.checksum,
      artifacts: artifactRecords,
      platform: { schema: "nuwax.agent.platform.v1", path: ".platform.json" },
    };
    writeFileSync(path.join(outDir, "package-checksums.json"), `${JSON.stringify(checksums, null, 2)}\n`);
    writeFileSync(path.join(outDir, "agent-package.release.json"), `${JSON.stringify(release, null, 2)}\n`);
    writeFileSync(path.join(PKG_DIR, "agent-package.release.json"), `${JSON.stringify(release, null, 2)}\n`);

    console.log(`Wrote ${path.join(outDir, "agent-package.release.json")}`);
    console.log(`Wrote ${path.join(outDir, "package-checksums.json")}`);
    console.log("\nPackage artifacts:");
    for (const artifact of artifacts) {
      console.log(`${sha256(artifact)}  ${artifact}`);
    }
  } finally {
    await rm(stagingDir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
