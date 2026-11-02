"""Stdlib acceptance tests for the independent frozen-corpus document source.

Run with: python -m unittest tests.test_corpus_documents -v
All inputs and output indexes are temporary synthetic fixtures.
"""

import hashlib
import json
import os
import tempfile
import tracemalloc
import unittest
from array import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from contracts.retrieval import EmbeddingSpec, VECTOR_MANIFEST, corpus_fingerprint
from offline.corpus_documents import CorpusDocuments
from offline.disk_documents import DiskDocuments
from offline.stream_build import build as stream_build
from offline.vector_build import build_vectors


ROWS = [
    {'doc_id': 'z', 'title': '标题\n第一行', 'text': '中文 Phone\n正文'},
    {'doc_id': 'a', 'title': '', 'text': '()!?，。'},
    {'doc_id': 'b', 'title': '空正文', 'text': ''},
]


class FakeEncoder:
    spec = EmbeddingSpec('fixture-only', 'fixed-v1', dimension=2)

    def __init__(self):
        self.texts = []

    def encode_documents(self, texts):
        self.texts.extend(texts)
        return [[1.0, 0.0] for _ in texts]


class RecordingWriter:
    def __init__(self):
        self.called = False

    def replace(self, manifest, records):
        self.called = True
        self.records = list(records)
        return manifest


class CorpusDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'corpus.jsonl'
        self.write_rows(ROWS)

    def write_rows(self, rows, *, newline=b'\n', final_newline=True):
        raw = newline.join(json.dumps(row, ensure_ascii=False).encode('utf-8')
                            for row in rows)
        if final_newline:
            raw += newline
        self.path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def open_source(self, count=len(ROWS), digest=None):
        return CorpusDocuments(
            self.path,
            expected_sha256=digest or hashlib.sha256(self.path.read_bytes()).hexdigest(),
            expected_n_docs=count,
        )

    def test_same_order_fields_fingerprint_and_vector_inputs_as_real_lexical(self):
        # CRLF bytes and a missing final newline do not alter logical fields.
        digest = self.write_rows(ROWS, newline=b'\r\n', final_newline=False)
        stream_build(self.path, self.root / 'lexical', 'synthetic-parallel')
        with self.open_source(digest=digest) as source, \
                DiskDocuments(self.root / 'lexical') as lexical:
            self.assertEqual(source.n_docs, lexical.n_docs)
            self.assertEqual(corpus_fingerprint(source), corpus_fingerprint(lexical))
            for docid, row in enumerate(ROWS):
                self.assertEqual(source.doc(docid), lexical.doc(docid))
                self.assertEqual(source.doc(docid).doc_id, row['doc_id'])
                self.assertEqual(source.doc(docid).offsets, [])
            for reader, name in ((source, 'corpus-vectors'), (lexical, 'lexical-vectors')):
                encoder, writer = FakeEncoder(), RecordingWriter()
                manifest = build_vectors(reader, encoder, writer, self.root / name,
                                         'test-collection', batch_size=2,
                                         backend='memory-exact')
                self.assertEqual(encoder.texts, [r['title'] + '\n' + r['text'] for r in ROWS])
                self.assertEqual([(r.docid, r.doc_id) for r in writer.records],
                                 list(enumerate(r['doc_id'] for r in ROWS)))
                self.assertEqual(manifest.corpus_sha256, corpus_fingerprint(source))
                if name == 'corpus-vectors':
                    first_records, first_manifest = writer.records, manifest
                else:
                    self.assertEqual(first_records, writer.records)
                    self.assertEqual(first_manifest, manifest)

    def test_source_needs_only_raw_corpus_and_does_not_create_artifacts(self):
        with self.open_source() as source:
            self.assertEqual(source.doc(1).text, '()!?，。')
            self.assertFalse(hasattr(source, 'postings'))
            self.assertFalse(hasattr(source, 'manifest'))
        self.assertEqual([self.path], list(self.root.iterdir()))

    def test_duplicate_external_ids_rejected_before_encoding_and_publication(self):
        self.write_rows([ROWS[0], ROWS[0]])
        encoder, writer = FakeEncoder(), RecordingWriter()
        output = self.root / 'duplicate-vectors'
        with self.open_source(count=2) as source:
            self.assertEqual(source.doc(0).doc_id, source.doc(1).doc_id)
            with self.assertRaisesRegex(ValueError, 'doc_id'):
                build_vectors(source, encoder, writer, output, 'duplicate',
                              backend='memory-exact')
        self.assertEqual([], encoder.texts)
        self.assertFalse(writer.called)
        self.assertFalse((output / VECTOR_MANIFEST).exists())
        self.assertFalse((output / '.vector-ids.sqlite').exists())

    def test_expected_identity_arguments_are_required_and_strict(self):
        good = hashlib.sha256(self.path.read_bytes()).hexdigest()
        for digest in (None, True, 64, '', '0' * 63, 'G' * 64, 'A' * 64):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                CorpusDocuments(self.path, expected_sha256=digest, expected_n_docs=3)
        for count in (None, True, False, 0, -1, 3.0, '3'):
            with self.subTest(count=count), self.assertRaises(ValueError):
                CorpusDocuments(self.path, expected_sha256=good, expected_n_docs=count)
        with self.assertRaises(TypeError):
            CorpusDocuments(self.path)

    def test_raw_hash_and_count_mismatch_close_descriptor(self):
        original_open = Path.open
        opened = []

        def record_open(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            opened.append(handle)
            return handle

        good = hashlib.sha256(self.path.read_bytes()).hexdigest()
        for count, digest in ((3, '0' * 64), (2, good), (4, good)):
            with self.subTest(count=count, digest=digest), \
                    patch.object(Path, 'open', record_open), self.assertRaises(ValueError):
                CorpusDocuments(self.path, expected_sha256=digest, expected_n_docs=count)
            self.assertTrue(opened[-1].closed)

    def test_rejects_malformed_rows_blank_lines_and_invalid_utf8(self):
        bad_rows = [b'\n', b' \t\r\n', b'{broken}\n', b'[]\n', b'null\n',
                    b'\xff\n', b'\xef\xbb\xbf{}\n']
        for key in ('doc_id', 'title', 'text'):
            for value in (None, True, 7, [], {}):
                bad_rows.append(json.dumps(dict(ROWS[0], **{key: value})).encode())
            row = dict(ROWS[0])
            del row[key]
            bad_rows.append(json.dumps(row).encode())
        bad_rows.append(json.dumps(dict(ROWS[0], doc_id='')).encode())
        for raw in bad_rows:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.open_source(count=1)
        self.path.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'count'):
            self.open_source(count=1)

    def test_implicit_ids_ignore_extra_input_docid_field(self):
        self.write_rows([dict(ROWS[0], docid=999)])
        with self.open_source(count=1) as source:
            self.assertEqual(source.doc(0).docid, 0)

    def test_invalid_docids_close_and_context_manager(self):
        source = self.open_source()
        with source:
            for docid in (-1, 3, True, False, 1.0, '0', None):
                with self.subTest(docid=docid), self.assertRaises(IndexError):
                    source.doc(docid)
        source.close()
        with self.assertRaisesRegex(ValueError, 'closed'):
            source.doc(0)
        with self.assertRaisesRegex(ValueError, 'closed'):
            source.__enter__()
        self.assertTrue(source._file.closed)

    def test_concurrent_random_access_keeps_seek_and_read_atomic(self):
        with self.open_source() as source:
            def read_many(offset):
                for i in range(100):
                    docid = (i + offset) % len(ROWS)
                    self.assertEqual(source.doc(docid).doc_id, ROWS[docid]['doc_id'])
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(read_many, range(16)))

    def test_runtime_content_change_and_metadata_change_fail_closed(self):
        for action in ('append', 'mtime'):
            with self.subTest(action=action):
                self.write_rows(ROWS)
                with self.open_source() as source:
                    if action == 'append':
                        with self.path.open('ab') as output:
                            output.write(b'\n')
                    else:
                        info = self.path.stat()
                        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        source.doc(0)
                    self.assertTrue(source._file.closed)
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        source.doc(0)

    def test_change_during_scan_closes_failed_source(self):
        original_parse = CorpusDocuments._parse_row
        original_open = Path.open
        opened = []

        def record_open(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            opened.append(handle)
            return handle

        def mutate(line, docid):
            document = original_parse(line, docid)
            if docid == 0:
                info = self.path.stat()
                os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            return document

        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch.object(Path, 'open', record_open), \
                patch.object(CorpusDocuments, '_parse_row', side_effect=mutate), \
                self.assertRaisesRegex(ValueError, 'changed'):
            self.open_source(digest=digest)
        self.assertTrue(opened[-1].closed)

    def test_change_during_read_rejected_before_returning_document(self):
        with self.open_source() as source:
            original_parse = source._parse_row

            def mutate(line, docid):
                result = original_parse(line, docid)
                info = self.path.stat()
                os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
                return result

            with patch.object(source, '_parse_row', side_effect=mutate), \
                    self.assertRaisesRegex(ValueError, 'changed'):
                source.doc(0)

    def test_path_identity_is_checked_in_addition_to_open_descriptor(self):
        with self.open_source() as source:
            with patch.object(Path, 'stat', side_effect=FileNotFoundError), \
                    self.assertRaisesRegex(ValueError, 'changed'):
                source.doc(0)
            self.assertTrue(source._file.closed)

    def test_large_synthetic_source_retains_only_compact_offsets(self):
        count = 16000
        digest = hashlib.sha256()
        with self.path.open('wb') as output:
            for docid in range(count):
                line = json.dumps({'doc_id': str(docid), 'title': '',
                                   'text': 'x' * 1024}).encode() + b'\n'
                digest.update(line)
                output.write(line)
        tracemalloc.start()
        try:
            with self.open_source(count=count, digest=digest.hexdigest()) as source:
                retained, peak = tracemalloc.get_traced_memory()
                self.assertIsInstance(source._starts, array)
                self.assertEqual(source._starts.typecode, 'Q')
                self.assertEqual(source._lengths.typecode, 'Q')
                self.assertEqual(source._starts.itemsize, 8)
                self.assertEqual(len(source._starts), count)
                self.assertLess(retained, count * 32 + 128 * 1024)
                self.assertLess(peak, count * 32 + 512 * 1024)
                self.assertEqual(source.doc(count - 1).doc_id, str(count - 1))
        finally:
            tracemalloc.stop()


if __name__ == '__main__':
    unittest.main()
