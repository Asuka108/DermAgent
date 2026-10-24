/**
 * 跨平台 staging 辅助（Windows PowerShell / macOS / Linux）。
 * 供 package-platforms.mjs 与 package.mjs 使用。
 */
import { spawn, spawnSync } from "node:child_process";
import { cp, lstat, mkdir, readdir, rm, stat } from "node:fs/promises";
import { createWriteStream, existsSync } from "node:fs";
import path from "node:path";
import { createGzip } from "node:zlib";
import { fileURLToPath } from "node:url";
import { commandExists } from "./tools.mjs";

export const STAGING_EXCLUDES = [
  ".git",
  ".github",
  ".idea",
  ".vscode",
  ".DS_Store",
  ".flow-sessions", // 本地 CLI 会话状态，不应打入发布包
  ".agents", // 本地 agent 工具目录，不应打入发布包
  ".claude",
  ".opencode",
  ".codex",
  "node_modules",
  "dist-packages",
  "logs",
  "coverage",
  "src",
  "tests",
  ".env",
  ".env.local",
  ".env.example",
  ".gitignore",
  ".version.json",
  ".platform.json",
  "agent-package.release.json",
  "package-lock.json",
  "code-graph.json",
  "tsconfig.json",
  "tsconfig.*.json",
  "vitest.config.*",
  "CLAUDE.md",
  "AGENTS.md", // 常为指向 CLAUDE.md 的符号链接，发布包不需要
  "QUICKSTART.md",
  "*.tgz",
  "*.tar.gz",
  "*.zip",
  "*.log",
  "*.local.json",
  "*.tsbuildinfo",
  "*.map",
  "*.tmp",
];

function globMatch(pattern, value) {
  const re = new RegExp(
    `^${pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`,
  );
  return re.test(value);
}

export function shouldExclude(relPath, { excludeDist = false } = {}) {
  const norm = relPath.replace(/\\/g, "/").replace(/^\.\//, "");
  if (!norm || norm === ".") return false;

  const segments = norm.split("/");
  const fileName = segments.at(-1) ?? "";
  const patterns = excludeDist ? [...STAGING_EXCLUDES, "dist"] : STAGING_EXCLUDES;

  for (const pat of patterns) {
    if (pat.includes("*")) {
      if (
        segments.some((s) => globMatch(pat, s)) ||
        globMatch(pat, norm) ||
        globMatch(pat, fileName)
      ) {
        return true;
      }
    } else if (segments.includes(pat) || fileName === pat) {
      return true;
    }
  }
  return false;
}

function rsyncExcludeArgs(excludeDist) {
  const patterns = excludeDist ? [...STAGING_EXCLUDES, "dist"] : STAGING_EXCLUDES;
  const args = [];
  for (const pat of patterns) {
    if (pat.includes("*")) {
      args.push("--exclude", pat);
    } else {
      args.push("--exclude", `${pat}/`, "--exclude", pat);
    }
  }
  return args;
}

export async function copyPackageTree(pkgDir, destDir, { excludeDist = false } = {}) {
  await mkdir(destDir, { recursive: true });

  if (commandExists("rsync")) {
    const src = `${path.resolve(pkgDir).replace(/\\/g, "/")}/`;
    const dest = `${path.resolve(destDir).replace(/\\/g, "/")}/`;
    const result = spawnSync("rsync", ["-a", ...rsyncExcludeArgs(excludeDist), src, dest], {
      stdio: "inherit",
      shell: false,
    });
    if (result.status === 0) return;
    throw new Error(`rsync exited ${result.status ?? 1}`);
  }

  const entries = await readdir(pkgDir);
  for (const name of entries) {
    const src = path.join(pkgDir, name);
    if (shouldExclude(name, { excludeDist })) continue;
    const dest = path.join(destDir, name);
    await cp(src, dest, {
      recursive: true,
      filter: (source) => {
        const rel = path.relative(pkgDir, source);
        return !shouldExclude(rel, { excludeDist });
      },
    });
  }
}

export async function pruneReleaseResidue(stageRoot) {
  async function walk(dir) {
    for (const name of await readdir(dir)) {
      const full = path.join(dir, name);
      // 使用 lstat 避免跟随符号链接；staging 中可能存在指向已排除文件的悬空链接
      const st = await lstat(full);
      if (st.isSymbolicLink()) {
        continue;
      }
      if (st.isDirectory()) {
        await walk(full);
        continue;
      }
      if (
        name === "agent-package.release.json" ||
        name.endsWith(".tgz") ||
        name.endsWith(".tar.gz") ||
        name.endsWith(".zip")
      ) {
        await rm(full, { force: true });
      }
    }
  }
  await walk(stageRoot);
}

function tarPathArg(p) {
  return process.platform === "win32" ? p.replace(/\\/g, "/") : p;
}

function tarCommand() {
  return "tar";
}

/** Windows: use bsdtar (System32) for zip — Git tar misparses `C:` in zip output paths. */
function zipTarCommand() {
  if (process.platform === "win32") {
    const winTar = path.join(process.env.SystemRoot ?? "C:\\Windows", "System32", "tar.exe");
    if (existsSync(winTar)) return winTar;
  }
  return tarCommand();
}

function zipTarAvailable() {
  const cmd = zipTarCommand();
  return path.isAbsolute(cmd) ? existsSync(cmd) : commandExists(cmd);
}

export async function createTarGz(artifact, parentDir, folderName) {
  if (process.platform === "darwin") {
    process.env.COPYFILE_DISABLE = "1";
  }

  const absArtifact = path.resolve(artifact);
  await mkdir(path.dirname(absArtifact), { recursive: true });

  if (commandExists("gzip")) {
    await new Promise((resolve, reject) => {
      const out = createWriteStream(absArtifact);
      const tar = spawn(
        tarCommand(),
        ["-C", tarPathArg(parentDir), "-cf", "-", folderName],
        { stdio: ["ignore", "pipe", "inherit"], shell: false },
      );
      const gzip = spawn("gzip", ["-n"], { stdio: ["pipe", "pipe", "inherit"], shell: false });
      tar.stdout.pipe(gzip.stdin);
      gzip.stdout.pipe(out);
      const fail = (err) => reject(err);
      tar.on("error", fail);
      gzip.on("error", fail);
      out.on("error", fail);
      out.on("finish", resolve);
      tar.on("close", (code) => {
        if (code !== 0 && code !== null) reject(new Error(`tar exited ${code}`));
      });
    });
    return;
  }

  await new Promise((resolve, reject) => {
    const out = createWriteStream(absArtifact);
    const tar = spawn(
      tarCommand(),
      ["-C", tarPathArg(parentDir), "-cf", "-", folderName],
      { stdio: ["ignore", "pipe", "inherit"], shell: false },
    );
    const gz = createGzip({ level: 9 });
    tar.stdout.pipe(gz).pipe(out);
    const fail = (err) => reject(err);
    tar.on("error", fail);
    gz.on("error", fail);
    out.on("error", fail);
    out.on("finish", resolve);
    tar.on("close", (code) => {
      if (code !== 0 && code !== null) reject(new Error(`tar exited ${code}`));
    });
  });
}

async function createZipTar(artifact, parentDir, folderName) {
  const absArtifact = path.resolve(artifact);
  const parent = path.resolve(parentDir);
  await new Promise((resolve, reject) => {
    const child = spawn(
      zipTarCommand(),
      ["-a", "-cf", tarPathArg(absArtifact), "-C", tarPathArg(parent), folderName],
      { stdio: "inherit", shell: false },
    );
    child.on("error", reject);
    child.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`tar zip exited ${code}`))));
  });
}

