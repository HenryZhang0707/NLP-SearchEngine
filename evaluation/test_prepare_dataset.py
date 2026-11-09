"""Tests for the external dataset normalizer and split manifest."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from evaluation import prepare_dataset as prepare_module
from evaluation.prepare_dataset import prepare_dataset, prepare_query_splits
from evaluation.evaluate import EvaluationError, load_queries


class PrepareDatasetTests(unittest.TestCase):
    def _inputs(self, root: Path):
        corpus = root / "corpus.jsonl"
        corpus.write_text("\n".join(json.dumps(item) for item in [
            {"_id": "d1", "title": "A", "text": "alpha"},
            {"id": "d2", "contents": "beta"},
        ]) + "\n", encoding="utf-8")
        queries = root / "queries.jsonl"
        queries.write_text("\n".join(json.dumps(item) for item in [
            {"id": "q1", "text": "alpha"}, {"qid": "q2", "query": "beta"},
        ]) + "\n", encoding="utf-8")
        qrels = root / "qrels.tsv"
        qrels.write_text("q1 0 d1 1\nq2 d2 1\n", encoding="utf-8")
        return corpus, queries, qrels

    def test_normalizes_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            first = prepare_dataset(*inputs, root / "one", seed=9,
                                    dev_fraction=0.25, test_fraction=0.5,
                                    expected_qrels=2)
            second = prepare_dataset(*inputs, root / "two", seed=9,
                                     dev_fraction=0.25, test_fraction=0.5)
            self.assertEqual(first["splits"], second["splits"])
            self.assertEqual(first["counts"], {"documents": 2, "queries": 2, "qrels": 2})
            self.assertEqual((root / "one" / "corpus.jsonl").read_text(),
                             '{"doc_id":"d1","title":"A","text":"alpha"}\n{"doc_id":"d2","title":"","text":"beta"}\n')
            self.assertEqual(first["files"]["corpus.jsonl"]["sha256"],
                             second["files"]["corpus.jsonl"]["sha256"])

    def test_rejects_qrels_pointing_outside_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, queries, qrels = self._inputs(root)
            qrels.write_text("q1 0 missing 1\n", encoding="utf-8")
            with self.assertRaises(EvaluationError):
                prepare_dataset(corpus, queries, qrels, root / "out")

    def test_rejects_unexpected_qrel_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, queries, qrels = self._inputs(root)
            with self.assertRaises(EvaluationError):
                prepare_dataset(corpus, queries, qrels, root / "out", expected_qrels=9839)

    def test_rejects_nonempty_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, queries, qrels = self._inputs(root)
            out = root / "out"
            out.mkdir()
            (out / "old-manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(EvaluationError):
                prepare_dataset(corpus, queries, qrels, out)

    def test_multiline_queries_round_trip_as_jsonl_and_tsv_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus, queries, qrels = self._inputs(root)
            expected = {"q1": "alpha\tbeta\n世界\rnext", "q2": "beta"}
            queries.write_text("".join(json.dumps({"qid": qid, "query": text}) + "\n"
                                       for qid, text in expected.items()), encoding="utf-8")
            out = root / "auto"
            manifest = prepare_dataset(corpus, queries, qrels, out)
            self.assertEqual(manifest["query_format"], "jsonl")
            restored = {}
            for path in out.glob("queries.*.jsonl"):
                self.assertNotIn(b"\r", path.read_bytes())
                if path.stat().st_size:
                    restored.update((record.qid, record.query) for record in load_queries(path))
            self.assertEqual(restored, expected)
            self.assertFalse(list(out.glob("queries.*.tsv")))
            with self.assertRaisesRegex(EvaluationError, "JSONL"):
                prepare_dataset(corpus, queries, qrels, root / "bad", query_format="tsv")
            self.assertFalse((root / "bad").exists())
            # JSON permits literal Unicode separators inside strings. They are
            # not physical JSONL record delimiters and must survive normalization.
            expected["q1"] = "alpha\u2028beta\u2029世界"
            queries.write_text("".join(json.dumps({"qid": qid, "query": text}, ensure_ascii=False) + "\n"
                                       for qid, text in expected.items()), encoding="utf-8")
            unicode_out = root / "unicode"
            self.assertEqual(prepare_dataset(corpus, queries, qrels, unicode_out)["query_format"], "jsonl")
            actual = {}
            for path in unicode_out.glob("queries.*.jsonl"):
                if path.stat().st_size:
                    actual.update((record.qid, record.query) for record in load_queries(path))
            self.assertEqual(actual, expected)

    def test_query_only_cli_does_not_load_or_publish_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, queries, qrels = self._inputs(root)
            args = ["--queries-only", "--queries", str(queries), "--qrels", str(qrels),
                    "--out", str(root / "out"), "--expected-queries", "2", "--expected-qrels", "2"]
            with patch.object(prepare_module, "load_corpus", side_effect=AssertionError("unexpected corpus load")), \
                    redirect_stdout(StringIO()) as stdout:
                self.assertEqual(prepare_module.main(args), 0)
            manifest = json.loads(stdout.getvalue())
            self.assertEqual(manifest["scope"], "query-splits-only")
            self.assertEqual(manifest["corpus_validation"], "not_performed")
            self.assertEqual(manifest["counts"], {"queries": 2, "qrels": 2})
            self.assertFalse((root / "out/corpus.jsonl").exists())
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                prepare_module.main([*args, "--expected-docs", "2"])

    def test_query_only_outputs_are_deterministic_and_source_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, queries, qrels = self._inputs(root)
            first = prepare_query_splits(queries, qrels, root / "one", seed=9,
                                         dev_fraction=0.25, test_fraction=0.5)
            second = prepare_query_splits(queries, qrels, root / "two", seed=9,
                                          dev_fraction=0.25, test_fraction=0.5)
            self.assertEqual(first, second)
            for path in (root / "one").iterdir():
                self.assertEqual(path.read_bytes(), (root / "two" / path.name).read_bytes())
                self.assertNotIn(b"\r", path.read_bytes())
            self.assertEqual(first["sources"]["queries"]["sha256"], prepare_module._sha256(queries))
            self.assertEqual(first["sources"]["qrels"]["sha256"], prepare_module._sha256(qrels))

    def test_query_only_rejects_unknown_count_stale_and_whitespace_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, queries, qrels = self._inputs(root)
            with self.assertRaises(EvaluationError):
                prepare_query_splits(queries, qrels, root / "out", expected_qrels=3)
            qrels.write_text("unknown 0 d1 1\n", encoding="utf-8")
            with self.assertRaisesRegex(EvaluationError, "未知 qid"):
                prepare_query_splits(queries, qrels, root / "out")
            self.assertFalse((root / "out").exists())
            self._inputs(root)
            out = root / "out"
            out.mkdir()
            (out / "sentinel").write_bytes(b"protected")
            with self.assertRaises(EvaluationError):
                prepare_query_splits(queries, qrels, out)
            self.assertEqual((out / "sentinel").read_bytes(), b"protected")
            queries.write_text('{"qid":"bad id","query":"alpha"}\n', encoding="utf-8")
            with self.assertRaisesRegex(EvaluationError, "空白"):
                prepare_query_splits(queries, qrels, root / "bad")

    def test_query_only_never_publishes_changed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, queries, qrels = self._inputs(root)
            original = prepare_module._write_splits

            def mutate(*args, **kwargs):
                result = original(*args, **kwargs)
                queries.write_bytes(queries.read_bytes() + b"\n")
                return result

            with patch.object(prepare_module, "_write_splits", side_effect=mutate), \
                    self.assertRaisesRegex(EvaluationError, "发生变化"):
                prepare_query_splits(queries, qrels, root / "out")
            self.assertFalse((root / "out").exists())
            self.assertFalse(list(root.glob(".out.query-splits-*")))


if __name__ == "__main__":
    unittest.main()
