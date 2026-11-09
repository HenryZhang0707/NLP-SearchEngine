"""Optional event-driven compact compiler; memory is bounded per posting/document.

The optional streaming compilers use ijson. The default stays dependency free.
"""
from array import array
import json
import math
from pathlib import Path
import sqlite3
import sys
import tempfile

from contracts import analyzer
from contracts.index_format import load_manifest, verify_files, sha256_of, REQUIRED_FILES
from offline.compact import PAIR, u32

BUFFER_VALUES = 16384


def _events(stream, *, multiple=False):
    try:
        import ijson
    except ImportError as exc:
        raise RuntimeError('streaming compact requires optional ijson==3.4.0.post0') from exc
    try:
        yield from ijson.parse(stream, multiple_values=multiple, buf_size=65536)
    except ijson.JSONError as exc:
        raise ValueError('invalid or incomplete source JSON') from exc


def _expect(events, prefix, event):
    try:
        actual_prefix, actual_event, value = next(events)
    except StopIteration as exc:
        raise ValueError('incomplete source JSON structure') from exc
    if actual_prefix != prefix or actual_event != event:
        raise ValueError('invalid source JSON structure')
    return value


def _integer(value):
    return type(value) is int


def _lengths(path, manifest):
    lengths = array('I', [0]) * manifest.n_docs
    seen_docids = bytearray(manifest.n_docs)
    seen = set()
    average = None
    with path.open('rb') as stream:
        events = _events(stream)
        _expect(events, '', 'start_map')
        for prefix, event, value in events:
            if (prefix, event) == ('', 'end_map'):
                break
            if (prefix, event) != ('', 'map_key') or value in seen:
                raise ValueError('invalid/duplicate source statistics field')
            seen.add(value)
            if value == 'avgdl':
                average = _expect(events, 'avgdl', 'number')
            elif value == 'doc_lengths':
                _expect(events, 'doc_lengths', 'start_map')
                count = 0
                for p, e, key in events:
                    if (p, e) == ('doc_lengths', 'end_map'):
                        break
                    if (p, e) != ('doc_lengths', 'map_key') or not key.isascii() or not key.isdecimal():
                        raise ValueError('invalid source document length key')
                    docid = int(key)
                    if str(docid) != key or not 0 <= docid < manifest.n_docs or seen_docids[docid]:
                        raise ValueError('invalid/duplicate source document length')
                    length = _expect(events, 'doc_lengths.' + key, 'number')
                    if not _integer(length) or not 0 <= length <= 0xffffffff:
                        raise ValueError('compact source requires nonnegative uint32 token lengths')
                    lengths[docid] = length
                    seen_docids[docid] = 1
                    count += 1
                if count != manifest.n_docs:
                    raise ValueError('source document lengths incomplete')
            else:
                raise ValueError('unknown source statistics field')
        if next(events, None) is not None or seen != {'avgdl', 'doc_lengths'} or not all(seen_docids):
            raise ValueError('source document lengths incomplete')
    if not math.isclose(sum(lengths)/len(lengths), manifest.avgdl, rel_tol=1e-9) or not math.isclose(average, manifest.avgdl, rel_tol=1e-9):
        raise ValueError('source length statistics mismatch')
    return lengths


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate source JSON field')
        result[key] = value
    return result


class _Writer:
    def __init__(self, stream):
        self.stream = stream
        self.buffer = array('I')

    def put(self, value):
        self.buffer.append(value)
        if len(self.buffer) >= BUFFER_VALUES:
            self.flush()

    def flush(self):
        if self.buffer:
            if sys.byteorder != 'little':
                self.buffer.byteswap()
            self.stream.write(self.buffer.tobytes())
            self.buffer = array('I')

    def tell(self):
        return self.stream.tell() + len(self.buffer) * 4


def _posting(events, lengths, totals, last, writer):
    docid = _expect(events, 'docs.item.item', 'number')
    tf = _expect(events, 'docs.item.item', 'number')
    _expect(events, 'docs.item.item', 'start_array')
    if not _integer(docid) or not _integer(tf) or not last < docid < len(lengths) or not 1 <= tf <= 0xffffffff:
        raise ValueError('invalid source posting')
    # A posting can itself be large; keep only the previous position, never a term.
    writer.put(docid)
    writer.put(tf)
    count, previous = 0, -1
    for prefix, event, position in events:
        if (prefix, event) == ('docs.item.item', 'end_array'):
            break
        if prefix != 'docs.item.item.item' or event != 'number' or not _integer(position) or not previous < position < lengths[docid]:
            raise ValueError('invalid source posting position')
        writer.put(position)
        previous = position
        count += 1
    _expect(events, 'docs.item', 'end_array')
    if count != tf:
        raise ValueError('invalid source posting tf')
    totals[docid] += tf
    if totals[docid] > lengths[docid]:
        raise ValueError('source posting token totals mismatch')
    return docid


