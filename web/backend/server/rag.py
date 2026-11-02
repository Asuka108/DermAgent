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

try:  # 新管线（MinerU → Milvus → rerank）；本模块只保留兼容入口
    import kb as kb_mod
except ImportError:  # 以 server 包方式导入
    from . import kb as kb_mod

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DERM_DATA_DIR") or os.path.join(HERE, "data")
RAG_DIR = os.path.join(DATA_DIR, "rag")
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
_STRENGTH_TOPK = {"low": 3, "mid": 5, "high": 8}   # 检索强度 → n 上限（新管线断崖截断）
CHUNK_MODES = ("section", "paragraph", "sentence", "book")


def _chunk_mod():
    try:
        from kb import chunk as chunk_mod
    except ImportError:  # 以 server 包方式导入
        from .kb import chunk as chunk_mod
    return chunk_mod


def _chunk_limits() -> dict:
    target = int(kb_mod.config.chunk_target())
    max_chars = int(kb_mod.config.chunk_max())
    if max_chars < target:
        max_chars = target
    overlap_max = max(0, min(_chunk_mod().OVERLAP_MAX, target - 1, max_chars - 1))
    return {"target": target, "max_chars": max_chars,
            "overlap_min": 0, "overlap_max": overlap_max}


def _validate_chunk(mode: str, overlap, limits: dict) -> None:
    _chunk_mod().validate(mode, overlap, limits["target"], limits["max_chars"])


def get_prefs() -> dict:
    """读取 RAG 偏好；存储值非法时保留默认值并回报错误（不静默吞掉）。"""
    limits = _chunk_limits()
    out = {"strength": "mid", "chunk_mode": "section",
           "chunk_overlap": int(kb_mod.config.chunk_overlap()),
           "chunk_mode_error": "", "chunk_overlap_error": "",
           "chunk_limits": limits}
    data = {}
    try:
        with _lock:
            with open(PREFS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        if data.get("strength") in _STRENGTH_TOPK:
            out["strength"] = data["strength"]
        mode = data.get("chunk_mode")
        if mode is not None and mode not in CHUNK_MODES:
            out["chunk_mode_error"] = f"切分方式非法：{mode}"
        elif mode in CHUNK_MODES:
            out["chunk_mode"] = mode
        if "chunk_overlap" in data:
            stored = data.get("chunk_overlap")
            try:
                _validate_chunk(out["chunk_mode"], stored, limits)
                out["chunk_overlap"] = int(stored)
            except (TypeError, ValueError) as exc:
                out["chunk_overlap_error"] = str(exc)
    try:
        _validate_chunk(out["chunk_mode"], out["chunk_overlap"], limits)
    except (TypeError, ValueError) as exc:
        out["chunk_overlap_error"] = out["chunk_overlap_error"] or str(exc)
        out["chunk_overlap"] = 0
    return out


def set_prefs(strength: str = None, chunk_mode: str = None,
              chunk_overlap: int = None) -> dict:
    cur = get_prefs()
    limits = cur["chunk_limits"]
    if strength in _STRENGTH_TOPK:
        cur["strength"] = strength
    mode = cur["chunk_mode"]
    if chunk_mode is not None:
        if chunk_mode not in CHUNK_MODES:
            raise ValueError(f"切分方式非法：{chunk_mode}（可选：{'/'.join(CHUNK_MODES)}）")
        mode = chunk_mode
    overlap = cur["chunk_overlap"]
    if chunk_overlap is not None:
        _validate_chunk(mode, chunk_overlap, limits)
        overlap = int(chunk_overlap)
    payload = {"strength": cur["strength"], "chunk_mode": mode,
               "chunk_overlap": overlap}
    os.makedirs(RAG_DIR, exist_ok=True)
    tmp = PREFS_FILE + ".tmp"
    with _lock:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, PREFS_FILE)
    return get_prefs()


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


def kb_snapshot(db: dict = None) -> str:
    """知识库快照版本：ready 书籍数 + 最新更新时间派生（新管线默认从服务层取）。"""
    if db is None:
        try:
            ready = [b for b in _svc().list_books() if b.get("status") == "ready"]
        except Exception:  # noqa: BLE001
            ready = []
        latest = max((b.get("updated_at") or "" for b in ready), default="none")
        return f"local-books@{len(ready)}books@{str(latest)[:19]}"
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

