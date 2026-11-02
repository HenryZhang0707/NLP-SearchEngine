"""Private temporary-directory, real process-death compact acceptance."""
from array import array
from contextlib import closing
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from contracts.index_format import REQUIRED_FILES, sha256_of
from offline.stream_build import build
from offline.compact import CompactIndex
from offline.compact_stream import compile_streaming
from offline import checkpoint_compact as compiler

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec("ijson"), "optional ijson required")
class CheckpointCompactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="checkpoint-compact-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.state_dir = self.root / "state"
        self.rows = [
            {"doc_id": "a", "title": "世界", "text": "alpha beta alpha 中文"},
            {"doc_id": "b", "title": "", "text": "() 😀"},
            {"doc_id": "c", "title": "c", "text": "gamma beta alpha"},
            {"doc_id": "d", "title": "d", "text": "delta gamma"},
        ]
        self.corpus = self.root / "corpus.jsonl"
        self.corpus.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows), encoding="utf-8")
        build(self.corpus, self.source, "mixed")

    def compile(self):
        return compiler.compile_checkpoint(self.source, self.target, state_dir=self.state_dir, checkpoint_docs=2)

    def state(self):
        with closing(sqlite3.connect(self.state_dir / "compact-state.sqlite")) as database:
            return json.loads(database.execute("SELECT value FROM checkpoint WHERE id=1").fetchone()[0])

    def crash(self, event, condition="True", checkpoint_terms=1):
        script = """
import os, sys
from pathlib import Path
from offline import checkpoint_compact as c
c.CHECKPOINT_TERMS = int(sys.argv[6])
c.SCAN_BYTES = 64
def hook(event, state):
    if event == sys.argv[4] and eval(sys.argv[5]):
        os._exit(73)
c._hook = hook
c.compile_checkpoint(Path(sys.argv[1]), Path(sys.argv[2]), state_dir=Path(sys.argv[3]), checkpoint_docs=2)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.source), str(self.target),
            str(self.state_dir), event, condition, str(checkpoint_terms)], cwd=ROOT, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors="replace"))

    def oracle(self):
        path = self.root / "oracle"
        if not path.exists():
            compile_streaming(self.source, path)
        for name in compiler.ARTIFACTS:
            if name != "terms.sqlite":
                self.assertEqual((path / name).read_bytes(), (self.target / name).read_bytes(), name)
        with closing(sqlite3.connect(path / "terms.sqlite")) as a, closing(sqlite3.connect(self.target / "terms.sqlite")) as b:
            self.assertEqual(a.execute("SELECT * FROM terms ORDER BY term").fetchall(),
                             b.execute("SELECT * FROM terms ORDER BY term").fetchall())
            self.assertEqual([("terms",)], b.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        with CompactIndex(path) as a, CompactIndex(self.target) as b:
            self.assertEqual(list(a.vocab()), list(b.vocab()))
            for term in a.vocab():
                self.assertEqual(a.postings(term), b.postings(term))
            for docid in range(a.n_docs):
                self.assertEqual(a.doc(docid), b.doc(docid))
                self.assertEqual(a.doc_length(docid), b.doc_length(docid))

    def refresh_manifest(self):
        path = self.source / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["files"] = {name: sha256_of(self.source / name) for name in REQUIRED_FILES}
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_equivalence_zero_tokens_and_success_reentry(self):
        metadata = self.compile()
        self.oracle()
        mtimes = {p.name: p.stat().st_mtime_ns for p in self.target.iterdir()}
        self.assertEqual(metadata, self.compile())
        self.assertEqual(mtimes, {p.name: p.stat().st_mtime_ns for p in self.target.iterdir()})

    def test_bootstrap_process_death_recovers_before_and_after_initial_commit(self):
        for event in ("bootstrap_connected", "bootstrap_pragmas", "bootstrap_schema",
                      "bootstrap_pending", "bootstrap_committed"):
            with self.subTest(event=event):
                self.target = self.root / event
                self.state_dir = self.root / (event + "-state")
                self.crash(event)
                self.compile()
                self.oracle()

    def test_bootstrap_rejects_unknown_damaged_or_previously_committed_state(self):
        for case in ("table", "dropped", "corrupt", "bad-json", "unknown", "stage", "target"):
            with self.subTest(case=case):
                self.state_dir = self.root / (case + "-state")
                self.target = self.root / (case + "-target")
                self.state_dir.mkdir()
                path = self.state_dir / "compact-state.sqlite"
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
                    (self.state_dir / "evidence.txt").write_bytes(b"keep")
                if case == "stage":
                    suffix = hashlib.sha256(str(self.state_dir.resolve()).encode()).hexdigest()[:20]
                    (self.target.parent / ("." + self.target.name + ".checkpoint-compact-" + suffix)).mkdir()
                if case == "target":
                    self.target.mkdir()
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    self.compile()
                self.assertEqual(before, path.read_bytes())
                if case == "unknown":
                    self.assertEqual(b"keep", (self.state_dir / "evidence.txt").read_bytes())

    def test_document_commit_windows(self):
        for event, count in (("docs_fsynced", 0), ("docs_committed", 2)):
            with self.subTest(event=event):
                self.target = self.root / event
                self.state_dir = self.root / (event + "-state")
                self.crash(event, "state['docs_done'] == 2")
                self.assertEqual(self.state()["docs_done"], count)
                self.compile()
                self.oracle()

    def test_offsets_commit_windows(self):
        for event, count in (("offsets_fsynced", 0), ("offsets_committed", 2)):
            with self.subTest(event=event):
                self.target = self.root / event
                self.state_dir = self.root / (event + "-state")
                self.crash(event, "state['offsets_done'] == 2")
                self.assertEqual(self.state()["offsets_done"], count)
                self.compile()
                self.oracle()

    def test_scanner_chunk_and_row_checkpoint_resume(self):
        for event in ("scan_chunk", "scan_committed"):
            with self.subTest(event=event):
                self.target = self.root / event
                self.state_dir = self.root / (event + "-state")
                self.crash(event)
                self.assertEqual(self.state()["status"], "SCAN")
                self.compile()
                self.oracle()

    def test_postings_inside_term_and_commit_windows(self):
        for event, count in (("posting_written", 0), ("postings_fsynced", 0), ("postings_committed", 1)):
            with self.subTest(event=event):
                self.target = self.root / event
                self.state_dir = self.root / (event + "-state")
                self.crash(event)
                self.assertEqual(self.state()["postings_row"], count)
                self.compile()
                self.oracle()

    def test_private_wal_can_checkpoint_at_postings_and_export_commits(self):
        checkpoints = []
        connect = sqlite3.connect

        class ObservedConnection(sqlite3.Connection):
            def commit(database):
                super().commit()
                with closing(database.execute("SELECT value FROM checkpoint WHERE id=1")) as cursor:
                    state = json.loads(cursor.fetchone()[0])
                if state["status"] in ("POSTINGS", "TERMS_SQLITE") and state["postings_row"]:
                    try:
                        result = database.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                    except sqlite3.OperationalError as error:
                        result = str(error)
                    checkpoints.append((state["status"], state["postings_row"], result))

        def observed_connect(path, *args, **kwargs):
            if Path(path) == self.state_dir / "compact-state.sqlite":
                kwargs["factory"] = ObservedConnection
            database = connect(path, *args, **kwargs)
            database.execute("PRAGMA wal_autocheckpoint=1")
            return database

        # Byte checkpoints can occur midway through a fetched page as well.
        with patch.object(compiler.sqlite3, "connect", side_effect=observed_connect), \
                patch.object(compiler, "CHECKPOINT_TERMS", 2), patch.object(compiler, "CHECKPOINT_BYTES", 1):
            self.compile()
        self.assertGreaterEqual(len(checkpoints), 4)
        self.assertEqual({status for status, _, _ in checkpoints}, {"POSTINGS", "TERMS_SQLITE"})
        for status, row, result in checkpoints:
            with self.subTest(status=status, row=row):
                self.assertIsInstance(result, tuple, result)
                self.assertEqual(result[0], 0)
                self.assertEqual(result[1], result[2], "private WAL is pinned at a durable checkpoint")
        self.oracle()

    def test_postings_page_boundary_process_death_preserves_all_artifact_bytes(self):
        reference = self.root / "reference"
        with patch.object(compiler, "CHECKPOINT_TERMS", 2):
            compiler.compile_checkpoint(self.source, reference, state_dir=self.root / "reference-state", checkpoint_docs=2)
        expected = {path.name: path.read_bytes() for path in reference.iterdir()}
        for event, committed in (("postings_fsynced", 2), ("postings_committed", 4)):
            with self.subTest(event=event):
                self.target = self.root / event
                self.state_dir = self.root / (event + "-state")
                self.crash(event, "state['postings_row'] == 4", checkpoint_terms=2)
                self.assertEqual(self.state()["postings_row"], committed)
                with patch.object(compiler, "CHECKPOINT_TERMS", 2):
                    self.compile()
                self.assertEqual(expected, {path.name: path.read_bytes() for path in self.target.iterdir()})
                self.oracle()

    def test_exports_and_publication_windows(self):
        for event in ("exports_finished", "published"):
            with self.subTest(event=event):
                self.target = self.root / event
                self.state_dir = self.root / (event + "-state")
                self.crash(event)
                self.assertEqual(self.state()["status"], "READY")
                self.compile()
                self.oracle()

    def test_source_completed_hash_cache_resume(self):
        self.crash("hash_committed", "state['hashing'] == 'docs.jsonl'")
        state = self.state()
        self.assertEqual(set(state["source_hashes"]), {"docs.jsonl"})
        original = compiler.sha256_of

        def skip_admitted(path):
            if Path(path) == self.source / "docs.jsonl":
                raise AssertionError("completed source hash must be reused")
            return original(path)

        with patch.object(compiler, "sha256_of", side_effect=skip_admitted):
            self.compile()
        self.oracle()

    def test_output_completed_hash_cache_resume(self):
        self.crash("hash_committed", "state['status'] == 'HASHING' and state['hashing'] == 'docs.jsonl'")
        self.assertEqual(set(self.state()["output_hashes"]), {"docs.jsonl"})
        original = compiler.sha256_of

        def skip_completed(path):
            if Path(path).name == "docs.jsonl":
                raise AssertionError("completed hash must be reused")
            return original(path)

        with patch.object(compiler, "sha256_of", side_effect=skip_completed):
            self.compile()
        self.oracle()

    def test_terms_sqlite_export_commit_windows(self):
        for event in ("terms_sqlite_fsynced", "terms_sqlite_committed", "terms_sqlite_finished"):
            with self.subTest(event=event):
                self.target = self.root / event
                self.state_dir = self.root / (event + "-state")
                self.crash(event)
                self.compile()
                self.oracle()

    def test_large_single_posting_leaves_physical_tail_and_recovers(self):
        self.rows[0]["text"] = "alpha " * 100000
        self.source = self.root / "large-source"
        self.corpus.write_text("".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")
        build(self.corpus, self.source, "large")
        self.crash("posting_written")
        state = self.state()
        self.assertEqual(state["postings_row"], 0)
        self.assertGreater((Path(state["stage"]) / "postings.bin").stat().st_size, state["postings_bytes"])
        self.compile()
        self.oracle()

    def test_bad_checksum_refused_before_consumption(self):
        with (self.source / "docs.jsonl").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.compile()
        self.assertEqual(self.state()["docs_done"], 0)
        self.assertFalse(self.target.exists())

    def test_binding_changes_rejected(self):
        self.crash("docs_committed")
        manifest = self.source / "manifest.json"
        raw = manifest.read_bytes()
        manifest.write_bytes(raw + b" ")
        with self.assertRaisesRegex(ValueError, "binding mismatch"):
            self.compile()
        manifest.write_bytes(raw)
        with patch.object(compiler.analyzer, "config_hash", return_value="f" * 64):
            with self.assertRaises(ValueError):
                self.compile()
        original = compiler.sha256_of

        def changed_code(path):
            return "a" * 64 if Path(path).name == "checkpoint_compact.py" else original(path)

        with patch.object(compiler, "sha256_of", side_effect=changed_code):
            with self.assertRaisesRegex(ValueError, "binding mismatch"):
                self.compile()
        self.compile()
        self.oracle()

    def test_concurrent_lock_and_process_death_release(self):
        script = """
