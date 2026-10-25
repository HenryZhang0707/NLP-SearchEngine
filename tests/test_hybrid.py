"""双索引、四种检索模式和 HTTP 契约测试。纯标准库，不下载模型/不启动数据库。"""

import json
import math
import tempfile
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from contracts.api import (
    ApiError, MODES, REQUEST_JSON_SCHEMA, RESPONSE_JSON_SCHEMA, SearchRequest,
)
from contracts.index_format import BM25Spec
from contracts.retrieval import (
    EmbeddingSpec, RetrievalConfig, RetrievalUnavailable, VectorHit, VectorManifest,
    VectorRecord, corpus_fingerprint, validate_vector,
)
from offline.build import build
from offline.build_hybrid import build_hybrid
from offline.disk_index import DiskIndex
from offline.vector_build import build_vectors
from online.engine import SearchEngine
from online.fusion import rrf
from online.memory_index import MemoryIndex
from online.parser import QuerySyntaxError
from online.router import QueryFeatures, RuleRouter
from online.scorers import bm25
from online.server import make_handler
from online.vector_index import MemoryVectorIndex

CORPUS = [
    {"doc_id": "z", "title": "", "text": "phone water"},
    {"doc_id": "a", "title": "", "text": "device liquid"},
    {"doc_id": "b", "title": "", "text": "camera lens"},
]


class FakeEncoder:
    """测试向量是手工映射，不表示真实 embedding 或真实检索质量。"""
    spec = EmbeddingSpec("test-only", "fixture-v1", dimension=2)

    def __init__(self):
        self.query_calls = 0
        self.document_calls = 0

    def encode_documents(self, texts):
        self.document_calls += 1
        return [[0.0, 1.0] if "camera" in text else [1.0, 0.0] for text in texts]

    def encode_query(self, query):
        self.query_calls += 1
        return [0.0, 1.0] if "camera" in query else [1.0, 0.0]


class RecordingWriter:
    def replace(self, manifest, records):
        self.records = list(records)
        self.metadata = manifest
        return manifest


def _setup(config=None):
    reader = MemoryIndex.from_corpus(CORPUS)
    encoder = FakeEncoder()
    metadata = VectorManifest(corpus_fingerprint(reader), reader.n_docs, encoder.spec,
                              "test-fixture", backend="memory-exact")
    records = [VectorRecord(i, d["doc_id"], v) for i, (d, v) in enumerate(zip(
        CORPUS, encoder.encode_documents([d["text"] for d in CORPUS])))]
    vectors = MemoryVectorIndex(metadata, records)
    return SearchEngine(reader, encoder, vectors, config), encoder, vectors


def _raises(kind, fn):
    try:
        fn()
    except kind:
        return
    raise AssertionError(f"应抛出 {kind.__name__}")


def test_vector_manifest_roundtrip():
    _, _, vectors = _setup()
    assert VectorManifest.from_dict(vectors.manifest().to_dict()) == vectors.manifest()


def test_vector_build_batches_and_publishes_manifest():
    reader = MemoryIndex.from_corpus(CORPUS)
    writer, encoder = RecordingWriter(), FakeEncoder()
    with tempfile.TemporaryDirectory() as tmp:
        manifest = build_vectors(reader, encoder, writer, Path(tmp), "test-fixture",
                                 batch_size=2, backend="memory-exact")
        assert encoder.document_calls == 2
        assert [(r.docid, r.doc_id) for r in writer.records] == list(enumerate(["z", "a", "b"]))
        assert VectorManifest.from_dict(json.loads((Path(tmp) / "vector_manifest.json").read_text())) == manifest
        assert len(MemoryVectorIndex(manifest, writer.records).search([1.0, 0.0], 10)) == 3
        _raises(ValueError, lambda: build_vectors(reader, encoder, writer, Path(tmp), "overwrite"))


def test_hybrid_build_aligns_disk_and_vectors():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        corpus = root / "corpus.jsonl"
        corpus.write_text("".join(json.dumps(d) + "\n" for d in CORPUS))
        writer = RecordingWriter()
        result = build_hybrid(corpus, root / "index", "test", FakeEncoder(), writer,
                              "test-fixture", backend="memory-exact", bm25=BM25Spec(k1=1.5))
        assert result["lexical"]["bm25"]["k1"] == 1.5
        engine = SearchEngine(DiskIndex(root / "index" / "bm25"), FakeEncoder(),
                              MemoryVectorIndex(writer.metadata, writer.records))
        assert engine.search(SearchRequest("soaked", mode="hybrid")).results
        _raises(ValueError, lambda: build_hybrid(corpus, root / "index", "test", FakeEncoder(), writer, "reuse"))


