# -*- coding: utf-8 -*-
"""书籍引用契约：RB-* ID、旧字段兼容、提示词片段裁剪。"""
import re

from . import config

_IDENTITY = re.compile(r"(?:姓名|名字|患者姓名|name|电话|手机|身份证号?|证件号?|地址)"
                       r"\s*[:：]?\s*[^\s，,；;。]{1,24}", re.I)
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_NO = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


def sanitize_query(query: str) -> str:
    text = str(query or "")
    text = _IDENTITY.sub("", text)
    text = _PHONE.sub("", text)
    text = _ID_NO.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:config.query_max_chars()]


def citation_id(hit: dict) -> str:
    anchor = str(hit.get("anchor_hash") or hit.get("content_hash") or "")
    return f"RB-{str(hit.get('book_id') or '')[:16]}-{anchor[:8] or 'unknown'}"


def _score(hit: dict) -> float:
    for key in ("rerank_score", "vector_score", "bm25_score"):
        value = hit.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def to_citation(hit: dict, *, retrieval_mode: str, k_used: int, n_used: int,
                k_source: str, rerank_state: str, kb_snapshot: str) -> dict:
    content = str(hit.get("content") or "")
    return {
        "citation_id": citation_id(hit),
        "source_type": "local_book",
        "book_id": hit.get("book_id"),
        "book_title": hit.get("book_title") or "",
        "version": hit.get("version") or "",
        "author": hit.get("author") or "",
        "publisher": hit.get("publisher") or "",
        "section": hit.get("section") or "",
        "section_path": hit.get("section_path") or "",
        "page": hit.get("page") or None,
        "page_hint": hit.get("page_hint") or None,
        "quoted_text": content,
        "chunk_hash": hit.get("content_hash") or "",
        "local_file": hit.get("filename") or "",
        "retrieval_score": round(_score(hit), 4),
        "vector_score": hit.get("vector_score"),
        "rerank_score": hit.get("rerank_score"),
        "retrieval_mode": retrieval_mode,
        "k_used": int(k_used),
        "n_used": int(n_used),
        "k_source": k_source,
        "rerank_state": rerank_state,
        "kb_snapshot": kb_snapshot,
    }


def build_context(citations: list, total_chars: int | None = None,
                  per_chars: int | None = None) -> dict:
    """拼成注入 LLM 的书籍块：每条 ≤350 字，总量 ≤2600 字，超限裁剪并标记。"""
    total_chars = int(total_chars or config.context_chars())
    per_chars = int(per_chars or config.citation_chars())
    lines, used, truncated = [], 0, False
    for cite in citations:
        content = str(cite.get("quoted_text") or "")
        snippet = content[:per_chars]
        if len(content) > per_chars:
            snippet = snippet.rstrip() + "…"
        title = cite.get("book_title") or "本地书籍"
        section = cite.get("section_path") or cite.get("section") or ""
        page = cite.get("page") or cite.get("page_hint")
        score = cite.get("rerank_score")
        score_txt = (f"rerank={float(score):.2f}" if score is not None
                     else f"score={float(cite.get('retrieval_score') or 0):.2f}")
        line = (f"[{cite.get('citation_id')}] 《{title}》"
                f"{(' ' + section) if section else ''}"
                f"{(' · p.' + str(page)) if page else ''} · {score_txt}：{snippet}")
        if used + len(line) + 1 > total_chars:
            truncated = True
            break
        lines.append(line)
        used += len(line) + 1
    return {"text": "\n".join(lines), "truncated": truncated, "used": len(lines),
            "chars": used}


def allowed_ids(citations: list) -> set:
    return {c.get("citation_id") for c in (citations or []) if c.get("citation_id")}


def validate_ids(raw_ids, citations: list) -> list:
    allowed = allowed_ids(citations)
    return [str(i) for i in (raw_ids or []) if str(i) in allowed]