import sys
from pathlib import Path
from offline.checkpoint_build import _state_lock
with _state_lock(Path(sys.argv[1])):
    print('LOCKED',flush=True)
    sys.stdin.read()
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(self.state_dir)], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            with self.assertRaisesRegex(ValueError, "locked"):
                self.compile()
        finally:
            child.kill()
            child.wait(timeout=10)
            for pipe in (child.stdin, child.stdout, child.stderr):
                pipe.close()
        self.compile()
        self.oracle()

    def test_invalid_semantics_do_not_publish(self):
        path = self.source / "postings.jsonl"
        rows = path.read_text(encoding="utf-8").splitlines()
        rows.pop(0)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, "totals"):
            self.compile()
        self.assertFalse(self.target.exists())

    def test_all_zero_token_source(self):
        self.source = self.root / "zero-source"
        self.corpus.write_text(json.dumps({"doc_id": "z", "title": "", "text": "()"}) + "\n", encoding="utf-8")
        build(self.corpus, self.source, "zero")
        self.compile()
        self.oracle()

    def test_nonempty_target_and_unknown_state_protected(self):
        self.target.mkdir()
        (self.target / "sentinel").write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "protected"):
            self.compile()
        self.assertEqual((self.target / "sentinel").read_bytes(), b"keep")
        self.state_dir = self.root / "unsafe"
        self.state_dir.mkdir()
        (self.state_dir / "old.sqlite").write_bytes(b"evidence")
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.compile()
        self.assertEqual((self.state_dir / "old.sqlite").read_bytes(), b"evidence")

    def test_high_df_parser_has_bounded_reads_and_memory(self):
        n = 30000
        raw = ('{"docs":[' + ','.join('[%d,1,[0]]' % i for i in range(n)) + '],"df":%d,"term":"common"}' % n).encode()
        class Guarded(io.BytesIO):
            def read(self, size=-1):
                if size < 0 or size > 65536:
                    raise AssertionError("unbounded source read")
                return super().read(size)
        database = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(database.close)
        database.execute("CREATE TABLE documents(docid INTEGER PRIMARY KEY, length INTEGER, total INTEGER)")
        database.executemany("INSERT INTO documents VALUES(?,1,0)", ((i,) for i in range(n)))
        output = io.BytesIO()
        writer = compiler.compact_stream._Writer(output)
        row = compiler._BoundedRow(Guarded(raw), 0, len(raw))
        tracemalloc.start()
        try:
            term, df = compiler._term(iter(compiler.compact_stream._events(row)), database, array('I', [1]) * n, writer, None)
            writer.flush()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual((term, df), ("common", n))
        self.assertEqual(len(output.getvalue()), n * 12)
        self.assertLess(peak, 16 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
