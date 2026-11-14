import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.evaluate import EvaluationError, load_run
from evaluation.external_run import main, validate_external_run


class ExternalRunTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.corpus = self.root / "corpus.jsonl"
        self.queries = self.root / "queries.jsonl"
        self.qrels = self.root / "qrels.tsv"
        self.run = self.root / "reference.trec"
        self.exceptions = self.root / "exceptions.json"
        self.corpus.write_text('{"doc_id":"d1","title":"A","text":"甲"}\n'
                               '{"doc_id":"d2","title":"B","text":"乙"}\n', encoding="utf-8")
        self.queries.write_text('{"qid":"q2","query":"甲"}\n'
                                '{"qid":"q1","query":"乙"}\n', encoding="utf-8")
        self.qrels.write_text("q1 0 d2 2\n", encoding="utf-8")
        self.run.write_text("q1 Q0 d1 2 -.5 ref\nq2 Q0 d1 1 2.0e+00 ref\n"
                             "q1 Q0 d2 1 2 ref\nq2 Q0 d2 2 1 ref\n", encoding="utf-8")

    def validate(self, **options):
        return validate_external_run(self.corpus, self.queries, self.qrels, self.run,
                                     depth=2, k=1, map_k=2, **options)

    @staticmethod
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def policy(self, short):
        self.exceptions.write_text(json.dumps({
            "schema_version": 1,
            "corpus_sha256": self.digest(self.corpus),
            "queries_sha256": self.digest(self.queries),
            "qrels_sha256": self.digest(self.qrels),
            "depth": 2,
            "short_queries": short,
        }), encoding="utf-8")
        return self.exceptions

    def test_scores_rank_order_and_preserves_unjudged_query(self):
        result = self.validate()
        self.assertEqual(result["run_tag"], "ref")
        self.assertEqual(result["input_sha256"]["run"], self.digest(self.run))
        self.assertEqual(result["input_sha256"]["corpus"], self.digest(self.corpus))
        self.assertEqual(result["counts"]["queries_with_qrels"], 1)
        self.assertEqual(result["counts"]["run_rows"], 4)
        self.assertEqual(result["metrics"]["ndcg_at_1"], 0.5)
        self.assertEqual(result["metrics"]["map_at_2"], 0.5)
        self.assertEqual([row["qid"] for row in result["metrics"]["per_query"]], ["q2", "q1"])
        self.assertIsNone(result["metrics"]["mean_latency_ms"])
        self.assertIsNone(result["metrics"]["routing"])

    def test_run_structure_and_membership(self):
        for content, error in (
            ("q1 d1 1 1\n", "expected qid Q0 docid rank score tag"),
            ("q1 BAD d1 1 1 ref\n", "expected qid Q0 docid rank score tag"),
            ("q1 Q0 d1 1 1 ref extra\n", "expected qid Q0 docid rank score tag"),
            ("q1 Q0 d1 +1 1 ref\n", "invalid rank or score format"),
            ("q1 Q0 d1 1 1e999 ref\n", "score"),
            ("q1 Q0 d1 1 NaN ref\n", "score"),
            ("q1 Q0 d1 1 1 ref\nq2 Q0 d2 1 1 other\n", "single run tag"),
            ("q3 Q0 d1 1 1 ref\n", "unknown query ID"),
            ("q1 Q0 d3 1 1 ref\n", "unknown corpus document IDs"),
            ("q1 Q0 d1 1 1 ref\nq1 Q0 d1 2 1 ref\n", "duplicate document"),
            ("q1 Q0 d1 1 1 ref\nq1 Q0 d2 1 1 ref\n", "duplicate rank"),
            ("q1 Q0 d1 2 1 ref\n", "ranks must be contiguous"),
        ):
            with self.subTest(error=error, content=content):
                self.run.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(EvaluationError, error):
                    self.validate()
        self.run.write_text("q1 Q0 d1 1 1 ref\nq1 Q0 d1 2 1 ref\n", encoding="utf-8")
        self.assertEqual(load_run(self.run), {"q1": [("d1", 1.0)]})

    def test_rejects_foreign_qrels_ids(self):
        self.qrels.write_text("q3 0 d2 2\n", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "unknown qrels query IDs"):
            self.validate()
        self.qrels.write_text("q1 0 d3 2\n", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "unknown corpus document IDs"):
            self.validate()

    def test_only_exact_short_and_empty_queries_are_admitted(self):
        self.run.write_text("q2 Q0 d1 1 1 ref\n", encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "invalid ranking depth for 2 queries"):
            self.validate()
        path = self.policy({"q1": {"expected_count": 0, "reason": "Frozen unsupported query"},
                            "q2": {"expected_count": 1, "reason": "Only one eligible candidate"}})
        result = self.validate(exceptions=path)
        self.assertEqual(result["input_sha256"]["exceptions"], self.digest(path))
        self.assertEqual(result["counts"]["short_queries"], 2)
        self.assertEqual(result["metrics"]["n_queries"], 2)
        self.assertEqual([row["retrieved"] for row in result["metrics"]["per_query"]], [1, 0])
        with self.run.open("a", encoding="utf-8") as handle:
            handle.write("q2 Q0 d2 2 .5 ref\n")
        with self.assertRaisesRegex(EvaluationError, "exception expected 1, got 2"):
            self.validate(exceptions=path)

    def test_exceptions_bind_all_dataset_inputs(self):
        for path, field in ((self.corpus, "corpus"), (self.queries, "queries"), (self.qrels, "qrels")):
            with self.subTest(field=field):
                saved = path.read_bytes()
                policy = self.policy({})
                path.write_bytes(saved + b"\n")
                try:
                    with self.assertRaisesRegex(EvaluationError, f"exceptions {field}_sha256"):
                        self.validate(exceptions=policy)
                finally:
                    path.write_bytes(saved)

    def test_bad_cutoffs_and_exception_schema(self):
        with self.assertRaisesRegex(EvaluationError, "depth must cover"):
            validate_external_run(self.corpus, self.queries, self.qrels, self.run, depth=10)
        for short, message in (
            ({"q3": {"expected_count": 0, "reason": "unknown"}}, "unknown exceptions query ID"),
            ({"q1": {"expected_count": False, "reason": "wrong type"}}, "invalid expected_count"),
            ({"q1": {"expected_count": 2, "reason": "full run"}}, "invalid expected_count"),
            ({"q1": {"expected_count": 0, "reason": " "}}, "reason must contain"),
        ):
            with self.subTest(short=short):
                with self.assertRaisesRegex(EvaluationError, message):
                    self.validate(exceptions=self.policy(short))

    def test_cli_never_overwrites_an_input_or_creates_a_receipt_for_failure(self):
        args = ["--corpus", str(self.corpus), "--queries", str(self.queries),
                "--qrels", str(self.qrels), "--run", str(self.run), "--depth", "2",
                "--k", "1", "--map-k", "2", "--output-json"]
        original = self.run.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main([*args, str(self.run)])
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.run.read_bytes(), original)
        receipt = self.root / "result.json"
        self.assertEqual(main([*args, str(receipt)]), 0)
        self.assertEqual(json.loads(receipt.read_bytes())["input_sha256"]["run"], self.digest(self.run))
        self.run.write_text("", encoding="utf-8")
        failed = self.root / "failed.json"
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main([*args, str(failed)])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(failed.exists())


if __name__ == "__main__":
    unittest.main()
