/**
 * 第一层：高精度规则（intent_core.py 的同构移植，逐条对齐）。
 *
 * 只处理「短、明确、无否定、单目标」的固定表达；命中即终局。
 * 长句 / 否定 / 引号内文本 / 多目标任务一律返回 null，交给第三层 LLM 路由。
 * 与原 Python 版的差异只有语言：关键词表、优先级顺序、四道守卫完全一致。
 */

export interface IntentState {
  phase?: string;
  workflow_state?: string;
  has_draft?: boolean;
  draft?: unknown;
  task_type?: string;
  case_empty?: boolean;
  last_assistant?: string;
  /** 已填字段「标签」（L3 上下文；不给值）。 */
  filled_fields?: string[];
  /** 最近两条用户消息（L3 上下文，调用方截断后传入）。 */
  recent_user?: string[];
}

export interface RuleHit {
  intent: string;
  confidence: number;
  layer: 1;
  reason: string;
  slots: Record<string, string>;
  candidates: [];
}

type PatternTable = ReadonlyArray<readonly [string, readonly string[]]>;

/** 业务任务关键词（顺序即优先级，与原 app.py TASK_PATTERNS 完全一致）。 */
export const TASK_PATTERNS: PatternTable = [
  ["settings_control", ["修改设置", "更改设置", "改一下设置", "设置修改", "切换连接", "激活连接",
    "换连接", "换线路", "切换模型", "换模型", "模型改成", "模型设为", "模型换成",
    "思考改成", "思考调", "思考等级",
    "语言改成", "语言设为", "语言换成", "语言切换", "切换语言", "切换到英文",
    "切换到中文", "改成英文", "改成中文", "设为英文", "设为中文", "界面语言"]],
  ["context_compress", ["压缩上下文", "压缩记忆", "压缩对话", "整理上下文", "上下文压缩"]],
  ["case_edit", ["改成", "改写", "重写", "修改病例", "编辑病例", "补充到病例", "更新病例", "修订",
    "重新整理", "根据修改重新整理"]],
  ["case_compare", ["只对比病例", "单独对比病例", "对比病例", "病例对比", "和历史病例对比", "与历史病例对比", "跟上次比较"]],
  ["image_analyze", ["只分析图片", "单独分析图片", "分析图片", "分析照片", "图片分析", "照片分析", "分析这张图", "看看这张照片"]],
  ["report_export", ["只导出报告", "单独导出报告", "导出报告", "生成报告", "导出病历文件", "导出门诊记录文件"]],
  ["privacy_deidentify", ["只做脱敏", "单独脱敏", "隐私脱敏", "数据脱敏", "病例脱敏"]],
  ["body_region_3d", ["只选身体部位", "单独选部位", "打开人体模型", "打开3D", "打开3d", "人体模型选点", "去选部位"]],
  ["medical_kb_search", ["只查知识库", "单独查知识库", "检索知识库", "搜索知识库", "查医学资料", "查文献"]],
  ["red_flag_screen", ["只查危险信号", "单独查危险信号", "危险信号筛查", "红旗筛查", "筛查红旗"]],
  ["differential", ["只做鉴别诊断", "单独做鉴别诊断", "只做鉴别", "单独鉴别", "鉴别诊断", "鉴别"]],
  ["treatment_ref", ["只做治疗方向", "单独做治疗方向", "只看治疗", "只做治疗参考", "治疗方向", "治疗参考", "治疗建议参考"]],
  ["medication_review", ["只查用药", "单独查用药", "只做用药核对", "单独用药核对", "用药核对", "用药清单", "药物核对", "用药审查"]],
  ["auxiliary", ["只做检查建议", "单独做检查建议", "只查检查", "检查建议", "辅助检查建议"]],
  ["patient_edu", ["只做患者教育", "单独做患者教育", "只做健康教育", "健康教育", "患者能看懂", "患者教育", "通俗解释"]],
  ["history", ["只写现病史", "单独写现病史", "现病史"]],
  ["lesion_desc", ["只写皮损描述", "单独写皮损描述", "皮损描述", "皮损记录", "皮损情况", "皮损表现"]],
  ["referral", ["只写转诊", "单独写转诊", "转诊摘要", "转诊"]],
  ["followup", ["只写随访", "单独写随访", "随访计划", "随访记录", "随访"]],
  ["summary", ["只写病例摘要", "单独写病例摘要", "病例摘要", "病历摘要", "摘要"]],
  ["case_organizer", ["只整理病历", "单独整理病历", "只写病历", "单独写病历", "帮我写病历", "整理一份病历", "病例整理"]],
  ["complete_analysis", ["确认提交", "开始完整分析", "开始诊断辅助", "进行完整分析", "完整病例分析"]],
  ["case_write", ["写一份", "写病例", "写个病例", "写一版", "门诊病历", "病例模板", "病历模板",
    "进入诊断", "开始诊断", "辅助评估", "建立病例", "建立评估病例"]],
];

