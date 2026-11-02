# -*- coding: utf-8 -*-
"""三层意图识别编排（倒金字塔）。

第一层：shared/intent_core.py 的高精度规则——命中即终局。
第二层：BGE 句向量（复用知识库已装模型）+ 对话状态先验——高置信直接执行。
第三层：当前激活的主 LLM 强制工具调用输出结构化路由——仍不确定则给医生候选卡。

设计边界：本模块只决定「走哪条管线」，绝不生成医学内容、绝不抽取病例字段；
任何一层失败都按兜底矩阵降级，不允许「不确定却直接执行」。
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

import intent_core as core
try:                                  # 以脚本方式运行（server/ 在 sys.path）
    import rag_embed
    from agent_loop import _http_context
except ImportError:                   # 以 server.intent 包方式导入（tests-server）
    from . import rag_embed  # type: ignore[no-redef]
    from .agent_loop import _http_context  # type: ignore[no-redef]

HERE = os.path.dirname(os.path.abspath(__file__))
_PROTO_DIR = os.path.join(HERE, "data", "intent")
_L3_TTL = 600.0
_L3_CACHE = {}          # key → (expire_at, result)
_PROTO_STATE = {"short": "", "items": None, "matrix": None}


# —— 第二层：原型向量 ——

def _proto_items() -> list:
    items = []
    for intent, meta in (core.spec().get("intents") or {}).items():
        for proto in meta.get("prototypes") or []:
            items.append((intent, str(proto)))
    return items


def _proto_path(short: str) -> str:
    return os.path.join(_PROTO_DIR, f"proto_{short}.npz")


def _proto_key(short: str, text: str) -> str:
    return hashlib.sha1(f"{short}|{text}".encode("utf-8")).hexdigest()


def _load_proto_cache(short: str) -> dict:
    try:
        import numpy as np
        with np.load(_proto_path(short), allow_pickle=False) as z:
            return {str(k): z["vecs"][i] for i, k in enumerate([str(x) for x in z["keys"]])}
    except Exception:  # noqa: BLE001 — 缺失/损坏 → 空缓存重建
        return {}


def _save_proto_cache(short: str, cache: dict) -> None:
    try:
        import numpy as np
        os.makedirs(_PROTO_DIR, exist_ok=True)
        keys = list(cache)
        np.savez_compressed(_proto_path(short),
                            keys=np.array(keys),
                            vecs=np.stack([cache[k] for k in keys]))
    except Exception:  # noqa: BLE001 — 缓存写失败不影响功能
        pass


def _proto_matrix():
    """返回 (items, matrix)；模型未就绪 → (None, None)。"""
    if not rag_embed.is_ready():
        return None, None
    short = rag_embed.model_short()
    if _PROTO_STATE["short"] == short and _PROTO_STATE["matrix"] is not None:
        return _PROTO_STATE["items"], _PROTO_STATE["matrix"]
    try:
        import numpy as np
    except ImportError:
        return None, None
    items = _proto_items()
    keys = [_proto_key(short, t) for _, t in items]
    cache = _load_proto_cache(short)
    missing = [i for i, k in enumerate(keys) if k not in cache]
    if missing:
        vecs = rag_embed.embed_texts([items[i][1] for i in missing], timeout_ms=20000)
        if not vecs:
            return None, None
        for i, vec in zip(missing, vecs):
            cache[keys[i]] = np.asarray(vec, dtype=np.float32)
        _save_proto_cache(short, cache)
    _PROTO_STATE.update({"short": short, "items": items,
                         "matrix": np.stack([cache[k] for k in keys])})
    return items, _PROTO_STATE["matrix"]


def rank_intents(text: str, state: dict | None = None, timeout_ms: int = 300):
    """原型向量相似度 + 状态先验 → [(intent, score)] 降序；模型未就绪/超预算 → None。

    校准脚本用更大的 timeout 复用同一打分路径，避免训练/评测口径漂移。
    """
    items, matrix = _proto_matrix()
    if items is None:
        return None
    q = rag_embed.embed_texts([text], timeout_ms=timeout_ms)
    if not q:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    qv = np.asarray(q[0], dtype=np.float32)
    sims = matrix @ qv
    scores = {}
    for (intent, _), sim in zip(items, sims):
        val = float(sim)
        if val > scores.get(intent, -1.0):
            scores[intent] = val
    prior = core.apply_state_prior(text, state or {}, scores)
    return sorted(prior.items(), key=lambda kv: (-kv[1], kv[0]))


def layer2(text: str, state: dict | None = None) -> dict | None:
    """句向量打分 + 状态先验。模型未就绪/编码超预算 → None（上层跳过第二层）。

    向量相似度不理解否定与并列：`不需要分析图片` 与 `分析图片` 的句向量几乎相同。
    因此以下三类输入即使分数很高也不得在第二层直接执行，只能作为候选交给第三层/反问：
    - 控制类意图（控制必须由医生明确说出固定短语，规则层未命中就不能猜）；
    - 关键词全部被否定/引号包裹的意图（guarded_intents）；
    - 显式并列的多目标请求（is_multi_target）。
    """
    ranked = rank_intents(text, state)
    if not ranked:
        return None
    top, top_s = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    th = core.thresholds()
    per_model = (th.get("per_model") or {}).get(rag_embed.model_short()) or {}
    key = "layer2_execute_medical" if core.intent_class(top) == "medical" else "layer2_execute_default"
    threshold = float(per_model.get(key, th.get(key, 0.8)))
    margin = float(per_model.get("layer2_margin", th.get("layer2_margin", 0.05)))
    guarded = (core.intent_class(top) == "control"
               or top in core.guarded_intents(text)
               or core.is_multi_target(text))
    return {"intent": top, "confidence": round(top_s, 4), "margin": round(top_s - second, 4),
            "threshold": threshold, "candidates": [[i, round(s, 4)] for i, s in ranked[:4]],
            "execute": (not guarded) and top_s >= threshold and (top_s - second) >= margin}


# —— 第三层：LLM 结构化路由 ——

_ROUTE_TOOL = {
    "type": "function",
    "function": {
        "name": "route_intent",
        "description": "只做意图路由，不回答医学问题、不生成医学内容",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": list(core.ALLOWED_INTENTS)},
                "confidence": {"type": "number"},
                "slots": {"type": "object", "properties": {
                    "case_id": {"type": "string"},
                    "language": {"type": "string", "enum": ["zh", "en"]},
                    "model_hint": {"type": "string"},
                }},
                "ambiguity": {"type": "boolean"},
                "alternatives": {"type": "array", "items": {"type": "object", "properties": {
                    "intent": {"type": "string"}, "reason": {"type": "string"}}}},
                "reason": {"type": "string"},
            },
            "required": ["intent", "confidence", "ambiguity", "reason"],
        },
    },
}

_ROUTE_SYSTEM = "\n".join([
    "你是皮肤科医生工作站的意图路由器。只输出 route_intent 工具调用，不回答医学问题、",
    "不生成诊断或治疗方案、不抽取病例字段。",
    "规则：",
    "1) 医生在描述患者症状/病史/皮损，或要求新建病历 → case_write。",
    "2) 医生在发出明确任务指令 → 对应业务意图（鉴别/治疗/用药/检查/患者教育/文书整理/导出等）。",
    "3) 医生在说界面操作（跳过3D/保存/撤销/打开面板/改设置）→ 对应 control 意图。",
    "4) 拿不准就 ambiguity=true，并在 alternatives 给 2-4 个最可能意图。",
    "5) confidence 是 0-1 的实数，表示你对该判断的把握。",
])

_CONTROL_SLOTS = {"case_compare": {"case_id"}, "settings_control": {"language", "model_hint"}}


def _l3_context(state: dict, cands) -> dict:
    fields = []
    rec = state.get("record") if isinstance(state.get("record"), dict) else {}
    for k in (rec.get("fields") or {}):
        if str((rec.get("fields") or {}).get(k) or "").strip():
            fields.append(k)
    return {"phase": state.get("phase") or state.get("workflow_state") or "",
            "prev_task": state.get("task_type") or "",
            "has_draft": bool(state.get("has_draft") or state.get("draft")),
            "has_doc": bool(state.get("doc")),
            "filled_fields": fields[:12],
            "recent": (state.get("recent_user") or [])[-2:],
            "layer2": [{"intent": i, "score": s} for i, s in (cands or [])]}


def _post(base_url, api_key, payload, timeout=20):
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=_http_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _sanitize_route(args: dict) -> dict | None:
    """校验 LLM 输出：意图必须在枚举内；槽位按意图白名单过滤；置信度截断。"""
    if not isinstance(args, dict):
        return None
    intent = str(args.get("intent") or "").strip()
    if intent not in core.ALLOWED_INTENTS:
        return None
    try:
        conf = float(args.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = min(1.0, max(0.0, conf))
    allowed = _CONTROL_SLOTS.get(intent, set())
    slots = {}
    for k, v in (args.get("slots") or {}).items():
        if k in allowed and str(v or "").strip():
            slots[k] = str(v).strip()[:80]
    alts, seen = [], {intent}
    for a in args.get("alternatives") or []:
        al = str((a or {}).get("intent") or "").strip()
        if al in core.ALLOWED_INTENTS and al not in seen:
            seen.add(al)
            alts.append({"intent": al, "reason": str((a or {}).get("reason") or "")[:120]})
        if len(alts) >= 4:
            break
    return {"intent": intent, "confidence": round(conf, 4),
            "ambiguity": bool(args.get("ambiguity")), "slots": slots,
            "alternatives": alts, "reason": str(args.get("reason") or "")[:200]}


def layer3(text: str, state: dict | None, cands, llm=None) -> dict | None:
    """强制工具调用的结构化路由。LLM 不可用/输出非法 → None。"""
    if not llm:
        return None
    base_url, api_key, model, effort = llm
    if not (base_url and model):
        return None
    state = state or {}
    key = hashlib.sha1(json.dumps([text, state.get("phase"), state.get("task_type"),
                                   bool(state.get("has_draft") or state.get("draft")),
                                   cands], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    now = time.time()
    hit = _L3_CACHE.get(key)
    if hit and hit[0] > now:
        return dict(hit[1])
    ctx = _l3_context(state, cands)
    messages = [
        {"role": "system", "content": _ROUTE_SYSTEM},
        {"role": "user", "content": "对话状态：" + json.dumps(ctx, ensure_ascii=False)[:1500]
                                    + "\n医生这句话：" + str(text)[:300]},
    ]
    payload = {"model": model, "temperature": 0, "stream": False, "messages": messages,
               "tools": [_ROUTE_TOOL],
               "tool_choice": {"type": "function", "function": {"name": "route_intent"}}}
    if effort:
        payload["reasoning_effort"] = effort
    data = None
    for attempt in range(2):
        try:
            data = _post(base_url, api_key, payload)
            break
        except (urllib.error.HTTPError, ConnectionError, TimeoutError, OSError, ValueError):
            if attempt == 1:
                return None
    try:
        msg = ((data or {}).get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        args = json.loads((calls[0] or {}).get("function", {}).get("arguments") or "{}")
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        return None
    out = _sanitize_route(args)
    if out:
        out["source"] = "llm"
        _L3_CACHE[key] = (now + _L3_TTL, dict(out))
        if len(_L3_CACHE) > 512:
            for k in sorted(_L3_CACHE, key=lambda k: _L3_CACHE[k][0])[:128]:
                _L3_CACHE.pop(k, None)
    return out


# —— 编排 ——

def _result(intent, layer, confidence, reason, source, *, slots=None, candidates=None,
            margin=0.0, degraded=False, needs_confirm=False, latency_ms=0):
    return {"intent": intent, "layer": layer, "confidence": round(float(confidence), 4),
            "margin": round(float(margin), 4), "reason": reason, "source": source,
            "slots": slots or {}, "candidates": candidates or [],
            "degraded": bool(degraded), "needs_confirm": bool(needs_confirm),
            "latency_ms": int(latency_ms)}


def route(text: str, state: dict | None = None, llm=None) -> dict:
    """倒金字塔路由：L1 → L2 → L3 → 确认卡 / 默认。返回值即 intent_meta。"""
    t0 = time.perf_counter()
    state = state or {}

    hit = core.classify_rules(text, state)
    if hit:
        return _result(hit["intent"], 1, hit["confidence"], hit["reason"], "rule",
                       slots=hit.get("slots"), latency_ms=(time.perf_counter() - t0) * 1000)

    l2 = layer2(text, state)
    cands = (l2 or {}).get("candidates") or []
    if l2 and l2.get("execute"):
        return _result(l2["intent"], 2, l2["confidence"], "vector", "vector",
                       candidates=cands, margin=l2["margin"],
                       latency_ms=(time.perf_counter() - t0) * 1000)

    l3 = layer3(text, state, cands, llm)
    if l3:
        th = core.thresholds()
        # 与第二层同口径的防线：LLM 也可能把被否定的动作路由回来（如「不要跳过3D」）。
        execute = ((not l3["ambiguity"])
                   and l3["confidence"] >= float(th.get("layer3_execute", 0.75))
                   and l3["intent"] not in core.guarded_intents(text))
        if execute:
            merged = [[c["intent"], 0.0] for c in l3["alternatives"]] or cands
            return _result(l3["intent"], 3, l3["confidence"], f"llm:{l3['reason']}", "llm",
                           slots=l3["slots"], candidates=merged,
                           latency_ms=(time.perf_counter() - t0) * 1000)
        opts = [[l3["intent"], l3["confidence"]]] + [[a["intent"], 0.0] for a in l3["alternatives"]]
        return _result(None, 3, l3["confidence"], "llm:ambiguous", "llm", slots=l3["slots"],
                       candidates=opts, needs_confirm=True,
                       latency_ms=(time.perf_counter() - t0) * 1000)

    if cands:
        return _result(None, 2, cands[0][1], "vector:ambiguous", "vector", candidates=cands,
                       needs_confirm=True, latency_ms=(time.perf_counter() - t0) * 1000)
    # 无候选：保留旧默认语义，但如实标注降级（离线首次描述病例不能卡死）
    return _result("case_write", 0, 0.0, "default", "default", degraded=True,
                   latency_ms=(time.perf_counter() - t0) * 1000)


def candidates_for_card(meta: dict) -> list:
    """把 intent_meta.candidates 转成确认卡候选（带标签）。

    confirm/deny 不是可执行任务（它们依赖「已有一张待确认的计划」），放进候选卡
    会被 /intent/choose 当成业务意图执行，因此不作为选项。
    """
    out = []
    for item in (meta or {}).get("candidates") or []:
        intent = str(item[0] if isinstance(item, (list, tuple)) else item)
        score = float(item[1]) if isinstance(item, (list, tuple)) and len(item) > 1 else 0.0
        if (intent in core.ALLOWED_INTENTS and intent not in ("confirm", "deny")
                and all(o["intent"] != intent for o in out)):
            out.append({"intent": intent, "label": core.intent_label(intent),
                        "confidence": score, "reason": ""})
    return out


def warmup() -> bool:
    """服务启动后后台预热原型向量（仅 BGE 已就绪时）。"""
    try:
        items, matrix = _proto_matrix()
        return items is not None
    except Exception:  # noqa: BLE001 — 预热失败不影响功能
        return False
