# -*- coding: utf-8 -*-
"""切分：按 Markdown 标题维护 section_path，表格原子保留，块间可配重叠。

四种模式（section/paragraph/sentence/book）由偏好设置选择；chunk_id 含序号，
重切会变；引用 ID 用 anchor_hash（章节 + 归一化正文）保持稳定。
"""
import hashlib
import re

from . import config

MODES = ("section", "paragraph", "sentence", "book")
OVERLAP_MAX = 300

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_PAGE_MARK = re.compile(r"^<!--\s*page(?:break|:\s*(\d+))\s*-->$", re.I)
_TABLE_SEP = re.compile(r"^[\s|:\-]+$")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9]+")
_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])")


def effective_target(mode: str, target: int, max_chars: int) -> int:
    """book 模式用硬上限做目标长度，其余模式用目标长度。"""
    return int(max_chars if mode == "book" else target)


def validate(mode: str, overlap, target: int, max_chars: int) -> None:
    """非法参数直接报错，不做静默截断（UI/配置/实际生效值必须一致）。"""
    if mode not in MODES:
        raise ValueError(f"切分方式非法：{mode}（可选：{'/'.join(MODES)}）")
    if isinstance(overlap, bool) or not isinstance(overlap, int):
        raise ValueError("overlap 必须是整数")
    limit = max(0, min(OVERLAP_MAX, int(target) - 1, int(max_chars) - 1))
    if overlap < 0 or overlap > limit:
        raise ValueError(f"overlap 必须在 0–{limit} 之间且小于目标块长 {int(target)}")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _hash16(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _tokens(text: str) -> int:
    return len(_CJK.findall(text)) + len(_WORD.findall(text))


def _units(markdown: str) -> list:
    lines = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    stack, units, buf = [], [], []
    section, section_path = "", ""
    page, page_known = 0, False

    def flush():
        if not buf:
            return
        text = "\n".join(buf).strip()
        buf.clear()
        if text:
            units.append({"kind": "text", "text": text, "section": section,
                          "section_path": section_path,
                          "page": page if page_known else None})

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        mark = _PAGE_MARK.match(stripped)
        if mark:
            flush()
            page = int(mark.group(1)) if mark.group(1) else page + 1
            page_known = True
            i += 1
            continue
        heading = _HEADING.match(stripped)
        if heading:
            flush()
            level, title = len(heading.group(1)), heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            if title:
                stack.append((level, title))
            section = title
            section_path = " > ".join(t for _, t in stack)
            if title:
                buf.append(title)
            i += 1
            continue
        if stripped.startswith("|") and stripped.count("|") >= 2:
            flush()
            rows = []
            while i < len(lines):
                row = lines[i].strip()
                if row.startswith("|") and row.count("|") >= 2:
                    rows.append(row)
                    i += 1
                else:
                    break
            units.append({"kind": "table", "text": "\n".join(rows), "section": section,
                          "section_path": section_path,
                          "page": page if page_known else None})
            continue
        buf.append(lines[i])
        i += 1
    flush()
    return units


def _hard_slice(text: str, size: int) -> list:
    return [text[i:i + size] for i in range(0, len(text), max(1, size))]


def _split_sentences(text: str, target: int, max_chars: int) -> list:
    parts = [p for p in re.split(r"(?<=[。！？；!?;])", text) if p and p.strip()]
    out, buf = [], ""
    for part in parts:
        if len(part) > max_chars:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_hard_slice(part, max_chars))
            continue
        if buf and len(buf) + len(part) > target:
            out.append(buf)
            buf = part
        else:
            buf = part if not buf else buf + part
    if buf.strip():
        out.append(buf)
    return out


