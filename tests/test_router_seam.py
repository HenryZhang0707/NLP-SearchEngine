"""Router seam and vector search-config tests; no model or database needed.

The fake policy objects below define no routing algorithm. They only prove that
the engine's trusted extension point, identity echo and hard-constraint guard
behave as contracted.
"""

import math
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock, patch

from contracts.api import SearchRequest
from contracts.retrieval import (
    VECTOR_BUILD_METADATA_KEY, EmbeddingSpec, RetrievalConfig, VectorManifest,
    VectorRecord, corpus_fingerprint,
)
from online.engine import SearchEngine
from online.memory_index import MemoryIndex
from online.router import RouteDecision, RuleRouter, load_router, router_identity
from online.vector_index import MemoryVectorIndex, QdrantVectorIndex

CORPUS = [
    {"doc_id": "z", "title": "", "text": "phone water"},
    {"doc_id": "a", "title": "", "text": "device liquid"},
    {"doc_id": "b", "title": "", "text": "camera lens"},
]


class FakeEncoder:
    spec = EmbeddingSpec("test-only", "fixture-v1", dimension=2)

    def __init__(self):
        self.query_calls = 0

    def encode_documents(self, texts):
        return [[0.0, 1.0] if "camera" in text else [1.0, 0.0] for text in texts]

    def encode_query(self, query):
        self.query_calls += 1
        return [0.0, 1.0] if "camera" in query else [1.0, 0.0]


class AlwaysVectorPolicy:
    name = "always-vector"
    policy_version = "test-always-vector"
    requires_statistics = False

    def __init__(self):
        self.calls = 0

    def decide(self, features):
        self.calls += 1
        return RouteDecision(True, "policy_forced_vector")


class AlwaysLexicalPolicy:
    name = "always-lexical"
    policy_version = "test-always-lexical"
    requires_statistics = False

    def decide(self, features):
        return RouteDecision(False, "policy_forced_lexical")


class StatisticsPolicy:
    name = "statistics"
    policy_version = "test-statistics"
    requires_statistics = True

    def __init__(self):
        self.calls = 0
        self.seen = None
        self.features = None

    def statistics(self, reader, terms, ranked):
        self.seen = (reader, terms, ranked)
        time.sleep(0.02)
        return {"probe": 1.0}

    def decide(self, features):
        self.calls += 1
        self.features = features
        return RouteDecision(False, "statistics_decision")


class HostilePolicy:
    name = "hostile"
    policy_version = "test-hostile"
    requires_statistics = False

    def __init__(self):
        self.calls = 0

    def decide(self, features):
        self.calls += 1
        return RouteDecision(True, "force_vector")


def make_test_policy(config):
    assert isinstance(config, RetrievalConfig)
    return AlwaysVectorPolicy()


def _raises(kind, fn):
    try:
        fn()
    except kind:
        return
    raise AssertionError(f"应抛出 {kind.__name__}")


def _engine(router=None, config=None):
    reader = MemoryIndex.from_corpus(CORPUS)
    encoder = FakeEncoder()
    manifest = VectorManifest(corpus_fingerprint(reader), reader.n_docs, encoder.spec,
                              "router-seam", backend="memory-exact")
    records = [VectorRecord(i, row["doc_id"], vector) for i, (row, vector) in enumerate(zip(
        CORPUS, encoder.encode_documents([row["text"] for row in CORPUS])))]
    vectors = MemoryVectorIndex(manifest, records)
    return SearchEngine(reader, encoder, vectors, config, router=router), encoder, vectors


def _dicts(items):
    return [item.to_dict() for item in items]


