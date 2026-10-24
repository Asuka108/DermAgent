/**
 * 跨平台路径工具 —— flow 文件/搜索工具的统一路径入口。
 *
 * 背景：
 * - Windows 上 `path.relative()` 产出反斜杠，glob 正则按 `/` 分段会匹配失败；
 * - `process.env.HOME` 在 Windows 常未设置，应使用 `os.homedir()`；
 * - 模型常传 `/test.txt` 表示 workspace 根相对路径，在 Windows 上若直接 `resolve(ws, '/test.txt')`
 *   会落到盘符根（如 `C:\test.txt`），需正确处理盘符根语义。
 *
 * 约定：
 * - 逻辑层（glob 匹配、workspace 边界比较）统一 POSIX 斜杠；
 * - 文件 IO 前 resolve 成 OS 原生绝对路径。
 */

import { homedir } from "node:os";
import { existsSync, realpathSync } from "node:fs";
import { resolve, isAbsolute, relative, dirname, basename, join } from "node:path";

/**
 * 逻辑路径规范化：反斜杠 → 正斜杠。
 * 用于 glob 匹配、sandbox 前缀比较等不涉及磁盘 IO 的场景。
 */
export function toPosixPath(p: string): string {
  return p.replace(/\\/g, "/");
}

/**
 * 判断是否为「系统级绝对路径」（已落在 workspace 外、不应再相对 workspace 解析）。
 *
 * 典型：/Users/...、/home/...、C:\...、\\server\share
 * 排除：/test.txt 这类 workspace 根相对的 POSIX 风格路径（单段或无盘符前缀）。
 */
function isSystemAbsolutePath(p: string): boolean {
  // Windows 盘符路径：C:\ 或 C:/
  if (/^[A-Za-z]:[\\/]/.test(p)) return true;
  // UNC 路径
  if (p.startsWith("\\\\")) return true;

  const posix = toPosixPath(p);
  if (!posix.startsWith("/")) return false;

  // 常见 Unix / macOS 系统目录前缀
  const systemPrefixes = [
    "/Users/",
    "/home/",
    "/tmp/",
    "/var/",
    "/opt/",
    "/usr/",
    "/etc/",
    "/bin/",
    "/sbin/",
    "/lib/",
    "/System/",
    "/Volumes/",
    "/private/",
    "/dev/",
    "/proc/",
    "/run/",
    "/boot/",
    "/mnt/",
    "/media/",
    "/srv/",
    "/sys/",
    "/snap/",
    "/nix/",
    "/net/",
    "/cygdrive/",
    "/c/",
    "/d/",
  ];
  return systemPrefixes.some((prefix) => posix.startsWith(prefix));
}

/**
 * 把用户/模型传入路径解析为 OS 绝对路径（用于 readFileSync / writeFileSync 等）。
 *
 * 解析顺序：
 * 1. `~/...` → homedir（跨 Win/Mac/Linux）
 * 2. 已是系统绝对路径 → resolve 规范化
 * 3. `/foo`（POSIX workspace 根相对）→ resolve(workspaceRoot, foo.slice(1))
 * 4. 其它相对路径 → resolve(workspaceRoot, input)
 */
export function toAbsolutePath(input: string, workspaceRoot: string): string {
  const trimmed = input.trim();
  if (!trimmed) return resolve(workspaceRoot);

  if (trimmed.startsWith("~/")) {
    return resolve(homedir(), trimmed.slice(2));
  }

  // Windows 上 `/test.txt` 的 isAbsolute 为 false，但仍表示 workspace 根相对路径
  if (
    (trimmed.startsWith("/") || trimmed.startsWith("\\")) &&
    !isSystemAbsolutePath(trimmed)
  ) {
    const rest = trimmed.replace(/^[\\/]+/, "");
    return resolve(workspaceRoot, rest);
  }

  if (isAbsolute(trimmed)) {
    return resolve(trimmed);
  }

  return resolve(workspaceRoot, trimmed);
}

/**
 * walk 输出用：相对 base 的 POSIX 路径，供 glob / grep 文件名匹配。
 */
export function toWorkspaceRelativePosix(absPath: string, base: string): string {
  return toPosixPath(relative(base, absPath));
}

/**
 * 规范化后的绝对路径，用于 sandbox 边界判定（消除 ..、. 段差异）。
 */
export function normalizeAbsolutePath(absPath: string): string {
  return resolve(absPath);
}

/**
 * 判断逻辑路径是否以某 glob 前缀匹配（deniedWritePaths 等）。
 * 双方先转 POSIX，支持尾部 `/**` 与单段 `*`。
 */
export function matchPosixGlob(path: string, glob: string): boolean {
  const p = toPosixPath(path);
  const g = toPosixPath(glob).replace(/\/+$/, "");
  if (g.endsWith("/**")) return p.startsWith(g.slice(0, -3));
  if (g.endsWith("*")) return p.startsWith(g.slice(0, -1));
  return p === g || p.startsWith(g + "/");
}

/**
 * 解析符号链接后的真实绝对路径 —— 写路径沙箱校验用，防 symlink 逃逸写穿。
 *
 * `normalizeAbsolutePath` 是纯词法 `resolve`，不跟随符号链接：workspace 内一条
 * `ln -s ~/.ssh/authorized_keys ./x` 后，词法判定仍在 workspace 内 → 放行，实际写穿到
 * 任意位置。写校验前必须 realpath。
 *
 * 目标已存在 → 直接 realpathSync；尚不存在（写新文件）→ 逐级向上找最近的**存在**祖先目录
 * realpath 后拼回不存在的尾部段，保证「父链含 symlink 指向 workspace 外」也能被正确校验。
 * realpath 失败（权限/不可达）回退词法 resolve，不抛错。
 */
export function resolveRealPath(absPath: string): string {
  const normalized = resolve(absPath);
  if (existsSync(normalized)) {
    try {
      return realpathSync(normalized);
    } catch {
      return normalized;
    }
  }
  // 目标不存在：realpath 最近存在的祖先目录，再拼回不存在的尾部段。
  let dir = normalized;
  const tail: string[] = [];
  let prev = "";
  // dirname 不再收缩（到 POSIX 根 / 或 Windows 盘符根）即停；prev 守卫平台无关，比硬编码盘符正则稳。
  while (dir && dir !== prev && !existsSync(dir)) {
    prev = dir;
    tail.unshift(basename(dir));
    dir = dirname(dir);
  }
  try {
    const real = realpathSync(dir);
    return tail.length ? join(real, ...tail) : real;
  } catch {
    return normalized;
  }
}
