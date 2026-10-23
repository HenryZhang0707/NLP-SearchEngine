"""同一套索引验收测试：DiskIndex 与 MemoryIndex 都必须过。

用法：
    测试代码里：check_reader(reader, corpus)
    命令行：    python -m contracts.conformance fixtures/tiny_index fixtures/tiny_corpus.jsonl

检查内容：
    不变量：postings 按 docid 升序、positions 严格递增、tf == len(positions)、
            df == len(docs)、doc_length == token 数、offsets 能回原文切出原词；
    一致性：reader 的 postings / doc_length / avgdl 与同一语料现场重建的期望完全一致。
"""

import json
import sys
from dataclasses import dataclass, field

from . import analyzer


@dataclass
class ExpectedIndex:
    """从语料现场重建的期望索引（oracle）。MemoryIndex 也复用这个构建逻辑。"""

    postings: dict = field(default_factory=dict)   # term -> [[docid, tf, [pos...]], ...]
    doc_lengths: dict = field(default_factory=dict)
    docs: dict = field(default_factory=dict)       # docid -> (doc_id, title, text, spans)
    avgdl: float = 0.0

    @property
    def n_docs(self) -> int:
        return len(self.docs)


def build_expected(corpus: list) -> ExpectedIndex:
    """corpus: [{"doc_id", "title", "text"}, ...]，docid 按行号 0..n-1。"""
    exp = ExpectedIndex()
    for docid, row in enumerate(corpus):
        tokens = analyzer.analyze(row["text"])
        exp.doc_lengths[docid] = len(tokens)
        spans = [[i, i + 1, t.start, t.end] for i, t in enumerate(tokens)]
        exp.docs[docid] = (row["doc_id"], row.get("title", ""), row["text"], spans)
        per_term: dict = {}
        for i, t in enumerate(tokens):
            per_term.setdefault(t.term, []).append(i)
        for term, positions in per_term.items():
            exp.postings.setdefault(term, []).append([docid, len(positions), positions])
    total = sum(exp.doc_lengths.values())
    exp.avgdl = total / len(exp.docs) if exp.docs else 0.0
    return exp


def _check_invariants(reader) -> None:
    assert reader.n_docs > 0, "索引为空"
    for term in reader.vocab():
        pl = reader.postings(term)
        assert pl is not None, f"vocab 里的词 {term!r} 取不到 postings"
        docids = [d for d, _, _ in pl]
        assert docids == sorted(docids), f"{term!r} 的 postings 未按 docid 升序"
        assert len(pl) == reader.df(term), f"{term!r} 的 df 与 postings 长度不一致"
        for docid, tf, positions in pl:
            assert tf == len(positions), f"{term!r}@doc{docid}: tf != len(positions)"
            assert positions == sorted(set(positions)), (
                f"{term!r}@doc{docid}: positions 非严格递增"
            )
    lens = [reader.doc_length(d) for d in range(reader.n_docs)]
    assert all(n > 0 for n in lens), "存在 doc_length 为 0 的文档"
    assert abs(sum(lens) / len(lens) - reader.avgdl) < 1e-6, "avgdl 与 doc_lengths 不一致"


def _check_offsets_roundtrip(reader) -> None:
    """offsets 必须能用 text[start:end] 回切出原词（高亮功能的根基）。"""
    for docid in range(reader.n_docs):
        doc = reader.doc(docid)
        assert len(doc.offsets) == reader.doc_length(docid), (
            f"doc{docid}: offsets 数 != token 数"
        )
        for cs, ce in doc.offsets:
            assert 0 <= cs < ce <= len(doc.text), f"doc{docid}: 偏移越界 ({cs},{ce})"
    # 词形核对：拿 postings 里的词回原文找一次
    for term in reader.vocab():
        for docid, _, positions in reader.postings(term):
            doc = reader.doc(docid)
            for pos in positions:
                cs, ce = doc.offsets[pos]
                piece = doc.text[cs:ce]
                assert piece == term or piece.lower() == term, (
                    f"doc{docid} token{pos}: 原文 {piece!r} != 词项 {term!r}"
                )


def _check_matches_expected(reader, corpus: list) -> None:
    exp = build_expected(corpus)
    assert reader.n_docs == exp.n_docs, f"n_docs: {reader.n_docs} != {exp.n_docs}"
    assert abs(reader.avgdl - exp.avgdl) < 1e-6, "avgdl 与期望不一致"
    for docid in range(exp.n_docs):
        assert reader.doc_length(docid) == exp.doc_lengths[docid], f"doc{docid} 长度不符"
        doc_id, title, text, spans = exp.docs[docid]
        doc = reader.doc(docid)
        assert doc.doc_id == doc_id and doc.title == title and doc.text == text, (
            f"doc{docid} 原文不符"
        )
        got = [[i, i + 1, cs, ce] for i, (cs, ce) in enumerate(doc.offsets)]
        assert got == spans, f"doc{docid} offsets 不符"
    assert set(reader.vocab()) == set(exp.postings), "词表与期望不一致"
    for term, want in exp.postings.items():
        got = reader.postings(term)
        assert got is not None, f"缺词 {term!r}"
        assert [list(p) for p in got] == want, f"词 {term!r} 的 postings 与期望不一致"


def check_reader(reader, corpus: list) -> None:
    """任何 IndexReader 实现都必须通过的全部检查。断言失败即不过。"""
    _check_invariants(reader)
    _check_offsets_roundtrip(reader)
    _check_matches_expected(reader, corpus)


def _load_corpus(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("用法: python -m contracts.conformance <index_dir> <corpus.jsonl>")
    from offline.disk_index import DiskIndex

    reader = DiskIndex(sys.argv[1])
    check_reader(reader, _load_corpus(sys.argv[2]))
    print(f"conformance 通过：{sys.argv[1]}（{reader.n_docs} 篇文档，"
          f"{len(list(reader.vocab()))} 个词项）")
