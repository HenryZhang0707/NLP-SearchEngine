"""Durability checks use disposable directories, never fixture indexes."""

from contextlib import closing
import json
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from contracts import analyzer
from contracts.index_format import BM25Spec, DOCS, MANIFEST, OFFSETS, POSTINGS, STATS, sha256_of
from offline import checkpoint_build as builder
from offline.stream_build import build as stream_build


ROOT = Path(__file__).resolve().parents[1]
FILES = (DOCS, OFFSETS, POSTINGS, STATS, MANIFEST)


class CheckpointBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="checkpoint-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.corpus = self.root / "corpus.jsonl"
        self.out = self.root / "out"
        self.state = self.root / "state"
        self.rows = [
            {"doc_id": "a", "title": "中文 😀", "text": "Hello 世界 hello"},
            {"doc_id": "zero", "title": "Empty", "text": "... 😀 !"},
            {"doc_id": "c", "title": "Newline\nTab\t", "text": "中文 abc 123"},
            {"doc_id": "d", "title": "", "text": ""},
            {"doc_id": "e", "title": "终", "text": "终 Last last"},
        ]
        self.corpus.write_bytes(("\n" + "\n\n".join(json.dumps(row, ensure_ascii=False)
            for row in self.rows) + "\n \n").encode("utf-8"))

    def build(self, **kwargs):
        options = {"state_dir": self.state, "checkpoint_docs": 2}
        options.update(kwargs)
        return builder.build(self.corpus, self.out, "mixed", **options)

    def checkpoint(self):
        with closing(sqlite3.connect(self.state / "checkpoint.sqlite")) as database:
            state = json.loads(database.execute("SELECT value FROM checkpoint WHERE id=1").fetchone()[0])
            counts = tuple(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                           for table in ("ids", "lengths", "postings"))
        return state, counts

    def crash(self, event, n_docs=2):
        script = """
import json, os, sys
from pathlib import Path
from offline import checkpoint_build as b
def hook(event, state):
    if event == sys.argv[4] and state['n_docs'] == int(sys.argv[5]):
        os._exit(73)
b._checkpoint_hook = hook
b.build(Path(sys.argv[1]), Path(sys.argv[2]), 'mixed', state_dir=Path(sys.argv[3]), checkpoint_docs=2)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.corpus), str(self.out),
            str(self.state), event, str(n_docs)], cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))

    def export_crash(self, event, condition="True"):
        script = """
import os, sys
from pathlib import Path
from offline import checkpoint_build as b
b.POSTINGS_CHECKPOINT_TERMS = 2
def hook(event, state):
    if event == sys.argv[4] and eval(sys.argv[5]):
        os._exit(73)
