"""Tests for the public DuReader-Retrieval format adapter."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evaluation.evaluate import EvaluationError
from evaluation.prepare_dureader import prepare_dureader


class DuReaderPreparationTests(unittest.TestCase):
    def _release(self, root: Path) -> None:
        (root / "passage-collection").mkdir(parents=True)
        (root / "dev").mkdir()
        mapping = {str(index): f"p{index}" for index in range(4)}
        (root / "passage-collection" / "passage2id.map.json").write_text(
            json.dumps(mapping), encoding="utf-8"
        )
        for index in range(4):
            (root / "passage-collection" / f"part-{index:02d}").write_text(
                f"-\t\tpassage {index}\tnull\n", encoding="utf-8"
            )
        dev = [
            {"question_id": "q1", "question": "first?", "answer_paragraphs": [{"paragraph_id": "p0", "paragraph_text": "x"}]},
            {"question_id": "q2", "question": "second?", "answer_paragraphs": [
                {"paragraph_id": "p1", "paragraph_text": "x"},
                {"paragraph_id": "p1", "paragraph_text": "x"},
            ]},
        ]
        (root / "dev" / "dev.json").write_text(
            "\n".join(json.dumps(item) for item in dev) + "\n", encoding="utf-8"
        )
        (root / "dev" / "q2qid.dev.json").write_text(
            json.dumps({"first?": "q1", "second?": "q2"}), encoding="utf-8"
        )

    def test_converts_stream_format_and_deduplicates_qrels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "release"
            out = Path(directory) / "out"
            self._release(root)
            manifest = prepare_dureader(root, out, expected_queries=2, expected_qrels=2)
            self.assertEqual(manifest["counts"], {"documents": 4, "queries": 2, "qrels": 2})
            self.assertEqual(len((out / "corpus.jsonl").read_text(encoding="utf-8").splitlines()), 4)
            self.assertEqual(len((out / "qrels.dev.tsv").read_text(encoding="utf-8").splitlines()), 2)
            self.assertEqual(manifest["split"], "dev")

    def test_rejects_missing_part(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "release"
            self._release(root)
            (root / "passage-collection" / "part-03").unlink()
            with self.assertRaises(EvaluationError):
                prepare_dureader(root, Path(directory) / "out")

    def test_rejects_duplicate_passage_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "release"
            self._release(root)
            mapping_path = root / "passage-collection" / "passage2id.map.json"
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            mapping["3"] = mapping["2"]
            mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
            with self.assertRaises(EvaluationError):
                prepare_dureader(root, Path(directory) / "out")

    def test_rejects_qrel_reference_outside_passage_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "release"
            self._release(root)
            dev_path = root / "dev" / "dev.json"
            records = [json.loads(line) for line in dev_path.read_text(encoding="utf-8").splitlines()]
            records[0]["answer_paragraphs"][0]["paragraph_id"] = "missing"
            dev_path.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
            with self.assertRaises(EvaluationError):
                prepare_dureader(root, Path(directory) / "out")


if __name__ == "__main__":
    unittest.main()