def _pack_paragraphs(text: str, target: int, max_chars: int) -> list:
    if len(text) <= max_chars:
        return [text]
    out, buf = [], ""
    for paragraph in re.split(r"\n{2,}", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > max_chars:
            if buf:
                out.append(buf)
                buf = ""
            out.extend(_split_sentences(paragraph, target, max_chars))
            continue
        if buf and len(buf) + 2 + len(paragraph) > target:
            out.append(buf)
            buf = paragraph
        else:
            buf = paragraph if not buf else buf + "\n\n" + paragraph
    if buf.strip():
        out.append(buf)
    return out or [text]


def _sentence_tokens(text: str) -> list:
    """按空行分段、段内按句末标点切句；返回 [(句子, 与前句的连接符)]。

    段内单换行视为 PDF 排版软换行，直接并入句子；段间保留空行。
    """
    out = []
    for block in re.split(r"\n{2,}", text or ""):
        block = re.sub(r"\s*\n\s*", "", block).strip()
        if not block:
            continue
        parts = [p for p in _SENTENCE_END.split(block) if p.strip()]
        for idx, part in enumerate(parts):
            out.append((part, "\n\n" if idx == 0 and out else ""))
    return out


def _pack_sentences(text: str, target: int, max_chars: int) -> list:
    out, buf = [], ""
    for token, sep in _sentence_tokens(text):
        if len(token) > max_chars:
            if buf.strip():
                out.append(buf)
                buf = ""
            out.extend(_hard_slice(token, max_chars))
            continue
        if buf and len(buf) + len(sep) + len(token) > target:
            out.append(buf)
            buf = token
        else:
            buf = (buf + sep + token) if buf else token
    if buf.strip():
        out.append(buf)
    return out or [text]


def _paragraph_pieces(text: str, target: int, max_chars: int) -> list:
    out = []
    for paragraph in re.split(r"\n{2,}", text or ""):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > max_chars:
            out.extend(_split_sentences(paragraph, target, max_chars))
        else:
            out.append(paragraph)
    return out or [text]


def _split_table(text: str, max_chars: int) -> list:
    if len(text) <= max_chars:
        return [text]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header, body = lines[:1], lines[1:]
    if len(lines) >= 2 and _TABLE_SEP.match(lines[1]):
        header, body = lines[:2], lines[2:]
    prefix = "\n".join(header)
    out, rows = [], []
    for row in body:
        candidate = prefix + "\n" + "\n".join(rows + [row])
        if rows and len(candidate) > max_chars:
            out.append(prefix + "\n" + "\n".join(rows))
            rows = [row]
        elif not rows and len(prefix) + len(row) + 1 > max_chars:
            out.extend(_hard_slice(prefix + "\n" + row, max_chars))
        else:
            rows.append(row)
    if rows:
        out.append(prefix + "\n" + "\n".join(rows))
    return out or [text]


def chunk_markdown(markdown: str, book_id: str, title: str = "",
                   target: int | None = None, overlap: int | None = None,
                   max_chars: int | None = None, generation: int = 0,
                   mode: str = "section") -> list:
    target = int(target or config.chunk_target())
    overlap = int(overlap if overlap is not None else config.chunk_overlap())
    max_chars = int(max_chars or config.chunk_max())
    if max_chars < target:
        max_chars = target
    mode = str(mode or "section")
    validate(mode, overlap, target, max_chars)
    target = effective_target(mode, target, max_chars)

    chunks = []
    state = {"prev_text": "", "prev_section": None, "prev_page": None}

    def emit(content: str, unit: dict, is_table: bool = False):
        content = re.sub(r"\n{3,}", "\n\n", content).strip()
        if not content:
            return
        index = len(chunks)
        section_path = unit.get("section_path") or ""
        anchor = _hash16(section_path + "\x00" + _normalize(content))
        prefix = f"{book_id}-g{int(generation)}" if generation else book_id
        chunks.append({
            "chunk_id": f"{prefix}-{index:05d}-{_hash16(_normalize(content))}",
            "book_id": book_id,
            "chunk_index": index,
            "section": unit.get("section") or "",
            "section_path": section_path,
            "page": unit.get("page"),
            "page_hint": unit.get("page"),
            "content": content,
            "content_hash": _hash16(_normalize(content)),
            "anchor_hash": anchor,
            "token_count": _tokens(content),
            "is_table": bool(is_table),
            "book_title": title,
        })

    buf, buf_len, buf_unit = [], 0, None

    def flush_text():
        nonlocal buf, buf_len, buf_unit
        if not buf or buf_unit is None:
            buf, buf_len, buf_unit = [], 0, None
            return
        content = "\n".join(buf)
        tail = state["prev_text"][-overlap:] if overlap > 0 else ""
        same_scope = mode == "book" or state["prev_section"] == buf_unit.get("section_path")
        if tail and same_scope and len(content) > overlap:
            room = max_chars - len(content) - 1
            if room > 0:
                content = tail[-room:] + "\n" + content
        emit(content, buf_unit)
        state["prev_text"] = content
        state["prev_section"] = buf_unit.get("section_path")
        state["prev_page"] = buf_unit.get("page")
        buf, buf_len, buf_unit = [], 0, None

    for unit in _units(markdown):
        if (mode != "book" and buf and buf_unit is not None
                and unit.get("section_path") != buf_unit.get("section_path")):
            flush_text()
            state["prev_text"] = ""
        if unit["kind"] == "table":
            flush_text()
            state["prev_text"] = ""
            for piece in _split_table(unit["text"], max_chars):
                emit(piece, unit, is_table=True)
            state["prev_section"] = unit.get("section_path")
            state["prev_page"] = unit.get("page")
            continue
        text = unit["text"].strip()
        if not text:
            continue
        if mode == "sentence":
            pieces = _pack_sentences(text, target, max_chars)
        elif mode == "paragraph":
            pieces = _paragraph_pieces(text, target, max_chars)
        else:
            pieces = _pack_paragraphs(text, target, max_chars)
        if mode in ("sentence", "paragraph"):
            for piece in pieces:
                buf, buf_len, buf_unit = [piece], len(piece), unit
                flush_text()
            continue
        for piece in pieces:
            if not buf:
                buf, buf_len, buf_unit = [piece], len(piece), unit
                continue
            if buf_len + 1 + len(piece) > target:
                flush_text()
                buf, buf_len, buf_unit = [piece], len(piece), unit
            else:
                buf.append(piece)
                buf_len += 1 + len(piece)
    flush_text()
    return chunks
