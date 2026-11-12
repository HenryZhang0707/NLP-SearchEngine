"""Exercise the peer routing and CE seams through the public search handler."""

from dataclasses import asdict
from http.server import ThreadingHTTPServer
import hashlib
import io
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from contracts.index_format import BM25Spec
from contracts.retrieval import EmbeddingSpec, VectorManifest, VectorRecord, corpus_fingerprint
from evaluation.evaluate import EvaluationError, QueryRecord, evaluate_http
from online.engine import SearchEngine
from online.gain_router import load_gain_router
from online.memory_index import MemoryIndex
from online.observability import SearchLogger
from online.reranking import HybridReranker
from online.router import RouteDecision
from online.router_features import FEATURE_NAMES, FEATURE_VERSION
from online.server import make_handler
from online.vector_index import MemoryVectorIndex


CORPUS = [
    {"doc_id": "z", "title": "", "text": "phone alpha"},
    {"doc_id": "a", "title": "", "text": "phone bravo"},
    {"doc_id": "m", "title": "", "text": "gadget charlie"},
    {"doc_id": "b", "title": "", "text": "camera delta"},
]


class FixtureEncoder:
    spec = EmbeddingSpec("test-only", "fixture-v1", dimension=2)

    def __init__(self):
        self.calls = []

    def encode_query(self, query):
        self.calls.append(query)
        return [1.0, 0.0]


class RecordingScorer:
    identity = {"model_id": "fixture-ce", "revision": "test-v1"}

    def __init__(self):
        self.calls = []
        self.scores = [2.0, 2.0, 9.0]

    def score(self, query, texts):
        self.calls.append((query, tuple(texts)))
        if isinstance(self.scores, Exception):
            raise self.scores
        return self.scores


class QueryAwareDensePolicy:
    policy_version = "test-query-aware-dense-v1"
    requires_statistics = True

    def __init__(self):
        self.statistics_calls = []
        self.decisions = []

    def statistics_for_query(self, query, reader, terms, ranked):
        self.statistics_calls.append((query, reader, terms, ranked))
        return {"query_chars": len(query)}

    def decide(self, features):
        self.decisions.append(features)
        return RouteDecision(True, "fixture_dense", semantic_mode="dense")


class PeerRoutingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.reader = MemoryIndex.from_corpus(CORPUS)
        self.encoder = FixtureEncoder()
        manifest = VectorManifest(corpus_fingerprint(self.reader), self.reader.n_docs,
                                  self.encoder.spec, "peer-integration", backend="memory-exact")
        self.vectors = MemoryVectorIndex(manifest, [
            VectorRecord(i, row["doc_id"], [1.0, 0.0]) for i, row in enumerate(CORPUS)
        ])
        self.opener = build_opener(ProxyHandler({}))

    def serve(self, *, router=None, reranker=None):
        engine = SearchEngine(self.reader, self.encoder, self.vectors,
                              router=router, reranker=reranker)
        sink = io.StringIO()
        handler = make_handler(engine, search_log=SearchLogger(sink))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                                  daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_port}/search", sink

    def post(self, endpoint, **payload):
        request = Request(endpoint, json.dumps(payload).encode("utf-8"),
                          {"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def search(self, endpoint, **payload):
        status, body = self.post(endpoint, **payload)
        self.assertEqual(status, 200, body)
        return body

    def test_hybrid_reranks_fixed_prefix_before_pagination(self):
        baseline_endpoint, _ = self.serve()
        scorer = RecordingScorer()
        rerank_endpoint, _ = self.serve(reranker=HybridReranker(scorer, depth=3))
        request = {"query": "phone", "mode": "hybrid"}

        baseline = self.search(baseline_endpoint, **request, k=4)
        self.assertEqual([row["doc_id"] for row in baseline["results"]], ["z", "a", "m", "b"])
        reranked = self.search(rerank_endpoint, **request, k=4)
        self.assertEqual([row["doc_id"] for row in reranked["results"]], ["m", "a", "z", "b"])
        self.assertEqual([row["rank"] for row in reranked["results"]], [1, 2, 3, 4])
        self.assertEqual({row["doc_id"]: row["score"] for row in reranked["results"]},
                         {row["doc_id"]: row["score"] for row in baseline["results"]})
        self.assertEqual(reranked["results"][-1], baseline["results"][-1])
        self.assertEqual(reranked["rerank"]["depth"], 3)
        self.assertEqual(reranked["rerank"]["scored"], 3)
        self.assertTrue(math.isfinite(reranked["timings_ms"]["rerank"]))
        self.assertGreaterEqual(reranked["timings_ms"]["rerank"], 0)
        self.assertNotIn("rerank", baseline)

        page = self.search(rerank_endpoint, **request, offset=1, k=1)
        self.assertEqual(page["results"], reranked["results"][1:2])
        self.assertEqual(page["rerank"]["scored"], 3)
        self.assertEqual(len(scorer.calls), 2)
        self.assertEqual(scorer.calls[0], scorer.calls[1])
        query, texts = scorer.calls[0]
        self.assertEqual(query, "phone")
        self.assertEqual([text.split()[-1] for text in texts], ["alpha", "bravo", "charlie"])

    def test_rerank_metadata_counts_actual_pairs_for_a_short_pool(self):
        scorer = RecordingScorer()
        scorer.scores = [1.0, 2.0, 3.0, 4.0]
        endpoint, _ = self.serve(reranker=HybridReranker(scorer, depth=6))
        body = self.search(endpoint, query="phone", mode="hybrid", k=1)
        self.assertEqual(body["rerank"]["depth"], 6)
        self.assertEqual(body["rerank"]["scored"], 4)
        self.assertEqual(body["results"][0]["doc_id"], "b")
        self.assertEqual(len(scorer.calls), 1)
        self.assertEqual(len(scorer.calls[0][1]), 4)

    def test_reranker_bypassed_for_explicit_constraints_and_other_modes(self):
        baseline_endpoint, _ = self.serve()
        scorer = RecordingScorer()
        rerank_endpoint, _ = self.serve(reranker=HybridReranker(scorer, depth=3))
        for query, mode in [("phone", "lexical"), ("phone", "dense"),
                            ("phone", "adaptive"), ("phone AND alpha", "hybrid")]:
            with self.subTest(query=query, mode=mode):
                baseline = self.search(baseline_endpoint, query=query, mode=mode, k=4)
                bypassed = self.search(rerank_endpoint, query=query, mode=mode, k=4)
                self.assertEqual(bypassed["results"], baseline["results"])
                self.assertEqual(bypassed["routing"], baseline["routing"])
                self.assertNotIn("rerank", bypassed)
                self.assertNotIn("rerank", bypassed["timings_ms"])
        self.assertEqual(scorer.calls, [])

    def test_scorer_failures_are_sanitized_on_the_public_endpoint(self):
        scorer = RecordingScorer()
        endpoint, sink = self.serve(reranker=HybridReranker(scorer, depth=3))
        secret = "private fixture scorer exception"
        for scores in (RuntimeError(secret), [1.0], [1.0, math.nan, 2.0]):
            with self.subTest(scores=scores):
                scorer.scores = scores
                status, body = self.post(endpoint, query="phone", mode="hybrid", k=4)
                self.assertEqual(status, 503, body)
                self.assertEqual(body["code"], "reranker_unavailable")
                self.assertNotIn(secret, json.dumps(body))
        self.assertNotIn(secret, sink.getvalue())

    def test_query_statistics_can_select_dense_without_bypassing_constraints(self):
        router = QueryAwareDensePolicy()
        endpoint, _ = self.serve(router=router)
        adaptive = self.search(endpoint, query="camera", mode="adaptive", k=4)
        dense = self.search(endpoint, query="camera", mode="dense", k=4)
        hybrid = self.search(endpoint, query="camera", mode="hybrid", k=4)
        self.assertEqual(adaptive["results"], dense["results"])
        self.assertNotEqual(adaptive["results"][0]["doc_id"], hybrid["results"][0]["doc_id"])
        route = adaptive["routing"]
        self.assertEqual(route["requested_mode"], "adaptive")
        self.assertEqual(route["executed_mode"], "dense")
        self.assertEqual(route["score_kind"], "cosine")
        self.assertEqual(route["reason"], "fixture_dense")
        self.assertEqual(route["policy_version"], router.policy_version)
        self.assertTrue(route["vector_attempted"])
        self.assertTrue(route["vector_used"])

        vector_calls = len(self.encoder.calls)
        constrained = self.search(endpoint, query="phone AND alpha", mode="adaptive", k=4)
        self.assertEqual([row["doc_id"] for row in constrained["results"]], ["z"])
        self.assertEqual(constrained["routing"]["reason"], "explicit_constraints")
        self.assertFalse(constrained["routing"]["vector_attempted"])
        self.assertEqual(len(self.encoder.calls), vector_calls)

        self.assertEqual(len(router.statistics_calls), 1)
        query, reader, terms, ranked = router.statistics_calls[0]
        self.assertEqual(query, "camera")
        self.assertIs(reader, self.reader)
        self.assertEqual(terms, ("camera",))
        self.assertIsInstance(ranked, tuple)
        self.assertEqual([docid for docid, _ in ranked], [3])
        self.assertEqual(len(router.decisions), 1)
        self.assertEqual(router.decisions[0].statistics, {"query_chars": 6})

    def test_http_evaluator_requires_opt_in_for_the_dense_adaptive_action(self):
        endpoint, _ = self.serve(router=QueryAwareDensePolicy())
        records = [QueryRecord("camera-qid", "camera")]
        qrels = {"camera-qid": {"b": 1.0}}
        with self.assertRaises(EvaluationError):
            evaluate_http(endpoint, records, qrels, mode="adaptive")
        summary = evaluate_http(endpoint, records, qrels, mode="adaptive",
                                allow_adaptive_dense=True, fail_on_fallback=True)
        self.assertEqual(summary.n_queries, 1)
        self.assertEqual(summary.routing.vector_successes, 1)
        self.assertEqual(summary.per_query[0].routing.executed_mode, "dense")

    def test_peer_factory_binds_model_digest_and_effective_vector_search(self):
        # Peer artifacts bind the published BM25 manifest. The generic
        # MemoryIndex omits that block and relies on the scorer's defaults.
        manifest = {**self.reader.manifest(), "bm25": asdict(BM25Spec())}
        manifest_override = patch.object(self.reader, "manifest", return_value=manifest)
        manifest_override.start()
        self.addCleanup(manifest_override.stop)
        model = {
            "schema_version": 1, "kind": "action-linear",
            "feature_version": FEATURE_VERSION, "feature_names": list(FEATURE_NAMES),
            "selected_features": ["query_chars"], "mean": [0.0], "scale": [1.0],
            "heads": {"dense": {"coef": [0.0], "intercept": 1.0},
                      "hybrid": {"coef": [0.0], "intercept": 0.0}},
            "threshold": 0.0, "margin": 0.0,
            "provenance": {"source": {
                "candidate_k": 200, "rrf_c": 60.0, "source_manifest": self.reader.manifest(),
                "vector_manifest": self.vectors.manifest().to_dict(),
                "vector_search_params": {"hnsw_ef": None, "exact": True},
            }},
        }
        raw = json.dumps(model).encode("utf-8")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test-model.json"
            path.write_bytes(raw)
            with patch.dict("os.environ", {"NLP_PEER_GAIN_MODEL": str(path),
                                            "NLP_PEER_GAIN_SHA256": hashlib.sha256(raw).hexdigest()}):
                router = load_gain_router(None)
                endpoint, _ = self.serve(router=router)
                actual = self.search(endpoint, query="camera", mode="adaptive", k=4)
                self.assertEqual(actual["routing"]["reason"], "predicted_dense_gain")
                router.model["provenance"]["source"]["vector_search_params"] = {
                    "hnsw_ef": 128, "exact": False,
                }
                with self.assertRaisesRegex(ValueError, "ANN configuration mismatch"):
                    SearchEngine(self.reader, self.encoder, self.vectors, router=router)
                path.write_bytes(raw + b"\n")
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    load_gain_router(None)

if __name__ == "__main__":
    unittest.main()
