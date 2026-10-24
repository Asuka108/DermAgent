#!/usr/bin/env node
/**
 * 校验生成的打包制品及可选 checksum 元数据。
 * 跨平台（PowerShell / macOS / Linux）。
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { commandExists } from "./lib/tools.mjs";

function usage() {
  console.log(`Usage: node scripts/validate-package.mjs --artifact PATH [--checksums package-checksums.json]

Options:
  --artifact PATH          Artifact to validate
  --checksums PATH         Optional checksum manifest
  --require-node-modules   Require bundled node_modules in the artifact
  -h, --help               Show help`);
}

function parseArgs(argv) {
  let artifact = "";
  let checksums = "";
  let requireNodeModules = false;

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--artifact") artifact = argv[++i] ?? "";
    else if (arg === "--checksums") checksums = argv[++i] ?? "";
    else if (arg === "--require-node-modules") requireNodeModules = true;
    else if (arg === "-h" || arg === "--help") return { help: true };
    else throw new Error(`Unknown option: ${arg}`);
  }
  return { help: false, artifact, checksums, requireNodeModules };
}

function run(cmd, args) {
  const result = spawnSync(cmd, args, { stdio: "pipe", encoding: "utf8", shell: false });
  if (result.status !== 0) {
    const detail = (result.stderr || result.stdout || "").trim();
    throw new Error(detail || `${cmd} exited ${result.status ?? 1}`);
  }
  return result.stdout ?? "";
}

function testArchive(artifact) {
  const abs = path.resolve(artifact);
  if (abs.endsWith(".zip")) {
    if (commandExists("unzip")) {
      run("unzip", ["-tq", abs]);
      return;
    }
    run("tar", ["-tf", abs]);
    return;
  }
  if (abs.endsWith(".tar.gz") || abs.endsWith(".tgz")) {
    run("tar", ["-tzf", abs]);
    return;
  }
  throw new Error(`Unsupported artifact type: ${artifact}`);
}

function listEntries(artifact) {
  const abs = path.resolve(artifact);
  if (abs.endsWith(".zip")) {
    if (commandExists("unzip")) {
      return run("unzip", ["-Z1", abs]);
    }
    return run("tar", ["-tf", abs]);
  }
  if (abs.endsWith(".tar.gz") || abs.endsWith(".tgz")) {
    return run("tar", ["-tzf", abs]);
  }
  throw new Error(`Unsupported artifact type: ${artifact}`);
}

function verifyChecksums(artifact, checksumsPath) {
  const absChecksums = path.resolve(checksumsPath);
  const doc = JSON.parse(readFileSync(absChecksums, "utf8"));
  const base = path.basename(artifact);
  const record = doc.artifacts?.find((item) => item.file === base);
  if (!record) {
    throw new Error(`No checksum record for ${base}`);
  }
  const actual = createHash("sha256").update(readFileSync(path.resolve(artifact))).digest("hex");
  if (actual !== record.sha256) {
    throw new Error(`Checksum mismatch for ${base}`);
  }
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) {
    usage();
    return;
  }

  const { artifact, checksums, requireNodeModules } = opts;
  if (!artifact) {
    console.error("--artifact must point to an existing file");
    process.exit(1);
  }

  let absArtifact;
  try {
    absArtifact = path.resolve(artifact);
    readFileSync(absArtifact);
  } catch {
    console.error("--artifact must point to an existing file");
    process.exit(1);
  }

  testArchive(absArtifact);

  if (requireNodeModules) {
    const entries = listEntries(absArtifact);
    if (!/node_modules\/deepagents\//.test(entries)) {
      console.error(`Bundled node_modules missing from artifact: ${artifact}`);
      process.exit(1);
    }
  }

  if (checksums) {
    try {
      readFileSync(path.resolve(checksums));
    } catch {
      console.error(`Checksum manifest not found: ${checksums}`);
      process.exit(1);
    }
    verifyChecksums(absArtifact, checksums);
  }

  console.log(`Package validation passed: ${absArtifact}`);
}

main();
