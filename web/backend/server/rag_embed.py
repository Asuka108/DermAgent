# -*- coding: utf-8 -*-
"""本地 embedding 模型管理（计划 §16.4，v49 支持自选安装）。

设计约束（来自执行计划 §16.4）：
- 用户在设置界面自选模型后**首次允许联网下载固定版本**；下载后**本地运行**——
  书籍原文绝不发送外部 embedding 服务；
- 记录模型名称、版本、文件哈希和下载/加载状态（manifest 落盘 server/data/rag/）；
- 下载或加载失败时保留原有 BM25 功能并如实提示（state=not_installed/error，绝不伪造 hybrid）。

隔离设计：torch / sentence-transformers 只在 ensure_loaded 内 import；下载用标准库 urllib
（不依赖重依赖）；rag.py 仅依赖 ensure_loaded / status / rank / install / catalog  vene，
依赖缺失时检索自动回退纯关键词模式。
"""
import hashlib
import json
import os
import ssl
import threading
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_ROOT = os.path.join(HERE, "data", "models")
MANIFEST_FILE = os.path.join(HERE, "data", "rag", "embedding_manifest.json")

# 可选模型清单（设置界面供医生自选；均为 ModelScope 可达的 BGE 检索模型，
# 文件结构一致便于统一下载器；按需扩充需保证 files 清单完整）。
# query_prefix：bge-zh v1.5 官方要求检索场景 query 加指令前缀；bge-m3 官方明确无需指令。
MODEL_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
MODEL_CATALOG = {
    "bge-small-zh-v1.5": {"id": "BAAI/bge-small-zh-v1.5",
                          "label": "BGE-small 中文（约 100MB，速度快）",
                          "query_prefix": MODEL_QUERY_PREFIX},
    "bge-base-zh-v1.5": {"id": "BAAI/bge-base-zh-v1.5",
                         "label": "BGE-base 中文（约 410MB，均衡，推荐）",
                         "query_prefix": MODEL_QUERY_PREFIX},
    "bge-large-zh-v1.5": {"id": "BAAI/bge-large-zh-v1.5",
                          "label": "BGE-large 中文（约 1.3GB，精度优先）",
                          "query_prefix": MODEL_QUERY_PREFIX},
    # bge-m3：XLM-RoBERTa 架构（多语言/长文本，1024 维）——tokenizer 无 vocab.txt，
    # 用 sentencepiece.bpe.model；权重同名 pytorch_model.bin，复用统一下载与哈希记录。
    "bge-m3": {"id": "BAAI/bge-m3",
               "label": "BGE-M3 多语言（约 2.3GB，精度最高，下载较慢）",
               "files": ["config.json", "pytorch_model.bin", "tokenizer.json",
                         "tokenizer_config.json", "sentencepiece.bpe.model",
                         "special_tokens_map.json", "config_sentence_transformers.json",
                         "modules.json", "sentence_bert_config.json", "1_Pooling/config.json"],
               "query_prefix": ""},
}
DEFAULT_MODEL = "bge-base-zh-v1.5"
# bge-zh 系列（BERT 架构 + sentence-transformers 池化）默认文件清单
MODEL_FILES = ["config.json", "pytorch_model.bin", "tokenizer.json", "tokenizer_config.json",
               "vocab.txt", "special_tokens_map.json", "config_sentence_transformers.json",
               "modules.json", "sentence_bert_config.json", "1_Pooling/config.json"]

# 如实状态机：not_installed（目录/依赖缺失）→ ready；加载异常 → error（detail 带原因）；
# installing 为下载进度（独立字段——下载中检索仍按未安装处理，绝不半途启用半成品）。
STATUS = {"model": None, "state": "not_installed", "detail": "", "installing": None}
_model = None
_loaded_short = None                      # 当前已加载权重对应的模型短名（切换 active 后强制重载）
_VEC_CACHE = None                         # v73：(model_short, {content_hash: 向量}) 内存缓存
_lock = threading.Lock()
_install_lock = threading.Lock()
_install_thread = None


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# —— manifest（含多模型安装记录与 active 指针，兼容 v48 平铺格式） ——

