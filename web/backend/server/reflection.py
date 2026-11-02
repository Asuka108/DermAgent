# -*- coding: utf-8 -*-
"""v78 独立核验节点（Reflection）：规则兜底 + LLM 反向校验。

与 ReAct 循环的分工：
- 循环只负责「决策 → 工具 → 观测」；本模块只评估「这轮准备发给医生的回复，
  是否与医生原话、本轮工具原始证据一致」；
- 规则层永远运行，LLM 评审只是追加问题项；评审不可用/超时/输出垃圾都不能
  洗掉规则发现的严重问题；
- 本模块不生成医学内容、不改写工具结果、不做处方建议，只输出问题码与短说明。

隐私边界（与 memory/redact.py 一致）：
- 只接收本轮 query、工具证据截断、待发草稿与病例事实字段；
- 禁止传入压缩摘要、患者档案/情景记忆、完整对话历史、身份字段与密钥；
- 进入评审模型前统一脱敏；问题 detail 入库前再脱敏并截断。
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

try:  # 直接运行 app.py（server 目录在 sys.path）与 tests-server 包导入两种方式
    from memory.redact import has_identity, redact_identity
except ImportError:  # pragma: no cover - 包导入路径
    from server.memory.redact import has_identity, redact_identity

MAX_QUERY_CHARS = 2000
MAX_DRAFT_CHARS = 4000
MAX_EXCERPT_CHARS = 1200
MAX_EVIDENCE_ITEMS = 12
MAX_CRITIC_EVIDENCE = 6
MAX_CRITIC_EXCERPT = 800
MAX_ISSUE_DETAIL = 120

# 中文紧邻引用 ID 时 `\b` 不成立（汉字也是 \w），因此用「前面不是 ID 字符」判断。
CITATION_RE = re.compile(r"(?<![A-Za-z0-9\-])(?:CIT|DOC|KB|RB)-[A-Za-z0-9\-]+")
DOSE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mg|g|ml|mL|片|粒|丸|袋|次/日|次每日)")
REGIMEN_RE = re.compile(r"(每日\s*\d+\s*次|每天\s*\d+\s*次|\d+\s*次/日|疗程|连续\s*\d+\s*[天周月])")
SOURCE_CLAIM_RE = re.compile(r"(根据|依据|参考)\s*(知识库|指南|文献|书籍|教材|研究)")
CERTAIN_DX_RE = re.compile(r"(?<!未)(?<!不能)(?<!无法)(?<!不)(?:确诊为?|诊断为|确定是|肯定是|必定是)")
SAVE_CLAIM_RE = re.compile(r"(已|已经)\s*(保存|更新|写入|记录|完成|提交)")
RAW_DUMP_RE = re.compile(r'(\{"ok"\s*:|"citation_id"\s*:|```json)')
ESCALATION_WORDS = ("红旗", "急诊", "立即", "尽快", "转诊", "就医", "面诊", "危险信号")

WRITE_TOOLS = frozenset({"update_record_fields", "complete_record_intake",
                         "finish_supplement_and_analyze"})
KB_TOOL = "search_knowledge_base"
ANALYZE_TOOL = "finish_supplement_and_analyze"

HIGH_CODES = frozenset({
    "unknown_citation", "citation_without_retrieval", "unsupported_source_claim",
    "write_status_mismatch", "certain_diagnosis", "red_flag_ignored",
    "raw_tool_dump", "identity_leak", "regimen_without_retrieval",
})
CRITIC_CODES = frozenset({
    "evidence_insufficient", "evidence_contradiction", "retrieval_irrelevant",
    "unsupported_inference", "critic_other",
})
KNOWN_CODES = HIGH_CODES | CRITIC_CODES | {"dose_not_in_evidence"}

CODE_LABELS = {
    "unknown_citation": "引用不在白名单",
    "citation_without_retrieval": "有引用但本轮未检索",
    "unsupported_source_claim": "声称有依据但无检索",
    "write_status_mismatch": "写入失败却称已保存",
    "certain_diagnosis": "确定性诊断措辞",
    "red_flag_ignored": "未提示本轮红旗",
    "raw_tool_dump": "回复含工具原始输出",
    "identity_leak": "疑似身份信息",
    "dose_not_in_evidence": "剂量未见于证据",
    "regimen_without_retrieval": "疗程无检索依据",
    "evidence_insufficient": "证据不足以支撑结论",
    "evidence_contradiction": "结论与证据矛盾",
    "retrieval_irrelevant": "检索结果与问题不相关",
    "unsupported_inference": "推断缺少证据",
    "critic_other": "其他一致性问题",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _clip(text, limit: int) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + "…"


def _label(code: str) -> str:
    return CODE_LABELS.get(code, code)


def _labels(codes) -> str:
    return "、".join(_label(c) for c in codes) or "未分类"


def extract_citation_ids(text: str) -> list:
    """按出现顺序去重提取引用 ID。"""
    out = []
    for cid in CITATION_RE.findall(str(text or "")):
        if cid not in out:
            out.append(cid)
    return out


def _json_digest(value) -> str:
    try:
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(value)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _extract_citations(result) -> list:
    if not isinstance(result, dict):
        return []
    found = []

    def _take(items):
        for c in items or []:
            if isinstance(c, dict) and c.get("citation_id"):
                if c not in found:
                    found.append(c)

    _take(result.get("citations"))
    _take(result.get("book_citations"))
    for key in ("draft", "doc"):
        sub = result.get(key)
        if isinstance(sub, dict):
            _take(sub.get("citations"))
            _take(sub.get("book_citations"))
    return found


def evidence_item(tool: str, args: dict, result, ok: bool, step: int) -> dict:
    """把一次工具原始结果收成核验证据；缺引用/查询条件字段时如实留空。"""
    try:
        excerpt = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        excerpt = str(result)
    cites = _extract_citations(result)
    return {
        "tool": str(tool or ""),
        "ok": bool(ok),
        "step": int(step or 0),
        "args_digest": _json_digest(args or {}),
        "excerpt": _clip(redact_identity(excerpt), MAX_EXCERPT_CHARS),
        "citation_ids": [str(c.get("citation_id")) for c in cites],
        "_citations": cites,
    }


def collect_citations(evidence) -> list:
    out = []
    seen = set()
    for item in evidence or []:
        for c in item.get("_citations") or []:
            cid = str(c.get("citation_id") or "")
            if cid and cid not in seen:
                seen.add(cid)
                out.append(c)
    return out


def _public_evidence(evidence) -> list:
    cap = MAX_EVIDENCE_ITEMS
    items = []
    for item in (evidence or [])[-cap:]:
        items.append({"tool": item.get("tool"), "ok": item.get("ok"),
                      "step": item.get("step"), "args_digest": item.get("args_digest"),
                      "excerpt": item.get("excerpt"),
                      "citation_ids": item.get("citation_ids") or []})
    return items


def build_bundle(sess: dict, query: str, evidence: list, draft: str = "",
                 allowed: list | None = None) -> dict:
    """构造核验输入；白名单字段见模块 docstring，绝不夹带记忆/摘要/身份原文。"""
    sess = sess if isinstance(sess, dict) else {}
    evidences = _public_evidence(evidence)
    citations = collect_citations(evidence)
    if allowed:
        for c in allowed:
            if isinstance(c, dict) and c.get("citation_id"):
                citations.append(c)
    allowed_ids = []
    for c in citations:
        cid = str(c.get("citation_id") or "")
        if cid and cid not in allowed_ids:
            allowed_ids.append(cid)

    kb_ok = any(e.get("tool") == KB_TOOL and e.get("ok")
                and e.get("citation_ids") for e in evidences)
    analyzed = any(e.get("tool") == ANALYZE_TOOL and e.get("ok") for e in evidences)
    writes = [e for e in evidences if e.get("tool") in WRITE_TOOLS]
    last_write = writes[-1] if writes else None
    failed_writes = []
    for e in writes:
        if not e.get("ok") and e.get("tool") not in failed_writes:
            failed_writes.append(e.get("tool"))

    raw_draft = str(draft or "")
    facts = {
        "phase": sess.get("phase") or sess.get("workflow_state") or "",
        "workflow_state": sess.get("workflow_state") or "",
        "red_flags": [str(x)[:80] for x in (sess.get("red_flags") or [])[:8]],
        "missing": [str(x)[:80] for x in (sess.get("missing") or [])[:8]],
        "kb_searched": bool(kb_ok),
        "analyzed": bool(analyzed),
        "last_write_ok": None if last_write is None else bool(last_write.get("ok")),
        "write_failed_tools": failed_writes,
        # 草稿脱敏后无法再检测身份：原文判定一次，只留布尔位，不留身份值。
        "draft_has_identity": has_identity(raw_draft),
    }
    return {"query": _clip(redact_identity(str(query or "")), MAX_QUERY_CHARS),
            "facts": facts,
            "allowed_citation_ids": allowed_ids,
            "allowed_citations": citations,
            "evidence": evidences,
            "draft": _clip(redact_identity(raw_draft), MAX_DRAFT_CHARS)}


def _mentions_red_flag(draft: str, flags) -> bool:
    if any(w in draft for w in ESCALATION_WORDS):
        return True
    for flag in flags or []:
        for token in re.split(r"[、,，;；/\s]+", str(flag)):
            if len(token) >= 2 and token in draft:
                return True
    return False


def _issue(code: str, severity: str, detail: str, source: str) -> dict:
    return {"code": code, "severity": severity,
            "detail": _clip(redact_identity(str(detail or "")), MAX_ISSUE_DETAIL),
            "source": source}


def run_rules(bundle: dict) -> list:
    """确定性校验；返回问题列表（可能为空）。不依赖网络与模型。"""
    bundle = bundle if isinstance(bundle, dict) else {}
    draft = str(bundle.get("draft") or "")
    facts = bundle.get("facts") if isinstance(bundle.get("facts"), dict) else {}
    allowed = {str(x) for x in (bundle.get("allowed_citation_ids") or [])}
    evidence_text = " ".join(str(e.get("excerpt") or "") for e in (bundle.get("evidence") or []))
    issues = []

    ids = extract_citation_ids(draft)
    unknown = [cid for cid in ids if cid not in allowed]
    if unknown:
        issues.append(_issue("unknown_citation", "high",
                             f"{len(unknown)} 个引用不在本轮证据白名单", "rule"))
    if any(cid.startswith("RB-") for cid in ids) and not facts.get("kb_searched"):
        issues.append(_issue("citation_without_retrieval", "high",
                             "回复含书籍引用，但本轮没有成功的知识库检索", "rule"))
    if SOURCE_CLAIM_RE.search(draft) and not ids and not facts.get("kb_searched"):
        issues.append(_issue("unsupported_source_claim", "high",
                             "回复声称依据知识库/指南，但本轮没有检索证据", "rule"))
    if facts.get("last_write_ok") is False and SAVE_CLAIM_RE.search(draft):
        issues.append(_issue("write_status_mismatch", "high",
                             "本轮写入类工具最后执行失败，回复仍称已保存/完成", "rule"))
    if CERTAIN_DX_RE.search(draft):
        issues.append(_issue("certain_diagnosis", "high",
                             "出现确诊/确定是等确定性诊断措辞", "rule"))
    flags = facts.get("red_flags") or []
    if flags and not _mentions_red_flag(draft, flags):
        issues.append(_issue("red_flag_ignored", "high",
                             f"本轮有 {len(flags)} 项红旗，回复未提示", "rule"))
    if RAW_DUMP_RE.search(draft):
        issues.append(_issue("raw_tool_dump", "high",
                             "回复包含工具原始 JSON/代码围栏", "rule"))
    if facts.get("draft_has_identity"):
        issues.append(_issue("identity_leak", "high",
                             "回复疑似包含身份证/手机号/姓名标注", "rule"))

    doses = DOSE_RE.findall(draft)
    if doses and not any(d in evidence_text for d in doses):
        issues.append(_issue("dose_not_in_evidence", "warn",
                             f"{len(doses)} 处剂量/频次未在工具证据中出现", "rule"))
    if REGIMEN_RE.search(draft) and not (facts.get("kb_searched") or facts.get("analyzed")):
        issues.append(_issue("regimen_without_retrieval", "high",
                             "给出明确疗程/频次，但本轮没有检索或分析工具结果", "rule"))
    return issues


_CRITIC_SYSTEM = "\n".join([
    "你是独立的医疗回复核验器，不是医生。只做一致性核验，不提供新的医学知识、不建议处方。",
    "输入是一个 JSON：query=医生本轮原话；facts=病例事实；evidence=本轮工具证据（可能截断）；"
    "draft=准备发给医生的回复；allowed_citation_ids=允许出现的引用 ID。",
    "核验点：",
    "1) draft 是否回答了 query；是否引入 evidence/query 都没有的事实、数字、剂量、引用；",
    "2) 引用 ID 是否都在 allowed_citation_ids；称「根据知识库/指南」时是否有检索证据；",
    "3) 工具报错或未执行时，draft 是否仍称「已保存/已完成」；",
    "4) facts.red_flags 是否被忽略；",
    "5) 是否出现确诊/处方式措辞、身份信息、工具原始 JSON。",
    "只输出 JSON，不要解释：",
    '{"verdict":"pass|correct","issues":[{"code":"...","severity":"high|warn","detail":"不超过80字"}]}',
    "没有问题输出 {\"verdict\":\"pass\",\"issues\":[]}。可用的 code："
    + "、".join(sorted(KNOWN_CODES)) + "。",
])


def _critic_view(bundle: dict, stage: str) -> dict:
    evidence = []
    source = (bundle.get("evidence") or [])[-MAX_CRITIC_EVIDENCE:]
    for e in source:
        item = dict(e)
        item["excerpt"] = _clip(item.get("excerpt") or "", MAX_CRITIC_EXCERPT)
        evidence.append(item)
    return {"stage": stage, "query": bundle.get("query") or "",
            "facts": bundle.get("facts") or {},
            "allowed_citation_ids": bundle.get("allowed_citation_ids") or [],
            "evidence": evidence, "draft": bundle.get("draft") or ""}


def build_critic_messages(bundle: dict, stage: str) -> list:
    return [{"role": "system", "content": _CRITIC_SYSTEM},
            {"role": "user", "content": json.dumps(_critic_view(bundle, stage),
                                                   ensure_ascii=False)[:8000]}]


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_critic(raw) -> dict:
    """严格解析评审输出；任何异常都返回 status=error，不抛。"""
    if isinstance(raw, dict):
        raw = raw.get("content")
    text = str(raw or "").strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    if not text:
        return {"status": "error", "verdict": "", "issues": []}
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {"status": "error", "verdict": "", "issues": []}
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return {"status": "error", "verdict": "", "issues": []}
    if not isinstance(data, dict):
        return {"status": "error", "verdict": "", "issues": []}
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "correct"):
        return {"status": "error", "verdict": "", "issues": []}
    issues = []
    for item in (data.get("issues") or [])[:8]:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code not in KNOWN_CODES:
            code = "critic_other"
        severity = "high" if str(item.get("severity") or "").lower() == "high" else "warn"
        if code == "critic_other":
            severity = "warn"            # 未知结构不允许阻塞回合
        issues.append(_issue(code, severity, item.get("detail") or "", "llm"))
    if verdict == "correct" and not issues:
        return {"status": "error", "verdict": "", "issues": []}
    return {"status": "ok", "verdict": verdict, "issues": issues}


def _blocking(issues, critic_verdict: str) -> list:
    high = [i for i in issues if i.get("severity") == "high"]
    if high:
        return high
    if critic_verdict == "correct":
        return [i for i in issues if i.get("source") == "llm"]
    return []


def evaluate(bundle: dict, stage: str, reflect=None, llm=None, allow_llm: bool = True) -> dict:
    """规则 + 可选 LLM 评审。任何异常都退化为规则结论，绝不抛出。"""
    t0 = time.monotonic()
    result = {"stage": stage, "verdict": "pass", "status": "ok", "issues": [],
              "blocking": [], "codes": [], "high_count": 0, "corrections": 0,
              "allowed_citations": [], "allowed_citation_ids": [],
              "critic_model": "", "latency_ms": 0, "at": _now(),
              "usage": None, "prompt_messages": [], "raw_output": ""}
    try:
        result["allowed_citations"] = list(bundle.get("allowed_citations")
                                          or collect_citations(bundle.get("evidence") or []))
        result["allowed_citation_ids"] = list(bundle.get("allowed_citation_ids") or [])
        issues = run_rules(bundle)
        critic_verdict = ""
        if not _env_bool("DERM_REFLECTION", True):
            result["status"] = "disabled"
        elif not allow_llm or not callable(reflect):
            result["status"] = "budget"
        else:
            try:
                base_url, api_key, model = (llm or ("", "", ""))[:3]
                messages = build_critic_messages(bundle, stage)
                result["prompt_messages"] = messages
                resp = reflect(base_url, api_key, model, messages) or {}
                result["usage"] = resp.get("_usage") if isinstance(resp, dict) else None
                result["raw_output"] = str(resp.get("content") or "") if isinstance(resp, dict) else ""
                parsed = parse_critic(resp)
                if parsed.get("status") == "ok":
                    critic_verdict = parsed.get("verdict") or ""
                    issues += parsed.get("issues") or []
                    result["critic_model"] = str(resp.get("_model") or model or "")
                else:
                    result["status"] = "error"
            except Exception:  # noqa: BLE001 — 评审失败只降级评审状态
                result["status"] = "llm_unavailable"
        blocking = _blocking(issues, critic_verdict)
        result["issues"] = issues
        result["blocking"] = blocking
        result["high_count"] = len(blocking)
        codes = []
        for item in blocking or issues:
            code = str(item.get("code") or "")
            if code and code not in codes:
                codes.append(code)
        result["codes"] = codes
        result["verdict"] = "correct" if blocking else "pass"
    except Exception:  # noqa: BLE001 — 规则异常也不阻断问诊
        result["status"] = "error"
        result["verdict"] = "pass"
    result["latency_ms"] = int((time.monotonic() - t0) * 1000)
    result["at"] = _now()
    return result


def correction_message(issues, stage: str) -> str:
    codes = []
    for item in issues or []:
        code = str(item.get("code") or "")
        if code and code not in codes:
            codes.append(code)
    label = _labels(codes)
    if stage == "retrieval":
        return ("[内部核验反馈，非医生消息] 本轮知识库检索结果可能不足以支撑回答（"
                + label + "）。如仍缺关键依据，请换更具体的检索词再检索一次；"
                "若仍无结果，请在最终回复中明确说明「本地知识库未检索到依据」，不要编造引用或剂量。")
    return ("[内部核验反馈，非医生消息] 上一版回复未通过一致性核验（" + label + "）。"
            "请直接修正并给出完整最终回复：删除或纠正无依据的引用、剂量、确诊措辞和工具原始输出；"
            "只使用本轮工具结果中已有的证据；无法核实的结论请标注「待核」或以"
            "「本地知识库未检索到依据」说明。不要向医生提及本反馈。")


def degrade_text(base: str, issues) -> str:
    codes = []
    for item in issues or []:
        code = str(item.get("code") or "")
        if code and code not in codes:
            codes.append(code)
    note = ("\n\n> ⚠️ 本轮回复有 " + str(len(issues or [])) + " 项未通过自动核验（"
            + _labels(codes) + "），已保留原文供你核对；"
            "引用/剂量相关内容请以原文与病历为准。")
    return str(base or "").rstrip() + note


def with_sanitized(result: dict, removed: int) -> dict:
    """终稿引用清洗发生删除时，把核验结论升为 corrected（不伪造通过）。"""
    out = dict(result or {})
    if removed <= 0:
        return out
    codes = list(out.get("codes") or [])
    if "unknown_citation" not in codes:
        codes.append("unknown_citation")
    out["codes"] = codes
    out["verdict"] = "corrected"
    out["high_count"] = max(1, int(out.get("high_count") or 0))
    out["summary"] = None
    return out


def compact_summary(result: dict) -> dict:
    """给快照/历史用的紧凑结论：不含 detail、excerpt、prompt、usage。"""
    result = result if isinstance(result, dict) else {}
    verdict = str(result.get("verdict") or "unverified")
    status = str(result.get("status") or "ok")
    codes = [str(c) for c in (result.get("codes") or [])][:8]
    summary = result.get("summary")
    if not summary:
        if verdict == "pass":
            summary = ("规则核验通过（模型复核未执行）" if status in ("budget", "disabled", "llm_unavailable", "error")
                       else "已核验通过")
        elif verdict == "corrected":
            summary = "已自动修正 " + str(len(codes)) + " 项（" + _labels(codes) + "）"
        elif verdict == "degraded":
            summary = "有 " + str(len(codes)) + " 项未核实（" + _labels(codes) + "），已标注供核对"
        else:
            summary = "未进行最终核验"
    return {"verdict": verdict, "status": status, "summary": summary,
            "codes": codes, "corrections": int(result.get("corrections") or 0),
            "checked_at": str(result.get("at") or _now()),
            "model": str(result.get("critic_model") or "")}


def audit_payload(result: dict) -> dict:
    result = result if isinstance(result, dict) else {}
    return {"verdict": result.get("verdict"), "status": result.get("status"),
            "stage": result.get("stage"), "codes": list(result.get("codes") or [])[:8],
            "issue_count": len(result.get("issues") or []),
            "high_count": int(result.get("high_count") or 0),
            "corrections": int(result.get("corrections") or 0),
            "model": str(result.get("critic_model") or ""),
            "latency_ms": int(result.get("latency_ms") or 0)}


def storage_view(result: dict) -> dict:
    """跨进程可落库的核验结论：紧凑摘要 + 白名单引用 id，不含证据/提示词/评审原文。"""
    out = compact_summary(result)
    out["stage"] = (result or {}).get("stage")
    out["allowed_citations"] = [{"citation_id": str(c.get("citation_id"))}
                                for c in ((result or {}).get("allowed_citations") or [])
                                if isinstance(c, dict) and c.get("citation_id")]
    out["issue_count"] = len((result or {}).get("issues") or [])
    out["high_count"] = int((result or {}).get("high_count") or 0)
    return out