def test_default_router_keeps_positional_compatibility_and_identity():
    engine, _, vectors = _engine()
    assert isinstance(engine.router, RuleRouter)
    resp = engine.search(SearchRequest("soaked", mode="adaptive"))
    assert resp.routing.reason == "no_lexical_hits"
    assert resp.routing.policy_version == "rules-v1"

    manifest = vectors.manifest()
    info = engine.info()
    assert info["vector_ready"] is True
    assert info["vector_manifest"] == manifest.to_dict()
    assert info["index_identity"] == {
        "dataset": "memory", "n_docs": 3, "schema_version": 1, "analyzer_hash": "-",
        "corpus_sha256": manifest.corpus_sha256, "bm25": None,
    }
    assert info["vector_identity"] == {
        **manifest.to_dict(),
        "search": {"hnsw_ef": None, "exact": True, "timeout_s": None},
    }
    assert info["search_config"] == {"candidate_k": 200, "rrf_c": 60.0}
    assert info["policy"] == {"name": "rule", "policy_version": "rules-v1",
                              "requires_statistics": False}

    lexical = SearchEngine(engine.reader)
    lexical_info = lexical.info()
    assert lexical_info["vector_ready"] is False
    assert lexical_info["vector_manifest"] is None
    assert lexical_info["vector_identity"] is None
    assert lexical_info["index_identity"]["corpus_sha256"] is None


def test_custom_policy_version_echoes_agree_and_config_stays_unchanged():
    config = RetrievalConfig(candidate_k=37, rrf_c=12.5)
    engine, _, _ = _engine(router=AlwaysLexicalPolicy(), config=config)
    assert engine.config is config
    info = engine.info()
    # info 的三个回显位（config/policy 与 routing）都必须是实际生效的策略版本。
    assert info["policy"]["policy_version"] == "test-always-lexical"
    assert info["config"]["policy_version"] == "test-always-lexical"
    assert info["config"] == {**asdict(config), "policy_version": "test-always-lexical"}
    # 其余检索参数逐字段保留，候选池/融合常量不被改写。
    assert (info["config"]["candidate_k"], info["config"]["rrf_c"]) == (37, 12.5)
    assert info["search_config"] == {"candidate_k": 37, "rrf_c": 12.5}
    resp = engine.search(SearchRequest("phone", mode="adaptive"))
    assert resp.routing.policy_version == "test-always-lexical"
    assert resp.routing.candidate_k == 37
    assert resp.routing.executed_mode == "lexical"
    # 冻结的 RetrievalConfig 本身没有被 info() 改写。
    assert config.policy_version == "rules-v1"
    assert (config.candidate_k, config.rrf_c) == (37, 12.5)


def test_policy_swap_leaves_lexical_pool_and_forced_modes_identical():
    baseline, _, _ = _engine()
    policy = AlwaysVectorPolicy()
    swapped, encoder, _ = _engine(router=policy)
    for request in (SearchRequest("phone", mode="lexical"),
                    SearchRequest("phone", mode="hybrid"),
                    SearchRequest("phone", mode="dense")):
        left = baseline.search(request)
        right = swapped.search(request)
        assert _dicts(left.results) == _dicts(right.results)
        assert left.routing.executed_mode == right.routing.executed_mode
    assert policy.calls == 0

    before_adaptive = encoder.query_calls
    adaptive = swapped.search(SearchRequest("phone", mode="adaptive"))
    assert adaptive.routing.reason == "policy_forced_vector"
    assert adaptive.routing.vector_used and adaptive.routing.executed_mode == "hybrid"
    assert adaptive.routing.policy_version == "test-always-vector"
    assert _dicts(adaptive.results) == _dicts(
        baseline.search(SearchRequest("phone", mode="hybrid")).results)
    assert encoder.query_calls == before_adaptive + 1


def test_custom_policy_can_keep_adaptive_lexical_without_encoding():
    engine, encoder, _ = _engine(router=AlwaysLexicalPolicy())
    resp = engine.search(SearchRequest("soaked", mode="adaptive"))
    assert resp.routing.reason == "policy_forced_lexical"
    assert not resp.routing.vector_used
    assert resp.routing.executed_mode == "lexical"
    assert resp.routing.policy_version == "test-always-lexical"
    assert encoder.query_calls == 0


