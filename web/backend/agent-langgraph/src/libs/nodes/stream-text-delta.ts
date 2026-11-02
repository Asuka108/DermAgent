/**
 * 流式文本 chunk 折叠 —— 兼容「增量 delta」与「累积全文」两种 provider 语义。
 *
 * 部分 OpenAI 兼容代理在 stream 里返回前缀续写的全文（"你"→"你好"→"你好世界"），
 * 若按增量直接拼接并 emit，UI 会出现叠词。本 helper 统一抽出新增后缀，供
 * streamLLMText、LangGraph messages 模式、task 子 agent 等路径复用。
 */

/** foldStreamTextChunk 的返回值：更新后的全文与本次应 emit 的增量（无新内容则为 null）。 */
export interface StreamTextFoldResult {
  full: string;
  delta: string | null;
}

/**
 * 将单个 stream chunk 折叠进已有全文，返回更新后的 full 与应向下游 emit 的 delta。
 *
 * - 累积模式：chunk 以当前 full 为前缀 → full 替换为 chunk，delta = 后缀
 * - 增量模式：chunk 非前缀续写 → full += chunk，delta = chunk 本身
 * - 空 chunk 或与前缀完全相同 → delta = null（跳过 emit）
 */
export function foldStreamTextChunk(full: string, chunk: string): StreamTextFoldResult {
  if (!chunk) return { full, delta: null };
  if (chunk.startsWith(full)) {
    const delta = chunk.slice(full.length);
    if (!delta) return { full, delta: null };
    return { full: chunk, delta };
  }
  return { full: full + chunk, delta: chunk };
}