/** 对话控制指令（顺序敏感；先于业务规则判定）。 */
export const AGENT_COMMANDS: PatternTable = [
  ["skip_3d", ["跳过3D", "跳过3d", "不用3D", "不用3d", "不使用3D", "不使用3d",
    "不用人体模型", "不用模型选点", "先不选部位"]],
  ["add_image", ["补充图片", "添加图片", "补充一张图片", "添加一张图片",
    "补充照片", "添加照片", "上传图片", "上传照片"]],
  ["reask", ["重新询问患者", "重新追问", "重新梳理缺失", "问我还缺什么", "还缺什么信息"]],
  ["undo_last", ["撤销刚才的信息", "撤销刚才补充", "撤销上一条信息", "撤销刚才那条"]],
  ["save_case", ["保存当前病例", "保存病例草稿", "保存当前草稿", "把病例保存", "先保存病例"]],
  ["back_to_chat", ["返回对话", "回到对话", "返回聊天", "回到聊天", "回到主对话", "回到对话框"]],
  ["show_sources", ["查看刚才的引用", "查看引用", "刚才的引用", "引用详情", "看引用", "查看文献引用"]],
  ["open_model", ["打开人体模型", "打开3D", "打开3d", "打开三维模型", "打开模型选点",
    "人体模型选点", "去选部位", "打开人体图"]],
  ["open_analysis", ["打开分析结果", "查看分析结果", "查看完整分析", "打开结果窗口"]],
];

export const GREET_EXACT: ReadonlySet<string> = new Set([
  "hi", "hello", "hey", "你好", "您好", "在吗", "在么", "嗨", "hello？", "hi？",
]);

export const SMALLTALK_KEYS: readonly string[] = [
  "你是谁", "你能做什么", "你能干什么", "你叫什么", "介绍你能", "介绍下你",
  "你会做什么", "你会什么", "你有什么功能", "有什么功能", "你能帮我做什么",
  "怎么使用", "这是什么软件", "你负责什么",
];

export const CORE_CONFIRMS: ReadonlySet<string> = new Set([
  "确认", "确认执行", "同意", "没问题", "可以了", "开始吧", "执行吧",
  "就这样", "好的", "可以开始", "继续执行", "ok", "go",
]);

export const DENY_KEYS: ReadonlySet<string> = new Set([
  "不对", "不是", "重来", "重新来", "拒绝", "不要这个", "这个不对",
  "不是这个意思", "取消", "算了", "先不要", "停", "停止", "退出",
  "不用了", "不做了", "重新生成", "否",
]);

// 「不需要分析图片」「不必做鉴别诊断」这类「否定词 + 情态/动词」结构必须判为否定；
// 可选连接组只吸收到关键词之间的虚词、不跨标点。无法判定时宁可下放（LLM 路由），
// 绝不执行被否定的动作。与 Python `_NEGATION_RE` 逐字对齐。
const NEGATION_RE =
  /(?:不|别|无需|无须|不必|不用|不要|不想|不打算|没必要|没有必要|没|没有|未|先不|暂不|暂时不)(?:需要|需|要|用|必|想|打算|再|做|写|生成|进行|帮我|给我|去|来){0,2}\s*$/;
const QUOTE_PAIRS: ReadonlyArray<readonly [string, string]> = [
  ["「", "」"], ["『", "』"], ["“", "”"], ["‘", "’"], ['"', '"'], ["'", "'"],
];
const CONJUNCTION_RE = /(?:然后|再|顺便|同时|并且|以及|，并|。并)/;
// 多目标：并列连词 +（≥2 个不同任务关键词 或 ≥2 个动作动词）。
const ACTION_VERB_RE =
  /(?:分析|整理|导出|生成|写|查|检索|做|看看|看一下|评估|总结|描述|推荐|建议|对比|比较|打开|保存|撤销|切换|记录|列出|给出|压缩|脱敏)/g;
const SEQ_VERB_RE =
  /^(?:请|帮我|麻烦|给我|我要|我想|需要)?\s*(?:把|写|整理|改成|改写|重写|导出|生成|查|检索|分析|对比|做|打开|保存|撤销|压缩|切换|记录|列出|描述|总结|给出|推荐|建议)/;

/** NFKC + 去首尾空白 + 小写 + 去尾部标点（与 Python normalize 一致）。 */
export function normalize(text: string): string {
  return String(text ?? "")
    .normalize("NFKC")
    .trim()
    .toLowerCase()
    .replace(/[。，！？!?~～…、；;：: ]+$/u, "");
}

export function isConfirm(text: string): boolean {
  return CORE_CONFIRMS.has(normalize(text));
}

export function isSmalltalk(text: string): boolean {
  const t = normalize(text);
  if (!t) return false;
  if (GREET_EXACT.has(t)) return true;
  return SMALLTALK_KEYS.some((k) => t.includes(k));
}

function quotedSpans(text: string): Array<[number, number]> {
  const spans: Array<[number, number]> = [];
  for (const [left, right] of QUOTE_PAIRS) {
    let start = 0;
    for (;;) {
      const i = text.indexOf(left, start);
      if (i < 0) break;
      const j = text.indexOf(right, i + left.length);
      if (j < 0) break;
      spans.push([i, j + right.length]);
      start = j + right.length;
    }
  }
  return spans;
}