def test_statistics_hook_runs_only_for_adaptive_and_inside_router_timing():
    policy = StatisticsPolicy()
    engine, _, _ = _engine(router=policy)
    for mode in ("lexical", "dense", "hybrid"):
        engine.search(SearchRequest("phone", mode=mode))
    assert policy.calls == 0 and policy.seen is None

    empty = engine.search(SearchRequest("soaked", mode="adaptive"))
    assert policy.calls == 1
    reader, terms, ranked = policy.seen
    assert reader is engine.reader
    assert terms == ("soaked",)
    assert ranked == ()
    assert policy.features.statistics == {"probe": 1.0}
    assert policy.features.explicit_constraints is False
    assert empty.timings_ms["router"] >= 15.0

    engine.search(SearchRequest("phone", mode="adaptive"))
    assert policy.calls == 2
    assert policy.seen[1] == ("phone",)
    assert [docid for docid, _ in policy.seen[2]] == [0]
    assert all(type(docid) is int and math.isfinite(score)
               for docid, score in policy.seen[2])


def test_explicit_adaptive_constraints_bypass_any_custom_policy():
    for router in (HostilePolicy(), StatisticsPolicy()):
        engine, encoder, _ = _engine(router=router)
        resp = engine.search(SearchRequest('"phone water"', mode="adaptive"))
        assert resp.routing.reason == "explicit_constraints"
        assert not resp.routing.vector_used
        assert not resp.routing.vector_attempted
        assert resp.routing.executed_mode == "lexical"
        assert [result.doc_id for result in resp.results] == ["z"]
        assert getattr(router, "calls", 0) == 0
        assert getattr(router, "seen", None) is None
        assert encoder.query_calls == 0


def test_invalid_router_contract_fails_closed():
    valid = dict(name="broken", policy_version="test-broken",
                 requires_statistics=False,
                 decide=lambda features: RouteDecision(False, "ok"))
    for override in ({"policy_version": ""}, {"policy_version": 1},
                     {"requires_statistics": 1}, {"requires_statistics": True},
                     {"decide": None}):
        router = SimpleNamespace(**{**valid, **override})
        _raises(ValueError, lambda: _engine(router=router))
    missing_version = dict(valid)
    del missing_version["policy_version"]
    _raises(ValueError, lambda: _engine(router=SimpleNamespace(**missing_version)))
    _raises(ValueError, lambda: _engine(router=object()))

    for output in (None, True, ("use", "reason"), RouteDecision(1, "x"),
                   RouteDecision(True, ""), RouteDecision(False, None)):
        router = SimpleNamespace(**{**valid, "decide": lambda features, out=output: out})
        engine, _, _ = _engine(router=router)
        _raises(ValueError, lambda: engine.search(SearchRequest("phone", mode="adaptive")))

    bad_statistics = SimpleNamespace(
        name="broken", policy_version="test-broken", requires_statistics=True,
        statistics=lambda reader, terms, ranked: [],
        decide=lambda features: RouteDecision(False, "ok"))
    engine, _, _ = _engine(router=bad_statistics)
    _raises(ValueError, lambda: engine.search(SearchRequest("phone", mode="adaptive")))


def test_load_router_selects_rule_or_trusted_module_factory():
    config = RetrievalConfig(policy_version="rules-test")
    router = load_router("rule", config)
    assert isinstance(router, RuleRouter)
    assert router.policy_version == "rules-test"
    assert router_identity(router) == {"name": "rule", "policy_version": "rules-test",
                                       "requires_statistics": False}

    loaded = load_router("tests.test_router_seam:make_test_policy", config)
    assert isinstance(loaded, AlwaysVectorPolicy)

    for selector in ("", "rule:", ":factory", "a:b:c",
                     "tests.test_router_seam:missing", "missing_module_xyz:factory"):
        _raises(ValueError, lambda: load_router(selector, config))


