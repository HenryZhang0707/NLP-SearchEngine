"""Mocked CLI routing and resource-lifetime checks; no model/GPU/database."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import build_vectors as cli


class BuildVectorsCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.common = ['--out', str(self.root / 'vectors'), '--collection', 'new-collection',
                       '--model-path', str(self.root / 'model')]

    def test_index_and_corpus_route_preserve_encoder_and_build_parameters(self):
        for kind in ('index', 'corpus'):
            with self.subTest(kind=kind):
                source = MagicMock()
                reader = source.__enter__.return_value
                reader.n_docs = 3
                encoder = MagicMock()
                encoder.spec = object()
                encoder.encode_documents.return_value = [[1.0, 0.0]]
                writer = object()
                manifest = SimpleNamespace(n_docs=3, collection='new-collection')
                output = io.StringIO()

                def make_encoder(**kwargs):
                    source.__enter__.assert_called_once()
                    source.__exit__.assert_not_called()
                    return encoder

                def build(reader_arg, encoder_arg, writer_arg, out, collection, **kwargs):
                    self.assertIs(reader_arg, reader)
                    self.assertIs(encoder_arg.spec, encoder.spec)
                    self.assertEqual(encoder_arg.encode_documents(['title\ntext']), [[1.0, 0.0]])
                    self.assertIs(writer_arg, writer)
                    self.assertEqual(out, self.root / 'vectors')
                    self.assertEqual(collection, 'new-collection')
                    self.assertEqual(kwargs, {'batch_size': 64,
                                             'index_params': {'m': 16, 'ef_construct': 100}})
                    return manifest

                extra = ['--index', 'lexical'] if kind == 'index' else [
                    '--corpus', 'frozen.jsonl', '--corpus-sha256', 'a' * 64,
                    '--corpus-n-docs', '3']
                with patch.object(cli, 'DiskDocuments', return_value=source) as disk, \
                        patch.object(cli, 'CorpusDocuments', return_value=source) as corpus, \
                        patch.object(cli, 'BGEEncoder', side_effect=make_encoder) as bge, \
                        patch.object(cli, 'QdrantSnapshotWriter', return_value=writer) as qdrant, \
                        patch.object(cli, 'build_vectors', side_effect=build), \
                        contextlib.redirect_stdout(output):
                    cli.main(self.common + extra)
                if kind == 'index':
                    disk.assert_called_once_with('lexical')
                    corpus.assert_not_called()
                else:
                    corpus.assert_called_once_with('frozen.jsonl', expected_sha256='a' * 64,
                                                   expected_n_docs=3)
                    disk.assert_not_called()
                bge.assert_called_once_with(model_path=str(self.root / 'model'), device='cpu',
                                            batch_size=64)
                qdrant.assert_called_once_with('http://127.0.0.1:16353', self.root / 'vectors',
                                               timeout=180)
                encoder.encode_documents.assert_called_once_with(['title\ntext'])
                source.__exit__.assert_called_once_with(None, None, None)
                summary = json.loads(output.getvalue())
                self.assertEqual(set(summary), {'n_docs', 'seconds', 'collection'})
                self.assertEqual(summary['n_docs'], 3)

    def test_custom_device_batch_and_endpoint_are_forwarded(self):
        source = MagicMock()
        with patch.object(cli, 'DiskDocuments', return_value=source), \
                patch.object(cli, 'BGEEncoder') as bge, \
                patch.object(cli, 'QdrantSnapshotWriter') as qdrant, \
                patch.object(cli, 'build_vectors', return_value=SimpleNamespace(n_docs=1, collection='c')) as build, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(self.common + ['--index', 'index', '--device', 'cuda:0',
                                    '--batch-size', '7', '--url', 'http://localhost:6333'])
        self.assertEqual(bge.call_args.kwargs['device'], 'cuda:0')
        self.assertEqual(bge.call_args.kwargs['batch_size'], 7)
        self.assertEqual(build.call_args.kwargs['batch_size'], 7)
        self.assertEqual(qdrant.call_args.args[0], 'http://localhost:6333')

    def test_explicit_hnsw_recipe_is_forwarded_without_changing_defaults(self):
        for disk_flag, expected in (('--hnsw-on-disk', True), ('--no-hnsw-on-disk', False)):
            with self.subTest(disk_flag=disk_flag), \
                    patch.object(cli, 'DiskDocuments', return_value=MagicMock()), \
                    patch.object(cli, 'BGEEncoder'), patch.object(cli, 'QdrantSnapshotWriter'), \
                    patch.object(cli, 'build_vectors', return_value=SimpleNamespace(n_docs=1, collection='c')) as build, \
                    contextlib.redirect_stdout(io.StringIO()):
                cli.main(self.common + ['--index', 'index', '--hnsw-m', '24',
                                        '--hnsw-ef-construct', '160',
                                        '--hnsw-full-scan-threshold-kb', '10',
                                        '--hnsw-max-indexing-threads', '2', disk_flag])
            self.assertEqual(build.call_args.kwargs['index_params'], {
                'm': 24, 'ef_construct': 160, 'full_scan_threshold': 10,
                'max_indexing_threads': 2, 'on_disk': expected})

    def test_argument_errors_precede_reader_model_and_writer_initialization(self):
        invalid = [[], ['--index', 'i', '--corpus', 'c'], ['--corpus', 'c'],
                   ['--corpus', 'c', '--corpus-sha256', 'a' * 64],
                   ['--corpus', 'c', '--corpus-n-docs', '1'],
                   ['--index', 'i', '--corpus-sha256', 'a' * 64],
                   ['--index', 'i', '--corpus-n-docs', '1'],
                   ['--corpus', 'c', '--corpus-sha256', 'A' * 64, '--corpus-n-docs', '1'],
                   ['--corpus', 'c', '--corpus-sha256', 'a' * 64, '--corpus-n-docs', '0'],
                   ['--corpus', 'c', '--corpus-sha256', 'a' * 64, '--corpus-n-docs', '-1'],
                   ['--corpus', 'c', '--corpus-sha256', 'a' * 64, '--corpus-n-docs', '1.0'],
                   ['--index', 'i', '--batch-size', '0'],
                   ['--index', 'i', '--hnsw-m', '-1'],
                   ['--index', 'i', '--hnsw-ef-construct', '0'],
                   ['--index', 'i', '--hnsw-full-scan-threshold-kb', '-1'],
                   ['--index', 'i', '--hnsw-max-indexing-threads', '-1']]
        invalid += [['--index', 'i', '--hnsw-ef-construct', str(value)] for value in range(1, 4)]
        invalid += [['--index', 'i', '--hnsw-full-scan-threshold-kb', str(value)]
                    for value in range(10)]
        with patch.object(cli, 'DiskDocuments') as disk, \
                patch.object(cli, 'CorpusDocuments') as corpus, \
                patch.object(cli, 'BGEEncoder') as bge, \
                patch.object(cli, 'QdrantSnapshotWriter') as writer:
            for args in invalid:
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as result:
                    cli.main(self.common + args)
                self.assertEqual(result.exception.code, 2)
            for mock in (disk, corpus, bge, writer):
                mock.assert_not_called()

    def test_actual_corpus_hash_count_and_fields_fail_before_model(self):
        path = self.root / 'corpus.jsonl'
        path.write_text('{"doc_id":"a","title":"","text":"text"}\n', encoding='utf-8')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for sha, count in (('0' * 64, '1'), (digest, '2')):
            with self.subTest(sha=sha, count=count), patch.object(cli, 'BGEEncoder') as bge, \
                    self.assertRaises(ValueError):
                cli.main(self.common + ['--corpus', str(path), '--corpus-sha256', sha,
                                        '--corpus-n-docs', count])
            bge.assert_not_called()
        path.write_bytes(b'{"doc_id":null,"title":"","text":""}\n')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch.object(cli, 'BGEEncoder') as bge, self.assertRaises(ValueError):
            cli.main(self.common + ['--corpus', str(path), '--corpus-sha256', digest,
                                    '--corpus-n-docs', '1'])
        bge.assert_not_called()

    def test_reader_is_closed_on_encoder_and_build_failure(self):
        for failure in ('encoder', 'build'):
            with self.subTest(failure=failure):
                source = MagicMock()
                with patch.object(cli, 'DiskDocuments', return_value=source), \
                        patch.object(cli, 'BGEEncoder', side_effect=RuntimeError('encoder failed')
                                     if failure == 'encoder' else None), \
                        patch.object(cli, 'QdrantSnapshotWriter'), \
                        patch.object(cli, 'build_vectors', side_effect=RuntimeError('build failed')), \
                        self.assertRaisesRegex(RuntimeError, 'failed'):
                    cli.main(self.common + ['--index', 'index'])
                source.__exit__.assert_called_once()
                self.assertIs(source.__exit__.call_args.args[0], RuntimeError)

    def test_existing_published_output_is_rejected_before_model(self):
        output = self.root / 'vectors'
        output.mkdir()
        (output / cli.VECTOR_MANIFEST).write_text('{}', encoding='utf-8')
        with patch.object(cli, 'BGEEncoder') as bge, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(self.common + ['--index', 'index'])
        bge.assert_not_called()

    def test_file_output_or_parent_is_rejected_before_input_scan_and_model(self):
        blocker = self.root / 'existing-file'
        blocker.write_bytes(b'user content')
        for output in (blocker, blocker / 'nested' / 'vectors'):
            for source_args in (['--index', 'unused'],
                                ['--corpus', 'unused', '--corpus-sha256', 'a' * 64,
                                 '--corpus-n-docs', '8096668']):
                with self.subTest(output=output, source=source_args), \
                        patch.object(cli, 'DiskDocuments') as disk, \
                        patch.object(cli, 'CorpusDocuments') as corpus, \
                        patch.object(cli, 'BGEEncoder') as bge, \
                        patch.object(cli, 'QdrantSnapshotWriter') as writer, \
                        contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as error:
                    cli.main(self.common + ['--out', str(output)] + source_args)
                self.assertEqual(error.exception.code, 2)
                for mock in (disk, corpus, bge, writer):
                    mock.assert_not_called()
        self.assertEqual(blocker.read_bytes(), b'user content')


if __name__ == '__main__':
    unittest.main()
