# -*- coding: utf-8 -*-
"""本地医学书籍 RAG 知识库核心（计划 §16，服务端·全部本地运行）。

设计约束（来自 §16.1-16.5）：
- 全部本地：书籍/解析文本/索引/元数据/审计落盘 server/data/rag/，不依赖云端服务；
- 不伪造：无法解析/格式不支持/损坏 → failed 且带原因，绝不进入可检索状态；
- 生命周期：queued → parsing → chunking → indexing → ready；软删除 → quarantined（立即停止检索，可恢复）；
- 去重：完整文件 SHA-256 一致 → 拒绝重复导入；同书不同版本独立保存；
- 切分：语义（章节/小节/段落）+ 长度限制，稳定 chunk ID + 内容哈希 + 页码/标题回溯；
- 检索：BM25 关键词召回（内置、确定性）+ 引用门槛——只返回 ready 书籍的片段，
  citation 携带书名/版本/章节/页码/原文摘录/chunk 哈希/知识库快照版本；
  本地 embedding 向量召回留有接口位（模型未装 → 仅关键词检索并在状态中如实标注）。
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
RAG_DIR = os.path.join(HERE, "data", "rag")
BOOKS_FILE = os.path.join(RAG_DIR, "books.json")
SOURCES_DIR = os.path.join(RAG_DIR, "sources")   # v73：解析后原文缓存（重建索引用）
# v73：book 文件夹候选路径（项目根/book、桌面工作区/book、当前目录/book）——第一个存在的生效
PROJECT_BOOK_DIRS = [os.path.join(os.path.dirname(HERE), "book"),
                     os.path.expanduser("~/Desktop/aiyl/book"),
                     os.path.join(os.getcwd(), "book")]


def _book_dirs() -> list:
    return [d for d in PROJECT_BOOK_DIRS if os.path.isdir(d)]
_lock = threading.RLock()

# —— 契约常量 ——

LIFECYCLE = ("queued", "parsing", "chunking", "indexing", "ready", "failed", "quarantined")
RETRIEVABLE = {"ready"}                      # 只有 ready 参与检索（不完整索引不进入可检索状态）
SUPPORTED_FORMATS = {"pdf", "txt", "md", "html", "docx", "epub"}
# 第一版内置解析器：纯文本系。PDF（含扫描 OCR）/DOCX/EPUB 解析器在 rag_parsers 注册，
# 未注册 → 导入即 failed("parser_unavailable")，明确失败，不伪造解析结果（§16.2）。
BUILTIN_PARSERS = {"txt", "md", "html"}
MAX_BOOK_BYTES = 200 * 1024 * 1024           # 压缩炸弹/超大文件防护：单书 ≤200MB
MAX_CHUNK_CHARS = 600                        # 单块硬上限（仅防止异常长句）
TARGET_CHUNK_CHARS = 360                     # 语义分块目标大小
MIN_CHUNK_MERGE = 60                         # 过短相邻段落在不破坏语义时合并的下限
BOOK_CHUNK_CHARS = 2400                      # v73 book 模式：整书大块目标大小
EMBEDDING_STATUS = {"model": None, "state": "not_installed", "detail": ""}   # 如实记录：未装 → 仅关键词检索

# —— v73 RAG 偏好（检索强度 / 分块方式），原子落盘 data/rag/prefs.json ——

PREFS_FILE = os.path.join(RAG_DIR, "prefs.json")
_STRENGTH_TOPK = {"low": 2, "mid": 3, "high": 6}
CHUNK_MODES = ("section", "paragraph", "book")


def get_prefs() -> dict:
    out = {"strength": "mid", "chunk_mode": "section"}
    try:
        with _lock:
            with open(PREFS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        if isinstance(data, dict):
            if data.get("strength") in _STRENGTH_TOPK:
                out["strength"] = data["strength"]
            if data.get("chunk_mode") in CHUNK_MODES:
                out["chunk_mode"] = data["chunk_mode"]
    except (OSError, ValueError):
        pass
    return out


def set_prefs(strength: str = None, chunk_mode: str = None) -> dict:
    cur = get_prefs()
    if strength in _STRENGTH_TOPK:
        cur["strength"] = strength
    if chunk_mode in CHUNK_MODES:
        cur["chunk_mode"] = chunk_mode
    os.makedirs(RAG_DIR, exist_ok=True)
    tmp = PREFS_FILE + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, PREFS_FILE)
    return cur


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# —— 存储 ——

def _ensure_dir():
    os.makedirs(RAG_DIR, exist_ok=True)


def load_books() -> dict:
    if not os.path.exists(BOOKS_FILE):
        return {"books": {}, "seq": 0}
    with open(BOOKS_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def save_books(db: dict) -> None:
    _ensure_dir()
    tmp = BOOKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(db, fh, ensure_ascii=False)
    os.replace(tmp, BOOKS_FILE)


def kb_snapshot(db: dict) -> str:
    """知识库快照版本：ready 书籍数 + 最新更新时间派生，随引用发布（可追溯）。"""
    ready = [b for b in db["books"].values() if b.get("status") == "ready"]
    latest = max((b.get("updated_at", "") for b in ready), default="none")
    return f"local-books@{len(ready)}books@{latest[:19]}"


# —— 元数据提取（§16.4）：无法提取用文件名/导入时间补全并标记 incomplete ——

_META_PATTERNS = [
    ("title", re.compile(r"《([^》]{1,60})》")),
    ("author", re.compile(r"(?:作者|主编)[:：]\s*(\S{1,30})")),
    ("publisher", re.compile(r"(?:出版社|出版)[:：]\s*(\S{1,30})")),
    ("version", re.compile(r"(?:版次|版本)[:：]\s*(\S{1,20})")),
]


def extract_meta(filename: str, text: str) -> dict:
    meta = {"format_incomplete": False}
    for key, pat in _META_PATTERNS:
        m = pat.search(text[:4000] or "")
        if m:
            meta[key] = m.group(1)
    if not meta.get("title"):
        meta["title"] = os.path.splitext(os.path.basename(filename))[0]
        meta["format_incomplete"] = True       # 文件名兜底 → 标记不完整
    for key in ("author", "publisher", "version"):
        if not meta.get(key):
            meta[key] = "（未提取）"
            meta["format_incomplete"] = True
    meta["imported_at"] = _now()
    return meta


# —— 切分（§16.4）：语义（章节/小节/段落）+ 长度限制，稳定 ID + 哈希 + 回溯 ——

_SECTION_RE = re.compile(r"^(第[一二三四五六七八九十百\d]+[章节篇]|附录[一二三四五六七八九十\d]?|\d+\.\d+(\.\d+)?)\s*(.*)$")


def _split_sections(text: str):
    """按章节/小节标题行切段；无标题文本视作单节。返回 [(章节标题, 段落文本)]。"""
    section, buf = "前言", []
    for line in text.splitlines():
        s = line.strip()
        if _SECTION_RE.match(s) and len(s) <= 60:
            if buf:
                yield section, "\n".join(buf)
                buf = []
            section = s
        else:
            buf.append(line)
    if buf:
        yield section, "\n".join(buf)


def _split_long(paragraph: str, target: int = None):
    """按句子/标点和关键词边界分块，避免在词语中间硬切。"""
    target = target or TARGET_CHUNK_CHARS
    # 中英文句末、分号和列表边界都是较稳定的语义断点
    units = [u.strip() for u in re.split(r"(?<=[。！？!?；;])\s*|\n+|(?<=:)\s+(?=[A-Z0-9一-龥])", paragraph) if u.strip()]
    if not units:
        return
    buf = ""
    for unit in units:
        # 超长单句仅在最后一道防线按词/空格或字符切分
        if len(unit) > MAX_CHUNK_CHARS * 4:
            if buf:
                yield buf.strip(); buf = ""
            for i in range(0, len(unit), MAX_CHUNK_CHARS * 4):
                yield unit[i:i + MAX_CHUNK_CHARS * 4].strip()
            continue
        candidate = (buf + " " + unit).strip() if buf else unit
        if buf and len(candidate) > target:
            yield buf.strip()
            buf = unit
        else:
            buf = candidate
    if buf:
        yield buf.strip()


def chunk_text(book_id: str, text: str, mode: str = None):
    """语义+长度切分。返回 chunk 列表：{chunk_id, section, content, content_hash, page_hint}。
    MVP 页码提示按段落序估算（真实页码由 PDF 解析器逐页提供后覆盖）。
    v73 分块方式（prefs.chunk_mode）：section=按章节（默认，章节内段落合并）；
    paragraph=按段落（细粒度，不跨段合并）；book=按书（整本大块，章节不分离）。"""
    mode = mode or get_prefs()["chunk_mode"]
    chunks, idx = [], 0

    def _emit(section: str, parts: list):
        nonlocal idx
        for p in parts:
            if not p:
                continue
            h = _sha256(p.encode("utf-8"))[:16]
            chunks.append({"chunk_id": f"{book_id}-{idx:05d}-{h}", "section": section,
                           "content": p, "content_hash": h, "page_hint": idx // 3 + 1})
            idx += 1

    if mode == "book":
        big = re.sub(r"\n{3,}", "\n\n", text.strip())
        _emit("全书", list(_split_long(big, target=BOOK_CHUNK_CHARS)))
        return chunks

    for section, para in _split_sections(text):
        para = re.sub(r"\n{3,}", "\n\n", para.strip())
        if not para:
            continue
        if mode == "paragraph":                     # 每段独立成块，不合并
            _emit(section, list(_split_long(para)))
            continue
        parts = list(_split_long(para))
        # 过短相邻段落在同一节内合并（不跨节，避免破坏语义）
        if len(parts) == 1 and len(parts[0]) < MIN_CHUNK_MERGE and chunks and \
                chunks[-1]["section"] == section and len(chunks[-1]["content"]) + len(parts[0]) <= MAX_CHUNK_CHARS:
            chunks[-1]["content"] += "\n" + parts[0]
            chunks[-1]["content_hash"] = _sha256(chunks[-1]["content"].encode("utf-8"))[:16]
            continue
        _emit(section, parts)
    return chunks


# —— 解析器注册表（§16.2/16.3）：缺失即明确失败 ——

PARSERS = {}          # ext -> callable(bytes) -> {"text": str, "pages": [..] or None, "ocr_quality": [...]}


def register_parser(ext: str, fn):
    PARSERS[ext] = fn


def _html_to_text(raw: str) -> str:
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    txt = re.sub(r"<br[^>]*>|</p>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", "", txt)
    return txt


def parse_book(filename: str, data: bytes) -> dict:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if ext not in SUPPORTED_FORMATS:
        raise ValueError(f"format_unsupported:{ext}")
    if ext in PARSERS:
        return PARSERS[ext](data)
    if ext in BUILTIN_PARSERS:
        raw = data.decode("utf-8", errors="replace")
        if ext == "html":
            raw = _html_to_text(raw)
        return {"text": raw, "pages": None, "ocr_quality": None}
    raise ValueError("parser_unavailable:" + ext)


# —— BM25（Okapi，k1=1.5 b=0.75；中文 2-gram + 拉丁词）——

def _tokens(s: str):
    out = []
    for word in re.split(r"[\s,，。；;：:！!？?\.\-—()（）\[\]【】《》\"']+",
                         (s or "").lower()):
        if not word:
            continue
        if re.fullmatch(r"[a-z0-9]+", word):
            out.append(word)
        else:
            out.extend(word[i:i + 2] for i in range(len(word) - 1))   # 中文 bigram
    return out


def _bm25_scores(query: str, docs):
    """docs: [(chunk_id, tokens)] → {chunk_id: score}。确定性纯 Python 实现。"""
    q = _tokens(query)
    if not q or not docs:
        return {}
    k1, b = 1.5, 0.75
    avgdl = sum(len(t) for _, t in docs) / max(len(docs), 1)
    df = {}
    for _, toks in docs:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1
    n = len(docs)
    scores = {}
    for cid, toks in docs:
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        score = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = max(0.01, (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * (tf[term] * (k1 + 1)) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scores[cid] = round(score, 4)
    return scores


# —— 导入 / 状态机 ——

def import_book(filename: str, data: bytes, meta_note: str = "") -> dict:
    """导入一本书：格式/大小检查 → SHA-256 去重 → 解析（失败明确原因）→ 切分 → ready。
    任何失败返回 {"ok": False, "reason": ...} 且不留下可检索条目（可按用户选择保留 failed 状态）。"""
    with _lock:
        db = load_books()
        size = len(data)
        if size > MAX_BOOK_BYTES:
            return {"ok": False, "reason": "file_too_large", "max_bytes": MAX_BOOK_BYTES}
        digest = _sha256(data)
        for b in db["books"].values():
            if b.get("sha256") == digest and b.get("status") != "quarantined":
                return {"ok": False, "reason": "duplicate_sha256", "book_id": b["book_id"]}
        ext = os.path.splitext(filename)[1].lower().lstrip(".")
        if ext not in SUPPORTED_FORMATS:
            return {"ok": False, "reason": f"format_unsupported:{ext}"}
        db["seq"] = db.get("seq", 0) + 1
        book_id = f"BOOK-{db['seq']:04d}"
        book = {"book_id": book_id, "filename": os.path.basename(filename), "sha256": digest,
                "size": size, "status": "parsing", "chunks": [], "error": None,
                "meta": {}, "meta_note": meta_note[:200],
                "created_at": _now(), "updated_at": _now(), "deleted_at": None}
        db["books"][book_id] = book
        try:
            parsed = parse_book(filename, data)          # 失败 → 明确原因
            book["status"] = "chunking"
            book["meta"] = extract_meta(filename, parsed["text"])
            # v73：保留解析后原文——供「重建索引」按新切分方式重切（重复导入同文件会被去重拦截）
            try:
                os.makedirs(SOURCES_DIR, exist_ok=True)
                tmp = os.path.join(SOURCES_DIR, book_id + ".txt.tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(parsed["text"])
                os.replace(tmp, os.path.join(SOURCES_DIR, book_id + ".txt"))
                book["has_source"] = True
            except OSError:
                book["has_source"] = False
            chunks = chunk_text(book_id, parsed["text"])
            if not chunks:
                raise ValueError("empty_text")
            book["status"] = "indexing"
            book["chunks"] = chunks
            book["ocr_quality"] = parsed.get("ocr_quality")
            book["status"] = "ready"                     # 立即进入全库检索（无人工审核门槛 §16.1）
        except (ValueError, UnicodeError) as exc:
            book["status"] = "failed"
            book["error"] = str(exc)                     # 不伪造解析结果；保留失败状态
        save_books(db)
        return {"ok": book["status"] == "ready", "book_id": book_id, "status": book["status"],
                "reason": book.get("error"), "chunks": len(book.get("chunks", [])),
                "kb_snapshot": kb_snapshot(db)}


def rebuild_index() -> dict:
    """v73：按当前切分方式重切全部已存原文的 ready 书籍——
    切分方式改了之后无需重新导入，一键让已有书籍按新方式分块。"""
    mode = get_prefs()["chunk_mode"]
    rebuilt, skipped, failed = 0, 0, []
    with _lock:
        db = load_books()
        for b in db["books"].values():
            if b.get("status") != "ready":
                continue
            src = os.path.join(SOURCES_DIR, b["book_id"] + ".txt")
            if b.get("has_source") is False or not os.path.exists(src):
                skipped += 1          # 旧版本导入、无缓存原文 → 需重新导入一次
                continue
            try:
                with open(src, "r", encoding="utf-8") as fh:
                    text = fh.read()
                chunks = chunk_text(b["book_id"], text, mode=mode)
                if not chunks:
                    raise ValueError("empty_text")
                b["chunks"] = chunks
                b["updated_at"] = _now()
                rebuilt += 1
            except Exception as exc:  # noqa: BLE001 — 单本失败不阻塞其余
                failed.append(f"{b.get('filename', '')[:40]}: {str(exc)[:60]}")
        save_books(db)
    return {"ok": True, "mode": mode, "rebuilt": rebuilt, "skipped": skipped, "failed": failed}


def import_folder(dir_path: str = None) -> dict:
    """v73：扫描 book 文件夹（多候选路径），导入其中全部支持的书籍；
    SHA-256 去重——已导入过的自动跳过，可安全反复执行。"""
    dirs = [dir_path] if dir_path else _book_dirs()
    imported, skipped, failed, scanned = 0, 0, [], []
    for d in dirs:
        scanned.append(d)
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ext not in SUPPORTED_FORMATS:
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                r = import_book(name, data, meta_note="book-folder")
                if r.get("ok"):
                    imported += 1
                elif r.get("reason") == "duplicate_sha256":
                    skipped += 1
                else:
                    failed.append(f"{name[:40]}: {r.get('reason')}")
            except OSError as exc:
                failed.append(f"{name[:40]}: {exc}")
    if not scanned:
        return {"ok": False, "reason": "dir_missing",
                "dirs": PROJECT_BOOK_DIRS, "imported": 0, "skipped": 0, "failed": []}
    return {"ok": True, "dir": scanned, "imported": imported, "skipped": skipped, "failed": failed}


def get_retrievable_chunks(db: dict):
    for b in db["books"].values():
        if b.get("status") in RETRIEVABLE:
            for c in b.get("chunks", []):
                yield b, c


def embedding_status() -> dict:
    """向量模型如实状态（惰性触发 ensure_loaded 并同步到 EMBEDDING_STATUS）。

    v50 修复：此前状态只在检索发生时同步，启动后未检索过 → 状态行误显示"未安装"。
    模型已就绪时本调用瞬时返回；首次调用触发加载（幂等、锁内）。"""
    try:
        import rag_embed
        emb = rag_embed.ensure_loaded()
        EMBEDDING_STATUS.update(model=emb["model"], state=emb["state"], detail=emb.get("detail", ""))
    except Exception:  # noqa: BLE001 — 依赖缺失/模型异常一律如实回退关键词模式
        EMBEDDING_STATUS.update(model=None, state="not_installed", detail="向量模块不可用")
    return dict(EMBEDDING_STATUS)


def search(query: str, top_k: int = 5) -> dict:
    """混合检索：BM25 关键词召回 +（本地向量模型 ready 时）语义召回 RRF 融合 + 引用门槛。

    向量召回经 rag_embed 本地推理（原文不出本机）；模型未装/加载失败 → 如实仅 BM25，
    retrieval_mode 与 embedding_state 照实标注，绝不伪造 hybrid。"""
    emb = embedding_status()
    rag_embed = None
    if emb["state"] == "ready":
        import rag_embed  # noqa: PLC0415 — embedding_status 就绪即说明模块可导入
    with _lock:
        db = load_books()
        pairs = list(get_retrievable_chunks(db))
        docs = [(c["chunk_id"], _tokens(c["content"])) for _, c in pairs]
        contents = [c["content"] for _, c in pairs]
        bm25 = _bm25_scores(query, docs)
        by_id = {c["chunk_id"]: (b, c) for b, c in pairs}
        hybrid = bool(rag_embed and EMBEDDING_STATUS["state"] == "ready" and pairs)
        if hybrid:
            # RRF 融合（k=60）：BM25 与语义召回各贡献一个排名，免分数归一化、确定性
            rrf = {cid: 0.0 for cid, _ in docs}
            for r, (cid, _) in enumerate(sorted(bm25.items(), key=lambda kv: (-kv[1], kv[0])), 1):
                rrf[cid] += 1.0 / (60 + r)
            try:
                for r, (idx, _sim) in enumerate(
                        rag_embed.rank(query, contents,
                                       keys=[c["content_hash"] for _, c in pairs]), 1):
                    rrf[docs[idx][0]] += 1.0 / (60 + r)
                scores = {cid: s for cid, s in rrf.items() if s > 0}
            except Exception:  # noqa: BLE001 — 向量通道任何异常都只降级回 BM25，不阻塞检索
                hybrid = False
                scores = bm25
        else:
            scores = bm25
        top = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
        mode = "hybrid" if hybrid else "bm25"
        citations, seen_meta = [], set()
        for rank, (cid, score) in enumerate(top, 1):
            b, c = by_id[cid]
            m = b["meta"]
            citations.append({
                "citation_id": f"RB-{b['book_id'][-4:]}-{c['chunk_id'].split('-')[-1]}",
                "source_type": "local_book",
                "book_title": m.get("title"), "version": m.get("version"),
                "author": m.get("author"), "publisher": m.get("publisher"),
                "section": c["section"], "page_hint": c.get("page_hint"),
                "quoted_text": c["content"][:220],
                "chunk_hash": c["content_hash"],
                "local_file": b["filename"],
                "retrieval_score": round(score, 4), "retrieval_mode": mode,
                "kb_snapshot": kb_snapshot(db),
            })
            _ = rank, seen_meta            # 简化显示/展开详情由医生面板处理（§16.5）
        return {"ok": True, "query": query[:200], "embedding_state": EMBEDDING_STATUS["state"],
                "retrieval_mode": mode,
                "kb_snapshot": kb_snapshot(db), "citations": citations,
                "total_indexed_books": len([b for b in db["books"].values() if b["status"] in RETRIEVABLE])}


def soft_delete_book(book_id: str) -> dict:
    """可恢复删除：立即停止检索（status→quarantined），数据保留 + 删除时间 + 审计在外层。"""
    with _lock:
        db = load_books()
        b = db["books"].get(book_id)
        if not b:
            return {"ok": False, "reason": "not_found"}
        b["status"] = "quarantined"
        b["deleted_at"] = _now()
        b["updated_at"] = _now()
        save_books(db)
        return {"ok": True, "book_id": book_id, "status": "quarantined", "kb_snapshot": kb_snapshot(db)}


def restore_book(book_id: str) -> dict:
    with _lock:
        db = load_books()
        b = db["books"].get(book_id)
        if not b or b.get("status") != "quarantined":
            return {"ok": False, "reason": "not_found_or_not_deleted"}
        b["status"] = "ready" if b.get("chunks") else "failed"
        b["deleted_at"] = None
        b["updated_at"] = _now()
        save_books(db)
        return {"ok": True, "book_id": book_id, "status": b["status"], "kb_snapshot": kb_snapshot(db)}


def delete_book(book_id: str) -> dict:
    """v71：物理删除——书籍与全部索引从 books.json 移除，不可恢复（与软删 quarantine 互补）。
    调用方（端点/桥）负责二次确认与审计；误删可经 backup_kb 备份恢复。"""
    with _lock:
        db = load_books()
        b = db["books"].get(book_id)
        if not b:
            return {"ok": False, "reason": "not_found"}
        removed = db["books"].pop(book_id)
        save_books(db)
        return {"ok": True, "book_id": book_id, "filename": removed.get("filename"),
                "kb_snapshot": kb_snapshot(db)}


def backup_kb() -> dict:
    """完整知识库备份（书籍/文本/索引/元数据一体导出）。"""
    with _lock:
        db = load_books()
        return {"ok": True, "exported_at": _now(), "kb_snapshot": kb_snapshot(db), "data": db}


def restore_kb(data: dict) -> dict:
    if not isinstance(data, dict) or "books" not in data or not isinstance(data["books"], dict):
        return {"ok": False, "reason": "invalid_backup"}
    with _lock:
        save_books(data)
        return {"ok": True, "books": len(data["books"]), "kb_snapshot": kb_snapshot(data)}
