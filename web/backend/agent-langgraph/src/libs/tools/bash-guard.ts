/**
 * bash 命令预检 —— 拦截易导致长时间挂起的全盘文件扫描。
 *
 * 模型在 cwd 找不到脚本时可能执行 `find /`；在 macOS 上极慢且易绕过 shell 级超时。
 * 找文件应使用 glob/grep 内置工具，或在 workspace 内带 -maxdepth 的 find。
 */

/** 命中时返回错误文案；合法命令返回 null。 */
export function validateBashCommand(command: string): string | null {
  const normalized = command.replace(/\s+/g, " ").trim();

  // find / 或 find /* —— 从根目录全盘扫描（无 -maxdepth 限制）
  if (/\bfind\s+\/(?:\s|\*|$)/.test(normalized)) {
    return (
      "Error: 禁止全盘文件扫描（find /）。请用 glob 工具（**/*.sh）或在 workspace 内 " +
      "find <dir> -maxdepth N。"
    );
  }

  // locate 无路径参数时扫描全索引库
  if (/\blocate\b/.test(normalized) && !/\blocate\s+-[a-z]*d\b/.test(normalized)) {
    return (
      "Error: 禁止无范围限制的 locate 全盘搜索。请用 glob 工具或在 workspace 内搜索。"
    );
  }

  // mdfind 无 -onlyin 时默认搜索整台机器（macOS Spotlight）
  if (/\bmdfind\b/.test(normalized) && !/\b-onlyin\b/.test(normalized)) {
    return (
      "Error: 禁止无 -onlyin 范围的 mdfind 全盘搜索。请用 glob 工具或在 workspace 内搜索。"
    );
  }

  return null;
}
