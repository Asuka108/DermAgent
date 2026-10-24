/** 日志用短预览，避免整段 prompt 刷屏。 */
export function previewText(text: string | undefined, max = 120): string | undefined {
  if (!text) return undefined;
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  if (trimmed.length <= max) return trimmed;
  return `${trimmed.slice(0, max)}…[+${trimmed.length - max} chars]`;
}