b._checkpoint_hook = hook
b.build(Path(sys.argv[1]), Path(sys.argv[2]), 'mixed', state_dir=Path(sys.argv[3]), checkpoint_docs=2)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.corpus), str(self.out),
            str(self.state), event, condition], cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))

    def assert_oracle(self):
        oracle = self.root / "oracle"
        stream_build(self.corpus, oracle, "mixed")
        for name in FILES:
            self.assertEqual((self.out / name).read_bytes(), (oracle / name).read_bytes(), name)
            self.assertEqual(sha256_of(self.out / name), sha256_of(oracle / name))

    def test_byte_identical_mixed_and_zero_tokens_and_reentry(self):
        manifest = self.build()
        self.assertEqual(manifest.n_docs, 5)
        self.assert_oracle()
        before = {name: ((self.out / name).stat().st_mtime_ns, sha256_of(self.out / name)) for name in FILES}
        self.assertEqual(manifest, self.build(checkpoint_docs=3, progress_every=1))
        after = {name: ((self.out / name).stat().st_mtime_ns, sha256_of(self.out / name)) for name in FILES}
        self.assertEqual(before, after)
        state, counts = self.checkpoint()
        self.assertEqual(state["status"], "PUBLISHED")
        self.assertEqual(counts[:2], (5, 5))

    def test_bootstrap_process_death_recovers_before_and_after_initial_commit(self):
        oracle = self.root / "bootstrap-oracle"
        stream_build(self.corpus, oracle, "mixed")
        for event in ("bootstrap_connected", "bootstrap_pragmas", "bootstrap_schema",
                      "bootstrap_pending", "bootstrap_committed"):
            with self.subTest(event=event):
                self.out = self.root / event
                self.state = self.root / (event + "-state")
                self.crash(event, 0)
                self.build()
                for name in FILES:
                    self.assertEqual((oracle / name).read_bytes(), (self.out / name).read_bytes())

    def test_bootstrap_rejects_unknown_damaged_or_previously_committed_state(self):
        for case in ("table", "dropped", "corrupt", "bad-json", "unknown", "stage", "target"):
            with self.subTest(case=case):
                self.state = self.root / (case + "-state")
                self.out = self.root / (case + "-out")
                self.state.mkdir()
                path = self.state / "checkpoint.sqlite"
                if case == "corrupt":
                    path.write_bytes(b"not a SQLite database")
                else:
                    with closing(sqlite3.connect(path)) as database:
                        if case in ("table", "dropped"):
                            database.execute("CREATE TABLE evidence(value)")
                            if case == "dropped":
                                database.execute("DROP TABLE evidence")
                        elif case == "bad-json":
                            database.execute("CREATE TABLE checkpoint(id,value)")
                            database.execute("INSERT INTO checkpoint VALUES(1,'broken')")
                        database.commit()
                if case == "unknown":
                    (self.state / "evidence.txt").write_bytes(b"keep")
                if case == "stage":
                    suffix = hashlib.sha256(str(self.state.resolve()).encode()).hexdigest()[:20]
                    (self.out.parent / f".{self.out.name}.checkpoint-{suffix}").mkdir()
                if case == "target":
                    self.out.mkdir()
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    self.build()
                self.assertEqual(before, path.read_bytes())
                if case == "unknown":
                    self.assertEqual(b"keep", (self.state / "evidence.txt").read_bytes())

    def test_hard_exit_after_file_fsync_before_first_commit(self):
        self.crash("files_fsynced")
        state, counts = self.checkpoint()
        self.assertEqual(state["n_docs"], 0)
        self.assertEqual(counts, (0, 0, 0))
        self.assertGreater((Path(state["stage"]) / DOCS).stat().st_size, 0)
        self.build()
        self.assert_oracle()

    def test_hard_exit_before_later_commit_preserves_previous_checkpoint(self):
        self.crash("files_fsynced", 4)
        state, counts = self.checkpoint()
        self.assertEqual(state["n_docs"], 2)
        self.assertEqual(counts[:2], (2, 2))
        self.assertGreater((Path(state["stage"]) / DOCS).stat().st_size, state["docs_bytes"])
        self.build()
        self.assert_oracle()

    def test_hard_exit_after_commit_replays_no_rows(self):
        self.crash("db_committed")
        state, counts = self.checkpoint()
        self.assertEqual(state["n_docs"], 2)
        self.assertEqual(counts[:2], (2, 2))
        self.build()
        self.assert_oracle()

    def test_export_and_publication_crash_windows(self):
        for event in ("export_committed", "published"):
            with self.subTest(event=event):
                self.out = self.root / event
                self.state = self.root / (event + "-state")
                self.crash(event, 5)
                self.build()
                oracle = self.root / (event + "-oracle")
                stream_build(self.corpus, oracle, "mixed")
                for name in FILES:
                    self.assertEqual((self.out / name).read_bytes(), (oracle / name).read_bytes())

    def test_ingested_export_can_be_rebuilt(self):
        with patch.object(builder, "_export_postings", side_effect=RuntimeError("export fault")):
            with self.assertRaisesRegex(RuntimeError, "export fault"):
                self.build()
        state, _ = self.checkpoint()
        self.assertEqual(state["status"], "EXPORTING_POSTINGS")
        self.assertFalse(self.out.exists())
        self.build()
        self.assert_oracle()

    def test_postings_hard_exit_within_large_term_replays_whole_term(self):
        # Exceed Python 3.14's large buffered-file block before os._exit.
        self.rows[0]["text"] = "same " * 100000
        self.corpus.write_text("\n".join(json.dumps(row) for row in self.rows) + "\n", encoding="utf-8")
        self.export_crash("postings_document_written", "state['writing_term'] == 'same'")
        state, _ = self.checkpoint()
        self.assertEqual(state["status"], "EXPORTING_POSTINGS")
        self.assertNotEqual(state["last_term"], "same")
        self.assertGreater((Path(state["stage"]) / POSTINGS).stat().st_size, state["postings_bytes"])
        self.assertFalse(self.out.exists())
        self.build()
        self.assert_oracle()

    def test_postings_hard_exit_after_fsync_before_metadata_commit(self):
        self.export_crash("postings_files_fsynced", "state['exported_terms'] == 4")
        state, _ = self.checkpoint()
        self.assertEqual(state["exported_terms"], 2)
        self.assertGreater((Path(state["stage"]) / POSTINGS).stat().st_size, state["postings_bytes"])
        self.build()
        self.assert_oracle()

    def test_postings_hard_exit_after_metadata_commit_skips_exported_terms(self):
        self.export_crash("postings_db_committed", "state['exported_terms'] == 2")
        state, _ = self.checkpoint()
        self.assertEqual(state["exported_terms"], 2)
        last_term = state["last_term"]
        written_terms = []

        def record(event, state):
            if event == "postings_document_written":
                written_terms.append(state["writing_term"])

        with patch.object(builder, "_checkpoint_hook", side_effect=record):
            self.build()
        self.assertTrue(written_terms)
        self.assertTrue(all(term > last_term for term in written_terms))
        self.assert_oracle()

    def test_postings_finish_and_completed_file_hash_resume(self):
        self.export_crash("postings_db_committed", "state['status'] == 'HASHING'")
        state, _ = self.checkpoint()
        self.assertEqual(state["status"], "HASHING")
        self.export_crash("hash_committed", "state['hash_file'] == 'docs.jsonl'")
        state, _ = self.checkpoint()
        self.assertEqual(set(state["hash_cache"]), {DOCS})
        original_hash = builder.sha256_of

        def reject_completed_rehash(path):
            if Path(path).name == DOCS:
                raise AssertionError("completed immutable file must not be rehashed")
            return original_hash(path)

        with patch.object(builder, "sha256_of", side_effect=reject_completed_rehash):
            self.build()
        self.assert_oracle()

    def test_hash_cache_detects_immutable_stage_change(self):
        self.export_crash("hash_committed", "state['hash_file'] == 'docs.jsonl'")
        state, _ = self.checkpoint()
        with (Path(state["stage"]) / DOCS).open("ab") as output:
            output.write(b"changed")
        with self.assertRaisesRegex(ValueError, "immutable staged file changed"):
            self.build()
        self.assertFalse(self.out.exists())

    def test_input_config_analyzer_and_code_binding_fail_closed(self):
        self.crash("db_committed")
        original = self.corpus.read_bytes()
        self.corpus.write_bytes(original.replace(b"Hello", b"HELLO"))
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.build()
        self.corpus.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.build(bm25=BM25Spec(k1=1.3))
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            builder.build(self.corpus, self.out, "different", state_dir=self.state)
        with patch.object(analyzer, "config_hash", return_value="a" * 64):
            with self.assertRaisesRegex(ValueError, "binding mismatch"):
                self.build()
        hashes = builder._code_hashes()
        hashes["stream_build"] = "f" * 64
        with patch.object(builder, "_code_hashes", return_value=hashes):
            with self.assertRaisesRegex(ValueError, "binding mismatch"):
                self.build()
        self.assertFalse(self.out.exists())
        self.build()
        self.assert_oracle()

    def test_illegal_tail_keeps_last_checkpoint_and_repair_cannot_rebind(self):
        with self.corpus.open("ab") as output:
            output.write(b"{invalid\n")
        with self.assertRaisesRegex(ValueError, "line 12"):
            self.build()
        self.assertFalse(self.out.exists())
        state, counts = self.checkpoint()
        self.assertEqual(state["n_docs"], 4)
        self.assertEqual(counts[:2], (4, 4))
        self.assertEqual((Path(state["stage"]) / DOCS).stat().st_size, state["docs_bytes"])
        self.corpus.write_bytes(self.corpus.read_bytes().replace(b"{invalid\n", b""))
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.build()

    def test_duplicate_after_checkpoint_is_rejected(self):
        with self.corpus.open("ab") as output:
            output.write((json.dumps(self.rows[0]) + "\n").encode())
        with self.assertRaisesRegex(ValueError, "unique"):
            self.build()
        state, counts = self.checkpoint()
        self.assertEqual(state["n_docs"], 4)
        self.assertEqual(counts[:2], (4, 4))
        self.assertFalse(self.out.exists())

    def test_lock_is_process_exclusive_and_death_releases(self):
        script = """
import sys
from pathlib import Path
from offline.checkpoint_build import _state_lock
with _state_lock(Path(sys.argv[1])):
    print('LOCKED', flush=True)
    sys.stdin.read()
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(self.state)], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            with self.assertRaisesRegex(ValueError, "locked"):
                self.build()
            child.kill()
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            for pipe in (child.stdin, child.stdout, child.stderr):
                pipe.close()
        self.build()
        self.assert_oracle()

    def test_nonempty_output_and_unknown_state_are_protected(self):
        self.out.mkdir()
        sentinel = self.out / "user-file"
        sentinel.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "protected"):
            self.build()
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.state = self.root / "unsafe-state"
        self.state.mkdir()
        (self.state / "build.sqlite").write_bytes(b"old unsafe evidence")
        with self.assertRaisesRegex(ValueError, "unknown files"):
            self.build()
        self.assertEqual((self.state / "build.sqlite").read_bytes(), b"old unsafe evidence")

    def test_published_corruption_is_not_overwritten(self):
        self.build()
        (self.out / DOCS).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.build()
        self.assertEqual((self.out / DOCS).read_bytes(), b"changed")

    def test_empty_and_bad_utf8_corpus_fail_without_publication(self):
        for content in (b" \n", b"\xff\n"):
            with self.subTest(content=content):
                self.state = self.root / ("state-" + content.hex())
                self.corpus.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.build()
                self.assertFalse(self.out.exists())
                state, counts = self.checkpoint()
                self.assertEqual(state["n_docs"], 0)
                self.assertEqual(counts, (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