async function createZipPowerShell(artifact, parentDir, folderName) {
  const absArtifact = path.resolve(artifact);
  const absFolder = path.join(path.resolve(parentDir), folderName);
  await rm(absArtifact, { force: true });
  await new Promise((resolve, reject) => {
    const ps = spawn(
      "powershell.exe",
      [
        "-NoProfile",
        "-Command",
        `Compress-Archive -LiteralPath '${absFolder.replace(/'/g, "''")}' -DestinationPath '${absArtifact.replace(/'/g, "''")}' -Force`,
      ],
      { stdio: "inherit", shell: false },
    );
    ps.on("error", reject);
    ps.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`Compress-Archive exited ${code}`))));
  });
}

/** bestzip@2 纯 Node 实现；经 npx 按需拉取，无需预装系统 zip（兼容 Node >=20）。 */
const NPX_BESTZIP = "bestzip@2.2.5";

function npxCommand() {
  return process.platform === "win32" ? "npx.cmd" : "npx";
}

/**
 * Node 兜底：npx -y bestzip --force node（纯 JS，不依赖系统 zip/tar/PowerShell）。
 * 若项目已安装 bestzip（如 devDependency），优先本地 import 避免重复下载。
 */
async function createZipNode(artifact, parentDir, folderName) {
  const absArtifact = path.resolve(artifact);
  const absParent = path.resolve(parentDir);

  try {
    const { nodeZip } = await import("bestzip");
    await nodeZip({ source: folderName, destination: absArtifact, cwd: absParent });
    return;
  } catch {
    // 未安装 bestzip → npx 按需拉取
  }

  await new Promise((resolve, reject) => {
    const child = spawn(
      npxCommand(),
      ["-y", NPX_BESTZIP, "--force", "node", absArtifact, folderName],
      { cwd: absParent, stdio: "inherit", shell: process.platform === "win32" },
    );
    child.on("error", reject);
    child.on("close", (code) =>
      code === 0 ? resolve() : reject(new Error(`npx ${NPX_BESTZIP} exited ${code}`)),
    );
  });
}

export async function createZipArchive(artifact, parentDir, folderName) {
  const absArtifact = path.resolve(artifact);
  await mkdir(path.dirname(absArtifact), { recursive: true });
  await rm(absArtifact, { force: true });

  if (commandExists("zip")) {
    await new Promise((resolve, reject) => {
      const child = spawn("zip", ["-qr", absArtifact, folderName], {
        cwd: parentDir,
        stdio: "inherit",
        shell: false,
      });
      child.on("error", reject);
      child.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`zip exited ${code}`))));
    });
    return;
  }

  if (zipTarAvailable()) {
    try {
      await createZipTar(absArtifact, parentDir, folderName);
      return;
    } catch (err) {
      if (process.platform !== "win32") throw err;
    }
  }

  if (process.platform === "win32") {
    try {
      await createZipPowerShell(absArtifact, parentDir, folderName);
      return;
    } catch {
      // PowerShell 不可用或失败 → Node 兜底
    }
  }

  await createZipNode(absArtifact, parentDir, folderName);
}
