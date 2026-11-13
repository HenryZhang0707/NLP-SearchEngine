"""Synthetic ownership regressions; no network, real corpus, or optional SDK."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from contracts.index_format import sha256_of
from contracts.reader import Document
from contracts.retrieval import EmbeddingSpec, VectorManifest
from offline.vector_build import QdrantSnapshotWriter, _validate_reader_ids, build_vectors


class _Reader:
    n_docs = 1

    def doc(self, docid):
        return Document(docid, 'external-id', 'title', 'text')


class _Encoder:
    spec = EmbeddingSpec('synthetic-model', 'fixed-revision', 2)

    def encode_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


class _NotFound(Exception):
    status_code = 404


class _Client:
    def __init__(self, name='build.snapshot'):
        self.name = name
        self.info = None
        self.deleted = []

    def get_collection(self, collection_name):
        if self.info is None:
            raise _NotFound()
        return self.info

    def create_collection(self, **kwargs):
        self.info = SimpleNamespace(config=SimpleNamespace(metadata=kwargs['metadata']))

    def upsert(self, **kwargs):
        pass

    def count(self, **kwargs):
        return SimpleNamespace(count=1)

    def create_snapshot(self, **kwargs):
        return SimpleNamespace(name=self.name)

    def delete_collection(self, collection_name):
        self.deleted.append(collection_name)


class ArtifactOwnershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.writer = QdrantSnapshotWriter('http://synthetic.invalid', self.root)
        self.manifest = VectorManifest('a' * 64, 1, _Encoder.spec, 'new-build')

    def _sdk(self, client):
        models = SimpleNamespace(VectorParams=SimpleNamespace, PointStruct=SimpleNamespace,
                                 Distance=SimpleNamespace(COSINE='Cosine'))
        return patch.dict(sys.modules, {'qdrant_client': SimpleNamespace(
            QdrantClient=lambda **kwargs: client, models=models)})

    def test_reader_validation_preserves_reserved_database_bytes(self):
        path = self.root / '.vector-ids.sqlite'
        path.write_bytes(b'previous database bytes')
        _validate_reader_ids(_Reader(), self.root)
        self.assertEqual(path.read_bytes(), b'previous database bytes')
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_full_build_preserves_both_reserved_databases_and_manifest_tmp(self):
        protected = {name: b'previous ' + name.encode() for name in (
            '.vector-ids.sqlite', '.qdrant-doc-ids.sqlite', 'vector_manifest.json.tmp')}
        for name, data in protected.items():
            (self.root / name).write_bytes(data)
        with self._sdk(_Client()), patch('urllib.request.urlopen', return_value=io.BytesIO(b'complete')):
            manifest = build_vectors(_Reader(), _Encoder(), self.writer, self.root, 'new-build')
        for name, data in protected.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertEqual(manifest.snapshot_files, {'build.snapshot': sha256_of(self.root / 'build.snapshot')})
        self.assertEqual(json.loads((self.root / 'vector_manifest.json').read_text('utf-8')), manifest.to_dict())
        self.assertEqual({p.name for p in self.root.iterdir()}, set(protected) | {
            'build.snapshot', 'vector_manifest.json'})

    def test_existing_snapshot_or_partial_is_never_overwritten_or_removed(self):
        for name in ('build.snapshot', 'build.snapshot.part'):
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(b'previous artifact')
                with patch('urllib.request.urlopen', return_value=io.BytesIO(b'new bytes')):
                    with self.assertRaises(RuntimeError):
                        self.writer._download_snapshot(_Client(), self.manifest)
                self.assertEqual(path.read_bytes(), b'previous artifact')
                self.assertEqual(list(self.root.iterdir()), [path])
                path.unlink()

    def test_unsafe_snapshot_basenames_fail_without_network_or_local_changes(self):
        for name in ('.', '..', '../escape.snapshot', 'dir/file.snapshot',
                     'dir\\file.snapshot', 'C:escape.snapshot', 'bad.snapshot.', 'NUL'):
            with self.subTest(name=name), patch('urllib.request.urlopen') as request:
                with self.assertRaises(RuntimeError):
                    self.writer._download_snapshot(_Client(name), self.manifest)
                request.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_transport_failure_removes_only_owned_partial(self):
        sentinel = self.root / 'unrelated.part'
        sentinel.write_bytes(b'protected')
        with patch('urllib.request.urlopen', side_effect=OSError('transport failed')):
            with self.assertRaises(RuntimeError):
                self.writer._download_snapshot(_Client(), self.manifest)
        self.assertEqual(sentinel.read_bytes(), b'protected')
        self.assertEqual(list(self.root.iterdir()), [sentinel])

    def test_partial_read_and_hash_failures_remove_only_owned_partial(self):
        class Interrupted(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError('interrupted after first chunk')
                return super().read(size)

        sentinel = self.root / 'protected.snapshot'
        sentinel.write_bytes(b'old artifact')
        cases = ((Interrupted(b'partial'), None), (io.BytesIO(b'complete'), OSError('hash failed')))
        for response, hash_error in cases:
            with self.subTest(hash_error=hash_error), \
                    patch('urllib.request.urlopen', return_value=response):
                if hash_error:
                    with patch('offline.vector_build.sha256_of', side_effect=hash_error):
                        with self.assertRaises(RuntimeError):
                            self.writer._download_snapshot(_Client(), self.manifest)
                else:
                    with self.assertRaises(RuntimeError):
                        self.writer._download_snapshot(_Client(), self.manifest)
            self.assertEqual(sentinel.read_bytes(), b'old artifact')
            self.assertEqual(list(self.root.iterdir()), [sentinel])

    def test_declared_snapshot_size_mismatch_rejects_truncation(self):
        client = _Client()
        client.create_snapshot = lambda **kwargs: SimpleNamespace(name=client.name, size=100)
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'truncated')):
            with self.assertRaisesRegex(RuntimeError, '大小'):
                self.writer._download_snapshot(client, self.manifest)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_database_connection_failure_cleans_only_own_directory(self):
        path = self.root / '.vector-ids.sqlite'
        path.write_bytes(b'previous database')
        with patch('offline.vector_build.sqlite3.connect', side_effect=OSError('open failed')):
            with self.assertRaises(OSError):
                _validate_reader_ids(_Reader(), self.root)
        self.assertEqual(path.read_bytes(), b'previous database')
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_snapshot_target_created_during_download_is_preserved(self):
        target = self.root / 'build.snapshot'

        def response(*args, **kwargs):
            target.write_bytes(b'concurrent publisher')
            return io.BytesIO(b'this download')

        with patch('urllib.request.urlopen', side_effect=response):
            with self.assertRaises(RuntimeError):
                self.writer._download_snapshot(_Client(), self.manifest)
        self.assertEqual(target.read_bytes(), b'concurrent publisher')
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_manifest_concurrent_publication_preserves_other_publishers_bytes(self):
        target = self.root / 'vector_manifest.json'

        class Writer:
            def replace(self, manifest, records):
                list(records)
                target.write_bytes(b'concurrent manifest')
                return manifest

        with self.assertRaises(FileExistsError):
            build_vectors(_Reader(), _Encoder(), Writer(), self.root, 'new-build', backend='memory-exact')
        self.assertEqual(target.read_bytes(), b'concurrent manifest')
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_existing_collection_is_not_deleted_and_reserved_database_survives(self):
        client = _Client()
        client.info = object()
        path = self.root / '.qdrant-doc-ids.sqlite'
        path.write_bytes(b'previous database')
        with self._sdk(client), self.assertRaises(ValueError):
            self.writer.replace(self.manifest, [])
        self.assertEqual(client.deleted, [])
        self.assertEqual(path.read_bytes(), b'previous database')
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_failed_create_never_claims_another_clients_collection(self):
        for error in (RuntimeError('another client created it first'),
                      TimeoutError('creation response was lost')):
            with self.subTest(error=error):
                client = _Client()
                protected_info = object()

                def failed_create(**kwargs):
                    # The preflight saw 404; now a collection exists, but its
                    # ownership cannot be established from this failed request.
                    client.info = protected_info
                    raise error

                client.create_collection = failed_create
                with self._sdk(client), self.assertRaises(type(error)):
                    self.writer.replace(self.manifest, [])
                self.assertIs(client.info, protected_info)
                self.assertEqual(client.deleted, [])
                self.assertEqual(list(self.root.iterdir()), [])

    def test_create_failure_without_remote_mutation_does_not_delete(self):
        client = _Client()

        def failed_create(**kwargs):
            raise OSError('request failed before reaching server')

        client.create_collection = failed_create
        with self._sdk(client), self.assertRaises(OSError):
            self.writer.replace(self.manifest, [])
        self.assertEqual(client.deleted, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_explicit_negative_create_result_does_not_claim_collection(self):
        client = _Client()
        client.create_collection = lambda **kwargs: False
        with self._sdk(client), self.assertRaises(RuntimeError):
            self.writer.replace(self.manifest, [])
        self.assertEqual(client.deleted, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_confirmed_create_is_cleaned_after_later_validation_failure(self):
        client = _Client()
        original_create = client.create_collection

        def confirmed_create(**kwargs):
            original_create(**kwargs)
            return True

        client.create_collection = confirmed_create
        with self._sdk(client), self.assertRaisesRegex(ValueError, '记录数'):
            self.writer.replace(self.manifest, [])
        self.assertEqual(client.deleted, [self.manifest.collection])
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
