"""Offline, crash-resumable contiguous A1/compact-v1 shard merge.

Only the stream_build/checkpoint_build canonical A1 serialization is accepted.
The logical plan records raw-source SHA256 and half-open document ranges; it is
an operator provenance assertion, not proof that shards came from that raw file.
Paths are supplied separately and persisted only as SHA256 identities. Public
artifacts retain their existing schemas. Private SQLite WAL/FULL checkpoints
refer only to complete documents/terms and fsynced byte boundaries. One decoded
document, fixed binary buffers, O(N) uint32 lengths and O(N) uint64 verification
totals are the memory bounds; no term's postings are materialized as a list.
"""
from __future__ import annotations

import argparse
from array import array
from contextlib import ExitStack, closing
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import sqlite3
import struct
import sys

from contracts import analyzer, index_format
from contracts.index_format import Manifest, REQUIRED_FILES, load_manifest, sha256_of
from offline import checkpoint_build, checkpoint_compact, compact, compact_stream, stream_build

COMPACT_FILES = ("docs.jsonl", "docs.idx", "offsets.bin", "offsets.idx",
                 "lengths.bin", "terms.sqlite", "postings.bin")
DOC_FILES = ("lexical/docs.jsonl", "lexical/offsets.jsonl", "compact/docs.jsonl",
             "compact/docs.idx", "compact/offsets.bin", "compact/offsets.idx",
             "compact/lengths.bin")
TERM_FILES = ("lexical/postings.jsonl", "compact/postings.bin")
BUFFER_BYTES = 65536
TERMS_EXPORT = "public-terms-export.sqlite"
SQLITE_SIDECARS = ("-journal", "-wal", "-shm")
CHECKPOINT_BYTES = 16 * 1024 * 1024
# Public output transaction schedule is the native compiler's default, not the
# merger's independently configurable private checkpoint interval.
PUBLIC_EXPORT_TERMS = 1000
U32 = struct.Struct("<I")


