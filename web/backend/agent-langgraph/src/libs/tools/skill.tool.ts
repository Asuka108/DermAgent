/**
 * load_skill 工具 —— 渐进式读取某个 skill 的完整说明（SKILL.md 正文）或 references 子文件。
 *
 * 配合 renderSkillsSection：系统提示词只列 skill 的 name + description（省 token），
 * 模型需要时调 load_skill(name) 拉 SKILL.md，或 load_skill(name, part) 拉 references 单文件。
 */

import { tool } from "@langchain/core/tools";
import { z } from "zod";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import type { DiscoveredSkill } from "../../runtime/index.js";

/** 在正文前注入 skill 根目录与 scripts 路径元信息。 */
function renderSkillMetaHeader(skill: DiscoveredSkill, loadedPath: string): string {
  const skillRoot = dirname(skill.path);
  const scriptsDir = join(skillRoot, "scripts");
  const scriptsNote = existsSync(scriptsDir)
    ? `- **scriptsDir**: ${scriptsDir}`
    : `- **scriptsDir**: ${scriptsDir}（目录不存在，脚本可能位于 skillRoot 其他子路径）`;

  return `## Skill 元信息
- **skillRoot**: ${skillRoot}
- **loadedFrom**: ${loadedPath}
${scriptsNote}

> 执行本 skill 的 \`./scripts/*.sh\` 前，先 \`cd\` 到 skillRoot，或使用 scriptsDir 下的绝对路径。禁止 \`find /\` 全盘搜索。

`;
}

/**
 * 在 skillRoot/references/ 下解析 part 参数对应的 markdown 文件。
 * part 可为完整文件名（含或不含 .md）或前缀（如 part2 → part2-xxx.md）。
 */
export function resolveSkillReferencePath(skillRoot: string, part: string): string | null {
  const trimmed = part.trim();
  if (!trimmed) return null;

  const refsDir = join(skillRoot, "references");
  if (!existsSync(refsDir)) return null;

  const withMd = trimmed.endsWith(".md") ? trimmed : `${trimmed}.md`;
  const direct = join(refsDir, withMd);
  if (existsSync(direct)) return direct;

  const baseName = withMd.replace(/\.md$/, "");
  const entries = readdirSync(refsDir).filter((f) => f.endsWith(".md"));
  const prefixMatches = entries.filter(
    (f) => f === withMd || f.startsWith(`${baseName}-`) || f.startsWith(`${baseName}.`)
  );
  if (prefixMatches.length === 1) return join(refsDir, prefixMatches[0]!);
  if (prefixMatches.length > 1) {
    const exact = prefixMatches.find((f) => f === withMd || f.replace(/\.md$/, "") === baseName);
    if (exact) return join(refsDir, exact);
    return join(refsDir, prefixMatches.sort()[0]!);
  }

  return null;
}

export function createSkillTool(skills: DiscoveredSkill[]) {
  const byName = new Map(skills.map((s) => [s.name, s]));
  const names = skills.map((s) => s.name).join(", ") || "(none)";

  return tool(
    async ({ name, part }) => {
      const skill = byName.get(name);
      if (!skill) return `Error: 未找到 skill "${name}"。可用: ${names}`;

      const skillRoot = dirname(skill.path);

      if (part?.trim()) {
        const refPath = resolveSkillReferencePath(skillRoot, part);
        if (!refPath) {
          const refsDir = join(skillRoot, "references");
          const hint = existsSync(refsDir)
            ? `references 下可用: ${readdirSync(refsDir).filter((f) => f.endsWith(".md")).join(", ") || "(空)"}`
            : "该 skill 无 references/ 目录";
          return `Error: 未找到 reference "${part}"（skill: ${name}）。${hint}`;
        }
        try {
          const body = readFileSync(refPath, "utf-8");
          return renderSkillMetaHeader(skill, refPath) + body;
        } catch (err) {
          return `Error: 读取 reference "${part}" 失败: ${err instanceof Error ? err.message : String(err)}`;
        }
      }

      try {
        const body = readFileSync(skill.path, "utf-8");
        return renderSkillMetaHeader(skill, skill.path) + body;
      } catch (err) {
        return `Error: 读取 skill "${name}" 失败: ${err instanceof Error ? err.message : String(err)}`;
      }
    },
    {
      name: "load_skill",
      description: `读取 skill 说明：默认 SKILL.md 正文；可选 part 加载 references/ 子文件（按文件名或前缀，如 part1、part2）。可用 skills: ${names}。`,
      schema: z.object({
        name: z.string().describe("skill 名（见系统提示词 Available Skills 列表）"),
        part: z
          .string()
          .optional()
          .describe("可选：references/ 下文件名或前缀（如 part2-xxx、part1）"),
      }),
    }
  );
}
