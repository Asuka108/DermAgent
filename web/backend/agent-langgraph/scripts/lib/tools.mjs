/**
 * 检测可选打包 CLI 工具（rsync、zip、gzip、tar）。
 * Windows 可通过 `pnpm run setup:tools`（Chocolatey）安装。
 * zip 缺失时 staging 会兜底 `npx -y bestzip@2.2.5 --force node`（纯 Node，无需预装 zip）。
 */
import { spawnSync } from "node:child_process";

const TOOL_PACKAGES = {
  rsync: "rsync",
  zip: "zip",
  gzip: "gzip",
  tar: "tar",
};

export function commandExists(cmd) {
  if (process.platform === "win32") {
    const result = spawnSync("where.exe", [cmd], { stdio: "pipe", encoding: "utf8" });
    return result.status === 0;
  }
  const result = spawnSync("sh", ["-c", `command -v ${cmd}`], { stdio: "pipe" });
  return result.status === 0;
}

export function detectPackagingTools() {
  return {
    rsync: commandExists("rsync"),
    zip: commandExists("zip"),
    gzip: commandExists("gzip"),
    tar: commandExists("tar"),
  };
}

export function formatToolReport(tools = detectPackagingTools()) {
  const lines = ["Packaging CLI tools:"];
  for (const [name, ok] of Object.entries(tools)) {
    lines.push(`  ${ok ? "✓" : "✗"} ${name}`);
  }
  if (process.platform === "win32") {
    const missing = Object.entries(tools)
      .filter(([, ok]) => !ok)
      .map(([name]) => name);
    if (missing.length > 0) {
      lines.push("");
      lines.push("Windows: install missing tools with Chocolatey:");
      lines.push("  pnpm run setup:tools");
      lines.push("  # or manually:");
      for (const name of missing) {
        const pkg = TOOL_PACKAGES[name] ?? name;
        lines.push(`  choco install ${pkg} -y`);
      }
    }
  }
  return lines.join("\n");
}
