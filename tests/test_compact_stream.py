"""Optional streaming compiler acceptance tests; fixtures are never modified."""
import builtins
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from contracts.index_format import REQUIRED_FILES, sha256_of
from offline.compact import compile_index, CompactIndex

ROOT = Path(__file__).resolve().parents[1]
HAS_IJSON = importlib.util.find_spec('ijson') is not None


@unittest.skipUnless(HAS_IJSON, 'optional ijson==3.4.0.post0 is not installed')
class StreamingCompactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source'
        shutil.copytree(ROOT/'fixtures'/'tiny_index', self.source)

    def refresh(self):
        path = self.source/'manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifest['files'] = {name: sha256_of(self.source/name) for name in REQUIRED_FILES}
        path.write_text(json.dumps(manifest), encoding='utf-8')

    def reject(self):
        self.refresh()
        target = self.root/'bad'
        with self.assertRaises((ValueError, KeyError, TypeError)):
            compile_index(self.source, target, streaming=True)
        self.assertFalse(target.exists())
        self.assertEqual([], list(self.root.glob('.compact-*')))

    def test_equivalent_artifact_and_reader(self):
        old = compile_index(self.source, self.root/'old')
        new = compile_index(self.source, self.root/'new', streaming=True)
        self.assertEqual(old, new)
        for name in old['files']:
            self.assertEqual((self.root/'old'/name).read_bytes(), (self.root/'new'/name).read_bytes(), name)
        self.assertEqual(7, len(new['files']))
        with CompactIndex(self.root/'old') as a, CompactIndex(self.root/'new') as b:
            self.assertEqual(list(a.vocab()), list(b.vocab()))
            for term in a.vocab():
                self.assertEqual(a.postings(term), b.postings(term))
            for docid in range(a.n_docs):
                self.assertEqual(a.doc(docid), b.doc(docid))

    def test_source_requires_valid_checksums_in_both_compilers(self):
        path = self.source/'manifest.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        for streaming in (False, True):
            for digest in (None, '', 'bad-hash', 'A'*64, '0'*64):
                with self.subTest(streaming=streaming, digest=digest):
                    manifest = dict(original, files={name:digest for name in REQUIRED_FILES})
                    path.write_text(json.dumps(manifest), encoding='utf-8')
                    target = self.root/'bad-hash'
                    with self.assertRaises(ValueError):
                        compile_index(self.source, target, streaming=streaming)
                    self.assertFalse(target.exists())
            manifest = dict(original, files={})
            path.write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaises(ValueError):
                compile_index(self.source, self.root/'missing-hash', streaming=streaming)
            self.assertFalse((self.root/'missing-hash').exists())

    def test_arbitrary_field_order(self):
        path = self.source/'postings.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        permutations = [('docs','term','df'), ('df','docs','term'), ('df','term','docs'), ('term','docs','df')]
        path.write_text(''.join(json.dumps({key: row[key] for key in permutations[i % 4]}, ensure_ascii=False)+'\n' for i, row in enumerate(rows)), encoding='utf-8')
        stats = self.source/'stats.json'
        row = json.loads(stats.read_text(encoding='utf-8'))
        stats.write_text(json.dumps({'avgdl':row['avgdl'], 'doc_lengths':dict(reversed(list(row['doc_lengths'].items())))}), encoding='utf-8')
        self.refresh()
        old = compile_index(self.source, self.root/'old')
        new = compile_index(self.source, self.root/'new', streaming=True)
        self.assertEqual(old, new)

    def test_invalid_posting_contracts_are_atomic(self):
        path = self.source/'postings.jsonl'
        original = path.read_text(encoding='utf-8')
        rows = original.splitlines()
        first = json.loads(rows[0])
        cases = [
            '{"term":"a","term":"b","df":1,"docs":[[0,1,[0]]]}',
            json.dumps({'term': first['term'], 'df':first['df']}),
            json.dumps(dict(first, df=first['df']+1)),
            json.dumps(dict(first, docs=[[0, True, [0]]])),
            json.dumps(dict(first, docs=[[0, 1, [True]]])),
            json.dumps(dict(first, docs=[[0, 2, [1, 0]]])),
            json.dumps(dict(first, docs=[[0, 1, [999999]]])),
            json.dumps(dict(first, docs=[[0, 1, [0], 2]])),
            json.dumps(dict(first, docs={'0':[0,1,[0]]})),
            json.dumps(dict(first, extra=1)),
            '{"docs":[],"docs":[],"term":"x","df":1}',
            '[]',
            '{"term":"x","df":1,"docs":[[0,1,[0]]]',
            json.dumps(dict(first, docs=[[0, 1, [0]], [0, 1, [1]]])),
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                path.write_text(invalid+'\n'+'\n'.join(rows[1:])+'\n', encoding='utf-8')
                self.reject()
        path.write_text(original+rows[-1]+'\n', encoding='utf-8')
        self.reject()
        # Removing one whole term leaves a valid shape but invalid global totals.
        path.write_text('\n'.join(rows[1:])+'\n', encoding='utf-8')
        self.reject()

    def test_statistics_duplicates_missing_and_invalid(self):
        path = self.source/'stats.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        lengths = json.dumps(original['doc_lengths'])
        for invalid in [
            '{"avgdl":23.3,"doc_lengths":'+lengths+',"avgdl":23.3}',
            '{"avgdl":23.3,"doc_lengths":{"0":24,"0":24}}',
            '{"avgdl":23.3,"doc_lengths":{"00":24}}',
            '{"avgdl":23.3}',
            '{"avgdl":23.3,"doc_lengths":{',
            json.dumps(dict(original, avgdl=99)),
            json.dumps({'avgdl':23.3, 'doc_lengths':dict(original['doc_lengths'], **{'0': True})}),
        ]:
            with self.subTest(invalid=invalid):
                path.write_text(invalid, encoding='utf-8')
                self.reject()

    def test_duplicate_document_id_and_offsets_rejected(self):
        path = self.source/'docs.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        rows[1]['doc_id'] = rows[0]['doc_id']
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
        self.reject()
        shutil.copyfile(ROOT/'fixtures'/'tiny_index'/'docs.jsonl', path)
        path = self.source/'offsets.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        rows[0]['spans'][0][3] = 999999
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
        self.reject()

    def test_zero_token_mixed_and_all_punctuation_preserve_contract(self):
        from contracts.api import SearchRequest
        from contracts.conformance import check_reader
        from offline.build import build
        from offline.packed_index import PackedIndex
        from online.engine import SearchEngine
        from online.memory_index import MemoryIndex
        corpora = {
            'mixed': [
                {'doc_id':'punct-0', 'title':'punctuation', 'text':'()'},
                {'doc_id':'word-1', 'title':'words', 'text':'alpha beta alpha'},
                {'doc_id':'punct-2', 'title':'more punctuation', 'text':'!!! ()'},
                {'doc_id':'word-3', 'title':'word', 'text':'beta'},
            ],
            'all-zero': [
                {'doc_id':'punct-0', 'title':'first', 'text':'()'},
                {'doc_id':'punct-1', 'title':'second', 'text':'!!! ()'},
            ],
        }
        for name, corpus in corpora.items():
            with self.subTest(corpus=name):
                corpus_path = self.root/(name+'.jsonl')
                corpus_path.write_text(''.join(json.dumps(row)+'\n' for row in corpus), encoding='utf-8')
                lexical = self.root/(name+'-lexical')
                build(corpus_path, lexical, name)
                old_path, stream_path = self.root/(name+'-old'), self.root/(name+'-stream')
                old = compile_index(lexical, old_path)
                stream = compile_index(lexical, stream_path, streaming=True)
                self.assertEqual(old, stream)
                for filename in old['files']:
                    self.assertEqual((old_path/filename).read_bytes(), (stream_path/filename).read_bytes(), filename)
                if name == 'all-zero':
                    self.assertEqual(0, old['source_manifest']['avgdl'])
                    self.assertEqual(0, old['terms'])
                    self.assertEqual(b'', (old_path/'postings.bin').read_bytes())
                    self.assertEqual(b'', (old_path/'offsets.bin').read_bytes())
                oracle = SearchEngine(MemoryIndex.from_corpus(corpus))
                for path in (old_path, stream_path):
                    for reader_type in (CompactIndex, PackedIndex):
                        with reader_type(path) as reader:
                            check_reader(reader, corpus)
                            self.assertEqual([row['doc_id'] for row in corpus], [reader.external_doc_id(i) for i in range(reader.n_docs)])
                            for docid, row in enumerate(corpus):
                                from contracts import analyzer
                                if not analyzer.analyze(row['text']):
                                    self.assertEqual(0, reader.doc_length(docid))
                                    self.assertEqual([], reader.doc(docid).offsets)
                            engine = SearchEngine(reader)
                            for query in ('alpha', 'beta', 'missingterm'):
                                for scorer in ('bm25', 'tfidf'):
                                    req = SearchRequest(query=query, scorer=scorer)
                                    want = oracle.search(req).to_dict()['results']
                                    self.assertEqual(want, engine.search(req).to_dict()['results'])

    def test_zero_length_duplicates_negative_and_forged_zero_rejected(self):
        from offline.build import build
        lexical = self.root/'zero-source'
        corpus_path = self.root/'zero-corpus.jsonl'
        corpus_path.write_text(json.dumps({'doc_id':'z', 'title':'', 'text':'()'})+'\n', encoding='utf-8')
        build(corpus_path, lexical, 'zero')
        self.source = lexical
        stats = lexical/'stats.json'
        stats.write_text('{"avgdl":0,"doc_lengths":{"0":0,"0":0}}', encoding='utf-8')
        self.reject()
        stats.write_text('{"avgdl":-1,"doc_lengths":{"0":-1}}', encoding='utf-8')
        self.refresh()
        for streaming in (False, True):
            target = self.root/('negative-'+str(streaming))
            with self.assertRaises(ValueError):
                compile_index(lexical, target, streaming=streaming)
            self.assertFalse(target.exists())
        # All structural totals/offsets agree on zero, but source text has tokens.
        stats.write_text('{"avgdl":0,"doc_lengths":{"0":0}}', encoding='utf-8')
        manifest = lexical/'manifest.json'
        row = json.loads(manifest.read_text(encoding='utf-8'))
        row['avgdl'] = 0
        manifest.write_text(json.dumps(row), encoding='utf-8')
        (lexical/'docs.jsonl').write_text(json.dumps({'docid':0, 'doc_id':'z', 'title':'', 'text':'alpha'})+'\n', encoding='utf-8')
        self.refresh()
        for streaming in (False, True):
            target = self.root/('forged-'+str(streaming))
            with self.assertRaisesRegex(ValueError, 'contradicts analyzer'):
                compile_index(lexical, target, streaming=streaming)
            self.assertFalse(target.exists())
        # A zero length cannot acquire a positive posting even with avgdl=0.
        (lexical/'docs.jsonl').write_text(json.dumps({'docid':0, 'doc_id':'z', 'title':'', 'text':'()'})+'\n', encoding='utf-8')
        (lexical/'postings.jsonl').write_text('{"term":"a","df":1,"docs":[[0,1,[0]]]}\n', encoding='utf-8')
        self.refresh()
        for streaming in (False, True):
            target = self.root/('zero-posting-'+str(streaming))
            with self.assertRaises(ValueError):
                compile_index(lexical, target, streaming=streaming)
            self.assertFalse(target.exists())

    def test_high_df_uses_bounded_reads_and_memory(self):
        from offline.compact_stream import _postings
        # A single term's docs exceed the parser buffer; no line/item materialization.
        n = 30000
        raw = ('{"docs":['+','.join('[%d,1,[0]]' % i for i in range(n))+'],"df":%d,"term":"common"}' % n).encode()
        class BoundedRead(io.BytesIO):
            def read(self, size=-1):
                if size < 0 or size > 65536:
                    raise AssertionError('unbounded parser read')
                return super().read(size)
            def readinto(self, buffer):
                if len(buffer) > 65536:
                    raise AssertionError('unbounded parser readinto')
                return super().readinto(buffer)
        source_path = self.root/'synthetic'
        source_path.write_bytes(raw)
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE terms(term TEXT PRIMARY KEY, start INTEGER, size INTEGER, df INTEGER) WITHOUT ROWID')
        from array import array
        with (self.root/'postings.bin').open('wb') as out, patch.object(Path, 'open', return_value=BoundedRead(raw)):
            tracemalloc.start()
            try:
                self.assertEqual(1, _postings(source_path, out, db, array('I', [1])*n))
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertEqual(n*12, (self.root/'postings.bin').stat().st_size)
        self.assertLess(peak, 16*1024*1024, 'streaming parser unexpectedly retained whole term')
        self.assertEqual(('common',0,n*12,n), db.execute('SELECT * FROM terms').fetchone())


class OptionalDependencyTests(unittest.TestCase):
    def test_default_stays_dependency_free_and_optional_fails_clearly(self):
        real_import = builtins.__import__
        def without_ijson(name, *args, **kwargs):
            if name == 'ijson' or name.startswith('ijson.'):
                raise ImportError('simulated missing optional dependency')
            return real_import(name, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch('builtins.__import__', side_effect=without_ijson):
            target = Path(directory)/'default'
            compile_index(ROOT/'fixtures'/'tiny_index', target)
            self.assertTrue((target/'compact.json').exists())
            with self.assertRaisesRegex(RuntimeError, 'ijson==3.4.0.post0'):
                compile_index(ROOT/'fixtures'/'tiny_index', Path(directory)/'stream', streaming=True)
            self.assertFalse((Path(directory)/'stream').exists())


if __name__ == '__main__':
    unittest.main()
