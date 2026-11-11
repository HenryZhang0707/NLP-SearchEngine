import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.annotation_packet import export_annotation_packet
from evaluation.evaluate import EvaluationError


def _with_hashes(row):
    document = json.dumps([row["doc_id"], row["title"], row["text"]],
                          ensure_ascii=False, separators=(",", ":"))
    return {**row, "document_sha256": hashlib.sha256(document.encode("utf-8")).hexdigest(),
            "query_sha256": hashlib.sha256(row["query"].encode("utf-8")).hexdigest()}


def _candidate(qid, doc_id):
    return _with_hashes({
        "query_id": qid,
        "query": f"问题 {qid[-1]}",
        "doc_id": doc_id,
        "title": f"标题 {doc_id[-1]}",
        "text": f"正文 {doc_id[-1]}\n第二行",
        "source_url": f"https://example.test/{doc_id}",
        "assistant_label": 2,
        "retrieval": {"system": "test-system", "score": 30.0, "rank": 1},
    })


class AnnotationPacketTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.first = self.root / "first.jsonl"
        self.second = self.root / "second.jsonl"
        self.packet = self.root / "reviewer.jsonl"
        self.mapping = self.root / "mapping.json"
        self.rows = [_candidate("query01", "doc01"), _candidate("query02", "doc01"),
                     _candidate("query02", "doc02")]
        self.write(self.first, self.rows[:2])
        self.write(self.second, self.rows[2:])

    @staticmethod
    def write(path, rows):
        path.write_bytes("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode())

    def export(self, **kwargs):
        return export_annotation_packet([self.first, self.second], self.packet, self.mapping, **kwargs)

    def test_public_allowlist_private_mapping_and_determinism(self):
        original = [path.read_bytes() for path in (self.first, self.second)]
        receipt = self.export()
        packet = self.packet.read_bytes()
        mapping = json.loads(self.mapping.read_bytes())
        rows = [json.loads(line) for line in packet.splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(receipt["packet_sha256"], hashlib.sha256(packet).hexdigest())
        self.assertEqual(receipt["inputs"], [
            {"filename": "first.jsonl", "sha256": hashlib.sha256(original[0]).hexdigest(), "pairs": 2},
            {"filename": "second.jsonl", "sha256": hashlib.sha256(original[1]).hexdigest(), "pairs": 1},
        ])
        expected = {(row["query_id"], row["doc_id"]): row for row in self.rows}
        self.assertEqual(list(mapping["pairs"]), [row["pair_id"] for row in rows])
        for row in rows:
            self.assertEqual(set(row), {"pair_id", "query", "title", "text"})
            self.assertRegex(row["pair_id"], "^p-[0-9a-f]{24}$")
            private = mapping["pairs"][row["pair_id"]]
            source = expected[(private["query_id"], private["doc_id"])]
            for name in ("query", "title", "text"):
                self.assertEqual(row[name], source[name])
            for name in ("document_sha256", "query_sha256"):
                self.assertEqual(private[name], source[name])
        for internal in (b"example.test", b"query01", b"doc01", b"test-system",
                         b"retrieval", b"assistant_label", b"document_sha256", b"query_sha256"):
            self.assertNotIn(internal, packet)
        self.assertEqual([path.read_bytes() for path in (self.first, self.second)], original)

        copy_packet, copy_map = self.root / "copy.jsonl", self.root / "copy.json"
        export_annotation_packet([self.first, self.second], copy_packet, copy_map)
        self.assertEqual(copy_packet.read_bytes(), packet)
        self.assertEqual(copy_map.read_bytes(), self.mapping.read_bytes())

    def test_seed_changes_opaque_ids_and_binds_input_bytes(self):
        first_receipt = self.export()
        first_ids = set(json.loads(self.mapping.read_bytes())["pairs"])
        other_packet, other_map = self.root / "other.jsonl", self.root / "other.json"
        receipt = export_annotation_packet([self.first, self.second], other_packet, other_map, seed=13)
        self.assertNotEqual(receipt["packet_sha256"], first_receipt["packet_sha256"])
        self.assertTrue(first_ids.isdisjoint(json.loads(other_map.read_bytes())["pairs"]))
        self.second.write_bytes(self.second.read_bytes() + b"\n")
        changed_packet, changed_map = self.root / "changed.jsonl", self.root / "changed.json"
        receipt = export_annotation_packet([self.first, self.second], changed_packet, changed_map)
        self.assertNotEqual(receipt["packet_sha256"], first_receipt["packet_sha256"])

    def test_rejects_duplicate_pairs_and_inconsistent_identity(self):
        for row, message in (
            (self.rows[0], "duplicate candidate pair"),
            ({**self.rows[0], "assistant_label": 0}, "duplicate candidate pair"),
            (_with_hashes({**self.rows[2], "query": "冲突的查询"}), "inconsistent query identity"),
            (_with_hashes({**self.rows[1], "query_id": "query03", "text": "冲突的正文"}),
             "inconsistent document identity"),
            ({**self.rows[2], "document_sha256": "wrong"}, "must be a lowercase SHA256"),
            ({**self.rows[2], "text": "changed"}, "document hash does not match content"),
            ({**self.rows[2], "query": "changed"}, "query hash does not match content"),
        ):
            with self.subTest(message=message):
                self.write(self.second, [row])
                with self.assertRaisesRegex(EvaluationError, message):
                    self.export()
                self.assertFalse(self.packet.exists())
                self.assertFalse(self.mapping.exists())

    def test_rejects_nonfresh_paths_and_pair_budget(self):
        with self.assertRaisesRegex(EvaluationError, "max_pairs=2"):
            self.export(max_pairs=2)
        self.assertFalse(self.packet.exists())
        self.assertFalse(self.mapping.exists())
        self.mapping.write_bytes(b"existing")
        with self.assertRaisesRegex(EvaluationError, "output path already exists"):
            self.export()
        self.assertEqual(self.mapping.read_bytes(), b"existing")
        self.assertFalse(self.packet.exists())
        with self.assertRaisesRegex(EvaluationError, "distinct paths"):
            export_annotation_packet([self.first], self.packet, self.packet)

    def test_output_failure_leaves_no_partial_mapping(self):
        missing = self.root / "missing" / "packet.jsonl"
        with self.assertRaises(OSError):
            export_annotation_packet([self.first], missing, self.mapping)
        self.assertFalse(self.mapping.exists())


if __name__ == "__main__":
    unittest.main()
