"""打分器注册点（在线组）。

新增打分器 = 在这里多注册一个函数，不改其他代码。
骨架版实现了 BM25 与 TF-IDF（暴力可对照版），参数对齐
MTEB 官方基线常用值 k1=1.2, b=0.75；E2 复现官方实现时再调 k1/b。
"""

import math
from collections import defaultdict

from contracts.index_format import BM25Spec
from contracts.reader import IndexReader


def bm25(reader: IndexReader, terms: list, k1: float | None = None, b: float | None = None,
         match_all: bool = False, candidate_docs: set[int] | None = None) -> dict:
    """返回 {docid: score}；自然语言查询默认词项并集，硬约束由过滤层处理。"""
    spec = BM25Spec(**reader.manifest().get("bm25", {}))
    k1 = spec.k1 if k1 is None else k1
    b = spec.b if b is None else b
    BM25Spec(k1=k1, b=b)  # 同时校验显式覆盖参数
    cand = _candidate_docs(reader, terms, match_all) if candidate_docs is None else set(candidate_docs)
    scores = defaultdict(float)
    n = reader.n_docs
    for term in terms:
        df = reader.df(term)
        if df == 0:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for docid, tf, _ in reader.postings(term):
            if docid not in cand:
                continue
            dl = reader.doc_length(docid)
            scores[docid] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / reader.avgdl))
    return dict(scores)


def tfidf(reader: IndexReader, terms: list, match_all: bool = False,
          candidate_docs: set[int] | None = None) -> dict:
    """兼容的 TF-IDF 基线：tf 取 1+log，idf 取 log(N/df)。"""
    cand = _candidate_docs(reader, terms, match_all) if candidate_docs is None else set(candidate_docs)
    scores = defaultdict(float)
    n = reader.n_docs
    for term in terms:
        df = reader.df(term)
        if df == 0:
            continue
        idf = math.log(n / df)
        for docid, tf, _ in reader.postings(term):
            if docid not in cand:
                continue
            scores[docid] += (1 + math.log(tf)) * idf
    return dict(scores)


def _candidate_docs(reader: IndexReader, terms: list, match_all: bool = False) -> set:
    """普通查询取词项并集；仅显式要求 match_all 时取交集。"""
    sets = []
    for t in terms:
        pl = reader.postings(t)
        if pl is None:
            if match_all:
                return set()
            continue
        sets.append({docid for docid, _, _ in pl})
    if not sets:
        return set()
    return set.intersection(*sets) if match_all else set.union(*sets)


def phrase_docs(reader: IndexReader, terms: list[str]) -> set[int]:
    """Return documents containing ``terms`` at consecutive token positions."""
    if not terms:
        return set()
    postings = [reader.postings(term) for term in terms]
    if any(not posting for posting in postings):
        return set()
    positions = [{docid: set(pos) for docid, _, pos in posting} for posting in postings]
    common = set(positions[0])
    for mapping in positions[1:]:
        common.intersection_update(mapping)
    matched = set()
    for docid in common:
        starts = positions[0][docid]
        if any(all(start + offset in positions[offset][docid]
                   for offset in range(1, len(positions))) for start in starts):
            matched.add(docid)
    return matched


def boolean_docs(reader: IndexReader, node: dict, analyze) -> set[int]:
    """Evaluate the parser AST as a hard lexical filter."""
    op = node["op"]
    if op == "term":
        terms = [token.term for token in analyze(node["value"])]
        return _candidate_docs(reader, terms, match_all=False)
    if op == "phrase":
        terms = [token.term for token in analyze(node["value"])]
        return phrase_docs(reader, terms)
    if op == "not":
        return set(range(reader.n_docs)) - boolean_docs(reader, node["child"], analyze)
    children = [boolean_docs(reader, child, analyze) for child in node["children"]]
    if not children:
        return set()
    if op == "and":
        result = children[0]
        for child in children[1:]:
            result = result & child
        return result
    result = set()
    for child in children:
        result |= child
    return result


SCORERS = {"bm25": bm25, "tfidf": tfidf}
