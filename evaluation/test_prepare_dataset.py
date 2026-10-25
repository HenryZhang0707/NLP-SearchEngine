"""Tests for the external dataset normalizer and split manifest."""

import json
import tempfile
import unittest
from pathlib import Path

from evaluation.prepare_dataset import prepare_dataset
from evaluation.evaluate import EvaluationError


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


if __name__ == "__main__":
    unittest.main()