function isNegated(text: string, idx: number): boolean {
  return NEGATION_RE.test(text.slice(Math.max(0, idx - 6), idx));
}

function isQuoted(idx: number, spans: Array<[number, number]>): boolean {
  return spans.some(([a, b]) => a <= idx && idx < b);
}

function keywordOk(text: string, key: string, spans: Array<[number, number]>): boolean {
  let start = 0;
  for (;;) {
    const idx = text.indexOf(key, start);
    if (idx < 0) return false;
    if (!isNegated(text, idx) && !isQuoted(idx, spans)) return true;
    start = idx + key.length;
  }
}

function collectMatches(text: string): {
  matches: Array<[string, string]>;
  spans: Array<[number, number]>;
} {
  const spans = quotedSpans(text);
  const matches: Array<[string, string]> = [];
  for (const [intent, keys] of TASK_PATTERNS) {
    const hit = keys.find((k) => keywordOk(text, k.toLowerCase(), spans));
    if (hit) matches.push([intent, hit]);
  }
  return { matches, spans };
}

/**
 * 控制指令：只认「未被否定、未被引号包裹」的明确短语（≤ 60 字）。
 * 「不要跳过3D」「别打开人体模型」是对控制动作的否定，不能按包含匹配执行成相反动作。
 */
export function classifyControl(text: string): string {
  const t = normalize(text);
  if (!t || t.length > 60) return "";
  const spans = quotedSpans(t);
  for (const [cmd, keys] of AGENT_COMMANDS) {
    if (keys.some((k) => keywordOk(t, k.toLowerCase(), spans))) return cmd;
  }
  return "";
}

/**
 * 关键词全部被否定 / 引号包裹的意图（intent_core.guarded_intents 的同构实现）。
 * 第一层漏判后 LLM 仍可能把被否定的动作路由回来（如「不要跳过3D」），
 * 调用方据此拒绝直接执行。
 */
export function guardedIntents(text: string): Set<string> {
  const t = normalize(text);
  const out = new Set<string>();
  if (!t) return out;
  const spans = quotedSpans(t);
  for (const [intent, keys] of [...TASK_PATTERNS, ...AGENT_COMMANDS]) {
    const lowered = keys.map((k) => k.toLowerCase());
    if (lowered.some((k) => t.includes(k)) && !lowered.some((k) => keywordOk(t, k, spans))) {
      out.add(intent);
    }
  }
  return out;
}

/** 显式并列（然后/再/同时/顺便…）且包含多个目标或动作。 */
export function isMultiTarget(text: string): boolean {
  const t = normalize(text);
  if (!t || !CONJUNCTION_RE.test(t)) return false;
  const { matches } = collectMatches(t);
  const distinct = new Set(matches.map(([i]) => i).filter((i) => i !== "case_write"));
  if (distinct.size >= 2) return true;
  return (t.match(ACTION_VERB_RE) ?? []).length >= 2;
}

/**
 * 第一层判定：返回 RuleHit 或 null。
 * 高精度优先——任何不确定都返回 null，由上层（LLM 路由）接管。
 */
export function classifyRules(text: string, state: IntentState = {}): RuleHit | null {
  const t = normalize(text);
  if (!t) return null;

  const cmd = classifyControl(t);
  if (cmd) {
    return { intent: cmd, confidence: 0.99, layer: 1, reason: `control:${cmd}`, slots: {}, candidates: [] };
  }

  const wf = String(state.workflow_state ?? "");
  const hasDraft = Boolean(state.has_draft ?? state.draft);
  if (
    isConfirm(t) &&
    (["planning", "awaiting_review", "awaiting_follow_up"].includes(wf) ||
      hasDraft ||
      state.phase === "planning")
  ) {
    return { intent: "confirm", confidence: 0.99, layer: 1, reason: "confirm", slots: {}, candidates: [] };
  }
  if (DENY_KEYS.has(t) && (["planning", "awaiting_review"].includes(wf) || hasDraft)) {
    return { intent: "deny", confidence: 0.99, layer: 1, reason: "deny", slots: {}, candidates: [] };
  }

  if (isSmalltalk(t)) {
    return { intent: "smalltalk", confidence: 0.99, layer: 1, reason: "smalltalk", slots: {}, candidates: [] };
  }

  const m = /^对比\s*([0-9a-f]{6,12})$/.exec(t);
  if (m) {
    return {
      intent: "case_compare",
      confidence: 0.99,
      layer: 1,
      reason: "case_id",
      slots: { case_id: m[1]! },
      candidates: [],
    };
  }

  const { matches } = collectMatches(t);
  if (matches.length === 0) return null;

  // 长度守卫：长句只有在「句首动词短语」时才允许 L1 终局
  if (t.length > 40 && !SEQ_VERB_RE.test(t)) return null;
  // 多目标守卫：显式并列且包含多个任务/动作 → 交第三层
  if (isMultiTarget(t)) return null;

  const [intent, key] = matches[0]!;
  return { intent, confidence: 0.99, layer: 1, reason: `rule:${key}`, slots: {}, candidates: [] };
}
