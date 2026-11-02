# -*- coding: utf-8 -*-
"""KB chunk 向量库：Milvus 单集合（COSINE + HNSW），按 generation 原子切换。

与情景记忆共用 Milvus 实例，独立 alias/collection；任何失败抛错由检索层降级。
"""
import time

from . import config

DIM = 768
_OUT_FIELDS = ["chunk_id", "book_id", "generation", "chunk_index", "section",
               "section_path", "page", "page_hint", "content", "content_hash",
               "anchor_hash", "token_count", "book_title"]


class KBVectorStore:
    def __init__(self, uri: str = None, token: str = None, collection: str = None,
                 alias: str = None, dim: int = DIM):
        self.uri = uri or config.milvus_uri()
        self.token = token if token is not None else config.milvus_token()
        self.collection = collection or config.collection()
        self.alias = alias or config.milvus_alias()
        self.dim = int(dim or DIM)
        self._connected = False
        self._loaded = False

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
        self._ensure_collection()

    def _schema(self, pm):
        fields = [
            pm.FieldSchema(name="chunk_id", dtype=pm.DataType.VARCHAR, is_primary=True, max_length=96),
            pm.FieldSchema(name="book_id", dtype=pm.DataType.VARCHAR, max_length=16),
            pm.FieldSchema(name="generation", dtype=pm.DataType.INT32),
            pm.FieldSchema(name="chunk_index", dtype=pm.DataType.INT32),
            pm.FieldSchema(name="section", dtype=pm.DataType.VARCHAR, max_length=256),
            pm.FieldSchema(name="section_path", dtype=pm.DataType.VARCHAR, max_length=512),
            pm.FieldSchema(name="page", dtype=pm.DataType.INT32),
            pm.FieldSchema(name="page_hint", dtype=pm.DataType.INT32),
            pm.FieldSchema(name="content", dtype=pm.DataType.VARCHAR, max_length=8192),
            pm.FieldSchema(name="content_hash", dtype=pm.DataType.VARCHAR, max_length=32),
            pm.FieldSchema(name="anchor_hash", dtype=pm.DataType.VARCHAR, max_length=32),
            pm.FieldSchema(name="token_count", dtype=pm.DataType.INT32),
            pm.FieldSchema(name="book_title", dtype=pm.DataType.VARCHAR, max_length=256),
            pm.FieldSchema(name="vector", dtype=pm.DataType.FLOAT_VECTOR, dim=self.dim),
        ]
        return pm.CollectionSchema(fields=fields, description="DermAgent book chunks v1")

    def _ensure_collection(self):
        pm = self._pymilvus()
        if pm.utility.has_collection(self.collection, using=self.alias):
            return
        col = pm.Collection(self.collection, schema=self._schema(pm), using=self.alias)
        col.create_index(field_name="vector", index_params={
            "metric_type": "COSINE", "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 200}})
        self._loaded = False

    def _collection(self):
        pm = self._pymilvus()
        self.connect()
        col = pm.Collection(self.collection, using=self.alias)
        if not self._loaded:
            col.load()
            self._loaded = True
        return col

    def upsert(self, chunks: list, vectors: list) -> int:
        if not chunks:
            return 0
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与向量数量不一致")
        if any(len(v) != self.dim for v in vectors):
            raise ValueError(f"向量维度不匹配：期望 {self.dim}")
        col = self._collection()
        rows = []
        for chunk in chunks:
            rows.append([
                str(chunk["chunk_id"])[:96],
                str(chunk.get("book_id") or "")[:16],
                int(chunk.get("generation") or 0),
                int(chunk.get("chunk_index") or 0),
                str(chunk.get("section") or "")[:256],
                str(chunk.get("section_path") or "")[:512],
                int(chunk.get("page") or 0),
                int(chunk.get("page_hint") or 0),
                str(chunk.get("content") or "")[:8192],
                str(chunk.get("content_hash") or "")[:32],
                str(chunk.get("anchor_hash") or "")[:32],
                int(chunk.get("token_count") or 0),
                str(chunk.get("book_title") or "")[:256],
            ])
        # ORM upsert 收列式数据：14 个字段各一列，长度均为 chunk 数
        columns = [list(field) for field in zip(*rows)]
        columns.append([[float(x) for x in vec] for vec in vectors])
        col.upsert(columns)
        return len(chunks)

    def search(self, vector: list, limit: int = 200, book_id: str = None) -> list:
        if not vector or len(vector) != self.dim:
            raise ValueError("query 向量不可用")
        col = self._collection()
        expr = f'book_id == "{str(book_id)[:16]}"' if book_id else None
        hits = col.search(data=[list(vector)], anns_field="vector",
                          param={"metric_type": "COSINE", "params": {"ef": 64}},
                          limit=max(1, int(limit)), expr=expr,
                          output_fields=_OUT_FIELDS)
        out = []
        for hit in (hits[0] if hits else []):
            entity = hit.entity
            row = {field: entity.get(field) for field in _OUT_FIELDS}
            row["vector_score"] = round(float(hit.distance), 6)
            out.append(row)
        return out

    def delete_book(self, book_id: str) -> None:
        col = self._collection()
        col.delete(f'book_id == "{str(book_id)[:16]}"')
        col.flush()          # 删除立刻对检索可见，避免一致性窗口内召回已删 chunk

    def delete_generation(self, book_id: str, generation: int) -> None:
        col = self._collection()
        col.delete(f'book_id == "{str(book_id)[:16]}" and generation == {int(generation)}')
        col.flush()

    def count(self, book_id: str = None) -> int:
        col = self._collection()
        col.flush()
        expr = f'book_id == "{str(book_id)[:16]}"' if book_id else 'chunk_id != ""'
        rows = col.query(expr=expr, output_fields=["count(*)"])
        return int(rows[0]["count(*)"]) if rows else 0

    def count_generation(self, book_id: str, generation: int) -> int:
        col = self._collection()
        col.flush()          # 计数前落盘，避免 Bounded 一致性读到未可见数据
        expr = f'book_id == "{str(book_id)[:16]}" and generation == {int(generation)}'
        # Milvus 2.6 禁止 count(*) 与分页 limit 同用
        rows = col.query(expr=expr, output_fields=["count(*)"])
        return int(rows[0]["count(*)"]) if rows else 0

    def generations(self, book_id: str) -> list:
        col = self._collection()
        rows = col.query(expr=f'book_id == "{str(book_id)[:16]}"',
                         output_fields=["generation"], limit=16384)
        return sorted({int(r.get("generation") or 0) for r in rows})

    def health(self) -> dict:
        t0 = time.perf_counter()
        try:
            self._collection()
            return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": f"milvus {self.uri}/{self.collection}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "detail": str(exc)[:160]}
