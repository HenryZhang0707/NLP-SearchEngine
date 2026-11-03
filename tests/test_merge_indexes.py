"""Real public-build oracles and process-death tests; private tiny fixtures only."""
from contextlib import closing
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from contracts.conformance import check_reader
from contracts.index_format import sha256_of
from contracts.api import SearchRequest
from offline import merge_indexes as merger
from offline.compact import CompactIndex
from offline.checkpoint_compact import compile_checkpoint
from offline.checkpoint_build import build
from offline.disk_index import DiskIndex
from online.engine import SearchEngine

ROOT = Path(__file__).resolve().parents[1]


class MergeIndexesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="merge-index-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = [
            dict(doc_id="a", title="世界", text="alpha beta alpha 中文"),
            dict(doc_id="b", title="zero", text="() 😀"),
            dict(doc_id="c", title="中文", text="beta alpha 世界"),
            dict(doc_id="d", title="phrase", text="alpha beta gamma"),
            dict(doc_id="e", title="reverse", text="beta alpha"),
            dict(doc_id="f", title="f", text="delta 中文 alpha"),
        ]
        self.shards = self.make_shards(self.rows)
        self.plan = self.make_plan(self.shards)
        self.target = self.root / "bundle"
        self.state = self.root / "private-state"

    def corpus(self, rows, name):
        path = self.root / (name + ".jsonl")
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return path

    def make_shards(self, rows, prefix="shard"):
        n = len(rows) // 3
        shards = []
        for i, group in enumerate((rows[:n], rows[n:2*n], rows[2*n:])):
            lexical, compact = self.root / (prefix + str(i)), self.root / (prefix + str(i) + "-compact")
            build(self.corpus(group, prefix + str(i)), lexical, "shard", state_dir=self.root / (prefix + str(i) + "-build-state"))
            compile_checkpoint(lexical, compact, state_dir=self.root / (prefix + str(i) + "-compact-state"))
            shards.append((lexical, compact))
        return shards

    def make_plan(self, shards, rows=None):
        rows = self.rows if rows is None else rows
        cursor, ranges = 0, []
        for lexical, _ in shards:
            count = json.loads((lexical / "manifest.json").read_text(encoding="utf-8"))["n_docs"]
            # Freeze the expected canonical documents independently from the
            # raw fixture rows, never by trusting the shard builder's hashes.
            canonical = b"".join((json.dumps(dict(docid=i, doc_id=row["doc_id"], title=row["title"], text=row["text"]),
                                            ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                                 for i, row in enumerate(rows[cursor:cursor + count]))
            ranges.append(dict(start=cursor, stop=cursor + count, expected_docs_sha256=hashlib.sha256(canonical).hexdigest()))
            cursor += count
        return dict(dataset="merged", source_sha256=sha256_of(self.corpus(rows, "independent-plan-source")), n_docs=cursor, shards=ranges)

    def merge(self):
        return merger.merge_indexes(self.shards, self.target, state_dir=self.state,
                                    plan=self.plan, checkpoint_docs=2, checkpoint_terms=2)

    def test_terms_export_writes_only_in_private_state(self):
        connect = sqlite3.connect
        writable_paths = []

        def record(database, *args, **kwargs):
            if not kwargs.get("uri"):
                writable_paths.append(Path(database))
            return connect(database, *args, **kwargs)

        with patch.object(merger.sqlite3, "connect", side_effect=record):
            self.merge()
        self.assertEqual(writable_paths, [
            self.state.resolve() / "merge-state.sqlite",
            self.state.resolve() / "public-terms-export.sqlite",
        ])
        self.assertFalse(writable_paths[-1].exists())
        self.oracle()

    def oracle(self, rows=None):
        rows = self.rows if rows is None else rows
        source, packed = self.root / "monolithic", self.root / "monolithic-compact"
        if not source.exists():
            build(self.corpus(rows, "full"), source, "merged", state_dir=self.root / "full-build-state")
            compile_checkpoint(source, packed, state_dir=self.root / "full-compact-state")
        for role, expected in (("lexical", source), ("compact", packed)):
            actual = self.target / role
            self.assertEqual(sorted(p.name for p in expected.iterdir()), sorted(p.name for p in actual.iterdir()))
            for path in expected.iterdir():
                self.assertEqual(path.read_bytes(), (actual / path.name).read_bytes(), role + "/" + path.name)
        return source, packed

    def test_three_shards_all_13_bytes_equal_public_monolithic_and_queries(self):
        result = self.merge()
        self.assertEqual(result["source_manifest"]["n_docs"], 6)
        source, packed = self.oracle()
        check_reader(DiskIndex(self.target / "lexical"), self.rows)
        with CompactIndex(self.target / "compact") as actual, CompactIndex(packed) as expected:
            check_reader(actual, self.rows)
            engine, oracle = SearchEngine(actual), SearchEngine(expected)
            for query in ('"alpha beta"', "alpha AND beta", "alpha NOT gamma", "中文", "missingword"):
                request = SearchRequest(query, k=10)
                self.assertEqual(engine.search(request).to_dict()["results"], oracle.search(request).to_dict()["results"])
            self.assertEqual([r.doc_id for r in engine.search(SearchRequest('"alpha beta"', k=10)).results], ["a", "d"])
            self.assertEqual(actual.df("alpha"), 5)
            self.assertEqual(actual.doc_length(1), 0)
        before = {str(p.relative_to(self.target)): p.stat().st_mtime_ns for p in self.target.rglob("*") if p.is_file()}
        self.assertEqual(result, self.merge())
        self.assertEqual(before, {str(p.relative_to(self.target)): p.stat().st_mtime_ns for p in self.target.rglob("*") if p.is_file()})
        checkpoint = (self.state / "merge-state.sqlite").read_bytes()
        self.assertNotIn(str(self.root).encode(), checkpoint)

    def test_frozen_plan_rejects_same_n_reordering_and_wrong_docs_sha(self):
        self.shards[0], self.shards[1] = self.shards[1], self.shards[0]
        with self.assertRaisesRegex(ValueError, "frozen plan"):
            self.merge()
        self.assertFalse(self.target.exists())
        self.shards[0], self.shards[1] = self.shards[1], self.shards[0]
        self.plan["shards"][1]["expected_docs_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "frozen plan"):
            self.merge()

    def test_global_duplicate_external_id_is_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[4]["doc_id"] = rows[0]["doc_id"]
        self.shards = self.make_shards(rows, "duplicate")
        self.plan = self.make_plan(self.shards, rows)
        with self.assertRaisesRegex(ValueError, "duplicate document identity"):
            self.merge()
        self.assertFalse(self.target.exists())

    def test_bad_hash_compact_manifest_and_noncontiguous_plan(self):
        path = self.shards[1][0] / "postings.jsonl"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.merge()
        path.write_bytes(original)
        path = self.shards[1][1] / "compact.json"
        metadata = json.loads(path.read_text(encoding="utf-8")); original = path.read_bytes()
        metadata["source_manifest"]["avgdl"] += 1
        path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "compact/source manifest"):
            self.merge()
        path.write_bytes(original)
        self.plan["shards"][1]["start"] += 1
        with self.assertRaisesRegex(ValueError, "contiguous"):
            self.merge()

    def test_existing_target_and_unrelated_state_protected(self):
        self.target.mkdir()
        sentinel = self.target / "user.txt"; sentinel.write_text("protected", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "target exists"):
            self.merge()
        self.assertEqual(sentinel.read_text(), "protected")
        self.target = self.root / "new-target"
        (self.state / "unrelated.txt").write_text("protected", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown files"):
            self.merge()

    def crash(self, event, condition="True"):
        plan_path = self.root / "logical-plan.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        arguments = [str(p) for pair in self.shards for p in pair]
        script = """
import json, os, sys
from pathlib import Path
from offline import merge_indexes as m
def hook(event, state):
    if event == sys.argv[1] and eval(sys.argv[2]): os._exit(73)
m._hook = hook
shards = list(zip(sys.argv[6::2], sys.argv[7::2]))
m.merge_indexes(shards, sys.argv[3], state_dir=sys.argv[4], plan=json.loads(Path(sys.argv[5]).read_text()), checkpoint_docs=2, checkpoint_terms=2)
"""
        result = subprocess.run([sys.executable, "-c", script, event, condition, str(self.target), str(self.state), str(plan_path), *arguments],
                                cwd=ROOT, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))

    def test_hardexit_boundaries_recover_identical_public_bytes(self):
        for i, (event, condition) in enumerate([
                ("bootstrap_pending", "True"), ("bootstrap_committed", "True"),
                ("document_written", "state['docs_done'] == 3"),
                ("files_fsynced", "state['status'] == 'DOCS' and state['docs_done'] == 2"),
                ("db_committed", "state['status'] == 'DOCS' and state['docs_done'] == 2"),
                ("term_written", "state['terms_done'] == 3"),
                ("files_fsynced", "state['status'] == 'TERMS' and state['terms_done'] == 2"),
                ("db_committed", "state['status'] == 'TERMS' and state['terms_done'] == 2"),
                ("db_committed", "state['status'] == 'VERIFY'"),
                ("db_committed", "state['status'] == 'READY'"), ("published", "True")]):
            with self.subTest(event=event, condition=condition):
                self.target, self.state = self.root / ("crash" + str(i)), self.root / ("state" + str(i))
                self.crash(event, condition)
                self.merge(); self.oracle()

    def test_resume_rejects_changed_frozen_plan(self):
        self.crash("db_committed", "state['docs_done'] == 2")
        self.plan["source_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.merge()

    def test_large_vocabulary_sqlite_pages_and_huge_term_are_bounded_and_byte_equal(self):
        self.rows = [dict(doc_id=str(i), title="", text="alpha " * 18000 + " ".join("word%04d" % j for j in range(1205))) for i in range(6)]
        self.shards = self.make_shards(self.rows, "pages")
        self.plan = self.make_plan(self.shards)
        self.merge(); self.oracle()
        with closing(sqlite3.connect(self.target / "compact/terms.sqlite")) as connection:
            self.assertGreater(connection.execute("PRAGMA page_count").fetchone()[0], 2)
        # Crash after the first public output database transaction (>1000
        # terms). Rebuilding that derived DB must reproduce native headers.
        self.target, self.state = self.root / "export-crash", self.root / "export-crash-state"
        self.crash("terms_export_chunk")
        self.merge(); self.oracle()

    def test_checkpoint_cannot_be_inside_the_staging_directory(self):
        digest = "a" * 64
        stage = self.target.parent / ("." + self.target.name + ".merge-" + digest[:20])
        self.state = stage / "private"
        with patch.object(merger, "_path_id", return_value=digest):
            with self.assertRaisesRegex(ValueError, "staging.*disjoint"):
                self.merge()
        self.assertFalse(stage.exists())
        self.assertFalse(self.target.exists())

    def test_fresh_checkpoint_refuses_an_orphan_staging_directory(self):
        stage = self.target.parent / ("." + self.target.name + ".merge-" + merger._path_id(self.state)[:20])
        lexical = stage / "lexical"
        lexical.mkdir(parents=True)
        protected = lexical / "docs.jsonl"
        protected.write_bytes(b"protected")
        with self.assertRaisesRegex(ValueError, "unexpected staging directory"):
            self.merge()
        self.assertEqual(protected.read_bytes(), b"protected")
        self.assertFalse(self.target.exists())

    def test_ready_staged_payload_corruption_is_rejected(self):
        self.crash("db_committed", "state['status'] == 'READY'")
        stage = self.target.parent / ("." + self.target.name + ".merge-" + merger._path_id(self.state)[:20])
        path = stage / "compact/postings.bin"
        path.write_bytes(path.read_bytes() + b"extra")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.merge()
        self.assertFalse(self.target.exists())

    def test_self_rehashed_invalid_tf_position_and_source_binary_binding_are_rejected(self):
        path = self.shards[0][1] / "postings.bin"
        metadata_path = self.shards[0][1] / "compact.json"
        original, metadata_original = path.read_bytes(), metadata_path.read_bytes()
        # First posting is alpha@doc0, tf=2, positions [0,2].
        for i, (offset, value, expected) in enumerate([(4, 0, "docid/tf"), (12, 0, "position"), (12, 1, "canonical content")]):
            with self.subTest(offset=offset, value=value):
                data = bytearray(original); struct.pack_into("<I", data, offset, value)
                path.write_bytes(data)
                metadata = json.loads(metadata_original)
                metadata["files"]["postings.bin"] = sha256_of(path)
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                self.target, self.state = self.root / ("invalid" + str(i)), self.root / ("invalid-state" + str(i))
                with self.assertRaisesRegex(ValueError, expected):
                    self.merge()
                self.assertFalse(self.target.exists())
        path.write_bytes(original); metadata_path.write_bytes(metadata_original)

    def test_all_zero_token_shards(self):
        self.rows = [dict(doc_id=str(i), title="", text="😀 ( )") for i in range(3)]
        self.shards = self.make_shards(self.rows, "zero")
        self.plan = self.make_plan(self.shards)
        result = self.merge(); self.oracle()
        self.assertEqual(result["terms"], 0)


if __name__ == "__main__":
    unittest.main()
