# -*- coding: utf-8 -*-
"""长期情景记忆：向量库双 collection（病例级 + 医生级）与内存假实现。

边界（计划 §3.3）：情景只作参考，绝不回写当前病例事实；写入前已去标识；
Milvus/BGE 任一不可用 → 抛错由 worker/装配层降级并审计，不伪造召回。
"""
import time

from . import config


def _bigrams(text: str) -> set:
    s = "".join(str(text or "").split())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _dice(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return round(2 * len(a & b) / (len(a) + len(b)), 6)


class InMemoryEpisodic:
    """单测/无向量环境：词法召回，真实扫描内存切片（不伪造语义分数之外的结论）。"""

    def __init__(self, lexical: bool = True):
        self.chunks: dict[str, dict] = {}
        self.lexical = lexical

    def health(self) -> dict:
        return {"ok": True, "latency_ms": 0,
                "detail": f"in-memory({'lexical' if self.lexical else 'exact'})"}

    def write(self, chunks: list) -> int:
        n = 0
        for chunk in chunks or []:
            cid = str(chunk.get("chunk_id") or "")
            if not cid:
                continue
            row = dict(chunk)
            row.setdefault("created_at", int(time.time()))
            self.chunks[cid] = row
            n += 1
        return n

    def recall(self, query: str, patient_id: str = "", case_id: str = "",
               exclude_msg_ids=None, top_k_case: int = 3, top_k_doctor: int = 3,
               threshold: float = 0.35, max_injected: int = 5) -> list:
        excluded = set(exclude_msg_ids or [])
        q = _bigrams(query)
        scored = []
        for chunk in self.chunks.values():
            if str(chunk.get("msg_id") or "") in excluded:
                continue
            scope = str(chunk.get("scope") or "case")
            if scope == "case" and case_id and chunk.get("case_id") != case_id:
                continue
            score = _dice(q, _bigrams(chunk.get("text"))) if self.lexical else 1.0
            if score < threshold:
                continue
            scored.append((score, chunk))
        scored.sort(key=lambda kv: -kv[0])
        case_hits = [(s, c) for s, c in scored
                     if c.get("scope") == "case"][:max(0, top_k_case)]
        doctor_hits = [(s, c) for s, c in scored
                       if c.get("scope") != "case"][:max(0, top_k_doctor)]
        out, seen = [], set()
        for score, chunk in sorted(case_hits + doctor_hits, key=lambda kv: -kv[0]):
            cid = chunk.get("chunk_id")
            if cid in seen:
                continue
            seen.add(cid)
            out.append({**{k: chunk.get(k) for k in
                           ("chunk_id", "scope", "case_id", "patient_id", "msg_id",
                            "title", "text", "created_at")}, "score": score})
            if len(out) >= max_injected:
                break
        return out

    def delete(self, case_id: str = "", patient_id: str = "") -> int:
        doomed = [cid for cid, chunk in self.chunks.items()
                  if (case_id and chunk.get("case_id") == case_id and chunk.get("scope") == "case")
                  or (patient_id and chunk.get("patient_id") == patient_id)]
        for cid in doomed:
            self.chunks.pop(cid, None)
        return len(doomed)

    def count(self, case_id: str = "", patient_id: str = "") -> int:
        return sum(1 for chunk in self.chunks.values()
                   if (not case_id or (chunk.get("case_id") == case_id
                                       and chunk.get("scope") == "case"))
                   and (not patient_id or chunk.get("patient_id") == patient_id))


class MilvusEpisodic:
    """Milvus Standalone 双 collection；写入/检索全部使用固定 768 维专用模型。"""
    alias = "derm_memory"

    def __init__(self, uri: str = None, token: str = None, model: str = None,
                 dim: int = None, case_collection: str = None, doctor_collection: str = None):
        rule = config.episodic_rule()
        self.uri = uri or config.milvus_uri()
        self.token = token if token is not None else config.milvus_token()
        self.model = model or config.embed_model()
        self.dim = int(dim or rule.get("dim") or 768)
        self.case_collection = case_collection or rule.get("case_collection") or "derm_episodic_case_v1"
        self.doctor_collection = doctor_collection or rule.get("doctor_collection") or "derm_episodic_doctor_v1"
        self._connected = False
        self._loaded = False
        self._error = ""

    # —— 连接/建表 ——
    def _pymilvus(self):
        import pymilvus
        return pymilvus

    def connect(self):
        if self._connected:
            return
        pm = self._pymilvus()
        kw = {"alias": self.alias, "uri": self.uri, "timeout": 3}
        if self.token:
            kw["token"] = self.token
        pm.connections.connect(**kw)
        self._connected = True
        self._ensure_collections()

    def _schema(self, pm):
        fields = [
            pm.FieldSchema(name="chunk_id", dtype=pm.DataType.VARCHAR, is_primary=True, max_length=64),
            pm.FieldSchema(name="scope", dtype=pm.DataType.VARCHAR, max_length=8),
            pm.FieldSchema(name="case_id", dtype=pm.DataType.VARCHAR, max_length=16),
            pm.FieldSchema(name="patient_id", dtype=pm.DataType.VARCHAR, max_length=16),
            pm.FieldSchema(name="doctor_id", dtype=pm.DataType.VARCHAR, max_length=16),
            pm.FieldSchema(name="msg_id", dtype=pm.DataType.VARCHAR, max_length=64),
            pm.FieldSchema(name="task_type", dtype=pm.DataType.VARCHAR, max_length=32),
            pm.FieldSchema(name="title", dtype=pm.DataType.VARCHAR, max_length=64),
            pm.FieldSchema(name="text", dtype=pm.DataType.VARCHAR, max_length=4096),
            pm.FieldSchema(name="generalizable", dtype=pm.DataType.BOOL),
            pm.FieldSchema(name="created_at", dtype=pm.DataType.INT64),
            pm.FieldSchema(name="vector", dtype=pm.DataType.FLOAT_VECTOR, dim=self.dim),
        ]
        return pm.CollectionSchema(fields=fields, description="DermAgent episodic memory v1")

    def _ensure_collections(self):
        pm = self._pymilvus()
        for name in (self.case_collection, self.doctor_collection):
            if pm.utility.has_collection(name, using=self.alias):
                continue
            col = pm.Collection(name, schema=self._schema(pm), using=self.alias)
            col.create_index(field_name="vector", index_params={
                "metric_type": "COSINE", "index_type": "HNSW",
                "params": {"M": 16, "efConstruction": 200}})
        self._loaded = False

    def _collection(self, name: str):
        pm = self._pymilvus()
        self.connect()
        if not self._loaded:
            for coll in (self.case_collection, self.doctor_collection):
                pm.Collection(coll, using=self.alias).load()
            self._loaded = True
        return pm.Collection(name, using=self.alias)

    # —— 写 ——
    def write(self, chunks: list) -> int:
        chunks = [c for c in (chunks or []) if isinstance(c, dict) and c.get("chunk_id")]
        if not chunks:
            return 0
        from rag_embed import embed_specific  # 延迟导入：无 sentence-transformers 也能加载本模块
        vecs = embed_specific([str(c.get("text") or "")[:4096] for c in chunks], self.model)
        if vecs is None:
            raise RuntimeError(f"embedding 不可用（模型 {self.model} 未安装或编码失败）")
        if any(len(v) != self.dim for v in vecs):
            raise RuntimeError(f"向量维度不匹配：期望 {self.dim}")
        by_scope = {"case": [], "doctor": []}
        for chunk, vec in zip(chunks, vecs):
            by_scope["doctor" if chunk.get("scope") == "doctor" else "case"].append((chunk, vec))
        now = int(time.time())
        written = 0
        for scope, pairs in by_scope.items():
            if not pairs:
                continue
            name = self.doctor_collection if scope == "doctor" else self.case_collection
            col = self._collection(name)
            rows = [row for row, _ in pairs]
            vectors = [vec for _, vec in pairs]
            col.upsert([[
                str(row["chunk_id"]), str(row.get("scope") or scope)[:8],
                str(row.get("case_id") or "")[:16], str(row.get("patient_id") or "")[:16],
                str(row.get("doctor_id") or "local")[:16], str(row.get("msg_id") or "")[:64],
                str(row.get("task_type") or "")[:32], str(row.get("title") or "")[:64],
                str(row.get("text") or "")[:4096], bool(row.get("generalizable")),
                int(row.get("created_at") or now),
            ] for row in rows] + [vectors])
            written += len(rows)
        return written

    # —— 读 ——
    def _search(self, name: str, vec: list, expr: str, limit: int):
        col = self._collection(name)
        hits = col.search(data=[vec], anns_field="vector",
                          param={"metric_type": "COSINE", "params": {"ef": 64}},
                          limit=max(1, int(limit)), expr=expr,
                          output_fields=["chunk_id", "scope", "case_id", "patient_id",
                                         "msg_id", "title", "text", "created_at"])
        out = []
        for hit in (hits[0] if hits else []):
            entity = hit.entity
            out.append({"chunk_id": entity.get("chunk_id"), "scope": entity.get("scope"),
                        "case_id": entity.get("case_id"), "patient_id": entity.get("patient_id"),
                        "msg_id": entity.get("msg_id"), "title": entity.get("title"),
                        "text": entity.get("text"), "created_at": entity.get("created_at"),
                        "score": round(float(hit.distance), 6)})
        return out

    def recall(self, query: str, patient_id: str = "", case_id: str = "",
               exclude_msg_ids=None, top_k_case: int = 3, top_k_doctor: int = 3,
               threshold: float = 0.35, max_injected: int = 5) -> list:
        from rag_embed import embed_specific
        vec = embed_specific([str(query or "")[:400]], self.model)
        if not vec:
            raise RuntimeError("query embedding 不可用")
        excluded = [str(x) for x in (exclude_msg_ids or []) if str(x)]
        clause = [""]
        if excluded:
            ids = ",".join('"' + x.replace('"', "")[:64] + '"' for x in excluded)
            clause.append(f"msg_id not in [{ids}]")
        case_expr = (" and ".join(x for x in
                                 ([f'case_id == "{str(case_id)[:16]}"'] if case_id else []) + clause
                                 if x) or None)
        doctor_expr = " and ".join(x for x in clause if x) or None
        case_hits = self._search(self.case_collection, vec[0], case_expr, top_k_case) if case_id else []
        doctor_hits = self._search(self.doctor_collection, vec[0], doctor_expr, top_k_doctor)
        merged, seen = [], set()
        for hit in sorted(case_hits + doctor_hits, key=lambda h: -h.get("score", 0)):
            if hit.get("score", 0) < threshold or hit.get("chunk_id") in seen:
                continue
            seen.add(hit.get("chunk_id"))
            merged.append(hit)
            if len(merged) >= max_injected:
                break
        return merged

    # —— 删除 / 健康 ——
    def delete(self, case_id: str = "", patient_id: str = "") -> int:
        exprs = []
        if case_id:
            exprs.append((self.case_collection, f'case_id == "{str(case_id)[:16]}"'))
        if patient_id:
            exprs.append((self.case_collection, f'patient_id == "{str(patient_id)[:16]}"'))
            exprs.append((self.doctor_collection, f'patient_id == "{str(patient_id)[:16]}"'))
        removed = 0
        for name, expr in exprs:
            col = self._collection(name)
            col.delete(expr)
            removed += 1
        return removed

    def count(self, case_id: str = "", patient_id: str = "") -> int:
        total = 0
        for name in (self.case_collection, self.doctor_collection):
            col = self._collection(name)
            expr = ""
            if case_id and name == self.case_collection:
                expr = f'case_id == "{str(case_id)[:16]}"'
            elif patient_id:
                expr = f'patient_id == "{str(patient_id)[:16]}"'
            if not expr:
                total += int(col.num_entities)
                continue
            rows = col.query(expr=expr, output_fields=["count(*)"], limit=1)
            total += int(rows[0]["count(*)"]) if rows else 0
        return total

    def health(self) -> dict:
        t0 = time.perf_counter()
        try:
            self.connect()
            self._collection(self.case_collection)
            return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": f"milvus {self.uri}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": str(exc)[:160]}
