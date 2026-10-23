"""MemoryIndex：在线组的 dummy 数据库，实现 IndexReader 协议。

不碰任何文件格式，直接用 dict 手工构造，用于在线组独立开发和单元测试。
构建逻辑复用 contracts.conformance.build_expected，保证与离线产物天然一致。
"""

from typing import Iterable, Optional

from contracts.conformance import build_expected
from contracts.reader import Document


class MemoryIndex:
    def __init__(self, exp, dataset: str = "memory"):
        self._exp = exp
        self.n_docs = exp.n_docs
        self.avgdl = exp.avgdl
        self._dataset = dataset

    @classmethod
    def from_corpus(cls, corpus: list, dataset: str = "memory") -> "MemoryIndex":
        """corpus: [{"doc_id", "title", "text"}, ...]"""
        return cls(build_expected(corpus), dataset)

    def postings(self, term: str) -> Optional[list]:
        pl = self._exp.postings.get(term)
        if pl is None:
            return None
        return [(docid, tf, positions) for docid, tf, positions in pl]

    def df(self, term: str) -> int:
        return len(self._exp.postings.get(term, []))

    def doc_length(self, docid: int) -> int:
        return self._exp.doc_lengths[docid]

    def doc(self, docid: int) -> Document:
        doc_id, title, text, spans = self._exp.docs[docid]
        return Document(
            docid=docid, doc_id=doc_id, title=title, text=text,
            offsets=[(cs, ce) for _, _, cs, ce in spans],
        )

    def vocab(self) -> Iterable[str]:
        return self._exp.postings.keys()

    def manifest(self) -> dict:
        return {
            "schema_version": 1,
            "dataset": self._dataset,
            "n_docs": self.n_docs,
            "avgdl": self.avgdl,
            "analyzer": {"name": "memory", "version": "0", "config_hash": "-"},
            "files": {},
        }