def _hook(event, state):
    """Fault-injection seam. Production does not install a hook."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _path_id(path):
    return hashlib.sha256(os.fsencode(str(Path(path).resolve()))).hexdigest()


def _plan(plan, counts):
    if not isinstance(plan, dict) or set(plan) != {"dataset", "source_sha256", "n_docs", "shards"}:
        raise ValueError("plan requires dataset, source_sha256, n_docs, shards")
    if not isinstance(plan["dataset"], str) or not plan["dataset"] or not _digest(plan["source_sha256"]):
        raise ValueError("invalid logical dataset/source identity")
    if type(plan["n_docs"]) is not int or not 0 < plan["n_docs"] <= 0xffffffff:
        raise ValueError("global document count must fit uint32")
    if not isinstance(plan["shards"], list) or len(plan["shards"]) != len(counts):
        raise ValueError("plan/shard count mismatch")
    cursor = 0
    for item, count in zip(plan["shards"], counts):
        if (not isinstance(item, dict) or not {"start", "stop", "expected_docs_sha256"} <= set(item)
                or set(item) - {"start", "stop", "expected_docs_sha256", "raw_sha256"}
                or any(type(item[k]) is not int for k in ("start", "stop"))
                or item["start"] != cursor or item["stop"] != cursor + count
                or not _digest(item["expected_docs_sha256"])
                or "raw_sha256" in item and not _digest(item["raw_sha256"])):
            raise ValueError("shards must be ordered, nonempty, contiguous half-open ranges")
        cursor += count
    if cursor != plan["n_docs"]:
        raise ValueError("plan global document count mismatch")
    return json.loads(json.dumps(plan))


def _verified_hash(path, expected=None):
    before = path.stat()
    value = sha256_of(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError("input changed during hashing")
    if expected is not None and (not _digest(expected) or value != expected):
        raise ValueError("source checksum mismatch: " + path.name)
    return value


def _compare(stream, data):
    if stream.read(len(data)) != data:
        raise ValueError("compact/source canonical content mismatch")


def _stats_parts(lengths, average):
    yield b'{"doc_lengths":{'
    for docid, length in enumerate(lengths):
        yield (b"," if docid else b"") + _json(str(docid)) + b":" + str(length).encode("ascii")
    yield b'},"avgdl":' + _json(average) + b'}\n'


def _inputs(shards, plan):
    sources, lengths, binding = [], array("I"), []
    for lexical, packed in shards:
        lexical, packed = Path(lexical).resolve(), Path(packed).resolve()
        manifest = load_manifest(lexical)
        if (manifest.n_docs <= 0 or manifest.analyzer_name != analyzer.NAME
                or manifest.analyzer_version != analyzer.VERSION or manifest.analyzer_hash != analyzer.config_hash()
                or set(manifest.files) != set(REQUIRED_FILES)):
            raise ValueError("source manifest/analyzer mismatch")
        hashes = {"lexical/manifest.json": _verified_hash(lexical / "manifest.json")}
        for name in REQUIRED_FILES:
            hashes["lexical/" + name] = _verified_hash(lexical / name, manifest.files[name])
        hashes["compact/compact.json"] = _verified_hash(packed / "compact.json")
        metadata = json.loads((packed / "compact.json").read_text(encoding="utf-8"), object_pairs_hook=compact_stream._unique_object)
        if (set(metadata) != {"compact_version", "source_manifest", "files", "terms"}
                or metadata["compact_version"] != 1 or metadata["source_manifest"] != manifest.to_dict()
                or set(metadata["files"]) != set(COMPACT_FILES)
                or type(metadata["terms"]) is not int or metadata["terms"] < 0):
            raise ValueError("compact/source manifest mismatch")
        for name in COMPACT_FILES:
            hashes["compact/" + name] = _verified_hash(packed / name, metadata["files"][name])
        local = compact.integers((packed / "lengths.bin").read_bytes())
        if len(local) != manifest.n_docs or sum(local) / len(local) != manifest.avgdl:
            raise ValueError("source length statistics mismatch")
        with (lexical / "stats.json").open("rb") as stream:
            for part in _stats_parts(local, manifest.avgdl):
                _compare(stream, part)
            if stream.read(1):
                raise ValueError("trailing source statistics bytes")
        for name in ("docs.idx", "offsets.idx"):
            if (packed / name).stat().st_size != manifest.n_docs * compact.PAIR.size:
                raise ValueError("source row index size mismatch")
        if sources and manifest.bm25 != sources[0]["manifest"].bm25:
            raise ValueError("shard BM25 mismatch")
        sources.append(dict(lexical=lexical, compact=packed, manifest=manifest,
                            metadata=metadata, base=len(lengths)))
        lengths.extend(local)
        binding.append(dict(paths=[_path_id(lexical), _path_id(packed)], files=hashes))
    if not sources:
        raise ValueError("at least one shard is required")
    logical = _plan(plan, [s["manifest"].n_docs for s in sources])
    for expected, entry in zip(logical["shards"], binding):
        if expected["expected_docs_sha256"] != entry["files"]["lexical/docs.jsonl"]:
            raise ValueError("shard document identity/order differs from frozen plan")
    return sources, lengths, dict(plan=logical, sources=binding)


def _commit(database, state, files=()):
    for stream in files:
        stream.flush()
        os.fsync(stream.fileno())
    _hook("files_fsynced", state)
    database.execute("UPDATE checkpoint SET value=? WHERE id=1", (json.dumps(state, sort_keys=True),))
    database.commit()
    _hook("db_committed", state)


def _outputs(stack, stage, state, names):
    result = {}
    created_in = set()
    for name in names:
        path = stage / name
        size = state["sizes"].get(name, 0)
        if not path.exists():
            if size:
                raise ValueError("missing committed output")
            path.touch(exist_ok=False)
            created_in.add(path.parent)
        if path.stat().st_size < size:
            raise ValueError("truncated committed output")
        stream = stack.enter_context(path.open("r+b"))
        stream.truncate(size)
        stream.seek(size)
        result[name] = stream
    for directory in sorted(created_in):
        checkpoint_build._fsync_dir(directory)
    return result


def _save(database, state, files):
    state["sizes"].update({name: stream.tell() for name, stream in files.items()})
    _commit(database, state, files.values())


def _read_exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("truncated source binary")
    return data


def _documents(database, state, sources, lengths, stage, every):
    with ExitStack() as stack:
        files = _outputs(stack, stage, state, DOC_FILES)
        for source_no, source in enumerate(sources):
            n, base = source["manifest"].n_docs, source["base"]
            done = max(0, min(n, state["docs_done"] - base))
            if done == n:
                cursor = state["doc_cursors"][source_no]
                if (cursor["docs"] != (source["lexical"] / "docs.jsonl").stat().st_size
                        or cursor["docs"] != (source["compact"] / "docs.jsonl").stat().st_size
                        or cursor["offsets"] != (source["lexical"] / "offsets.jsonl").stat().st_size
                        or cursor["binary_offsets"] != (source["compact"] / "offsets.bin").stat().st_size):
                    raise ValueError("trailing committed source document/offset bytes")
                continue
            with ExitStack() as inputs:
                docs = inputs.enter_context((source["lexical"] / "docs.jsonl").open("rb"))
                offsets = inputs.enter_context((source["lexical"] / "offsets.jsonl").open("rb"))
                packed = {name: inputs.enter_context((source["compact"] / name).open("rb"))
                          for name in ("docs.jsonl", "docs.idx", "offsets.bin", "offsets.idx")}
                cursors = state["doc_cursors"][source_no]
                docs.seek(cursors["docs"]); offsets.seek(cursors["offsets"])
                packed["docs.jsonl"].seek(cursors["docs"])
                packed["offsets.bin"].seek(cursors["binary_offsets"])
                packed["docs.idx"].seek(done * compact.PAIR.size)
                packed["offsets.idx"].seek(done * compact.PAIR.size)
                for local in range(done, n):
                    docid = base + local
                    line = docs.readline()
                    row = json.loads(line, object_pairs_hook=compact_stream._unique_object)
                    if (tuple(row) != ("docid", "doc_id", "title", "text") or type(row["docid"]) is not int
                            or row["docid"] != local or not isinstance(row["doc_id"], str) or not row["doc_id"]
                            or not isinstance(row["title"], str) or not isinstance(row["text"], str)
                            or line != _json(row) + b"\n"):
                        raise ValueError("invalid source document/order/canonical bytes")
                    if compact.PAIR.unpack(_read_exact(packed["docs.idx"], 16)) != (packed["docs.jsonl"].tell(), len(line)):
                        raise ValueError("source document index mismatch")
                    _compare(packed["docs.jsonl"], line)
                    try:
                        database.execute("INSERT INTO identities VALUES(?)", (row["doc_id"],))
                    except sqlite3.IntegrityError as error:
                        raise ValueError("duplicate document identity across shards") from error
                    offset_line = offsets.readline()
                    offset_row = json.loads(offset_line, object_pairs_hook=compact_stream._unique_object)
                    spans = offset_row.get("spans")
                    if (tuple(offset_row) != ("docid", "spans") or type(offset_row["docid"]) is not int
                            or offset_row["docid"] != local or not isinstance(spans, list)
                            or len(spans) != lengths[docid] or offset_line != _json(offset_row) + b"\n"):
                        raise ValueError("source offsets count/order mismatch")
                    binary_start = packed["offsets.bin"].tell()
                    if compact.PAIR.unpack(_read_exact(packed["offsets.idx"], 16)) != (binary_start, len(spans) * 8):
                        raise ValueError("source offset index mismatch")
                    out_start = files["compact/offsets.bin"].tell()
                    for i, span in enumerate(spans):
                        if (not isinstance(span, list) or len(span) != 4 or any(type(v) is not int for v in span)
                                or span[:2] != [i, i + 1] or not 0 <= span[2] < span[3] <= len(row["text"])):
                            raise ValueError("invalid source offset span")
                        binary = struct.pack("<II", *span[2:])
                        _compare(packed["offsets.bin"], binary)
                        files["compact/offsets.bin"].write(binary)
                    if not spans and analyzer.analyze(row["text"]):
                        raise ValueError("zero source length contradicts analyzer")
                    row["docid"] = docid
                    global_line = _json(row) + b"\n"
                    files["compact/docs.idx"].write(compact.PAIR.pack(files["compact/docs.jsonl"].tell(), len(global_line)))
                    files["compact/docs.jsonl"].write(global_line)
                    files["lexical/docs.jsonl"].write(global_line)
                    offset_row["docid"] = docid
                    files["lexical/offsets.jsonl"].write(_json(offset_row) + b"\n")
                    files["compact/offsets.idx"].write(compact.PAIR.pack(out_start, len(spans) * 8))
                    files["compact/lengths.bin"].write(U32.pack(lengths[docid]))
                    state["docs_done"] = docid + 1
                    cursors.update(docs=docs.tell(), offsets=offsets.tell(), binary_offsets=packed["offsets.bin"].tell())
                    _hook("document_written", state)
                    if state["docs_done"] % every == 0:
                        _save(database, state, files)
                if docs.read(1) or offsets.read(1) or any(stream.read(1) for stream in packed.values()):
                    raise ValueError("trailing source document/offset bytes")
        state["status"] = "TERMS"
        _save(database, state, files)


class _Words:
    """Bounded binary cursor, including terms larger than Python memory."""
    def __init__(self, stream, start, size):
        if start != stream.tell() or type(size) is not int or size < 0 or size % 4:
            raise ValueError("invalid source term binary boundary")
        self.stream, self.remaining = stream, size
        self.buffer, self.offset = b"", 0

    def pop(self):
        if self.offset == len(self.buffer):
            if not self.remaining:
                raise ValueError("truncated source posting")
            self.buffer = _read_exact(self.stream, min(BUFFER_BYTES, self.remaining))
            self.remaining -= len(self.buffer)
            self.offset = 0
        value = U32.unpack_from(self.buffer, self.offset)[0]
        self.offset += 4
        return value

    def finished(self):
        return not self.remaining and self.offset == len(self.buffer)


def _term_header(term, df):
    return b'{"term":' + json.dumps(term, ensure_ascii=False).encode("utf-8") + b',"df":' + str(df).encode("ascii") + b',"docs":['


def _posting_parts(words, df, n, lengths, base, binary=None, lexical=None, source_json=None, totals=None):
    last = -1
    for posting_no in range(df):
        docid, tf = words.pop(), words.pop()
        if not last < docid < n or not 1 <= tf <= lengths[base + docid]:
            raise ValueError("invalid source posting docid/tf")
        last = docid
        global_id = base + docid
        prefix = (b"," if posting_no else b"") + b"["
        if source_json is not None:
            _compare(source_json, prefix + str(docid).encode("ascii") + b"," + str(tf).encode("ascii") + b",[")
        if lexical is not None:
            lexical.write(prefix + str(global_id).encode("ascii") + b"," + str(tf).encode("ascii") + b",[")
        if binary is not None:
            binary.write(struct.pack("<II", global_id, tf))
        previous = -1
        # Position buffers are fixed size; even an enormous single posting is streamed.
        text, data = bytearray(), bytearray()
        for i in range(tf):
            position = words.pop()
            if not previous < position < lengths[global_id]:
                raise ValueError("invalid source posting position")
            previous = position
            text.extend((b"," if i else b"") + str(position).encode("ascii"))
            data.extend(U32.pack(position))
            if len(text) >= BUFFER_BYTES or len(data) >= BUFFER_BYTES or i + 1 == tf:
                if source_json is not None: _compare(source_json, text)
                if lexical is not None: lexical.write(text)
                if binary is not None: binary.write(data)
                text.clear(); data.clear()
        if source_json is not None: _compare(source_json, b"]]")
        if lexical is not None: lexical.write(b"]]")
        if totals is not None:
            totals[global_id] += tf
            if totals[global_id] > lengths[global_id]:
                raise ValueError("posting token totals exceed document length")
    if not words.finished():
        raise ValueError("trailing source term binary values")


def _open_terms(stack, path):
    connection = stack.enter_context(closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)))
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise ValueError("invalid compact term database")
    if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() != [("terms",)]:
        raise ValueError("unexpected compact term schema")
    if connection.execute("PRAGMA table_info(terms)").fetchall() != [
            (0, "term", "TEXT", 1, None, 1), (1, "start", "INTEGER", 0, None, 0),
            (2, "size", "INTEGER", 0, None, 0), (3, "df", "INTEGER", 0, None, 0)]:
        raise ValueError("unexpected compact term columns")
    return connection


def _terms(database, state, sources, lengths, stage, every):
    with ExitStack() as stack:
        files = _outputs(stack, stage, state, TERM_FILES)
        readers, heap = [], []
        for i, source in enumerate(sources):
            connection = _open_terms(stack, source["compact"] / "terms.sqlite")
            if connection.execute("SELECT COUNT(*) FROM terms").fetchone()[0] != source["metadata"]["terms"]:
                raise ValueError("source term metadata count mismatch")
            cursor = stack.enter_context(closing(connection.execute(
                "SELECT term,start,size,df FROM terms WHERE term>? ORDER BY term", (state["last_term"] or "",))))
            binary = stack.enter_context((source["compact"] / "postings.bin").open("rb"))
            lexical = stack.enter_context((source["lexical"] / "postings.jsonl").open("rb"))
            binary.seek(state["term_cursors"][i]["binary"])
            lexical.seek(state["term_cursors"][i]["lexical"])
            readers.append((cursor, binary, lexical))
            row = cursor.fetchone()
            if row is not None: heapq.heappush(heap, (row[0], i, row))
        while heap:
            term = heap[0][0]
            if not isinstance(term, str) or not term or state["last_term"] is not None and term <= state["last_term"]:
                raise ValueError("invalid source term order")
            group = []
            while heap and heap[0][0] == term:
                group.append(heapq.heappop(heap))
            group.sort(key=lambda item: item[1])
            df = sum(item[2][3] for item in group)
            start = files["compact/postings.bin"].tell()
            files["lexical/postings.jsonl"].write(_term_header(term, df))
            first = True
            for _, i, (_, offset, size, local_df) in group:
                source = sources[i]
                if type(offset) is not int or type(local_df) is not int or not 1 <= local_df <= source["manifest"].n_docs:
                    raise ValueError("invalid source term offset/df")
                cursor, binary, lexical = readers[i]
                _compare(lexical, _term_header(term, local_df))
                if not first: files["lexical/postings.jsonl"].write(b",")
                first = False
                _posting_parts(_Words(binary, offset, size), local_df, source["manifest"].n_docs,
                               lengths, source["base"], files["compact/postings.bin"],
                               files["lexical/postings.jsonl"], lexical)
                _compare(lexical, b"]}\n")
                state["term_cursors"][i].update(binary=binary.tell(), lexical=lexical.tell())
                state["source_terms"][i] += 1
                row = cursor.fetchone()
                if row is not None: heapq.heappush(heap, (row[0], i, row))
            files["lexical/postings.jsonl"].write(b"]}\n")
            database.execute("INSERT INTO terms VALUES(?,?,?,?)", (term, start, files["compact/postings.bin"].tell() - start, df))
            state["last_term"], state["terms_done"] = term, state["terms_done"] + 1
            _hook("term_written", state)
            if state["terms_done"] % every == 0 or files["compact/postings.bin"].tell() - state["sizes"].get("compact/postings.bin", 0) >= CHECKPOINT_BYTES:
                _save(database, state, files)
        for i, (_, binary, lexical) in enumerate(readers):
            if binary.read(1) or lexical.read(1) or state["source_terms"][i] != sources[i]["metadata"]["terms"]:
                raise ValueError("source term count/trailing bytes mismatch")
        state["status"] = "FINALIZE"
        _save(database, state, files)


def _durable_write(path, parts):
    with path.open("wb") as output:
        for part in parts: output.write(part)
        output.flush(); os.fsync(output.fileno())


def _metadata_bytes(value):
    # Public builders use Path.write_text's platform newline translation for
    # metadata, while their JSONL/statistics writers explicitly use LF.
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").replace("\n", os.linesep).encode("utf-8")


def _finalize(database, state, stage, state_dir, sources, lengths):
    average = sum(lengths) / len(lengths)
    _durable_write(stage / "lexical/stats.json", _stats_parts(lengths, average))
    manifest = Manifest(dataset=state["binding"]["inputs"]["plan"]["dataset"], n_docs=len(lengths), avgdl=average,
                        analyzer_name=analyzer.NAME, analyzer_version=analyzer.VERSION, analyzer_hash=analyzer.config_hash(),
                        files={name: sha256_of(stage / "lexical" / name) for name in REQUIRED_FILES}, bm25=sources[0]["manifest"].bm25)
    _durable_write(stage / "lexical/manifest.json", [_metadata_bytes(manifest.to_dict())])
    # Public SQLite bytes use the native checkpoint compiler's DDL, sorted
    # INSERT OR REPLACE and default 1000-term commit schedule. Private WAL
    # history never enters the bundle. An interrupted export restarts this one
    # derived SQLite file; it cannot mix private/export transaction histories.
    # Keep the journaled export on the checkpoint filesystem. The output
    # filesystem only receives a closed SQLite file and is read immutably.
    term_path = state_dir / TERMS_EXPORT
    term_path.unlink(missing_ok=True)
    for suffix in SQLITE_SIDECARS:
        sidecar = Path(str(term_path) + suffix)
        sidecar.unlink(missing_ok=True)
    # Do not hold SELECT statements on the private writer connection across COMMIT.
    # This export is bounded, and finishes/ closes its cursor before saving state.
    with closing(sqlite3.connect(term_path)) as output:
        output.execute("PRAGMA synchronous=FULL")
        output.execute("CREATE TABLE IF NOT EXISTS terms(term TEXT PRIMARY KEY, start INTEGER, size INTEGER, df INTEGER) WITHOUT ROWID")
        with closing(database.execute("SELECT term,start,size,df FROM terms ORDER BY term")) as cursor:
            count = 0
            for row in cursor:
                output.execute("INSERT OR REPLACE INTO terms VALUES(?,?,?,?)", row)
                count += 1
                if count % PUBLIC_EXPORT_TERMS == 0:
                    output.commit()
                    with term_path.open("r+b") as stream: os.fsync(stream.fileno())
                    _hook("terms_export_chunk", state)
        output.commit()
    with term_path.open("r+b") as stream: os.fsync(stream.fileno())
    published_terms = stage / "compact/terms.sqlite"
    # FINALIZE can be restarted after a partial copy. The private stage is
    # never visible to readers before verification and directory publication.
    published_terms.unlink(missing_ok=True)
    with term_path.open("rb") as stream:
        _durable_write(published_terms, iter(lambda: stream.read(BUFFER_BYTES), b""))
    files = {name: sha256_of(stage / "compact" / name) for name in checkpoint_compact.ARTIFACTS}
    if files["terms.sqlite"] != sha256_of(term_path):
        raise ValueError("closed terms export copy checksum mismatch")
    term_path.unlink()
    metadata = dict(compact_version=1, source_manifest=manifest.to_dict(), files=files, terms=state["terms_done"])
    _durable_write(stage / "compact/compact.json", [_metadata_bytes(metadata)])
    checkpoint_build._fsync_dir(stage / "lexical")
    checkpoint_build._fsync_dir(stage / "compact")
    state["status"] = "VERIFY"
    _commit(database, state)


def _verify(stage, lengths):
    """Full public hashes plus streamed canonical correspondence and token totals."""
    for role, expected in (("lexical", {*REQUIRED_FILES, "manifest.json"}), ("compact", {*COMPACT_FILES, "compact.json"})):
        if {p.name for p in (stage / role).iterdir()} != expected:
            raise ValueError("unexpected bundle files")
    sources, actual_lengths, _ = _inputs([(stage / "lexical", stage / "compact")], {
        "dataset": "verification", "source_sha256": "0" * 64, "n_docs": len(lengths),
        "shards": [{"start": 0, "stop": len(lengths), "expected_docs_sha256": sha256_of(stage / "lexical/docs.jsonl")}]})
    if actual_lengths != lengths:
        raise ValueError("merged document lengths mismatch")
    del actual_lengths
    totals = array("Q", [0]) * len(lengths)
    with ExitStack() as stack:
        connection = _open_terms(stack, stage / "compact/terms.sqlite")
        binary = stack.enter_context((stage / "compact/postings.bin").open("rb"))
        lexical = stack.enter_context((stage / "lexical/postings.jsonl").open("rb"))
        count, previous = 0, None
        with closing(connection.execute("SELECT term,start,size,df FROM terms ORDER BY term")) as rows:
            for term, start, size, df in rows:
                if not isinstance(term, str) or not term or previous is not None and term <= previous or type(df) is not int or not 1 <= df <= len(lengths):
                    raise ValueError("invalid merged term/df")
                _compare(lexical, _term_header(term, df))
                _posting_parts(_Words(binary, start, size), df, len(lengths), lengths, 0, source_json=lexical, totals=totals)
                _compare(lexical, b"]}\n")
                count += 1; previous = term
        if binary.read(1) or lexical.read(1) or count != sources[0]["metadata"]["terms"]:
            raise ValueError("merged term count/trailing bytes mismatch")
    if any(a != b for a, b in zip(totals, lengths)):
        raise ValueError("merged posting token totals mismatch")
    # Every document/index/offset boundary is rechecked, independently of cursors.
    with ExitStack() as stack:
        docs = stack.enter_context((stage / "lexical/docs.jsonl").open("rb"))
        offsets = stack.enter_context((stage / "lexical/offsets.jsonl").open("rb"))
        packed = {n: stack.enter_context((stage / "compact" / n).open("rb"))
                  for n in ("docs.jsonl", "docs.idx", "offsets.bin", "offsets.idx")}
        for docid, length in enumerate(lengths):
            line = docs.readline(); row = json.loads(line, object_pairs_hook=compact_stream._unique_object)
            if row["docid"] != docid or line != _json(row) + b"\n": raise ValueError("merged document order mismatch")
            if compact.PAIR.unpack(_read_exact(packed["docs.idx"], 16)) != (packed["docs.jsonl"].tell(), len(line)):
                raise ValueError("merged document boundary mismatch")
            _compare(packed["docs.jsonl"], line)
            offset_line = offsets.readline(); spans = json.loads(offset_line)["spans"]
            if len(spans) != length: raise ValueError("merged offset length mismatch")
            if compact.PAIR.unpack(_read_exact(packed["offsets.idx"], 16)) != (packed["offsets.bin"].tell(), length * 8):
                raise ValueError("merged offset boundary mismatch")
            for i, span in enumerate(spans):
                if span[:2] != [i, i + 1] or not 0 <= span[2] < span[3] <= len(row["text"]):
                    raise ValueError("invalid merged offset")
                _compare(packed["offsets.bin"], struct.pack("<II", *span[2:]))
        if docs.read(1) or offsets.read(1) or any(s.read(1) for s in packed.values()):
            raise ValueError("merged document/offset trailing bytes")
    return {role + "/" + p.name: sha256_of(p) for role in ("lexical", "compact") for p in (stage / role).iterdir()}


def merge_indexes(shards, target, *, state_dir, plan, checkpoint_docs=10000, checkpoint_terms=1000):
    """Publish a fresh bundle containing lexical/ (5) and compact/ (8 files).

    Reentry with this exact checkpoint verifies and returns a completed bundle.
    Existing outputs without a matching checkpoint are always refused.
    """
    if any(type(n) is not int or n < 1 for n in (checkpoint_docs, checkpoint_terms)):
        raise ValueError("checkpoint intervals must be positive integers")
    shards = tuple((Path(lexical), Path(packed)) for lexical, packed in shards)
    target, state_dir = Path(target).resolve(), Path(state_dir).resolve()
    stage = target.parent / ("." + target.name + ".merge-" + _path_id(state_dir)[:20])
    target.parent.mkdir(parents=True, exist_ok=True)
    if any(p == state_dir or p in state_dir.parents or state_dir in p.parents for p in (target, stage)):
        raise ValueError("output, staging, and checkpoint directories must be disjoint")
    for lexical, packed in shards:
        for source in (Path(lexical).resolve(), Path(packed).resolve()):
            if any(p == source or p in source.parents or source in p.parents for p in (target, state_dir, stage)):
                raise ValueError("input/output/checkpoint paths must be disjoint")
    with checkpoint_build._state_lock(state_dir):
        sources, lengths, identity = _inputs(shards, plan)
        binding = dict(version=1, inputs=identity, output=_path_id(target), state=_path_id(state_dir),
                       code={name: sha256_of(Path(module.__file__)) for name, module in (
                           ("merge_indexes", sys.modules[__name__]), ("compact", compact), ("compact_stream", compact_stream),
                           ("checkpoint_build", checkpoint_build), ("stream_build", stream_build),
                           ("checkpoint_compact", checkpoint_compact),
                           ("analyzer", analyzer), ("index_format", index_format))})
        db_path = state_dir / "merge-state.sqlite"
        fresh = checkpoint_build._fresh_checkpoint(
            db_path, stage, target, recovery_files={TERMS_EXPORT, *(TERMS_EXPORT + s for s in SQLITE_SIDECARS)})
        if fresh and target.exists(): raise ValueError("target exists; refusing overwrite")
        if stage.is_symlink() or (fresh and stage.exists()):
            raise ValueError("unexpected staging directory; refusing unsafe adoption")
        with closing(sqlite3.connect(db_path)) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("PRAGMA synchronous=FULL")
            database.execute("PRAGMA cache_size=-8192")
            database.execute("PRAGMA temp_store=FILE")
            if fresh:
                state = dict(binding=binding, stage="bundle", status="DOCS", sizes={}, docs_done=0, terms_done=0,
                             last_term=None, source_terms=[0] * len(sources),
                             doc_cursors=[dict(docs=0, offsets=0, binary_offsets=0) for _ in sources],
                             term_cursors=[dict(binary=0, lexical=0) for _ in sources],
                             memory_contract=dict(lengths="uint32[N]", verification_totals="uint64[N]",
                                                  binary_buffer_bytes=BUFFER_BYTES, decoded_documents=1,
                                                  sqlite_cache_bytes=8 * 1024 * 1024))
                database.executescript("BEGIN IMMEDIATE; CREATE TABLE checkpoint(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);"
                                       "CREATE TABLE identities(doc_id TEXT PRIMARY KEY) WITHOUT ROWID;"
                                       "CREATE TABLE terms(term TEXT PRIMARY KEY, start INTEGER, size INTEGER, df INTEGER) WITHOUT ROWID;")
                database.execute("INSERT INTO checkpoint VALUES(1,?)", (json.dumps(state),))
                _hook("bootstrap_pending", state)
                database.commit()
                _hook("bootstrap_committed", state)
            else:
                state = json.loads(database.execute("SELECT value FROM checkpoint WHERE id=1").fetchone()[0])
                if state["binding"] != binding: raise ValueError("merge checkpoint binding mismatch")
            if target.exists():
                if state["status"] not in ("READY", "PUBLISHED") or stage.exists():
                    raise ValueError("target exists; refusing overwrite")
                if _verify(target, lengths) != state["artifact_hashes"]:
                    raise ValueError("published bundle changed")
                checkpoint_build._fsync_dir(target.parent)
                state["status"] = "PUBLISHED"; _commit(database, state)
                return json.loads((target / "compact/compact.json").read_text(encoding="utf-8"))
            if state["status"] == "PUBLISHED": raise ValueError("published target is missing")
            if fresh or not stage.exists():
                if state["docs_done"] or state["terms_done"]: raise ValueError("committed stage is missing")
                stage.mkdir(exist_ok=False)
            (stage / "lexical").mkdir(exist_ok=True); (stage / "compact").mkdir(exist_ok=True)
            checkpoint_build._fsync_dir(stage)
            checkpoint_build._fsync_dir(target.parent)
            if state["status"] == "DOCS": _documents(database, state, sources, lengths, stage, checkpoint_docs)
            if state["status"] == "TERMS": _terms(database, state, sources, lengths, stage, checkpoint_terms)
            if state["status"] == "FINALIZE": _finalize(database, state, stage, state_dir, sources, lengths)
            verified_this_call = False
            if state["status"] == "VERIFY":
                state["artifact_hashes"] = _verify(stage, lengths)
                # Rehash all sources immediately before publication, without trusting mtime caches.
                _, _, final_identity = _inputs(shards, plan)
                if final_identity != identity: raise ValueError("source changed during merge")
                state["status"] = "READY"; _commit(database, state)
                verified_this_call = True
            if state["status"] != "READY": raise ValueError("invalid merge checkpoint phase")
            if not verified_this_call and _verify(stage, lengths) != state["artifact_hashes"]:
                raise ValueError("ready bundle changed")
            checkpoint_build._fsync_dir(stage / "lexical"); checkpoint_build._fsync_dir(stage / "compact")
            checkpoint_build._fsync_dir(stage)
            # os.rename is exclusive on Windows. On POSIX use renameat2 with
            # RENAME_NOREPLACE: plain rename could replace an empty target dir.
            _publish(stage, target)
            checkpoint_build._fsync_dir(target.parent)
            _hook("published", state)
            state["status"] = "PUBLISHED"; _commit(database, state)
            return json.loads((target / "compact/compact.json").read_text(encoding="utf-8"))


def _publish(stage, target):
    if os.name == "nt":
        os.rename(stage, target)
    elif sys.platform.startswith("linux"):
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        function = getattr(library, "renameat2", None)
        if function is None: raise RuntimeError("exclusive publication requires renameat2")
        if function(-100, os.fsencode(stage), -100, os.fsencode(target), 1):
            error = ctypes.get_errno()
            raise OSError(error, "exclusive bundle publication failed")
    else:
        raise RuntimeError("exclusive publication supported only on Windows and Linux")


def main():
    parser = argparse.ArgumentParser(description="Offline contiguous A1/compact-v1 merge")
    parser.add_argument("--shard", nargs=2, action="append", required=True, metavar=("LEXICAL", "COMPACT"))
    parser.add_argument("--plan", type=Path, required=True, help="nonsecret logical provenance JSON")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--checkpoint-docs", type=int, default=10000)
    parser.add_argument("--checkpoint-terms", type=int, default=1000)
    args = parser.parse_args()
    try:
        result = merge_indexes(args.shard, args.out, state_dir=args.state_dir,
                               plan=json.loads(args.plan.read_text(encoding="utf-8")),
                               checkpoint_docs=args.checkpoint_docs, checkpoint_terms=args.checkpoint_terms)
    except (OSError, sqlite3.Error) as error:
        # Native operating-system exception strings can contain confidential
        # absolute account paths. Keep those strings out of CLI receipts.
        print(json.dumps({"status": "ERROR", "error": type(error).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({"documents": result["source_manifest"]["n_docs"], "terms": result["terms"], "status": "PUBLISHED"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
