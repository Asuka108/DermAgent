# -*- coding: utf-8 -*-
"""v71 Agent 工具循环：LLM 自主决策调用工具，推进「开始诊断 → 病历填写 → 情况补充 →
医生确认提交 → LLM 辅助诊断」全流程（AI转医疗Agent执行计划）。

边界（医生确认的设计，v71 需求确认）：
- LLM 决定「何时调用哪个工具」；工具内部保留既有纪律——字段白名单、医生已确认字段
  不可覆盖、红旗/引用/免责声明、阶段推进校验（「LLM 决定时机，规则守住底线」）；
- 生成 AI 辅助诊断必须在医生确认提交之后（服务端强制校验，不依赖提示词自觉）；
- LLM 未配置 / 端点不支持 function calling / 调用失败 → 返回 None，调用方回退既有
  零 LLM 关键词流程（断网降级不变性；书籍检索全程本地，原文不出本机）；
- 工具调用全程由调用方审计；单回合最多 _MAX_STEPS 轮工具循环，防止失控。

隔离设计：本模块不 import app（避免循环依赖）；app.py 通过 ctx 注入桥接函数——
save_fields / complete_intake / run_screen / kb_search / finalize / record_labels。
"""
import json
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone

_MAX_STEPS = 6                       # 单回合工具循环上限（防失控）
_MAX_TOOL_RESULT_CHARS = 4000        # 工具结果回传给 LLM 的截断长度
_MAX_HISTORY_MSGS = 60               # 注入上下文的最近对话条数
_MAX_HISTORY_CHARS = 200_000         # v73：上下文记忆预算（约 200k 字符，超出丢最旧）
_TOOLS_UNSUPPORTED = False           # 进程级缓存：端点不支持 tools → 直接降级不再报错


class _ToolsUnsupported(Exception):
    """端点不接受 tools/tool_choice 参数（含 reasoning_effort 重试后仍 4xx）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_context() -> ssl.SSLContext:
    """macOS 框架 Python 常缺系统 CA：certifi 可用时用其证书束（与 app.py/rag_embed 同因同解）。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


# —— 工具 schema（OpenAI function calling 格式） ——

TOOLS = [
    {"type": "function", "function": {
        "name": "get_case_status",
        "description": "查看当前病例状态：流程阶段、病历单各字段内容与来源、红旗、是否已有诊断草稿。开始处理医生消息前先调用它了解现状。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "update_record_fields",
        "description": "把医生在对话中口述的病例信息写入病历单字段（主诉/现病史/既往史/个人史/过敏史/用药史/体格检查/皮损描述/辅助检查/身体部位等，字段名以 get_case_status 返回为准）。医生已手工确认或修改过的字段不会被覆盖。每一条口述信息都应落进对应字段。",
        "parameters": {"type": "object",
                       "properties": {"fields": {"type": "object",
                                                 "description": "字段名→内容，如 {\"chief_complaint\": \"右前臂红斑伴瘙痒3天\"}"}},
                       "required": ["fields"]}}},
    {"type": "function", "function": {
        "name": "complete_record_intake",
        "description": "完成「病历填写」阶段，进入「情况补充」阶段。仅当医生明确表示填完了/确认/继续时调用；服务器会校验医生确认，未确认会失败。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "finish_supplement_and_analyze",
        "description": "完成「情况补充」并生成 AI 辅助诊断草稿（内部执行 RAG 检索与规则管线）。仅当医生明确确认提交或要求直接出诊断时调用；服务器会校验。返回缺失项/红旗/草稿要点，之后你在最终回复里用自然语言向医生汇报病状分析、疑似疾病排名、治疗方向与用药推荐（药名/用法教程/参考使用时间，均供医生核对）。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "search_knowledge_base",
        "description": "在本地医学书籍知识库中检索（全本地运行，原文不外发）。用于核对医学事实、为解释提供引用依据。",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"},
                                      "top_k": {"type": "integer", "default": 3}},
                       "required": ["query"]}}},
]


# —— LLM 调用（OpenAI 兼容 /chat/completions + tools） ——