def test_failed_vector_build_does_not_publish():
    class BadEncoder(FakeEncoder):
        def encode_documents(self, texts):
            return [[0.0, 0.0] for _ in texts]
    with tempfile.TemporaryDirectory() as tmp:
        _raises(ValueError, lambda: build_vectors(MemoryIndex.from_corpus(CORPUS), BadEncoder(),
                                                  RecordingWriter(), Path(tmp), "bad"))
        assert not (Path(tmp) / "vector_manifest.json").exists()


def test_vector_writer_must_consume_all_records():
    class LazyWriter:
        def replace(self, manifest, records):
            pass
    with tempfile.TemporaryDirectory() as tmp:
        _raises(ValueError, lambda: build_vectors(MemoryIndex.from_corpus(CORPUS), FakeEncoder(),
                                                  LazyWriter(), Path(tmp), "bad"))
        assert not (Path(tmp) / "vector_manifest.json").exists()


def test_vector_validation_rejects_dimension_nan_and_zero():
    for vector in ([1.0], [float("nan"), 0.0], [0.0, 0.0], [2.0, 0.0], [True, 0.0]):
        _raises(ValueError, lambda: validate_vector(vector, FakeEncoder.spec))


def test_startup_rejects_encoder_or_corpus_mismatch():
    engine, encoder, vectors = _setup()
    wrong_encoder = FakeEncoder()
    wrong_encoder.spec = replace(encoder.spec, revision="other-version")
    _raises(ValueError, lambda: SearchEngine(engine.reader, wrong_encoder, vectors))
    changed = [dict(d) for d in CORPUS]
    changed[0]["text"] = "changed"
    _raises(ValueError, lambda: SearchEngine(MemoryIndex.from_corpus(changed), encoder, vectors))
    _raises(ValueError, lambda: SearchEngine(engine.reader, encoder))


def test_memory_vectors_reject_duplicate_or_unknown_ids():
    _, _, vectors = _setup()
    records = [VectorRecord(0, "z", [1.0, 0.0])] * 3
    _raises(ValueError, lambda: MemoryVectorIndex(vectors.manifest(), records))


def test_rrf_uses_ranks_not_scores_and_tiebreaks():
    result = rrf([[2, 0], [1, 0]])
    assert [d for d, _ in result] == [0, 1, 2]
    assert math.isclose(result[0][1], 2 / 62)
    _raises(ValueError, lambda: rrf([[0, 0]]))
    _raises(ValueError, lambda: rrf([[0]], c=0))


def test_natural_query_uses_union_and_optional_intersection():
    reader = MemoryIndex.from_corpus(CORPUS)
    assert set(bm25(reader, ["phone", "device"])) == {0, 1}
    assert bm25(reader, ["phone", "device"], match_all=True) == {}
    assert set(bm25(reader, ["phone", "unknown"])) == {0}


def test_lexical_and_adaptive_can_skip_query_encoding():
    engine, encoder, _ = _setup()
    for mode in ("lexical", "adaptive"):
        resp = engine.search(SearchRequest("phone", mode=mode))
        assert resp.results[0].doc_id == "z"
        assert not resp.routing.vector_used
        assert resp.routing.executed_mode == "lexical"
    assert encoder.query_calls == 0


def test_dense_and_hybrid_force_vector_search():
    engine, encoder, _ = _setup()
    dense = engine.search(SearchRequest("phone", mode="dense"))
    hybrid = engine.search(SearchRequest("phone", mode="hybrid"))
    assert dense.routing.score_kind == "cosine"
    assert hybrid.routing.score_kind == "rrf"
    assert hybrid.routing.executed_mode == "hybrid"
    assert [r.doc_id for r in dense.results[:2]] == ["z", "a"]  # 同分按内部 docid，不是外部字符串
    assert hybrid.results[0].doc_id == "z"
    assert encoder.query_calls == 2


def test_adaptive_rescues_unknown_query_with_vectors():
    engine, encoder, _ = _setup()
    resp = engine.search(SearchRequest("soaked", mode="adaptive"))
    assert resp.routing.reason == "no_lexical_hits"
    assert resp.routing.vector_used and resp.routing.executed_mode == "hybrid"
    assert resp.results and encoder.query_calls == 1