def _svc():
    return kb_mod.service()


def import_book(filename: str, data: bytes, meta_note: str = "") -> dict:
    """兼容入口：只上传登记（不再同步解析/索引）。解析由医生手动触发的 index 任务完成。"""
    r = _svc().upload(filename, data, meta_note)
    if not r.get("ok"):
        return {"ok": False, "reason": r.get("reason"), "book_id": r.get("book_id"),
                "status": r.get("status"), "duplicate_of": r.get("duplicate_of")}
    return {"ok": True, "book_id": r["book_id"], "status": r.get("status") or "uploaded",
            "reason": None, "chunks": 0, "needs_index": True,
            "kb_snapshot": kb_snapshot()}


def rebuild_index() -> dict:
    """兼容入口：为全部 ready 书籍入队 reindex（旧 generation 在切换前保持可检索）。"""
    svc = _svc()
    queued, failed, skipped = [], [], 0
    for book in svc.list_books(include_quarantined=False):
        if book.get("status") != "ready":
            skipped += 1
            continue
        r = svc.index(book["book_id"], reindex=True)
        if r.get("ok"):
            queued.append({"book_id": book["book_id"], "job_id": r.get("job_id")})
        else:
            failed.append({"book_id": book["book_id"], "reason": r.get("reason")})
    return {"ok": True, "queued": len(queued), "skipped": skipped,
            "failed": failed, "jobs": queued}


def import_folder(dir_path: str = None) -> dict:
    """兼容入口：扫描 book 文件夹并上传登记（SHA-256 去重）；索引需另行触发。"""
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
                r = _svc().upload(name, data, meta_note="book-folder")
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
    return {"ok": True, "dir": scanned, "imported": imported, "skipped": skipped,
            "failed": failed, "needs_index": True}


def get_retrievable_chunks(db: dict):
    for b in db["books"].values():
        if b.get("status") in RETRIEVABLE:
            for c in b.get("chunks", []):
                yield b, c


def embedding_status() -> dict:
    """向量模型如实状态（新管线：kb.embed 的廉价探测，不触发加载）。"""
    try:
        emb = kb_mod.embed.health()
        EMBEDDING_STATUS.update(model=emb.get("model"), state=emb.get("state", "not_installed"),
                                detail=emb.get("detail", ""))
    except Exception:  # noqa: BLE001 — 依赖缺失/模型异常一律如实回退关键词模式
        EMBEDDING_STATUS.update(model=None, state="not_installed", detail="向量模块不可用")
    return dict(EMBEDDING_STATUS)


def search(query: str, top_k: int = 5) -> dict:
    """兼容入口：委托 kb.search（Milvus 召回 → rerank → n 断崖；缺失时 BM25 降级）。"""
    result = _svc().search(query, top_n=top_k)
    if not result.get("ok"):
        return result
    return result


def soft_delete_book(book_id: str) -> dict:
    """可恢复删除：立即停止检索（quarantined），向量/镜像移除，清洗文本保留供恢复。"""
    r = _svc().quarantine(book_id)
    if r.get("ok"):
        r["kb_snapshot"] = kb_snapshot()
    return r


def restore_book(book_id: str) -> dict:
    """恢复：复用清洗文本重嵌入（不重跑 MinerU），异步任务排队。"""
    r = _svc().restore(book_id)
    if r.get("ok"):
        r["kb_snapshot"] = kb_snapshot()
    return r


def delete_book(book_id: str) -> dict:
    """物理删除：向量、镜像、磁盘目录与书目行全部移除，不可恢复。"""
    r = _svc().delete(book_id)
    if r.get("ok"):
        r["kb_snapshot"] = kb_snapshot()
    return r


def backup_kb() -> dict:
    """备份 v2：书目 + 清洗文本（不含向量）；恢复后需重新索引。"""
    return _svc().backup()


def restore_kb(data: dict) -> dict:
    """恢复备份 v2：重建书目与清洗文本并重新入队索引。"""
    return _svc().restore_backup(data)
