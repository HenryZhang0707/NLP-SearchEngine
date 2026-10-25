"""Tests for the public DuReader-Retrieval format adapter."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from evaluation.evaluate import EvaluationError, load_queries
from evaluation.prepare_dureader import main, prepare_dureader


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

    def test_query_jsonl_roundtrips_line_breaks_without_phantom_ids(self):
        queries = [
            "first?\nq999 extra-query",
            "first?\r\nq999 extra-query",
            "心肌\t梗死\r第二行\u2028第三行\u2029第四行\u0085第五行",
        ]
        for query in queries:
            with self.subTest(query=query), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "release"
                out = Path(directory) / "out"
                self._release(root)
                path = root / "dev" / "dev.json"
                records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                records[0]["question"] = query
                path.write_text("\n".join(json.dumps(record) for record in records) + "\n",
                                encoding="utf-8")
                (root / "dev" / "q2qid.dev.json").write_text(
                    json.dumps({record["question"]: record["question_id"] for record in records}),
                    encoding="utf-8",
                )
                manifest = prepare_dureader(root, out, expected_queries=2, expected_qrels=2)
                query_path = out / "queries.dev.jsonl"
                loaded = load_queries(query_path)
                self.assertEqual([(record.qid, record.query) for record in loaded],
                                 [("q1", query), ("q2", "second?")])
                self.assertEqual(len(loaded), manifest["counts"]["queries"])
                raw = query_path.read_bytes()
                self.assertEqual(len(raw.decode("utf-8").splitlines()), 2)
                self.assertEqual(manifest["files"][query_path.name], {
                    "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                })
                self.assertFalse((out / "queries.dev.tsv").exists())

    def test_archive_sha256_requires_archive_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "release"
            out = Path(directory) / "out"
            self._release(root)
            with self.assertRaisesRegex(EvaluationError, "--archive"):
                prepare_dureader(root, out, archive_sha256="0" * 64)
            self.assertFalse(out.exists())
            errors, output = io.StringIO(), io.StringIO()
            with redirect_stderr(errors), redirect_stdout(output):
                code = main(["--root", str(root), "--out", str(out),
                             "--archive-sha256", "0" * 64])
            self.assertEqual(code, 2)
            self.assertIn("--archive", errors.getvalue())
            self.assertEqual(output.getvalue(), "")
            self.assertFalse(out.exists())

    def test_mismatched_archive_sha256_does_not_create_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "release"
            out = Path(directory) / "out"
            self._release(root)
            archive = Path(directory) / "archive.tar.gz"
            archive.write_bytes(b"checksum fixture")
            with self.assertRaisesRegex(EvaluationError, "SHA256"):
                prepare_dureader(root, out, archive=archive, archive_sha256="0" * 64)
            self.assertFalse(out.exists())

    def test_matching_archive_sha256_accepts_uppercase_and_records_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "release"
            out = Path(directory) / "out"
            self._release(root)
            archive = Path(directory) / "archive.tar.gz"
            content = b"checksum fixture"
            archive.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            manifest = prepare_dureader(root, out, archive=archive, archive_sha256=digest.upper())
            self.assertEqual(manifest["source_archive"], {
                "path": str(archive), "sha256": digest, "bytes": len(content),
            })

    def test_manifest_records_canonical_official_download_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "release"
            self._release(root)
            manifest = prepare_dureader(root, Path(directory) / "out")
            self.assertEqual(manifest["source_url"],
                             "https://dataset-bj.cdn.bcebos.com/qianyan/"
                             "dureader-retrieval-baseline-dataset.tar.gz")

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