def test_router_hard_constraints_always_stay_lexical():
    router = RuleRouter(RetrievalConfig())
    assert not router.decide(QueryFeatures(True, 1, 0, 0, None)).use_vector
    assert router.decide(QueryFeatures(False, 2, 0.5, 1, None)).reason == "low_vocab_coverage"
    assert router.decide(QueryFeatures(False, 2, 1, 2, 0.01)).reason == "small_relative_gap"


def test_explicit_syntax_is_filtered_before_optional_vector_search():
    engine, encoder, _ = _setup()
    lexical = engine.search(SearchRequest('"phone water"', mode="lexical"))
    assert [r.doc_id for r in lexical.results] == ["z"]
    adaptive = engine.search(SearchRequest('"phone water"', mode="adaptive"))
    assert [r.doc_id for r in adaptive.results] == ["z"]
    hybrid = engine.search(SearchRequest('"phone water"', mode="hybrid"))
    assert [r.doc_id for r in hybrid.results] == ["z"]
    _raises(ApiError, lambda: engine.search(SearchRequest('"phone water"', mode="dense")))
    _raises(QuerySyntaxError, lambda: engine.search(SearchRequest('"phone water', mode="lexical")))


def test_missing_vector_backend_is_visible_not_silent():
    engine = SearchEngine(MemoryIndex.from_corpus(CORPUS))
    for mode in ("dense", "hybrid"):
        _raises(RetrievalUnavailable, lambda: engine.search(SearchRequest("soaked", mode=mode)))
    resp = engine.search(SearchRequest("soaked", mode="adaptive"))
    assert resp.routing.fallback and not resp.routing.vector_used
    assert resp.routing.vector_attempted
    assert resp.routing.reason == "vector_unavailable"
    assert resp.routing.executed_mode == "lexical"


def test_runtime_vector_failure_and_bad_payload_handled():
    engine, encoder, vectors = _setup()
    vectors.search = lambda q, k: [VectorHit(0, "wrong-external-id", 1.0)]
    _raises(RetrievalUnavailable, lambda: engine.search(SearchRequest("soaked", mode="hybrid")))
    assert engine.search(SearchRequest("soaked", mode="adaptive")).routing.fallback
    vectors.search = lambda q, k: [VectorHit(999, "unknown", 1.0)]
    assert engine.search(SearchRequest("soaked", mode="adaptive")).routing.fallback
    encoder.encode_query = lambda query: [float("nan"), 0]
    assert engine.search(SearchRequest("soaked", mode="adaptive")).routing.fallback


def test_pagination_is_stable_and_bounded():
    engine, _, _ = _setup(RetrievalConfig(candidate_k=3))
    full = engine.search(SearchRequest("phone", mode="hybrid", k=3)).results
    page = engine.search(SearchRequest("phone", mode="hybrid", offset=1, k=2)).results
    assert [(r.doc_id, r.rank) for r in page] == [(r.doc_id, r.rank) for r in full[1:]]
    _raises(ApiError, lambda: engine.search(SearchRequest("phone", mode="hybrid", k=4)))


def test_request_contract_rejects_bad_types_and_combinations():
    for value in (None, [], "x", {"query": " "}, {"query": "x", "k": True},
                  {"query": "x", "k": "2"}, {"query": "x", "offset": 0.5},
                  {"query": "x", "mode": "unknown"}, {"query": "x", "extra": 1},
                  {"query": "x", "mode": "dense", "scorer": "tfidf"},
                  {"query": "x", "mode": "adaptive", "expander": "rm3"}):
        _raises(ApiError, lambda: SearchRequest.from_dict(value))
    assert SearchRequest.from_dict({"query": "x"}).mode == "lexical"


def _check_schema(value, schema):
    """只检查本仓库响应 schema 使用的子集，不替代完整 JSON Schema 校验器。"""
    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict)
        assert set(schema.get("required", [])) <= set(value)
        for key, item in value.items():
            if key in schema.get("properties", {}):
                _check_schema(item, schema["properties"][key])
            elif schema.get("additionalProperties") is False:
                raise AssertionError(f"未知字段: {key}")
    elif kind == "array":
        assert isinstance(value, list)
        for item in value:
            _check_schema(item, schema["items"])
    elif kind == "string":
        assert isinstance(value, str)
    elif kind == "integer":
        assert type(value) is int
    elif kind == "number":
        assert type(value) in (int, float) and math.isfinite(value)
    elif kind == "boolean":
        assert type(value) is bool
    if "minimum" in schema:
        assert value >= schema["minimum"]