def _postings(path, out, database, lengths):
    # Empty JSONL is the canonical vocabulary for an all-zero-token corpus.
    # ijson otherwise treats an empty stream as an incomplete JSON value.
    if path.stat().st_size == 0:
        if any(lengths):
            raise ValueError('source posting token totals mismatch')
        return 0
    totals = array('Q', [0]) * len(lengths)
    writer = _Writer(out)
    previous, terms = None, 0
    with path.open('rb') as stream:
        events = _events(stream, multiple=True)
        for prefix, event, value in events:
            if (prefix, event) != ('', 'start_map'):
                raise ValueError('invalid source term structure')
            seen, term, df, count = set(), None, None, 0
            start = writer.tell()
            for prefix, event, value in events:
                if (prefix, event) == ('', 'end_map'):
                    break
                if (prefix, event) != ('', 'map_key') or value in seen:
                    raise ValueError('invalid/duplicate source term field')
                seen.add(value)
                if value == 'term':
                    term = _expect(events, 'term', 'string')
                elif value == 'df':
                    df = _expect(events, 'df', 'number')
                elif value == 'docs':
                    _expect(events, 'docs', 'start_array')
                    last = -1
                    for p, e, v in events:
                        if (p, e) == ('docs', 'end_array'):
                            break
                        if (p, e) != ('docs.item', 'start_array'):
                            raise ValueError('invalid source posting structure')
                        last = _posting(events, lengths, totals, last, writer)
                        count += 1
                else:
                    raise ValueError('unknown source term field')
            if (seen != {'term', 'df', 'docs'} or not term or previous is not None and term <= previous
                    or not _integer(df) or df < 1 or df != count):
                raise ValueError('source term order/df mismatch')
            previous = term
            database.execute('INSERT INTO terms VALUES(?,?,?,?)', (term, start, writer.tell()-start, df))
            terms += 1
    writer.flush()
    if any(total != length for total, length in zip(totals, lengths)):
        raise ValueError('source posting token totals mismatch')
    return terms


def compile_streaming(source, target):
    # Fail before source work when the explicitly requested optional extra is absent.
    try:
        import ijson  # noqa: F401
    except ImportError as exc:
        raise RuntimeError('streaming compact requires optional ijson==3.4.0.post0') from exc
    source, target = Path(source), Path(target)
    manifest = load_manifest(source)
    if set(REQUIRED_FILES) - set(manifest.files):
        raise ValueError('source manifest requires all file checksums')
    verify_files(source, manifest, require_hashes=True)
    if manifest.analyzer_hash != analyzer.config_hash() or manifest.n_docs <= 0:
        raise ValueError('source analyzer/document count mismatch')
    if target.exists() and any(target.iterdir()):
        raise ValueError('compact target must be fresh')
    target.parent.mkdir(parents=True, exist_ok=True)
    lengths = _lengths(source/'stats.json', manifest)
    with tempfile.TemporaryDirectory(prefix='.compact-', dir=target.parent) as tmp:
        stage = Path(tmp)
        char_lengths, count = array('Q'), 0
        identity_path = stage/'identities.sqlite'
        identity = sqlite3.connect(identity_path)
        try:
            identity.execute('CREATE TABLE identities(doc_id TEXT PRIMARY KEY) WITHOUT ROWID')
            with (source/'docs.jsonl').open('rb') as docs, (stage/'docs.jsonl').open('wb') as out, (stage/'docs.idx').open('wb') as index:
                for count, line in enumerate(docs, 1):
                    row = json.loads(line, object_pairs_hook=_unique_object)
                    if (type(row['docid']) is not int or row['docid'] != count-1 or count > manifest.n_docs
                            or not isinstance(row['doc_id'], str) or not row['doc_id']
                            or not isinstance(row['text'], str) or not isinstance(row['title'], str)):
                        raise ValueError('invalid source document')
                    try:
                        identity.execute('INSERT INTO identities VALUES(?)', (row['doc_id'],))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError('duplicate source document identity') from exc
                    if lengths[count-1] == 0 and analyzer.analyze(row['text']):
                        raise ValueError('zero source length contradicts analyzer tokens')
                    char_lengths.append(len(row['text']))
                    index.write(PAIR.pack(out.tell(), len(line)))
                    out.write(line)
            if count != manifest.n_docs:
                raise ValueError('source document count mismatch')
        finally:
            identity.close()
        identity_path.unlink()
        count = 0
        with (source/'offsets.jsonl').open(encoding='utf-8') as offsets, (stage/'offsets.bin').open('wb') as out, (stage/'offsets.idx').open('wb') as index:
            for count, line in enumerate(offsets, 1):
                row = json.loads(line, object_pairs_hook=_unique_object)
                docid, spans = count-1, row['spans']
                if type(row['docid']) is not int or row['docid'] != docid or docid >= manifest.n_docs or not isinstance(spans, list) or len(spans) != lengths[docid]:
                    raise ValueError('source offset count/order mismatch')
                start = out.tell()
                writer = _Writer(out)
                for i, span in enumerate(spans):
                    if (not isinstance(span, list) or len(span) != 4 or any(type(v) is not int for v in span)
                            or span[:2] != [i, i+1] or not 0 <= span[2] < span[3] <= char_lengths[docid]):
                        raise ValueError('invalid source offset span')
                    writer.put(span[2]); writer.put(span[3])
                writer.flush()
                index.write(PAIR.pack(start, out.tell()-start))
        if count != manifest.n_docs:
            raise ValueError('source offset document count mismatch')
        (stage/'lengths.bin').write_bytes(u32(lengths))
        database = sqlite3.connect(stage/'terms.sqlite')
        try:
            database.execute('CREATE TABLE terms(term TEXT PRIMARY KEY, start INTEGER, size INTEGER, df INTEGER) WITHOUT ROWID')
            with (stage/'postings.bin').open('wb') as out:
                terms = _postings(source/'postings.jsonl', out, database, lengths)
            database.commit()
        finally:
            database.close()
        files = {p.name: sha256_of(p) for p in stage.iterdir()}
        metadata = dict(compact_version=1, source_manifest=manifest.to_dict(), files=files, terms=terms)
        (stage/'compact.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        if target.exists():
            target.rmdir()
        stage.replace(target)
    return metadata
