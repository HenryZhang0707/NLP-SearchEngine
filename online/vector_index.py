"""在线向量库读取接口的测试替身与生产适配器桩。只读，不负责建库。"""

import math
from typing import Sequence

from contracts.retrieval import (
    RetrievalUnavailable, VectorHit, VectorManifest, VectorRecord, validate_vector,
)


class MemoryVectorIndex:
    """精确余弦全扫，仅用于小样本契约测试，不是 HNSW 性能替代品。"""

    def __init__(self, manifest: VectorManifest, records: list[VectorRecord]):
        if manifest.backend != "memory-exact":
            raise ValueError("内存替身必须显式标为 memory-exact")
        if len(records) != manifest.n_docs:
            raise ValueError("向量记录数与 manifest 不符")
        if sorted(r.docid for r in records) != list(range(manifest.n_docs)):
            raise ValueError("内部 docid 必须唯一、连续且与倒排索引对齐")
        if len({r.doc_id for r in records}) != len(records):
            raise ValueError("外部 doc_id 必须唯一")
        for record in records:
            validate_vector(record.vector, manifest.embedding)
        self._manifest = manifest
        self._records = tuple(VectorRecord(r.docid, r.doc_id, tuple(r.vector)) for r in records)

    def manifest(self) -> VectorManifest:
        return self._manifest

    def search(self, query_vector: Sequence[float], limit: int) -> list[VectorHit]:
        validate_vector(query_vector, self._manifest.embedding)
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        hits = [VectorHit(r.docid, r.doc_id,
                          max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(query_vector, r.vector)))))
                for r in self._records]
        return sorted(hits, key=lambda h: (-h.score, h.docid))[:limit]


class QdrantVectorIndex:
    """在线组待实现：加载离线快照并只读查询，暴露 VectorReader。

    接入时必须校验 collection 元数据与 manifest；不得接受任意同维度库。
    """

    def __init__(self, manifest: VectorManifest, url: str):
        raise RetrievalUnavailable("TODO: 接入 Qdrant 只读查询适配器")

    def manifest(self) -> VectorManifest:
        raise RetrievalUnavailable("Qdrant 适配器尚未实现")

    def search(self, query_vector: Sequence[float], limit: int) -> list[VectorHit]:
        raise RetrievalUnavailable("Qdrant 适配器尚未实现")
