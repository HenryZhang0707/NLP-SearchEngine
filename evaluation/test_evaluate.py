"""Small stdlib-only tests for the HTTP-only evaluator."""

import io
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from evaluation.evaluate import (
    EvaluationError,
    QueryRecord,
    evaluate_http,
    evaluate_run,
    load_qrels,
    load_queries,
    load_run,
    main,
)
from evaluation.compare import paired_bootstrap


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        self.server.requests.append(request)
        query, mode = request["query"], request["mode"]
        rows = [{"doc_id": "d1", "score": 2.0}, {"doc_id": "d2", "score": 1.0}]
        if query == "deep":
            rows = [{"doc_id": f"d{i}", "score": float(121 - i)} for i in range(1, 121)]
        elif query == "q2":
            rows = [{"doc_id": "d3", "score": 1.0}]
        fallback = mode == "adaptive" and query == "fallback"
        used = mode in ("dense", "hybrid") or mode == "adaptive" and query not in ("skip", "fallback")
        routing = {
            "requested_mode": mode,
            "executed_mode": ("dense" if mode == "dense" else "hybrid") if used else "lexical",
            "reason": "vector_unavailable" if fallback else "test",
            "vector_attempted": used or fallback,
            "vector_used": used,
            "fallback": fallback,
        }
        payload = {"results": rows[:request["k"]], "routing": routing}
        if query == "missing-routing":
            payload.pop("routing")
        elif query == "bad-routing":
            routing["fallback"] = "false"
        elif query == "wrong-mode":
            routing["requested_mode"] = "unexpected"
        elif query == "inconsistent-routing":
            routing["vector_used"] = True
            routing["vector_attempted"] = False
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@contextmanager
def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/search", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class EvaluatorTests(unittest.TestCase):
    def test_metrics_and_trec_loaders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qrels_path = root / "qrels.tsv"
            qrels_path.write_text("q1 0 d1 2\nq1 0 d9 1\nq2 0 d3 1\n")
            run_path = root / "run.trec"
            run_path.write_text("q1 Q0 d2 1 3.0 tag\nq1 Q0 d1 2 2.0 tag\nq2 Q0 d3 1 1.0 tag\n")
            query_path = root / "queries.tsv"
            query_path.write_text("q1\tquery one\nq2\tquery two\n")
            qrels = load_qrels(qrels_path)
            summary = evaluate_run(qrels, load_run(run_path), k=2)
            self.assertEqual(summary.n_queries, 2)
            self.assertGreater(summary.ndcg_at_10, 0.0)
            self.assertGreater(summary.map_at_100, 0.0)
            self.assertEqual([q.qid for q in load_queries(query_path)], ["q1", "q2"])
            self.assertIsNone(summary.routing)
            self.assertTrue(all(item.routing is None for item in summary.per_query))

    def test_http_mode_and_run_output(self):
        with _serve() as (endpoint, _), tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.trec"
            summary = evaluate_http(
                endpoint, [QueryRecord("q1", "q1"), QueryRecord("q2", "q2")],
                {"q1": {"d1": 1.0}, "q2": {"d3": 1.0}}, run_output=path,
            )
            self.assertEqual(summary.n_queries, 2)
            self.assertIsNotNone(summary.p95_latency_ms)
            self.assertEqual(len(path.read_text().splitlines()), 3)
            self.assertEqual(summary.routing.vector_attempts, 0)
            self.assertIsNone(summary.routing.vector_success_rate)

    def test_http_fetches_map_depth_without_changing_top_ten_metrics(self):
        with _serve() as (endpoint, server), tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.trec"
            summary = evaluate_http(
                endpoint, [QueryRecord("q1", "deep")], {"q1": {"d11": 1.0}},
                run_output=path,
            )
            self.assertEqual(server.requests[0]["k"], 100)
            self.assertEqual(len(load_run(path)["q1"]), 100)
            self.assertEqual(summary.per_query[0].retrieved, 100)
            self.assertEqual(summary.ndcg_at_10, 0.0)
            self.assertEqual(summary.recall_at_10, 0.0)
            self.assertEqual(summary.mrr_at_10, 0.0)
            self.assertAlmostEqual(summary.map_at_100, 1.0 / 11)
            reloaded = evaluate_run({"q1": {"d11": 1.0}}, load_run(path))
            self.assertEqual(reloaded.map_at_100, summary.map_at_100)

    def test_http_fetches_larger_ranking_cutoff_independently_of_map_cutoff(self):
        with _serve() as (endpoint, server):
            summary = evaluate_http(
                endpoint, [QueryRecord("q1", "deep")], {"q1": {"d11": 1.0}},
                k=15, map_k=10,
            )
            self.assertEqual(server.requests[0]["k"], 15)
            self.assertEqual(summary.recall_at_10, 1.0)
            self.assertEqual(summary.map_at_100, 0.0)

    def test_http_rejects_unsupported_depth_before_touching_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.trec"
            path.write_text("previous run", encoding="utf-8")
            with patch("evaluation.evaluate._request_search") as request:
                for k, map_k in ((201, 100), (10, 201)):
                    with self.assertRaisesRegex(EvaluationError, "200"):
                        evaluate_http("http://unused", [QueryRecord("q1", "q1")], {},
                                      k=k, map_k=map_k, run_output=path)
                request.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "previous run")

    def test_http_reports_success_skip_and_fallback_per_query(self):
        with _serve() as (endpoint, _):
            summary = evaluate_http(
                endpoint, [QueryRecord("success", "q1"), QueryRecord("skip", "skip"),
                           QueryRecord("failed", "fallback")],
                {}, mode="adaptive",
            )
            self.assertEqual(summary.routing.n_queries, 3)
            self.assertEqual(summary.routing.vector_attempts, 2)
            self.assertEqual(summary.routing.vector_successes, 1)
            self.assertEqual(summary.routing.fallbacks, 1)
            self.assertAlmostEqual(summary.routing.vector_attempt_rate, 2.0 / 3)
            self.assertEqual(summary.routing.vector_success_rate, 0.5)
            self.assertAlmostEqual(summary.routing.fallback_rate, 1.0 / 3)
            value = summary.to_dict(include_per_query=True)
            self.assertTrue(value["per_query"][2]["routing"]["fallback"])
            self.assertEqual(value["routing"]["fallbacks"], 1)

    def test_http_validates_routing_instead_of_silently_losing_telemetry(self):
        with _serve() as (endpoint, _):
            for query in ("missing-routing", "bad-routing", "wrong-mode", "inconsistent-routing"):
                with self.subTest(query=query), self.assertRaisesRegex(EvaluationError, "routing"):
                    evaluate_http(endpoint, [QueryRecord("q1", query)], {}, mode="adaptive")
            for mode in ("lexical", "dense", "hybrid", "adaptive"):
                with self.subTest(mode=mode):
                    summary = evaluate_http(endpoint, [QueryRecord("q1", "q1")], {}, mode=mode)
                    self.assertEqual(summary.per_query[0].routing.requested_mode, mode)

    def test_strict_fallback_rejection_is_exposed_by_cli(self):
        with _serve() as (endpoint, _), tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(EvaluationError, "q1.*降级"):
                evaluate_http(endpoint, [QueryRecord("q1", "fallback")], {},
                              mode="adaptive", fail_on_fallback=True)
            root = Path(directory)
            (root / "queries.tsv").write_text("q1\tfallback\n", encoding="utf-8")
            (root / "qrels.tsv").write_text("q1 0 d1 1\n", encoding="utf-8")
            errors = io.StringIO()
            with redirect_stderr(errors):
                code = main(["--endpoint", endpoint, "--queries", str(root / "queries.tsv"),
                             "--qrels", str(root / "qrels.tsv"), "--mode", "adaptive",
                             "--fail-on-fallback"])
            self.assertEqual(code, 2)
            self.assertIn("降级", errors.getvalue())

    def test_map_and_paired_bootstrap_are_reproducible(self):
        qrels = {"q1": {"d1": 1.0}, "q2": {"d2": 1.0}}
        baseline = {"q1": [("d9", 2.0), ("d1", 1.0)], "q2": [("d2", 1.0)]}
        improved = {"q1": [("d1", 2.0)], "q2": [("d2", 1.0)]}
        first = paired_bootstrap(qrels, baseline, improved, samples=200, seed=7)
        second = paired_bootstrap(qrels, baseline, improved, samples=200, seed=7)
        self.assertEqual(first, second)
        self.assertGreater(first.observed_difference, 0.0)
        self.assertLessEqual(first.ci_lower, first.observed_difference)
        self.assertGreaterEqual(first.ci_upper, first.observed_difference)


if __name__ == "__main__":
    unittest.main()