def test_all_mode_responses_follow_published_schema():
    engine, _, _ = _setup()
    assert REQUEST_JSON_SCHEMA["properties"]["mode"]["enum"] == list(MODES)
    for mode in MODES:
        value = engine.search(SearchRequest("phone", mode=mode)).to_dict()
        _check_schema(value, RESPONSE_JSON_SCHEMA)
        assert value["routing"]["requested_mode"] == mode
        assert value["api_version"] == "2.0"


def test_http_contract_success_errors_and_capabilities():
    engine = SearchEngine(MemoryIndex.from_corpus(CORPUS))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        def post(payload):
            request = Request(base + "/search", json.dumps(payload).encode(),
                              {"Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)
        status, body = post({"query": "phone"})
        assert status == 200
        _check_schema(body, RESPONSE_JSON_SCHEMA)
        assert post([])[0] == 400
        assert post({"query": "phone", "k": True})[0] == 400
        assert post({"query": '"phone"'})[0] == 200
        assert post({"query": '"phone'})[0] == 400
        assert post({"query": "phone", "mode": "dense"})[0] == 503
        assert post({"query": "phone", "mode": "hybrid", "offset": 200})[0] == 400
        rm3_status, rm3_body = post({"query": "phone", "expander": "rm3"})
        assert rm3_status == 200
        assert rm3_body["expanded"] and all(item["src"] == "rm3" for item in rm3_body["expanded"])
        assert post({"query": "phone", "expander": "slm"})[0] == 501
        with urlopen(base + "/index/info", timeout=5) as response:
            assert json.load(response)["retrieval"]["vector_ready"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_ui_static_routes_are_safe_and_present():
    engine = SearchEngine(MemoryIndex.from_corpus(CORPUS))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(engine))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/", timeout=5) as response:
            assert response.status == 200
            assert "中文搜索引擎" in response.read().decode("utf-8")
        # Both registered JavaScript MIME types occur in system MIME databases.
        for mime_type in ("text/javascript", "application/javascript"):
            with patch("online.server.mimetypes.guess_type", return_value=(mime_type, None)):
                with urlopen(base + "/static/app.js", timeout=5) as response:
                    assert response.status == 200
                    assert response.headers.get_content_type() == mime_type
                    assert response.headers.get_content_charset() == "utf-8"
        try:
            urlopen(base + "/static/%2e%2e/online/server.py", timeout=5)
        except HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("静态文件路由允许路径穿越")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_snapshot_artifact_is_required_and_verified():
    from contracts.index_format import sha256_of
    reader = MemoryIndex.from_corpus(CORPUS)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        class SnapshotWriter(RecordingWriter):
            def replace(self, manifest, records):
                super().replace(manifest, records)
                snapshot = root / "vectors.snapshot"
                snapshot.write_bytes(b"test artifact only, not a real Qdrant snapshot")
                return replace(manifest, snapshot_files={snapshot.name: sha256_of(snapshot)})
        published = build_vectors(reader, FakeEncoder(), SnapshotWriter(), root, "new-collection")
        assert "vectors.snapshot" in published.snapshot_files
    with tempfile.TemporaryDirectory() as tmp:
        _raises(ValueError, lambda: build_vectors(reader, FakeEncoder(), RecordingWriter(),
                                                  Path(tmp), "missing-snapshot"))
        assert not (Path(tmp) / "vector_manifest.json").exists()


def test_writer_cannot_change_contract_or_forge_snapshot():
    reader = MemoryIndex.from_corpus(CORPUS)
    class ForgedWriter(RecordingWriter):
        def replace(self, manifest, records):
            super().replace(manifest, records)
            return replace(manifest, snapshot_files={"missing.snapshot": "0" * 64})
    class WrongMetadata(RecordingWriter):
        def replace(self, manifest, records):
            super().replace(manifest, records)
            return replace(manifest, collection="unexpected-collection")
    for writer in (ForgedWriter(), WrongMetadata()):
        with tempfile.TemporaryDirectory() as tmp:
            _raises(ValueError, lambda: build_vectors(reader, FakeEncoder(), writer,
                                                      Path(tmp), "test-fixture"))
            assert not (Path(tmp) / "vector_manifest.json").exists()


def test_typed_backend_failure_falls_back_only_for_adaptive():
    engine, _, vectors = _setup()
    def unavailable(query, limit):
        raise RetrievalUnavailable("simulated timeout")
    vectors.search = unavailable
    assert engine.search(SearchRequest("soaked", mode="adaptive")).routing.fallback
    _raises(RetrievalUnavailable, lambda: engine.search(SearchRequest("soaked", mode="hybrid")))


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
