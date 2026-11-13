"""Resumable compact-v1 compiler; optional ijson is required.

Only verified public v1 files are consumed. Private WAL/FULL state stores
document identities/totals, JSONL byte boundaries, term rows and checkpoints.
Input/output SHA caches require unchanged size+mtime; these are operational
resume caches, not an independent integrity proof. Interrupted single-file
hashes, statistics parsing, single documents and single terms are replayed.
Windows process-death recovery is supported; no power-loss guarantee is made.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys

from contracts import analyzer, index_format
from contracts.index_format import load_manifest, sha256_of, REQUIRED_FILES
from offline import compact, compact_stream, checkpoint_build

SCAN_BYTES = 65536
CHECKPOINT_TERMS = 1000
CHECKPOINT_BYTES = 16 * 1024 * 1024
ARTIFACTS = ("docs.jsonl", "docs.idx", "offsets.bin", "offsets.idx", "lengths.bin",
             "postings.bin", "terms.sqlite")


def _hook(event, state):
    """Subprocess fault seam; no production per-posting metadata copies."""


_NO_HOOK = _hook


def _commit(database, state):
    if not database.in_transaction:
        database.execute("BEGIN IMMEDIATE")
    database.execute("UPDATE checkpoint SET value=? WHERE id=1", (json.dumps(state, sort_keys=True),))
    database.commit()


def _stamp(path):
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _hash(database, state, path, cache_key, expected=None):
    cache = state.setdefault(cache_key, {})
    stamp = _stamp(path)
    entry = cache.get(path.name)
    if entry and entry["stamp"] == stamp:
        digest = entry["sha256"]
    else:
        if entry and cache_key == "output_hashes":
            raise ValueError("immutable staged file changed after hashing")
        _hook("hash_started", dict(state, hashing=path.name))
        digest = sha256_of(path)
        if _stamp(path) != stamp:
            raise ValueError("file changed during hashing")
        if expected is not None and digest != expected:
            raise ValueError("source checksum mismatch: " + path.name)
        cache[path.name] = {"stamp": stamp, "sha256": digest}
        _commit(database, state)
        _hook("hash_committed", dict(state, hashing=path.name))
    if expected is not None and digest != expected:
        raise ValueError("source checksum mismatch: " + path.name)
    return digest


def _sync_files(files):
    for output in files.values():
        output.flush()
        os.fsync(output.fileno())


def _files(stack, stage, sizes):
    files = {}
    for name, size in sizes.items():
        path = stage / name
        if not path.exists():
            if size:
                raise ValueError("committed output missing: " + name)
            path.touch(exist_ok=False)
        if path.stat().st_size < size:
            raise ValueError("committed output truncated: " + name)
        output = stack.enter_context(path.open("r+b"))
        output.truncate(size)
        output.seek(size)
        files[name] = output
    _sync_files(files)
    return files


def _docs(database, state, source, stage, lengths, every):
    with ExitStack() as stack:
        files = _files(stack, stage, state["doc_sizes"])
        stream = stack.enter_context((source / "docs.jsonl").open("rb"))
        stream.seek(state["doc_cursor"])
        database.execute("BEGIN IMMEDIATE")

        def save(status="DOCS"):
            _sync_files(files)
            state.update(doc_cursor=stream.tell(), doc_sizes={n: f.tell() for n, f in files.items()}, status=status)
            _hook("docs_fsynced", state)
            _commit(database, state)
            _hook("docs_committed", state)

        while line := stream.readline():
            docid = state["docs_done"]
            row = json.loads(line, object_pairs_hook=compact_stream._unique_object)
            if (type(row["docid"]) is not int or row["docid"] != docid or docid >= len(lengths)
                    or not isinstance(row["doc_id"], str) or not row["doc_id"]
                    or not isinstance(row["title"], str) or not isinstance(row["text"], str)):
                raise ValueError("invalid source document")
            if lengths[docid] == 0 and analyzer.analyze(row["text"]):
                raise ValueError("zero source length contradicts analyzer tokens")
            try:
                database.execute("INSERT INTO documents VALUES(?,?,?,?,0)",
                    (docid, row["doc_id"], lengths[docid], len(row["text"])))
            except sqlite3.IntegrityError as error:
                raise ValueError("duplicate source document identity") from error
            files["docs.idx"].write(compact.PAIR.pack(files["docs.jsonl"].tell(), len(line)))
            files["docs.jsonl"].write(line)
            files["lengths.bin"].write(struct.pack("<I", lengths[docid]))
            state["docs_done"] += 1
            if state["docs_done"] % every == 0:
                save()
                database.execute("BEGIN IMMEDIATE")
        if state["docs_done"] != len(lengths):
            raise ValueError("source document count mismatch")
        save("OFFSETS")


def _offsets(database, state, source, stage, lengths, every):
    with ExitStack() as stack:
        files = _files(stack, stage, state["offset_sizes"])
        stream = stack.enter_context((source / "offsets.jsonl").open("rb"))
        stream.seek(state["offset_cursor"])
        while line := stream.readline():
            docid = state["offsets_done"]
            row = json.loads(line, object_pairs_hook=compact_stream._unique_object)
            spans = row["spans"]
            if (type(row["docid"]) is not int or row["docid"] != docid or docid >= len(lengths)
                    or not isinstance(spans, list) or len(spans) != lengths[docid]):
                raise ValueError("source offset count/order mismatch")
            chars = database.execute("SELECT chars FROM documents WHERE docid=?", (docid,)).fetchone()[0]
            start = files["offsets.bin"].tell()
            writer = compact_stream._Writer(files["offsets.bin"])
            for i, span in enumerate(spans):
                if (not isinstance(span, list) or len(span) != 4 or any(type(v) is not int for v in span)
                        or span[:2] != [i, i + 1] or not 0 <= span[2] < span[3] <= chars):
                    raise ValueError("invalid source offset span")
                writer.put(span[2]); writer.put(span[3])
            writer.flush()
            files["offsets.idx"].write(compact.PAIR.pack(start, files["offsets.bin"].tell() - start))
            state["offsets_done"] += 1
            if state["offsets_done"] % every == 0:
                _sync_files(files)
                state.update(offset_cursor=stream.tell(), offset_sizes={n: f.tell() for n, f in files.items()})
                _hook("offsets_fsynced", state)
                _commit(database, state)
                _hook("offsets_committed", state)
        if state["offsets_done"] != len(lengths):
            raise ValueError("source offset document count mismatch")
        _sync_files(files)
        state.update(offset_cursor=stream.tell(), offset_sizes={n: f.tell() for n, f in files.items()}, status="SCAN")
        _commit(database, state)


def _scan(database, state, path):
    """Index JSONL newlines in fixed blocks, never materializing a whole term."""
    with path.open("rb") as stream:
        stream.seek(state["scan_cursor"])
        start = state["scan_cursor"]
        nonwhite = False
        database.execute("BEGIN IMMEDIATE")
        while True:
            base = stream.tell()
            chunk = stream.read(SCAN_BYTES)
            if not chunk:
                break
            previous = 0
            while True:
                end = chunk.find(b"\n", previous)
                if end < 0:
                    nonwhite = nonwhite or bool(chunk[previous:].strip())
                    break
                nonwhite = nonwhite or bool(chunk[previous:end].strip())
                boundary = base + end + 1
                if nonwhite:
                    database.execute("INSERT INTO source_rows VALUES(?,?,?)", (state["scan_rows"], start, boundary - start))
                    state["scan_rows"] += 1
                start, nonwhite = boundary, False
                if (state["scan_rows"] % CHECKPOINT_TERMS == 0
                        or boundary - state["scan_cursor"] >= CHECKPOINT_BYTES):
                    state["scan_cursor"] = boundary
                    _commit(database, state)
                    _hook("scan_committed", state)
                    database.execute("BEGIN IMMEDIATE")
                previous = end + 1
            _hook("scan_chunk", state)
        if nonwhite:
            database.execute("INSERT INTO source_rows VALUES(?,?,?)", (state["scan_rows"], start, stream.tell() - start))
            state["scan_rows"] += 1
        state.update(scan_cursor=stream.tell(), status="POSTINGS")
        _commit(database, state)
        _hook("scan_finished", state)


class _BoundedRow(io.RawIOBase):
    """Limit parser reads to one indexed row and at most 64KiB per read."""
    def __init__(self, stream, start, size):
        self.stream, self.remaining = stream, size
        stream.seek(start)

    def readable(self):
        return True

    def read(self, size=-1):
        size = min(self.remaining, SCAN_BYTES, SCAN_BYTES if size < 0 else size)
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data

    def readinto(self, buffer):
        data = self.read(min(len(buffer), SCAN_BYTES))
        buffer[:len(data)] = data
        return len(data)


def _term(events, database, lengths, writer, previous):
    expect = compact_stream._expect
    expect(events, "", "start_map")
    seen, term, df, count, last = set(), None, None, 0, -1
    for prefix, event, value in events:
        if (prefix, event) == ("", "end_map"):
            break
        if (prefix, event) != ("", "map_key") or value in seen:
            raise ValueError("invalid/duplicate source term field")
        seen.add(value)
        if value == "term":
            term = expect(events, "term", "string")
        elif value == "df":
            df = expect(events, "df", "number")
        elif value == "docs":
            expect(events, "docs", "start_array")
            for p, e, _ in events:
                if (p, e) == ("docs", "end_array"):
                    break
                if (p, e) != ("docs.item", "start_array"):
                    raise ValueError("invalid source posting structure")
                docid = expect(events, "docs.item.item", "number")
                tf = expect(events, "docs.item.item", "number")
                expect(events, "docs.item.item", "start_array")
                if (type(docid) is not int or type(tf) is not int
                        or not last < docid < len(lengths) or not 1 <= tf <= 0xffffffff):
                    raise ValueError("invalid source posting")
                writer.put(docid); writer.put(tf)
                positions, old = 0, -1
                for p, e, position in events:
                    if (p, e) == ("docs.item.item", "end_array"):
                        break
                    if (p != "docs.item.item.item" or e != "number" or type(position) is not int
                            or not old < position < lengths[docid]):
                        raise ValueError("invalid source posting position")
                    writer.put(position)
                    old, positions = position, positions + 1
                expect(events, "docs.item", "end_array")
                if positions != tf:
                    raise ValueError("invalid source posting tf")
                if database.execute("UPDATE documents SET total=total+? WHERE docid=? AND total+?<=length",
                        (tf, docid, tf)).rowcount != 1:
                    raise ValueError("source posting token totals mismatch")
                last, count = docid, count + 1
                if _hook is not _NO_HOOK:
                    _hook("posting_written", {"docid": docid})
        else:
            raise ValueError("unknown source term field")
    if (seen != {"term", "df", "docs"} or not term or previous is not None and term <= previous
            or type(df) is not int or df < 1 or df != count or next(events, None) is not None):
        raise ValueError("source term order/df mismatch")
    return term, df


def _postings(database, state, source, stage, lengths):
    with ExitStack() as stack:
        files = _files(stack, stage, {"postings.bin": state["postings_bytes"]})
        output = files["postings.bin"]
        stream = stack.enter_context((source / "postings.jsonl").open("rb"))
        writer = compact_stream._Writer(output)
        database.execute("BEGIN IMMEDIATE")
        batch_start = state["postings_row"]

        def save(status="POSTINGS"):
            writer.flush()
            _sync_files(files)
            state.update(postings_bytes=output.tell(), status=status)
            _hook("postings_fsynced", state)
            _commit(database, state)
            _hook("postings_committed", state)

        while True:
            # A live SELECT on this connection pins WAL frames across commits.
            # Materialize only row boundaries, and close before any output/write.
            cursor = database.execute("SELECT seq,start,size FROM source_rows WHERE seq>=? ORDER BY seq LIMIT ?",
                                      (state["postings_row"], CHECKPOINT_TERMS))
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
            if not rows:
                break
            for seq, start, size in rows:
                begin = writer.tell()
                row = _BoundedRow(stream, start, size)
                term, df = _term(iter(compact_stream._events(row)), database, lengths, writer, state["last_term"])
                database.execute("INSERT INTO terms VALUES(?,?,?,?)", (term, begin, writer.tell() - begin, df))
                state.update(postings_row=seq + 1, last_term=term)
                if state["postings_row"] - batch_start >= CHECKPOINT_TERMS or writer.tell() - state["postings_bytes"] >= CHECKPOINT_BYTES:
                    save()
                    batch_start = state["postings_row"]
                    database.execute("BEGIN IMMEDIATE")
        if database.execute("SELECT 1 FROM documents WHERE total<>length LIMIT 1").fetchone():
            raise ValueError("source posting token totals mismatch")
        save("TERMS_SQLITE")


def _terms_database(database, state, stage):
    path = stage / "terms.sqlite"
    out = sqlite3.connect(path)
    try:
        out.execute("PRAGMA synchronous=FULL")
        out.execute("CREATE TABLE IF NOT EXISTS terms(term TEXT PRIMARY KEY, start INTEGER, size INTEGER, df INTEGER) WITHOUT ROWID")
        count = 0
        while True:
            query = "SELECT * FROM terms"
            params = ()
            if state["terms_export_last"] is not None:
                query += " WHERE term>?"
                params = (state["terms_export_last"],)
            cursor = database.execute(query + " ORDER BY term LIMIT ?", (*params, CHECKPOINT_TERMS))
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
            if not rows:
                break
            for row in rows:
                out.execute("INSERT OR REPLACE INTO terms VALUES(?,?,?,?)", row)
                count += 1
                state["terms_export_last"] = row[0]
                if count % CHECKPOINT_TERMS == 0:
                    out.commit()
                    with path.open("r+b") as file:
                        os.fsync(file.fileno())
                    _hook("terms_sqlite_fsynced", state)
                    _commit(database, state)
                    _hook("terms_sqlite_committed", state)
        out.commit()
    finally:
        out.close()
    with path.open("r+b") as file:
        os.fsync(file.fileno())
    state["status"] = "HASHING"
    _commit(database, state)
    _hook("terms_sqlite_finished", state)


def _verify_published(target, state, *, staged=False):
    hashes = state["artifact_hashes"]
    if not target.is_dir() or set(p.name for p in target.iterdir()) != set(hashes):
        raise ValueError("compact artifact missing/unexpected files")
    for name, digest in hashes.items():
        cache = state["output_hashes"].get(name)
        actual = cache["sha256"] if staged and cache and cache["stamp"] == _stamp(target / name) else sha256_of(target / name)
        if actual != digest:
            raise ValueError("compact artifact hash mismatch")
    return json.loads((target / "compact.json").read_text(encoding="utf-8"))


def compile_checkpoint(source, target, *, state_dir, checkpoint_docs=10000):
    import ijson
    if type(checkpoint_docs) is not int or checkpoint_docs < 1:
        raise ValueError("checkpoint_docs must be positive")
    source, target, state_dir = (Path(p).resolve() for p in (source, target, state_dir))
    for a, b in ((source, target), (source, state_dir), (target, state_dir)):
        if a == b or a in b.parents or b in a.parents:
            raise ValueError("source, target and state-dir must be separate")
    with checkpoint_build._state_lock(state_dir):
        manifest = load_manifest(source)
        if (set(REQUIRED_FILES) - set(manifest.files) or manifest.n_docs <= 0
                or manifest.analyzer_hash != analyzer.config_hash()
                or any(not isinstance(manifest.files[n], str) or len(manifest.files[n]) != 64
                       or any(c not in "0123456789abcdef" for c in manifest.files[n]) for n in REQUIRED_FILES)):
            raise ValueError("source requires complete checksums and matching analyzer")
        binding = {"format": 1, "source": str(source), "target": str(target), "state": str(state_dir),
            "manifest_hash": sha256_of(source / "manifest.json"), "manifest": manifest.to_dict(),
            "source_sizes": {n: (source / n).stat().st_size for n in REQUIRED_FILES},
            "analyzer": analyzer.config_hash(), "python": list(sys.version_info[:3]),
            "ijson": ijson.__version__, "code": {name: sha256_of(Path(module.__file__)) for name, module in (
                ("checkpoint_compact", sys.modules[__name__]), ("compact_stream", compact_stream),
                ("compact", compact), ("checkpoint_build", checkpoint_build), ("analyzer", analyzer), ("index_format", index_format))}}
        path = state_dir / "compact-state.sqlite"
        stage = target.parent / ("." + target.name + ".checkpoint-compact-" + hashlib.sha256(str(state_dir).encode()).hexdigest()[:20])
        fresh = checkpoint_build._fresh_checkpoint(path, stage, target)
        if fresh and target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError("target must be absent/empty; existing artifacts protected")
        database = sqlite3.connect(path, isolation_level=None)
        try:
            if fresh:
                _hook("bootstrap_connected", {"docs_done": 0})
            if database.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise ValueError("SQLite WAL required")
            database.execute("PRAGMA synchronous=FULL")
            if fresh:
                _hook("bootstrap_pragmas", {"docs_done": 0})
                target.parent.mkdir(parents=True, exist_ok=True)
                if stage.exists():
                    raise ValueError("unowned stage exists")
                state = {"binding": binding, "stage": str(stage), "status": "VERIFY_SOURCE",
                    "source_hashes": {}, "output_hashes": {}, "docs_done": 0, "doc_cursor": 0,
                    "doc_sizes": {"docs.jsonl": 0, "docs.idx": 0, "lengths.bin": 0},
                    "offsets_done": 0, "offset_cursor": 0, "offset_sizes": {"offsets.bin": 0, "offsets.idx": 0},
                    "scan_cursor": 0, "scan_rows": 0, "postings_row": 0, "postings_bytes": 0,
                    "last_term": None, "terms_export_last": None}
                database.executescript("""BEGIN IMMEDIATE;
                    CREATE TABLE checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                    CREATE TABLE documents(docid INTEGER PRIMARY KEY, external_id TEXT UNIQUE, length INTEGER NOT NULL, chars INTEGER NOT NULL, total INTEGER NOT NULL);
                    CREATE TABLE source_rows(seq INTEGER PRIMARY KEY, start INTEGER NOT NULL, size INTEGER NOT NULL);
                    CREATE TABLE terms(term TEXT PRIMARY KEY, start INTEGER, size INTEGER, df INTEGER) WITHOUT ROWID;
                """)
                _hook("bootstrap_schema", state)
                database.execute("INSERT INTO checkpoint VALUES(1,?)", (json.dumps(state),))
                _hook("bootstrap_pending", state)
                database.commit()
                checkpoint_build._fsync_dir(state_dir)
                _hook("bootstrap_committed", state)
            else:
                try:
                    state = json.loads(database.execute("SELECT value FROM checkpoint WHERE id=1").fetchone()[0])
                except (sqlite3.DatabaseError, TypeError, ValueError) as error:
                    raise ValueError("invalid private compact checkpoint") from error
                if state["binding"] != binding:
                    raise ValueError("compact checkpoint binding mismatch")
                stage = Path(state["stage"])
            # Every source file must be admitted before even statistics are consumed.
            for name in REQUIRED_FILES:
                _hash(database, state, source / name, "source_hashes", manifest.files[name])
            if state["status"] in ("READY", "PUBLISHED") and target.exists() and any(target.iterdir()):
                result = _verify_published(target, state)
                state["status"] = "PUBLISHED"
                _commit(database, state)
                return result
            if state["status"] == "PUBLISHED":
                raise ValueError("published target missing")
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise ValueError("existing target protected")
            if not stage.exists():
                if state["status"] != "VERIFY_SOURCE":
                    raise ValueError("committed stage missing")
                stage.mkdir()
            if state["status"] == "VERIFY_SOURCE":
                state["status"] = "DOCS"
                _commit(database, state)
            if state["status"] in ("DOCS", "OFFSETS", "POSTINGS"):
                lengths = compact_stream._lengths(source / "stats.json", manifest)
            if state["status"] == "DOCS":
                _docs(database, state, source, stage, lengths, checkpoint_docs)
            if state["status"] == "OFFSETS":
                _offsets(database, state, source, stage, lengths, checkpoint_docs)
            if state["status"] == "SCAN":
                _scan(database, state, source / "postings.jsonl")
                lengths = compact_stream._lengths(source / "stats.json", manifest)
            if state["status"] == "POSTINGS":
                _postings(database, state, source, stage, lengths)
            if state["status"] == "TERMS_SQLITE":
                _terms_database(database, state, stage)
            if state["status"] == "HASHING":
                files = {name: _hash(database, state, stage / name, "output_hashes") for name in ARTIFACTS}
                if "compact.json" not in state["output_hashes"]:
                    metadata = {"compact_version": 1, "source_manifest": manifest.to_dict(),
                                "files": files, "terms": state["postings_row"]}
                    (stage / "compact.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    with (stage / "compact.json").open("r+b") as file:
                        os.fsync(file.fileno())
                files["compact.json"] = _hash(database, state, stage / "compact.json", "output_hashes")
                state.update(artifact_hashes=files, status="READY")
                _commit(database, state)
                _hook("exports_finished", state)
            for name in REQUIRED_FILES:
                _hash(database, state, source / name, "source_hashes", manifest.files[name])
            result = _verify_published(stage, state, staged=True)
            checkpoint_build._fsync_dir(stage)
            if target.exists():
                target.rmdir()
            stage.rename(target)
            checkpoint_build._fsync_dir(target.parent)
            _hook("published", state)
            state["status"] = "PUBLISHED"
            _commit(database, state)
            return result
        finally:
            database.close()


def main():
    parser = argparse.ArgumentParser(description="Resumable compact-v1 conversion")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--checkpoint-docs", type=int, default=10000)
    args = parser.parse_args()
    result = compile_checkpoint(args.source, args.out, state_dir=args.state_dir, checkpoint_docs=args.checkpoint_docs)
    print(f"compact complete: {result['source_manifest']['n_docs']} documents, {result['terms']} terms -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
