"""E0 验收测试：pytest 可直接跑，无 pytest 时用 `python tests/run.py`。

覆盖：
- MemoryIndex / DiskIndex 过同一套 conformance（契约的核心承诺）
- manifest 被篡改 / 分词器不一致时 DiskIndex 拒绝加载
- 引擎端到端：查询 -> BM25 -> Top-K -> 同分 docid 升序
- 接口 B 的请求校验与解析器错误行为
"""

import json
import tempfile
from pathlib import Path

from contracts import analyzer
from contracts.api import ApiError, SearchRequest
from contracts.conformance import check_reader
from offline.build import build
from offline.disk_index import DiskIndex
from online.engine import SearchEngine
from online.memory_index import MemoryIndex
from online.parser import QuerySyntaxError, parse

ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = ROOT / "fixtures" / "tiny_corpus.jsonl"


def _corpus() -> list:
    with open(CORPUS_PATH, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def test_memory_index_conformance():
    check_reader(MemoryIndex.from_corpus(_corpus()), _corpus())


def test_disk_index_conformance():
    with tempfile.TemporaryDirectory() as tmp:
        build(CORPUS_PATH, Path(tmp), "tiny")
        check_reader(DiskIndex(tmp), _corpus())


def test_build_is_deterministic():
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        ma = build(CORPUS_PATH, Path(a), "tiny")
        mb = build(CORPUS_PATH, Path(b), "tiny")
        assert ma.files == mb.files, "两次构建的 sha256 不一致"


def test_disk_index_rejects_stale_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        build(CORPUS_PATH, Path(tmp), "tiny")
        m = json.loads((Path(tmp) / "manifest.json").read_text(encoding="utf-8"))
        m["analyzer"]["config_hash"] = "0" * 64
        (Path(tmp) / "manifest.json").write_text(
            json.dumps(m, ensure_ascii=False), encoding="utf-8")
        try:
            DiskIndex(tmp)
        except ValueError:
            return
        raise AssertionError("分词器哈希不一致却没有拒绝加载")


def test_disk_index_rejects_malformed_rows_even_without_checksum_check():
    """结构校验不能把重复/越界 JSONL 行留到在线查询阶段才暴露。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build(CORPUS_PATH, root, "tiny")
        rows = [
            json.loads(line)
            for line in (root / "docs.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        rows[1]["docid"] = rows[0]["docid"]
        (root / "docs.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        try:
            DiskIndex(root, verify_checksums=False)
        except ValueError:
            return
        raise AssertionError("重复 docid 的索引未被启动校验拒绝")


def test_search_end_to_end():
    engine = SearchEngine(MemoryIndex.from_corpus(_corpus()))
    resp = engine.search(SearchRequest(query="心肌梗死 症状", k=5))
    assert resp.results, "查询'心肌梗死 症状'应有结果"
    query_terms = {t.term for t in analyzer.analyze("心肌梗死 症状")}
    for r in resp.results:
        text = next(d for d in _corpus() if d["doc_id"] == r.doc_id)["text"]
        assert query_terms & {t.term for t in analyzer.analyze(text)}, "普通查询取 OR 候选"
    scores = [r.score for r in resp.results]
    assert scores == sorted(scores, reverse=True), "结果未按分数降序"
    for r in resp.results:
        for h in r.highlights:
            assert 0 <= h.start < h.end, "高亮区间不合法"


def test_search_tiebreak_by_docid():
    engine = SearchEngine(MemoryIndex.from_corpus(_corpus()))
    resp = engine.search(SearchRequest(query="心肌梗死", k=10))
    internal_ids = {d["doc_id"]: i for i, d in enumerate(_corpus())}
    for left, right in zip(resp.results, resp.results[1:]):
        if left.score == right.score:
            assert internal_ids[left.doc_id] < internal_ids[right.doc_id]
    assert resp.to_dict()["results"] == engine.search(
        SearchRequest(query="心肌梗死", k=10)).to_dict()["results"]


def test_unknown_term_gives_empty():
    engine = SearchEngine(MemoryIndex.from_corpus(_corpus()))
    resp = engine.search(SearchRequest(query="zzzznotexist"))
    assert resp.results == []


def test_request_validation():
    try:
        SearchRequest.from_dict({"query": "x", "scorer": "lucene"})
    except ApiError:
        pass
    else:
        raise AssertionError("非法 scorer 未被拒绝")


def test_parser_supports_phrase_and_rejects_malformed_syntax():
    parsed = parse('"心肌梗死"')
    assert parsed["type"] == "boolean" and parsed["explicit_constraints"]
    assert parsed["ast"]["op"] == "phrase"
    assert parse("心肌 OR 症状")["explicit_constraints"] is True
    for query in ('"未闭合', '心肌 AND', '(症状', '心肌 OR )'):
        try:
            parse(query)
        except QuerySyntaxError:
            continue
        raise AssertionError(f"非法查询未被拒绝: {query!r}")
    assert parse("爱国诗人是( )")["terms"] == ["爱国诗人是"]


def test_boolean_filter_honors_and_not_and_phrase_positions():
    corpus = [
        {"doc_id": "one", "title": "", "text": "red apple fruit"},
        {"doc_id": "two", "title": "", "text": "red green apple"},
        {"doc_id": "three", "title": "", "text": "apple fruit"},
    ]
    engine = SearchEngine(MemoryIndex.from_corpus(corpus))
    assert [item.doc_id for item in engine.search(SearchRequest('"red apple"')).results] == ["one"]
    assert [item.doc_id for item in engine.search(SearchRequest("apple NOT green")).results] == ["three", "one"]
    assert [item.doc_id for item in engine.search(SearchRequest("red AND apple")).results] == ["one", "two"]
    assert [item.doc_id for item in engine.search(SearchRequest("red OR NOT apple")).results] == ["one", "two"]


def test_analyzer_offsets_roundtrip():
    text = "心肌梗死发作前常有 chest pain 等症状"
    for t in analyzer.analyze(text):
        piece = text[t.start:t.end]
        assert piece == t.term or piece.lower() == t.term


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
