"""Small stdlib-only tests for the HTTP-only evaluator."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from evaluation.evaluate import (
    QueryRecord,
    evaluate_http,
    evaluate_run,
    load_qrels,
    load_queries,
    load_run,
)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        payload = {"results": [
            {"doc_id": "d1", "score": 2.0},
            {"doc_id": "d2", "score": 1.0},
        ] if request["query"] == "q1" else [{"doc_id": "d3", "score": 1.0}]}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


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
            self.assertEqual([q.qid for q in load_queries(query_path)], ["q1", "q2"])

    def test_http_mode_and_run_output(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run.trec"
                summary = evaluate_http(
                    f"http://127.0.0.1:{server.server_port}/search",
                    [QueryRecord("q1", "q1"), QueryRecord("q2", "q2")],
                    {"q1": {"d1": 1.0}, "q2": {"d3": 1.0}},
                    run_output=path,
                )
                self.assertEqual(summary.n_queries, 2)
                self.assertIsNotNone(summary.p95_latency_ms)
                self.assertEqual(len(path.read_text().splitlines()), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
