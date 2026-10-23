"""DiskIndex：读离线组产出的索引目录，实现 IndexReader 协议。

加载时的两道闸门：
1. manifest 里 analyzer.config_hash 必须等于当前 contracts.analyzer 的配置哈希，
   否则拒绝启动（并行期 90% 的"查不到东西"都是分词器不一致）；
2. 各文件 sha256 与 manifest 一致（可选关闭，CI 冒烟时省时间）。

骨架版直接全量读进内存；10 万文档的性能优化（平坦化 numpy postings、
按需加载）是性能联调阶段的事，接口不变。
"""

import json
from pathlib import Path
from typing import Iterable, Optional

from contracts import analyzer
from contracts.index_format import (
    DOCS, OFFSETS, POSTINGS, STATS, load_manifest, verify_files,
)
from contracts.reader import Document


class DiskIndex:
    def __init__(self, index_dir, verify_checksums: bool = True):
        index_dir = Path(index_dir)
        self._manifest = load_manifest(index_dir)
        if self._manifest.analyzer_hash != analyzer.config_hash():
            raise ValueError(
                "分词器配置不一致：索引由 "
                f"{self._manifest.analyzer_name}@{self._manifest.analyzer_version} 构建，"
                f"当前 analyzer 为 {analyzer.NAME}@{analyzer.VERSION}。"
                "请确认两侧 contracts/ 一致后重建索引。"
            )
        if verify_checksums:
            verify_files(index_dir, self._manifest)

        raw_docs: dict = {}
        with open(index_dir / DOCS, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                raw_docs[d["docid"]] = d
        offsets: dict = {}
        with open(index_dir / OFFSETS, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                offsets[d["docid"]] = [(cs, ce) for _, _, cs, ce in d["spans"]]
        self._docs: dict = {
            docid: Document(
                docid=docid, doc_id=d["doc_id"], title=d["title"],
                text=d["text"], offsets=offsets.get(docid, []),
            )
            for docid, d in raw_docs.items()
        }
        self._postings: dict = {}
        with open(index_dir / POSTINGS, encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                self._postings[d["term"]] = [
                    (docid, tf, positions) for docid, tf, positions in d["docs"]
                ]
        with open(index_dir / STATS, encoding="utf-8") as f:
            stats = json.load(f)
        self._doc_lengths = {int(k): v for k, v in stats["doc_lengths"].items()}
        self.n_docs = self._manifest.n_docs
        self.avgdl = self._manifest.avgdl

    def postings(self, term: str) -> Optional[list]:
        return self._postings.get(term)

    def df(self, term: str) -> int:
        pl = self._postings.get(term)
        return len(pl) if pl else 0

    def doc_length(self, docid: int) -> int:
        return self._doc_lengths[docid]

    def doc(self, docid: int) -> Document:
        return self._docs[docid]

    def vocab(self) -> Iterable[str]:
        return self._postings.keys()

    def manifest(self) -> dict:
        return self._manifest.to_dict()