def _load_manifest() -> dict:
    try:
        with open(MANIFEST_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            if "models" not in data:      # v48 平铺格式迁移：dir 尾段即短名
                short = os.path.basename(str(data.get("dir") or "")) or DEFAULT_MODEL
                data = {"active": short if data.get("state") == "ready" else DEFAULT_MODEL,
                        "models": {short: {k: data.get(k) for k in
                                           ("model", "state", "weights_sha256", "weights_bytes")}}}
            data.setdefault("active", DEFAULT_MODEL)
            data.setdefault("models", {})
            return data
    except (OSError, ValueError):
        pass
    return {"active": DEFAULT_MODEL, "models": {}}


def _save_manifest(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(MANIFEST_FILE), exist_ok=True)
        tmp = MANIFEST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, MANIFEST_FILE)
    except OSError:
        pass                              # manifest 落盘失败不影响检索功能本身


def active_model() -> str:
    return _load_manifest().get("active") or DEFAULT_MODEL


def model_dir(short: str = None) -> str:
    return os.path.join(MODELS_ROOT, short or active_model())


def is_installed(short: str) -> bool:
    d = model_dir(short)
    return os.path.exists(os.path.join(d, "config.json")) and \
        os.path.exists(os.path.join(d, "pytorch_model.bin"))


def catalog() -> list:
    """设置界面清单：每个候选模型的状态与 active 标注。"""
    mf = _load_manifest()
    out = []
    for short, info in MODEL_CATALOG.items():
        rec = mf["models"].get(short) or {}
        installed = is_installed(short) or bool(rec.get("weights_sha256"))
        out.append({"short": short, "id": info["id"], "label": info["label"],
                    "installed": installed,
                    "active": installed and mf.get("active") == short,   # 未装不标 active（指针目标缺失时如实回落）
                    "weights_sha256": rec.get("weights_sha256"),
                    "installed_at": rec.get("installed_at")})
    return out


def last_error() -> str:
    mf = _load_manifest()
    return str(mf.get("last_error") or "")


# —— 下载（§16.4：首次安装允许联网，仅下载模型文件；原文/检索全程本地） ——

