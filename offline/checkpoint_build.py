"""Crash-resumable, zero-dependency v1 builder with durable checkpoints.

Run as ``python -m offline.checkpoint_build --corpus ... --out ...
--state-dir ... --dataset ...``. State is private to this builder; it cannot
resume the unsafe temporary databases produced by ``stream_build``.
The SQLite WAL/FULL commit records only file bytes already flushed and fsynced.
Keep the state directory until the published artifact has been accepted.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unicodedata

from contracts import analyzer, index_format
from contracts.index_format import (
    BM25Spec, DOCS, MANIFEST, OFFSETS, POSTINGS, STATS, Manifest, sha256_of,
)
from offline import stream_build

POSTINGS_CHECKPOINT_TERMS = 1000
POSTINGS_CHECKPOINT_BYTES = 16 * 1024 * 1024


@contextmanager
def _state_lock(state_dir: Path):
    """An OS lock, released on process death; never unlink its inode."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "state.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
            os.fsync(lock.fileno())
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("state-dir is locked by another builder") from error
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _fsync_dir(path: Path) -> None:
    # Windows does not provide directory fsync through the standard library.
    # File flushes and SQLite FULL remain mandatory on both platforms.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _fresh_checkpoint(path: Path, stage: Path, target: Path, *, recovery_files=()) -> bool:
    """Adopt only an empty SQLite bootstrap, never a damaged committed state.

    The initial DDL and checkpoint row commit together. Before that commit,
    process death can leave a zero-byte or schema-version-zero SQLite file.
    Inspect it read-only; do not delete databases, sidecars or user artifacts.
    """
    if not path.exists():
        if any(p.name != "state.lock" for p in path.parent.iterdir()):
            raise ValueError("state-dir contains unknown files; refusing unsafe adoption")
        return True
    known = {"state.lock", path.name, *(path.name + suffix for suffix in ("-wal", "-shm", "-journal"))}
    if any(p.name not in known.union(recovery_files) or not p.is_file() for p in path.parent.iterdir()):
        raise ValueError("state-dir contains unknown files; refusing unsafe adoption")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as probe:
            if probe.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                state = json.loads(probe.execute("SELECT value FROM checkpoint WHERE id=1").fetchone()[0])
                if not isinstance(state, dict) or not {"binding", "stage", "status"} <= state.keys():
                    raise ValueError("not a valid checkpoint database; no unsafe recovery")
                return False
            empty = all(probe.execute("PRAGMA " + name).fetchone()[0] == 0
                        for name in ("schema_version", "user_version", "application_id", "freelist_count"))
            empty = empty and probe.execute("PRAGMA page_count").fetchone()[0] <= 1
    except (sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise ValueError("not a valid checkpoint database; no unsafe recovery") from error
    if not empty or any(p.name in recovery_files for p in path.parent.iterdir()):
        raise ValueError("not an uncommitted empty checkpoint; no unsafe recovery")
    if stage.exists() or target.exists():
        raise ValueError("empty checkpoint has existing stage/target; artifacts are protected")
    return True


def _code_hashes() -> dict:
    return {name: sha256_of(Path(module.__file__)) for name, module in (
        ("checkpoint_build", sys.modules[__name__]),
        ("stream_build", stream_build), ("analyzer", analyzer),
        ("index_format", index_format),
    )}


def _input_identity(path: Path) -> dict:
    before = path.stat()
    digest = sha256_of(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("corpus changed while hashing")
    return {"sha256": digest, "size": after.st_size}


def _save(database, state):
    database.execute("UPDATE checkpoint SET value=? WHERE id=1", (
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    ))


def _checkpoint_hook(event: str, state: dict) -> None:
    """No-op fault-injection seam for subprocess hard-exit tests."""


def _restore_files(stage: Path, state: dict) -> None:
    for name, size in ((DOCS, state["docs_bytes"]), (OFFSETS, state["offsets_bytes"])):
        path = stage / name
        if not path.exists():
            if size:
                raise ValueError(f"missing committed file: {path}")
            path.touch(exist_ok=False)
        if path.stat().st_size < size:
            raise ValueError(f"committed file is truncated: {path}")
        with path.open("r+b") as output:
            output.truncate(size)
            output.flush()
            os.fsync(output.fileno())


def _file_stamp(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _verify_published(out: Path, state: dict, *, cached_stage=False) -> Manifest:
    expected = state["artifact_hashes"]
    if not out.is_dir() or set(p.name for p in out.iterdir()) != set(expected):
        raise ValueError("published artifact is missing or has unexpected files")
    for name, digest in expected.items():
        cached = state.get("hash_cache", {}).get(name)
        if cached_stage and cached and cached["stamp"] == _file_stamp(out / name):
            actual = cached["sha256"]
        else:
            actual = sha256_of(out / name)
        if actual != digest:
            raise ValueError(f"published artifact hash mismatch: {name}")
    return Manifest.from_dict(json.loads((out / MANIFEST).read_text(encoding="utf-8")))


def _ingest(database, state, corpus, stage, checkpoint_docs, progress_every):
    _restore_files(stage, state)
    pending = dict(state)
    try:
        with corpus.open("rb") as source, (stage / DOCS).open("r+b") as docs, \
                (stage / OFFSETS).open("r+b") as offsets:
            source.seek(state["input_offset"])
            docs.seek(state["docs_bytes"])
            offsets.seek(state["offsets_bytes"])
            database.execute("BEGIN IMMEDIATE")

            def checkpoint(status="INGESTING"):
                nonlocal state, pending
                for output in (docs, offsets):
                    output.flush()
                    os.fsync(output.fileno())
                pending.update(input_offset=source.tell(), docs_bytes=docs.tell(),
                               offsets_bytes=offsets.tell(), status=status)
                _checkpoint_hook("files_fsynced", pending)
                _save(database, pending)
                database.commit()
                state = dict(pending)
                _checkpoint_hook("db_committed", state)

            while True:
                line = source.readline()
                if not line:
                    break
                pending["line_number"] += 1
                number = pending["line_number"]
                try:
                    decoded = line.decode("utf-8")
                    if not decoded.strip():
                        continue
                    row = json.loads(decoded)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError(f"corpus line {number} is not valid UTF-8 JSON") from error
                if (not isinstance(row, dict) or not isinstance(row.get("doc_id"), str)
                        or not row["doc_id"] or not isinstance(row.get("title"), str)
                        or not isinstance(row.get("text"), str)):
                    raise ValueError(f"corpus line {number} requires doc_id and string title/text")
                try:
                    database.execute("INSERT INTO ids VALUES (?)", (row["doc_id"],))
                except sqlite3.IntegrityError as error:
                    raise ValueError(f"doc_id must be unique: {row['doc_id']}") from error
                tokens = analyzer.analyze(row["text"])
                spans, per_term = [], {}
                for position, token in enumerate(tokens):
                    spans.append([position, position + 1, token.start, token.end])
                    per_term.setdefault(token.term, []).append(position)
                docid = pending["n_docs"]
                docs.write((json.dumps(
                    {"docid": docid, "doc_id": row["doc_id"], "title": row["title"],
                     "text": row["text"]}, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                offsets.write((json.dumps({"docid": docid, "spans": spans},
                    ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                database.execute("INSERT INTO lengths VALUES (?, ?)", (docid, len(tokens)))
                database.executemany("INSERT INTO postings VALUES (?, ?, ?, ?)", (
                    (term, docid, len(positions), json.dumps(positions, separators=(",", ":")))
                    for term, positions in per_term.items()))
                pending["n_docs"] += 1
                pending["total_length"] += len(tokens)
                if pending["n_docs"] % checkpoint_docs == 0:
                    checkpoint()
                    database.execute("BEGIN IMMEDIATE")
                if progress_every and pending["n_docs"] % progress_every == 0:
                    print(f"checkpoint documents {pending['n_docs']}", flush=True)
            if not pending["n_docs"]:
                raise ValueError("corpus must not be empty")
            # Do not admit a mutable input as a completed ingestion.
            if _input_identity(corpus) != state["binding"]["input"]:
                raise ValueError("corpus changed during ingestion")
            checkpoint("INGESTED")
        return state
    except BaseException:
        database.rollback()
        # Read the last committed metadata, including a commit whose hook failed.
        committed = json.loads(database.execute("SELECT value FROM checkpoint WHERE id=1").fetchone()[0])
        _restore_files(stage, committed)
        raise


def _commit_state(database, state):
    database.execute("BEGIN IMMEDIATE")
    _save(database, state)
    database.commit()


def _export_postings(database, stage, state):
    """Reproduce stream_build's bytes, checkpointing only complete terms.

    A single exceptionally large term is the maximum replay unit. A crash
    inside a term or before commit discards all bytes since the last batch.
    """
    path = stage / POSTINGS
    committed_bytes = state["postings_bytes"]
    if not path.exists():
        if committed_bytes:
            raise ValueError("committed postings file is missing")
        path.touch(exist_ok=False)
    if path.stat().st_size < committed_bytes:
        raise ValueError("committed postings file is truncated")
    with path.open("r+b") as output:
        output.truncate(committed_bytes)
        output.flush()
        os.fsync(output.fileno())
        output.seek(committed_bytes)
        pending = dict(state)
        terms_in_batch = 0
        query = "SELECT term, COUNT(*) FROM postings"
        parameters = ()
        if state["last_term"] is not None:
            query += " WHERE term > ?"
            parameters = (state["last_term"],)
        query += " GROUP BY term ORDER BY term"

        def checkpoint(status="EXPORTING_POSTINGS"):
            nonlocal state, terms_in_batch, committed_bytes
            output.flush()
            os.fsync(output.fileno())
            pending.update(postings_bytes=output.tell(), status=status)
            _checkpoint_hook("postings_files_fsynced", pending)
            _commit_state(database, pending)
            state = dict(pending)
            committed_bytes = state["postings_bytes"]
            terms_in_batch = 0
            _checkpoint_hook("postings_db_committed", state)

        for term, df in database.execute(query, parameters):
            output.write(('{"term":' + json.dumps(term, ensure_ascii=False)
                + ',"df":' + str(int(df)) + ',"docs":[').encode("utf-8"))
            first = True
            for docid, tf, positions in database.execute(
                    "SELECT docid, tf, positions FROM postings WHERE term=? ORDER BY docid", (term,)):
                if not first:
                    output.write(b",")
                first = False
                output.write(json.dumps([int(docid), int(tf), json.loads(positions)],
                    ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                _checkpoint_hook("postings_document_written", dict(pending, writing_term=term))
            output.write(b"]}\n")
            pending["last_term"] = term
            pending["exported_terms"] += 1
            terms_in_batch += 1
            if (terms_in_batch >= POSTINGS_CHECKPOINT_TERMS
                    or output.tell() - committed_bytes >= POSTINGS_CHECKPOINT_BYTES):
                checkpoint()
        checkpoint("HASHING")
    return state


def _checkpoint_hash(database, stage, state, name):
    """Cache completed whole-file SHA only; interrupted files are rehashed."""
    path = stage / name
    stamp = _file_stamp(path)
    cached = state.setdefault("hash_cache", {}).get(name)
    if cached:
        if cached["stamp"] != stamp:
            raise ValueError(f"immutable staged file changed after hashing: {name}")
        return cached["sha256"]
    _checkpoint_hook("hash_started", dict(state, hash_file=name))
    digest = sha256_of(path)
    if _file_stamp(path) != stamp:
        raise ValueError(f"staged file changed during hashing: {name}")
    state["hash_cache"][name] = {"stamp": stamp, "sha256": digest}
    _commit_state(database, state)
    _checkpoint_hook("hash_committed", dict(state, hash_file=name))
    return digest


def _export(database, stage, state, dataset, bm25):
    avgdl = state["total_length"] / state["n_docs"]
    if state["status"] == "INGESTED":
        with (stage / STATS).open("w", encoding="utf-8", newline="\n") as stats:
            stats.write('{"doc_lengths":{')
            first = True
            for docid, length in database.execute("SELECT docid, length FROM lengths ORDER BY docid"):
                if not first:
                    stats.write(",")
                first = False
                stats.write(json.dumps(str(docid)) + ":" + str(int(length)))
            stats.write('},"avgdl":' + json.dumps(avgdl) + '}\n')
            stats.flush()
            os.fsync(stats.fileno())
        state.update(status="EXPORTING_POSTINGS", last_term=None, postings_bytes=0,
                     exported_terms=0, hash_cache={})
        _commit_state(database, state)
        _checkpoint_hook("stats_exported", state)
    if state["status"] == "EXPORTING_POSTINGS":
        state = _export_postings(database, stage, state)
    files = {name: _checkpoint_hash(database, stage, state, name)
             for name in (DOCS, POSTINGS, OFFSETS, STATS)}
    manifest = Manifest(dataset=dataset, n_docs=state["n_docs"], avgdl=avgdl,
        analyzer_name=analyzer.NAME, analyzer_version=analyzer.VERSION,
        analyzer_hash=analyzer.config_hash(), files=files, bm25=bm25)
    if MANIFEST not in state["hash_cache"]:
        (stage / MANIFEST).write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
        with (stage / MANIFEST).open("r+b") as output:
            os.fsync(output.fileno())
    _fsync_dir(stage)
    state["artifact_hashes"] = dict(files, **{MANIFEST: _checkpoint_hash(database, stage, state, MANIFEST)})
    state["status"] = "READY"
    _commit_state(database, state)
    _checkpoint_hook("export_committed", state)
    return state


def build(corpus_path: Path, out_dir: Path, dataset: str,
          bm25: BM25Spec | None = None, *, state_dir: Path,
          checkpoint_docs: int = 10000, progress_every: int = 0) -> Manifest:
    """Resume only a matching, exclusively locked checkpoint; publish once."""
    if type(checkpoint_docs) is not int or checkpoint_docs < 1:
        raise ValueError("checkpoint_docs must be a positive integer")
    if type(progress_every) is not int or progress_every < 0:
        raise ValueError("progress_every must be a nonnegative integer")
    corpus, out, state_dir = (Path(p).resolve() for p in (corpus_path, out_dir, state_dir))
    if out == state_dir or out in state_dir.parents or state_dir in out.parents:
        raise ValueError("state-dir and out must be separate directories")
    bm25 = bm25 or BM25Spec()
    with _state_lock(state_dir):
        binding = {"format": 1, "corpus": str(corpus), "out": str(out),
            "state_dir": str(state_dir), "input": _input_identity(corpus),
            "dataset": dataset, "bm25": bm25.to_dict(),
            "analyzer_hash": analyzer.config_hash(), "code_hashes": _code_hashes(),
            "python": list(sys.version_info[:3]), "unicode": unicodedata.unidata_version}
        database_path = state_dir / "checkpoint.sqlite"
        suffix = hashlib.sha256(str(state_dir).encode()).hexdigest()[:20]
        stage = out.parent / f".{out.name}.checkpoint-{suffix}"
        fresh = _fresh_checkpoint(database_path, stage, out)
        if fresh and out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise ValueError("output must be absent or empty; existing artifacts are protected")
        database = sqlite3.connect(database_path, isolation_level=None)
        try:
            if fresh:
                _checkpoint_hook("bootstrap_connected", {"n_docs": 0})
            if database.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise ValueError("SQLite WAL is required")
            database.execute("PRAGMA synchronous=FULL")
            if fresh:
                _checkpoint_hook("bootstrap_pragmas", {"n_docs": 0})
                out.parent.mkdir(parents=True, exist_ok=True)
                if stage.exists():
                    raise ValueError("unowned staging directory already exists")
                state = {"binding": binding, "stage": str(stage), "status": "INGESTING",
                    "input_offset": 0, "line_number": 0, "docs_bytes": 0,
                    "offsets_bytes": 0, "n_docs": 0, "total_length": 0}
                database.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE ids(doc_id TEXT PRIMARY KEY) WITHOUT ROWID;
                    CREATE TABLE lengths(docid INTEGER PRIMARY KEY, length INTEGER NOT NULL);
                    CREATE TABLE postings(term TEXT NOT NULL, docid INTEGER NOT NULL,
                        tf INTEGER NOT NULL, positions TEXT NOT NULL,
                        PRIMARY KEY(term, docid)) WITHOUT ROWID;
                    CREATE TABLE checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                """)
                _checkpoint_hook("bootstrap_schema", state)
                database.execute("INSERT INTO checkpoint VALUES(1, ?)", (json.dumps(state),))
                _checkpoint_hook("bootstrap_pending", state)
                database.commit()
                _fsync_dir(state_dir)
                _checkpoint_hook("bootstrap_committed", state)
            else:
                try:
                    state = json.loads(database.execute("SELECT value FROM checkpoint WHERE id=1").fetchone()[0])
                except (sqlite3.DatabaseError, TypeError, ValueError) as error:
                    raise ValueError("not a valid checkpoint database; no unsafe recovery") from error
                if state["binding"] != binding:
                    raise ValueError("checkpoint binding mismatch: input/config/analyzer/code changed")
                stage = Path(state["stage"])
            if state["status"] in ("READY", "PUBLISHED") and out.exists() and any(out.iterdir()):
                manifest = _verify_published(out, state)
                if state["status"] != "PUBLISHED":
                    database.execute("BEGIN IMMEDIATE")
                    state["status"] = "PUBLISHED"
                    _save(database, state)
                    database.commit()
                return manifest
            if state["status"] == "PUBLISHED":
                raise ValueError("published artifact is missing; refusing to rebuild over publication")
            if out.exists() and (not out.is_dir() or any(out.iterdir())):
                raise ValueError("output is nonempty; existing artifacts are protected")
            if not stage.exists():
                if state["n_docs"] or state["status"] != "INGESTING":
                    raise ValueError("committed staging directory is missing")
                stage.mkdir()
                _fsync_dir(stage.parent)
            if state["status"] == "INGESTING":
                state = _ingest(database, state, corpus, stage, checkpoint_docs, progress_every)
            if state["status"] in ("INGESTED", "EXPORTING_POSTINGS", "HASHING"):
                state = _export(database, stage, state, dataset, bm25)
            manifest = _verify_published(stage, state, cached_stage=True)
            if out.exists():
                out.rmdir()  # Only an empty directory can be removed.
            stage.rename(out)
            _fsync_dir(out.parent)
            _checkpoint_hook("published", state)
            state["status"] = "PUBLISHED"
            database.execute("BEGIN IMMEDIATE")
            _save(database, state)
            database.commit()
            return manifest
        finally:
            database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Durable SQLite checkpoint v1 index builder")
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--checkpoint-docs", type=int, default=10000)
    parser.add_argument("--progress-every", type=int, default=0)
    parser.add_argument("--k1", type=float, default=1.2)
    parser.add_argument("--b", type=float, default=0.75)
    args = parser.parse_args()
    started = time.perf_counter()
    manifest = build(args.corpus, args.out, args.dataset, BM25Spec(k1=args.k1, b=args.b),
        state_dir=args.state_dir, checkpoint_docs=args.checkpoint_docs, progress_every=args.progress_every)
    print(f"complete: {manifest.n_docs} documents in {time.perf_counter() - started:.2f}s -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
