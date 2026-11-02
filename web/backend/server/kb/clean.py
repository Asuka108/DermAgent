# -*- coding: utf-8 -*-
"""标准清洗：去页眉页脚/页码/目录/参考文献，修复 PDF 断行与连字符。

只做“去噪 + 归一”，不改写医学正文；删除量全部写进 clean_log 供审计。
"""
import re
from collections import Counter

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_PAGE_NUMBER = re.compile(r"^\s*[-—–]?\s*\d{1,4}\s*[-—–]?\s*$")
_TOC_LINE = re.compile(r"(?:\.{3,}|…{2,})\s*\d+\s*$")
_SYMBOLS_ONLY = re.compile(r"^[\s|·•*—–_\-+~=<>{}[\]()]+$")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HYPHEN_BREAK = re.compile(r"([A-Za-z])-\n([a-z])")
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_REF_HEADING = re.compile(r"^(#{0,6})\s*(参考文献|参考书目|references|bibliography)\s*$", re.I)
_EMPTY_HEADING = re.compile(r"^#{1,6}\s*$")
_PROTECTED = re.compile(r"^(图|表|Figure|Table|Fig\.)\s*\d+", re.I)
_PAGE_MARK = re.compile(r"^<!--\s*page(?:break|:\s*\d+)?\s*-->$", re.I)


def _strip_refs(lines: list) -> tuple:
    out, removed = [], 0
    skip_level = 0
    for line in lines:
        match = _HEADING.match(line)
        if skip_level:
            if match and len(match.group(1)) <= skip_level:
                skip_level = 0
            else:
                removed += len(line)
                continue
        ref = _REF_HEADING.match(line)
        if ref:
            skip_level = len(ref.group(1)) or 7
            removed += len(line)
            continue
        out.append(line)
    return out, removed


def _repeated_lines(lines: list) -> set:
    """页眉页脚：短、无句末标点、非标题/表格/页码/图注，出现 ≥3 次。"""
    counter = Counter()
    for line in lines:
        stripped = line.strip()
        if (2 <= len(stripped) <= 60 and not _HEADING.match(stripped)
                and not stripped.startswith("|") and not _PROTECTED.match(stripped)
                and not _PAGE_MARK.match(stripped) and not re.search(r"[。！？；.!?;:]$", stripped)):
            counter[stripped] += 1
    return {text for text, count in counter.items() if count >= 3}


def clean_markdown(text: str) -> dict:
    log = {"input_chars": len(text or "")}
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\f", "\n<!-- pagebreak -->\n")
    text = text.translate(_ZERO_WIDTH)
    text = text.replace("\u00a0", " ").replace("\u3000", " ")

    images = _IMAGE.findall(text)
    text = _IMAGE.sub("", text)
    hyphenations = len(_HYPHEN_BREAK.findall(text))
    text = _HYPHEN_BREAK.sub(r"\1\2", text)

    lines = [ln.rstrip() for ln in text.split("\n")]
    lines, ref_chars = _strip_refs(lines)

    repeated = _repeated_lines(lines)
    kept = []
    dropped_repeat = dropped_page = dropped_toc = dropped_symbol = dropped_heading = 0
    for line in lines:
        stripped = line.strip()
        if stripped in repeated:
            dropped_repeat += 1
            continue
        if _PAGE_NUMBER.match(stripped):
            dropped_page += 1
            continue
        if _TOC_LINE.search(stripped):
            dropped_toc += 1
            continue
        if _EMPTY_HEADING.match(stripped):
            dropped_heading += 1
            continue
        if stripped and len(stripped) <= 3 and _SYMBOLS_ONLY.match(stripped):
            dropped_symbol += 1
            continue
        kept.append(line)

    out = "\n".join(kept).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    log.update({
        "dropped_images": len(images),
        "fixed_hyphenations": hyphenations,
        "dropped_reference_chars": ref_chars,
        "dropped_repeated_lines": dropped_repeat,
        "dropped_page_numbers": dropped_page,
        "dropped_toc_lines": dropped_toc,
        "dropped_symbol_lines": dropped_symbol,
        "dropped_empty_headings": dropped_heading,
        "output_chars": len(out),
    })
    if not out.strip():
        log["warning"] = "empty_after_clean"
    return {"text": out, "log": log}