class CountingReader:
    """Delegate an IndexReader while counting lexical scoring entry points."""

    def __init__(self, reader):
        self._reader = reader
        self.n_docs = reader.n_docs
        self.avgdl = reader.avgdl
        self.postings_calls = 0
        self.df_calls = 0

    def postings(self, term):
        self.postings_calls += 1
        return self._reader.postings(term)

    def df(self, term):
        self.df_calls += 1
        return self._reader.df(term)

    def doc_length(self, docid):
        return self._reader.doc_length(docid)

    def doc(self, docid):
        return self._reader.doc(docid)

    def vocab(self):
        return self._reader.vocab()

    def manifest(self):
        return self._reader.manifest()


def test_dense_performs_zero_lexical_postings_or_df_calls():
    reader = CountingReader(MemoryIndex.from_corpus(CORPUS))
    encoder = FakeEncoder()
    manifest = VectorManifest(corpus_fingerprint(reader), reader.n_docs, encoder.spec,
                              "router-seam", backend="memory-exact")
    records = [VectorRecord(i, row["doc_id"], vector) for i, (row, vector) in enumerate(zip(
        CORPUS, encoder.encode_documents([row["text"] for row in CORPUS])))]
    engine = SearchEngine(reader, encoder, MemoryVectorIndex(manifest, records))

    dense = engine.search(SearchRequest("phone", mode="dense"))
    assert dense.results and dense.routing.score_kind == "cosine"
    assert reader.postings_calls == 0 and reader.df_calls == 0

    hybrid = engine.search(SearchRequest("phone", mode="hybrid"))
    assert hybrid.routing.executed_mode == "hybrid"
    assert reader.postings_calls > 0 and reader.df_calls > 0


@contextmanager
def _fake_qdrant(manifest):
    vectors_config = SimpleNamespace(size=manifest.embedding.dimension, distance="Cosine")
    collection = SimpleNamespace(
        config=SimpleNamespace(params=SimpleNamespace(vectors=vectors_config),
                               metadata={VECTOR_BUILD_METADATA_KEY: manifest.build_metadata()}))
    client = SimpleNamespace(
        get_collection=Mock(return_value=collection),
        count=Mock(return_value=SimpleNamespace(count=manifest.n_docs)),
        query_points=Mock(return_value=SimpleNamespace(points=[])))
    models = SimpleNamespace(SearchParams=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)))
    module = SimpleNamespace(QdrantClient=Mock(return_value=client), models=models)
    with patch.dict(sys.modules, {"qdrant_client": module}):
        yield module, client


def _qdrant_manifest():
    return VectorManifest(
        "a" * 64, 1, EmbeddingSpec("fixture", "v1", dimension=2), "fixture",
        snapshot_files={"fixture.snapshot": "b" * 64})


def test_qdrant_timeout_is_validated_forwarded_and_echoed():
    manifest = _qdrant_manifest()
    with _fake_qdrant(manifest) as (module, client):
        reader = QdrantVectorIndex(manifest, "http://unused.invalid",
                                   hnsw_ef=64, timeout_s=12.5)
        module.QdrantClient.assert_called_once_with(url="http://unused.invalid", timeout=12.5)
        assert reader.search_config == {"hnsw_ef": 64, "exact": False, "timeout_s": 12.5}
        reader.search([1.0, 0.0], 1)
        call = client.query_points.call_args.kwargs
        assert call["search_params"].hnsw_ef == 64
        assert call["search_params"].exact is False

    with _fake_qdrant(manifest) as (module, _):
        default = QdrantVectorIndex(manifest, "http://unused.invalid")
        module.QdrantClient.assert_called_once_with(url="http://unused.invalid", timeout=30.0)
        assert default.search_config == {"hnsw_ef": None, "exact": False, "timeout_s": 30.0}

    for value in (0, -1, 0.0, float("nan"), float("inf"), True, False, "30", None):
        with patch.dict(sys.modules, {"qdrant_client": None}):
            _raises(ValueError, lambda: QdrantVectorIndex(
                manifest, "http://unused.invalid", timeout_s=value))

    _, _, memory = _engine()
    assert memory.search_config == {"hnsw_ef": None, "exact": True, "timeout_s": None}


ALL_TESTS = [value for name, value in sorted(globals().items())
             if name.startswith("test_")]
