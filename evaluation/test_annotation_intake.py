import hashlib
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.annotation_intake import import_human_judgments, main, prepare_adjudication
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
        self.dispute_packet = root / "disputes.jsonl"
        self.template = root / "adjudication-template.jsonl"
        self.report = root / "review-report.json"
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

    def intake(self, *, adjudication=True, reviewer_files=None, preparation=None):
        if reviewer_files is None:
            reviewer_files = [("a", self.first), ("b", self.second)]
        return import_human_judgments(
            self.packet, self.mapping, self.plan, self.plan_sha256, reviewer_files,
            self.qrels, self.receipt, adjudication=self.adjudication if adjudication else None,
            preparation=preparation,
        )

    def assert_no_outputs(self):
        self.assertFalse(self.qrels.exists())
        self.assertFalse(self.receipt.exists())

    def prepare(self, *, reviewer_files=None):
        if reviewer_files is None:
            reviewer_files = [("a", self.first), ("b", self.second)]
        return prepare_adjudication(
            self.packet, self.mapping, self.plan, self.plan_sha256, reviewer_files,
            self.dispute_packet, self.template, self.report,
        )

    def assert_no_adjudication_outputs(self):
        for path in (self.dispute_packet, self.template, self.report):
            self.assertFalse(path.exists())

    def test_prepares_only_disputes_and_reports_unanimous_overlap(self):
        self.plan_value["reviewers"] = {"a": [self.a, self.b, self.c], "b": [self.a, self.b, self.c]}
        self.freeze_plan()
        self.write_labels(self.first, [(self.a, 0), (self.b, 2), (self.c, 0)])
        self.write_labels(self.second, [(self.b, 0), (self.c, 1), (self.a, 0)])
        result = self.prepare(reviewer_files=[("b", self.second), ("a", self.first)])
        packet_rows = [json.loads(line) for line in self.packet.read_bytes().splitlines()]
        expected = [row for row in packet_rows if row["pair_id"] in {self.b, self.c}]
        self.assertEqual([json.loads(line) for line in self.dispute_packet.read_bytes().splitlines()],
                         expected)
        self.assertEqual([json.loads(line) for line in self.template.read_bytes().splitlines()],
                         [{"pair_id": row["pair_id"], "relevance": None} for row in expected])
        self.assertEqual(json.loads(self.report.read_bytes()), result)
        self.assertEqual(result["pair_count"], 3)
        self.assertEqual(result["overlap_pair_count"], 3)
        self.assertEqual(result["disputed_pair_count"], 2)
        self.assertEqual(result["agreed_overlap_pair_count"], 1)
        self.assertEqual(result["agreement_rate"], 1 / 3)
        self.assertEqual(result["dispute_packet"]["sha256"], _sha256(self.dispute_packet))
        self.assertEqual(result["adjudication_template"]["sha256"], _sha256(self.template))
        self.assert_no_outputs()
        self.adjudication.write_bytes(self.template.read_bytes().replace(b"null", b"1"))
        self.intake()
        self.assertEqual(load_qrels(self.qrels), {"q1": {"d1": 0, "d2": 1}, "q2": {"d1": 1}})

    def test_no_disputes_writes_only_report(self):
        self.write_labels(self.second, [(self.b, 2), (self.c, 0)])
        result = self.prepare()
        self.assertEqual(result["disputed_pair_count"], 0)
        self.assertEqual(result["agreement_rate"], 1.0)
        self.assertIsNone(result["dispute_packet"])
        self.assertIsNone(result["adjudication_template"])
        self.assertFalse(self.dispute_packet.exists())
        self.assertFalse(self.template.exists())
        self.assertTrue(self.report.exists())
        result = self.intake(adjudication=False, preparation=(self.report, _sha256(self.report)))
        self.assertEqual(result["pre_adjudication"]["sha256"], _sha256(self.report))

    def test_bound_preparation_rejects_modified_reviewer_without_dispute_change(self):
        self.prepare()
        digest = _sha256(self.report)
        preparation = (self.report, digest)
        original = self.first.read_bytes()
        self.write_labels(self.first, [(self.a, 1), (self.b, 2)])
        with self.assertRaisesRegex(EvaluationError, "frozen reviewers differs"):
            self.intake(preparation=preparation)
        self.assert_no_outputs()
        self.first.write_bytes(original)
        prepared = json.loads(self.report.read_bytes())
        prepared["dispute_packet"]["sha256"] = "a" * 64
        amended = self.report.with_name("different-preparation.json")
        amended.write_text(json.dumps(prepared), encoding="utf-8")
        with self.assertRaisesRegex(EvaluationError, "SHA256 mismatch"):
            self.intake(preparation=(amended, digest))
        with self.assertRaisesRegex(EvaluationError, "frozen dispute_packet differs"):
            self.intake(preparation=(amended, _sha256(amended)))
        self.assert_no_outputs()
        self.intake(preparation=preparation)
        self.assertEqual(json.loads(self.receipt.read_bytes())["pre_adjudication"],
                         {"filename": self.report.name, "sha256": digest})

    def test_prepare_cli(self):
        out = StringIO()
        with redirect_stdout(out):
            code = main([
                "--packet", str(self.packet), "--private-map", str(self.mapping),
                "--plan", str(self.plan), "--plan-sha256", self.plan_sha256,
                "--reviewer", "a", str(self.first), "--reviewer", "b", str(self.second),
                "--prepare-adjudication", str(self.dispute_packet), str(self.template), str(self.report),
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()),
                         {"pair_count": 3, "overlap_pair_count": 1, "disputed_pair_count": 1})
        self.assertTrue(self.dispute_packet.exists())
        self.assertTrue(self.template.exists())
        self.assertTrue(self.report.exists())

    def test_preparation_rejects_partial_reviews_and_missing_overlap(self):
        self.write_labels(self.second, [(self.c, 0)])
        with self.assertRaisesRegex(EvaluationError, "coverage mismatch"):
            self.prepare()
        self.assert_no_adjudication_outputs()
        self.plan_value["reviewers"] = {"a": [self.a, self.b], "b": [self.c]}
        self.freeze_plan()
        with self.assertRaisesRegex(EvaluationError, "must be assigned to multiple reviewers"):
            self.prepare()
        self.assert_no_adjudication_outputs()

    def test_preparation_keeps_existing_outputs_and_rolls_back_write_failure(self):
        self.dispute_packet.write_bytes(b"existing packet")
        with self.assertRaisesRegex(EvaluationError, "output path already exists"):
            self.prepare()
        self.assertEqual(self.dispute_packet.read_bytes(), b"existing packet")
        self.dispute_packet.unlink()
        original_template = self.template
        self.template = self.dispute_packet
        with self.assertRaisesRegex(EvaluationError, "distinct paths"):
            self.prepare()
        self.assert_no_adjudication_outputs()
        self.template = original_template
        self.dispute_packet = self.dispute_packet.parent / "missing" / "disputes.jsonl"
        with self.assertRaises(OSError):
            self.prepare()
        self.assert_no_adjudication_outputs()

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