def _post_chat(base_url, api_key, payload):
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 # 浏览器式 UA：网关（Cloudflare）拦截 Python-urllib 默认签名（403/1010，同 app._llm_chat）
                 "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90, context=_http_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404, 422):
            raise _ToolsUnsupported(f"HTTP {exc.code}") from exc
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise ConnectionError(f"HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConnectionError(f"网络不可达：{exc}") from exc


def _chat_tools(base_url, api_key, model, messages, effort):
    """带工具的对话补全。reasoning_effort 不被支持时去掉重试一次；仍 4xx → 端点不支持 tools。"""
    payload = {"model": model, "temperature": 0, "stream": False, "messages": messages,
               "tools": TOOLS, "tool_choice": "auto"}
    if effort:
        payload["reasoning_effort"] = effort
    try:
        data = _post_chat(base_url, api_key, payload)
    except _ToolsUnsupported:
        if not effort:
            raise
        payload.pop("reasoning_effort", None)      # 思考等级参数不被支持 → 去掉重试
        try:
            data = _post_chat(base_url, api_key, payload)
        except _ToolsUnsupported:
            raise
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    if not msg:
        if data.get("error"):
            raise ConnectionError(str(data["error"])[:200])
        raise ConnectionError("空响应")
    return msg


# —— 系统提示词与工具实现 ——

def _case_status(sess: dict, ctx: dict) -> dict:
    rec = sess.get("record") if isinstance(sess.get("record"), dict) else {}
    fields = rec.get("fields") if isinstance(rec.get("fields"), dict) else {}
    meta = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
    labels = ctx.get("record_labels") or {}
    out = {}
    for key, value in fields.items():
        out[key] = {"label": labels.get(key, key), "value": str(value or "").strip()[:160],
                    "source": (meta.get(key) or {}).get("source", "")}
    return {"ok": True, "phase": sess.get("phase"),
            "workflow_state": sess.get("workflow_state"),
            "record_fields": out, "red_flags": sess.get("red_flags") or [],
            "missing": sess.get("missing") or [], "has_draft": bool(sess.get("draft")),
            "record_review_status": rec.get("review_status", "")}


def _system_prompt(sess: dict, ctx: dict) -> str:
    status_txt = json.dumps(_case_status(sess, ctx), ensure_ascii=False)[:2600]
    return "\n".join([
        "你是皮肤科诊疗流程 Agent（肤智 DermAgent），面向医生提供决策支持演示，不构成诊疗建议或处方。",
        "工作方式：先读当前病例状态，自主决定调用哪些工具，根据每个工具结果继续决策，"
        "直到能给出面向医生的最终回复。",
        "当前病例状态 JSON：" + status_txt,
        "流程与工具规则：",
        "1. 医生口述的病例信息（症状/病史/部位/用药等）必须调用 update_record_fields 写入病历单——"
        "这是主要工作方式；字段名以状态里 record_fields 的键为准。尤其是你上一轮追问后医生给出的回答，"
        "必须先合并进对应字段（病程/诱因/症状变化/伴随症状通常合并到 present_illness，"
        "皮损形态合并到 skin_lesions，接触史或暂无专用字段的信息合并到 notes），再继续追问或分析。"
        "已有字段不能只保留旧值：应在不丢旧内容的前提下追加新事实；不要只在聊天回复里复述。",
        "2. 医生明确表示填完了/确认/继续时，调用 complete_record_intake 进入情况补充阶段"
        "（「开始诊断」只是启动工作的指令，不算填写完成确认）。",
        "3. 医生明确确认提交或要求直接出诊断时，调用 finish_supplement_and_analyze 生成 AI 辅助诊断"
        "（服务器校验医生确认，未确认会失败——那就继续向医生说明并请求确认）。",
        "4. 资料不足时不要编造：在最终回复里向医生追问，一次聚焦最关键的 2-4 个问题，"
        "不限轮次，直到你判断理解充分。若病历单为空（流程刚开始），先用一句简短开场"
        "（如「好的，开始诊断，我们先填写病历」）再提问；填写病历本就是第一步，"
        "不要用「无法进入诊断分析」这类否定措辞。",
        "5. 需要医学事实依据时调用 search_knowledge_base（本地知识库）；没有依据就如实说明，禁止编造引用。",
        "6. 医学措辞纪律：鉴别诊断用「待排/可能/建议医生核对」；拿不准就标 unknown；"
        "用药只能给「参考」信息（药名/用法教程/参考使用时长），并注明「供医生核对，不构成处方」；"
        "禁止自动处方、不得确诊。",
        "7. 最终回复永远是给医生看的自然语言简体中文（不是 JSON、不是工具输出转储）。"
        "若本轮生成了诊断草稿，用自然语言汇报：病例摘要、疑似疾病排名（按可能性排序）、"
        "治疗方向、用药推荐（必须精确到具体药品通用名，如「莫匹罗星软膏」「氯雷他定」；"
        "禁止用「外用抗生素」「抗组胺药」等类别词代替药名；每药给出用法教程与参考使用时间/疗程），"
        "风险提示、仍建议补充的信息，并提醒医生在分析结果/草稿面板中审核确认。",
    ])


def _execute_tool(name: str, args: dict, sess: dict, ctx: dict):
    if name == "get_case_status":
        return _case_status(sess, ctx)
    if name == "update_record_fields":
        return ctx["save_fields"](sess, args.get("fields") or {})
    if name == "complete_record_intake":
        if not ctx.get("advance_confirm"):
            return {"ok": False,
                    "error": "医生尚未明确表示填写完成/确认继续；请继续收集资料或在回复中向医生确认"}
        return ctx["complete_intake"](sess)
    if name == "finish_supplement_and_analyze":
        confirmed_record = str((sess.get("record") or {}).get("review_status") or "") == "confirmed"
        if not (ctx.get("diagnose_confirm") or confirmed_record):
            return {"ok": False,
                    "error": "AI 辅助诊断需要医生先确认提交（如「确认提交」「直接出诊断」）；"
                             "请先向医生汇总已收集的信息并请求确认"}
        return ctx["run_screen"](sess)
    if name == "search_knowledge_base":
        q = str(args.get("query") or "").strip()
        if not q:
            return {"ok": False, "error": "query 为空"}
        try:
            top_k = int(args.get("top_k") or 3)
        except (TypeError, ValueError):
            top_k = 3
        return {"ok": True, "citations": ctx["kb_search"](q, top_k)}
    return {"ok": False, "error": f"未知工具：{name}"}


def _tool_summary(fn: str, args: dict, result) -> str:
    """人类可读的单行工具摘要（前端 tool_history 卡片展示）。"""
    if fn == "update_record_fields":
        updated = result.get("updated") or []
        rejected = result.get("rejected") or []
        s = "写入病历字段：" + "、".join(updated) if updated else "未写入任何字段"
        if rejected:
            s += "（医生已确认，未覆盖：" + "、".join(rejected) + "）"
        return s
    if fn == "complete_record_intake":
        return "病历填写完成 → 进入情况补充" if result.get("ok") else "病历完成失败：" + str(result.get("error", ""))[:60]
    if fn == "finish_supplement_and_analyze":
        if not result.get("ok"):
            return "辅助分析未执行：" + str(result.get("error", ""))[:60]
        return (f"RAG 检索 + 辅助分析（缺失 {len(result.get('missing') or [])} 项，"
                f"红旗 {len(result.get('red_flags') or [])} 项，草稿：{'已生成' if result.get('draft_generated') else '未生成'}）")
    if fn == "search_knowledge_base":
        return f"知识库检索「{str(args.get('query') or '')[:40]}」→ {len(result.get('citations') or [])} 条引用"
    if fn == "get_case_status":
        return "查看病例状态"
    return fn


# —— 主循环 ——

def _skip(sess: dict, ctx: dict, reason: str):
    """静默降级也要留痕：经 ctx 注入的 audit 记录跳过原因（保持不 import app 的隔离）。"""
    audit = ctx.get("audit")
    if callable(audit):
        try:
            audit("agent_turn_skip", {"reason": reason})
        except Exception:  # noqa: BLE001
            pass


def run_agent_turn(sess: dict, text: str, ctx: dict):
    """一轮 Agent 对话：LLM + 工具循环，产出与 chat 端点同构的快照。

    返回 None 表示「本次交给调用方走零 LLM 关键词流程」——发生在 LLM 未配置、
    端点不支持 function calling、或首次 LLM 调用即失败且会话未被工具改动时
    （会话一旦被工具改动就不再回退，如实降级收口，避免关键词路径重复并入）。"""
    global _TOOLS_UNSUPPORTED
    if _TOOLS_UNSUPPORTED:
        _skip(sess, ctx, "tools_unsupported_cached")
        return None
    base_url, api_key, model, effort = ctx.get("llm") or ("", "", "", "")
    if not (base_url and model):
        _skip(sess, ctx, "llm_not_configured")
        return None

    msgs = [{"role": "system", "content": _system_prompt(sess, ctx)}]
    hist = [{"role": m["role"], "content": str(m["content"])[:1200]}
            for m in (sess.get("messages") or [])[-_MAX_HISTORY_MSGS:]
            if m.get("role") in ("user", "assistant") and m.get("content")]
    # v73：到 200k 预算自动压缩——较旧一半压成要点（医生指令留原文片段、助手回复留首句），
    # 最近一半原样保留；压缩后仍超预算才逐条丢弃最旧
    if sum(len(x["content"]) for x in hist) > _MAX_HISTORY_CHARS and len(hist) >= 6:
        cut = len(hist) // 2
        old, recent = hist[:cut], hist[cut:]
        bullets = ["医生：" + m["content"][:200] if m["role"] == "user" else "助手：" + m["content"][:80]
                   for m in old]
        summary = "【较早对话压缩摘要（自动生成，非 AI）】\n" + "\n".join(bullets)
        while len(summary) + sum(len(x["content"]) for x in recent) > _MAX_HISTORY_CHARS and len(bullets) > 2:
            bullets.pop(0)
            summary = "【较早对话压缩摘要（自动生成，非 AI）】\n" + "\n".join(bullets)
        hist = [{"role": "assistant", "content": summary}] + recent
    while hist and sum(len(x["content"]) for x in hist) > _MAX_HISTORY_CHARS:
        hist.pop(0)
    msgs.extend(hist)
    msgs.append({"role": "user", "content": text[:2000]})

    tool_trace, mutated, final = [], False, ""
    try:
        for _step in range(_MAX_STEPS):
            try:
                assistant = _chat_tools(base_url, api_key, model, msgs, effort)
            except ConnectionError:
                if _step != 0:
                    raise
                # 首次调用偶发网关慢载超时（实测 90s 冷启动）→ 重试一次，避免医生
                # 第一条口述静默落回关键词流程；此后步骤超时仍如实降级。
                assistant = _chat_tools(base_url, api_key, model, msgs, effort)
            calls = assistant.get("tool_calls") or []
            if not calls:
                final = str(assistant.get("content") or "").strip()
                break
            msgs.append({"role": "assistant", "content": str(assistant.get("content") or ""),
                         "tool_calls": calls})
            for call in calls:
                fn = str((call.get("function") or {}).get("name") or "")
                try:
                    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except ValueError:
                    args = {}
                try:
                    result = _execute_tool(fn, args, sess, ctx)
                    mutated = True
                except Exception as exc:  # noqa: BLE001 — 工具异常如实回传模型，不让回合 500
                    result = {"ok": False, "error": str(exc)[:200]}
                tool_trace.append({"tool": fn, "summary": _tool_summary(fn, args, result),
                                   "ok": bool(result.get("ok", True)), "at": _now()})
                msgs.append({"role": "tool", "tool_call_id": str(call.get("id") or ""),
                             "content": json.dumps(result, ensure_ascii=False)[:_MAX_TOOL_RESULT_CHARS]})
        else:
            final = "本轮工具循环达到上限，已完成的部分都已保存；请继续对话。"
    except _ToolsUnsupported:
        _TOOLS_UNSUPPORTED = True        # 端点不支持 tools → 本次及后续回合全部走关键词流程
        if not mutated:
            _skip(sess, ctx, "tools_unsupported")
            return None
        return ctx["finalize"](
            sess, text, "（当前模型端点不支持工具调用，已回退基础流程。）已保存本轮已完成的部分。",
            tool_trace, model)
    except (ConnectionError, TimeoutError, OSError) as exc:
        if not mutated:
            _skip(sess, ctx, f"connection_error:{str(exc)[:120]}")   # 会话未动 → 降级，关键词流程接手
            return None
        final = f"（Agent 连接中断：{str(exc)[:120]}。已完成的部分已保存，已回退基础流程。）"
    except Exception as exc:  # noqa: BLE001 — 任何意外都不让 chat 500：如实降级
        if not mutated:
            _skip(sess, ctx, f"unexpected_error:{str(exc)[:120]}")
            return None
        final = f"（Agent 内部异常：{str(exc)[:120]}。已完成的部分已保存，已回退基础流程。）"

    if not final:
        if tool_trace:
            final = "已按你的要求处理病例信息；请继续补充或告诉我下一步。"
        else:
            _skip(sess, ctx, "empty_final")  # 模型既无工具调用也无内容 → 交回关键词流程
            return None
    return ctx["finalize"](sess, text, final, tool_trace, model)