def _ssl_context() -> ssl.SSLContext:
    """macOS 框架 Python 常缺系统 CA（CERTIFICATE_VERIFY_FAILED）：certifi 可用时用其
    证书束，否则回退默认上下文（Linux/自装 CA 环境不受影响）。仅用于首次模型下载。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download_file(url: str, dest: str, progress) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "DermAgent/1.0"})
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                fh.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
        os.replace(tmp, dest)             # 完整才落正式名（半成品以 .part 存在）


def install(short: str) -> dict:
    """后台线程安装所选模型；返回是否受理。完成或失败结果写 STATUS / manifest。"""
    global _install_thread
    info = MODEL_CATALOG.get(short)
    if info is None:
        return {"ok": False, "reason": "unknown_model"}
    with _install_lock:
        if STATUS.get("installing"):
            return {"ok": False, "reason": "install_in_progress"}
        if is_installed(short):
            return {"ok": False, "reason": "already_installed"}
        STATUS["installing"] = {"model": short, "file": "", "pct": 0,
                                "downloaded": 0, "total": 0}

    def _run():
        try:
            base = f"https://modelscope.cn/models/{info['id']}/resolve/master"
            files = []
            for rel in (info.get("files") or MODEL_FILES):   # bge-m3 等 per-model 清单覆盖默认
                dest = os.path.join(model_dir(short), *rel.split("/"))
                url = f"{base}/{rel}"

                def progress(done, total, _f=rel):
                    pct = int(done * 100 / total) if total else 0
                    STATUS["installing"] = {"model": short, "file": os.path.basename(_f),
                                            "pct": pct, "downloaded": done, "total": total}

                _download_file(url, dest, progress)
                files.append((dest, rel))
            weights = os.path.join(model_dir(short), "pytorch_model.bin")
            mf = _load_manifest()
            mf["models"][short] = {"model": info["id"], "state": "ready",
                                   "weights_sha256": _sha256_file(weights),
                                   "weights_bytes": os.path.getsize(weights),
                                   "installed_at": datetime.now(timezone.utc).isoformat()}
            mf["active"] = short          # 新装即用：医生刚选装的模型立即生效
            mf.pop("last_error", None)
            _save_manifest(mf)
            with _lock:
                global _model, _loaded_short
                _model, _loaded_short = None, None   # 强制 ensure_loaded 重载新模型
                STATUS.update(model=None, state="not_installed", detail="")
        except Exception as exc:  # noqa: BLE001 — 下载失败如实记录，检索保留 BM25
            mf = _load_manifest()
            mf["last_error"] = f"{short}: {str(exc)[:160]}"
            _save_manifest(mf)
            STATUS["installing"] = None
            STATUS.update(model=None, state="not_installed",
                          detail=f"安装失败：{str(exc)[:160]}（已保留关键词检索）")
            return
        finally:
            STATUS["installing"] = None
            with _install_lock:
                _install_thread = None

    _install_thread = threading.Thread(target=_run, daemon=True, name="emb-install")
    _install_thread.start()
    return {"ok": True, "model": short}


def set_active(short: str) -> dict:
    """在已安装模型间切换激活项；下次检索自动重载。"""
    if short not in MODEL_CATALOG:
        return {"ok": False, "reason": "unknown_model"}
    if not is_installed(short):
        return {"ok": False, "reason": "not_installed"}
    mf = _load_manifest()
    mf["active"] = short
    _save_manifest(mf)
    with _lock:
        global _model, _loaded_short
        _model, _loaded_short = None, None
        STATUS.update(model=None, state="not_installed", detail="")
    return {"ok": True, "active": short}


def ensure_loaded() -> dict:
    """惰性加载 active 模型（线程安全，成功后幂等；切换 active 后自动重载）。绝不联网。"""
    global _model, _loaded_short
    with _lock:
        short = active_model()
        if STATUS["state"] == "ready" and _loaded_short == short:
            return dict(STATUS)
        d = model_dir(short)
        if not os.path.exists(os.path.join(d, "config.json")):
            STATUS.update(model=None, state="not_installed",
                          detail=f"模型目录缺失（server/data/models/{short}）")
            return dict(STATUS)
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            STATUS.update(model=None, state="not_installed",
                          detail=f"sentence-transformers 未安装：{exc}")
            return dict(STATUS)
        try:
            # v73：限制 torch CPU 线程数——此前编码打满全部核心，导入/检索时整机卡顿发烫
            try:
                import torch
                torch.set_num_threads(max(1, min(4, (os.cpu_count() or 4) // 2)))
            except Exception:  # noqa: BLE001 — 线程控制失败不影响功能
                pass
            _model = SentenceTransformer(d, local_files_only=True, device="cpu")
        except Exception as exc:  # noqa: BLE001 — 模型文件损坏/版本不兼容等，一律如实降级
            _model = None
            STATUS.update(model=None, state="error", detail=str(exc)[:200])
            mf = _load_manifest()
            mf.setdefault("models", {}).setdefault(short, {})["state"] = "error"
            mf["last_error"] = f"{short}: {str(exc)[:160]}"
            _save_manifest(mf)
            return dict(STATUS)
        _loaded_short = short
        STATUS.update(model=MODEL_CATALOG.get(short, {}).get("id", short),
                      state="ready", detail="")
        # 哈希记录（§16.4）：权重文件 sha256 + 大小，一次计算后写入 manifest 供追溯
        try:
            weights = os.path.join(d, "pytorch_model.bin")
            mf = _load_manifest()
            mf["models"][short] = {**mf["models"].get(short, {}),
                                   "model": MODEL_CATALOG.get(short, {}).get("id", short),
                                   "state": "ready",
                                   "weights_sha256": _sha256_file(weights),
                                   "weights_bytes": os.path.getsize(weights)}
            _save_manifest(mf)
        except OSError:
            pass
        return dict(STATUS)


def status() -> dict:
    return dict(STATUS)


def rank(query: str, texts: list, keys=None):
    """查询与文本列表的相似度排序。返回 [(index, cos_sim)] 按相似度降序；未 ready 返回 []。

    bge 官方用法：bge-zh v1.5 检索场景 query 加指令前缀；bge-m3 无需指令——前缀随模型配置。
    v73 性能修复：块向量按 content_hash 缓存到 emb_cache_{model}.npz——此前每次检索都把
    全库重新编码一遍（CPU 打满、整机发烫）；现在只有新增/变更块编码一次，检索仅编码查询。"""
    if STATUS["state"] != "ready" or _model is None or not texts:
        return []
    prefix = MODEL_CATALOG.get(_loaded_short or active_model(), {}).get("query_prefix", "")
    import math
    try:
        import numpy as np
    except ImportError:                       # 无 numpy → 退回旧行为（全量编码）
        q = _model.encode([prefix + query], normalize_embeddings=True)[0]
        vecs = _model.encode(list(texts), normalize_embeddings=True)
        scored = []
        for i, v in enumerate(vecs):
            sim = sum(float(a) * float(b) for a, b in zip(q, v))
            if math.isnan(sim) or math.isinf(sim):
                sim = 0.0
            scored.append((i, round(sim, 6)))
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return scored

    short = _loaded_short or active_model()
    cache = _load_vec_cache(short)
    if keys is None or len(keys) != len(texts):
        keys = [f"auto-{i}" for i in range(len(texts))]
    keys = [f"{short}:{k}" for k in keys]
    missing, sample = [], {}
    for k, t in zip(keys, texts):
        if k not in cache:
            missing.append(k)
            sample.setdefault(k, t)
    if missing:
        vecs = _model.encode([sample[k] for k in missing], normalize_embeddings=True,
                             batch_size=16, show_progress_bar=False)
        for k, v in zip(missing, vecs):
            cache[k] = np.asarray(v, dtype=np.float32)
        _save_vec_cache(short, cache)
    qv = np.asarray(_model.encode([prefix + query], normalize_embeddings=True)[0], dtype=np.float32)
    scored = []
    for i, k in enumerate(keys):
        sim = float(np.dot(cache[k], qv))
        if math.isnan(sim) or math.isinf(sim):
            sim = 0.0
        scored.append((i, round(sim, 6)))
    scored.sort(key=lambda kv: (-kv[1], kv[0]))
    return scored


# —— v73：块向量缓存（按模型分文件；内容哈希 → 向量） ——

def _cache_path(short: str) -> str:
    return os.path.join(HERE, "data", "rag", f"emb_cache_{short}.npz")


def _load_vec_cache(short: str) -> dict:
    global _VEC_CACHE
    if _VEC_CACHE is not None and _VEC_CACHE[0] == short:
        return _VEC_CACHE[1]
    cache = {}
    try:
        import numpy as np
        with np.load(_cache_path(short), allow_pickle=False) as z:
            mat = z["vecs"]
            for i, k in enumerate([str(x) for x in z["keys"]]):
                cache[k] = mat[i]
    except Exception:  # noqa: BLE001 — 文件缺失/损坏 → 空缓存重建
        cache = {}
    _VEC_CACHE = (short, cache)
    return cache


def _save_vec_cache(short: str, cache: dict) -> None:
    try:
        import numpy as np
        os.makedirs(os.path.dirname(_cache_path(short)), exist_ok=True)
        ks = list(cache.keys())
        mat = np.stack([cache[k] for k in ks]) if ks else np.zeros((0, 1), dtype=np.float32)
        tmp = _cache_path(short) + ".tmp.npz"
        np.savez_compressed(tmp, keys=np.array(ks), vecs=mat)
        os.replace(tmp, _cache_path(short))
    except Exception:  # noqa: BLE001 — 缓存写失败只影响下次检索速度，不影响正确性
        pass
