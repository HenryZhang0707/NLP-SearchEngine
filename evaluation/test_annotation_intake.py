import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.annotation_intake import import_human_judgments
from evaluation.annotation_packet import export_annotation_packet
from evaluation.evaluate import EvaluationError, load_qrels
from evaluation.test_annotation_packet import _candidate


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(path, rows):
    path.write_bytes("".join(json.dumps(row) + "\n" for row in rows).encode("utf-8"))


class AnnotationIntakeTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.candidates = root / "candidates.jsonl"
        self.packet = root / "packet.jsonl"
        self.mapping = root / "map.json"
        self.plan = root / "plan.json"
        self.first = root / "review-a.jsonl"
        self.second = root / "review-b.jsonl"
        self.adjudication = root / "adjudication.jsonl"
        self.qrels = root / "qrels.tsv"
        self.receipt = root / "receipt.json"
        _write_jsonl(self.candidates, [_candidate("q1", "d1"), _candidate("q1", "d2"),
                                       _candidate("q2", "d1")])
        export_annotation_packet([self.candidates], self.packet, self.mapping)
        self.map_value = json.loads(self.mapping.read_bytes())
        pair_ids = {(pair["query_id"], pair["doc_id"]): pair_id
                    for pair_id, pair in self.map_value["pairs"].items()}
        self.a = pair_ids["q1", "d1"]
        self.b = pair_ids["q1", "d2"]
        self.c = pair_ids["q2", "d1"]
        self.plan_value = {
            "schema_version": 1,
            "packet_sha256": _sha256(self.packet),
            "private_map_sha256": _sha256(self.mapping),
            "reviewers": {"a": [self.a, self.b], "b": [self.b, self.c]},
        }
        self.freeze_plan()
        self.write_labels(self.first, [(self.a, 0), (self.b, 2)])
        self.write_labels(self.second, [(self.b, 0), (self.c, 0)])
        self.write_labels(self.adjudication, [(self.b, 1)])

    @staticmethod
    def write_labels(path, labels):
        _write_jsonl(path, [{"pair_id": pair_id, "relevance": grade} for pair_id, grade in labels])

    def freeze_plan(self):
        self.plan.write_text(json.dumps(self.plan_value), encoding="utf-8")
        self.plan_sha256 = _sha256(self.plan)

    def intake(self, *, adjudication=True, reviewer_files=None):
        if reviewer_files is None:
            reviewer_files = [("a", self.first), ("b", self.second)]
        return import_human_judgments(
            self.packet, self.mapping, self.plan, self.plan_sha256, reviewer_files,
            self.qrels, self.receipt, adjudication=self.adjudication if adjudication else None,
        )

    def assert_no_outputs(self):
        self.assertFalse(self.qrels.exists())
        self.assertFalse(self.receipt.exists())

    def test_adjudicates_and_binds_every_file(self):
        result = self.intake()
        self.assertEqual(load_qrels(self.qrels), {"q1": {"d1": 0, "d2": 1}, "q2": {"d1": 0}})
        self.assertEqual(json.loads(self.receipt.read_bytes()), result)
        self.assertEqual(result["pair_count"], 3)
        self.assertEqual(result["overlap_pair_count"], 1)
        self.assertEqual(result["disputed_pair_count"], 1)
        for entry, path in ((result["plan"], self.plan), (result["packet"], self.packet),
                            (result["private_map"], self.mapping), (result["reviewers"]["a"], self.first),
                            (result["reviewers"]["b"], self.second), (result["adjudication"], self.adjudication),
                            (result["qrels"], self.qrels)):
            with self.subTest(path=path):
                self.assertEqual(entry["sha256"], _sha256(path))
        output_bytes = self.qrels.read_bytes()
        with self.assertRaisesRegex(EvaluationError, "output path already exists"):
            self.intake()
        self.assertEqual(self.qrels.read_bytes(), output_bytes)

    def test_agreement_accepts_independent_files_with_identical_grades(self):
        self.plan_value["reviewers"] = {"a": [self.a, self.b, self.c], "b": [self.a, self.b, self.c]}
        self.freeze_plan()
        self.write_labels(self.first, [(self.a, 0), (self.b, 2), (self.c, 0)])
        self.second.write_bytes(self.first.read_bytes())
        with self.assertRaisesRegex(EvaluationError, "separate judgment files"):
            self.intake(adjudication=False, reviewer_files=[("a", self.first), ("b", self.first)])
        self.assert_no_outputs()
        result = self.intake(adjudication=False)
        self.assertIsNone(result["adjudication"])
        self.assertEqual(result["disputed_pair_count"], 0)

    def test_requires_exact_dispute_adjudication(self):
        with self.assertRaisesRegex(EvaluationError, "adjudication is required"):
            self.intake(adjudication=False)
        self.assert_no_outputs()
        for labels in ([], [(self.a, 0), (self.b, 1)]):
            with self.subTest(labels=labels):
                self.write_labels(self.adjudication, labels)
                with self.assertRaisesRegex(EvaluationError, "coverage mismatch"):
                    self.intake()
                self.assert_no_outputs()

    def test_rejects_invalid_and_incomplete_reviews(self):
        cases = (
            ([(self.a, 0)], "coverage mismatch"),
            ([(self.a, 0), (self.c, 1)], "coverage mismatch"),
            ([(self.a, 0), (self.b, 2), (self.b, 2)], "duplicate judgment"),
            ([(self.a, 0), (self.b, True)], "must be the integer"),
            ([(self.a, 0), (self.b, 1.0)], "must be the integer"),
        )
        for labels, error in cases:
            with self.subTest(labels=labels):
                self.write_labels(self.first, labels)
                with self.assertRaisesRegex(EvaluationError, error):
                    self.intake()
                self.assert_no_outputs()

    def test_rejects_incomplete_and_nonoverlapping_assignments(self):
        for reviewers, error in (
            ({"a": [self.a, self.b], "b": [self.b]}, "coverage mismatch"),
            ({"a": [self.a, self.b], "b": [self.c]}, "must be assigned to multiple reviewers"),
            ({"a": [self.a, self.b, self.c]}, "at least two distinct reviewer IDs"),
        ):
            with self.subTest(reviewers=reviewers):
                self.plan_value["reviewers"] = reviewers
                self.freeze_plan()
                with self.assertRaisesRegex(EvaluationError, error):
                    self.intake()
                self.assert_no_outputs()

    def test_requires_original_plan_packet_and_private_map_bytes(self):
        for path in (self.plan, self.packet, self.mapping):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(EvaluationError, "SHA256 mismatch"):
                    self.intake()
                self.assert_no_outputs()
                path.write_bytes(original)

    def test_rejects_rehashed_but_inconsistent_map(self):
        self.map_value["pairs"][self.a]["document_sha256"] = "0" * 64
        self.mapping.write_text(json.dumps(self.map_value), encoding="utf-8")
        self.plan_value["private_map_sha256"] = _sha256(self.mapping)
        self.freeze_plan()
        with self.assertRaisesRegex(EvaluationError, "document hash does not match content"):
            self.intake()
        self.assert_no_outputs()

    def test_qrels_write_error_leaves_no_receipt(self):
        self.qrels = self.qrels.parent / "missing" / "qrels.tsv"
        with self.assertRaises(OSError):
            self.intake()
        self.assert_no_outputs()


if __name__ == "__main__":
    unittest.main()
