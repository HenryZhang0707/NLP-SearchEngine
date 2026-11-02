"""HNSW request/response wiring checks; no optional SDK or server required."""

import copy
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from contracts.index_format import sha256_of
from contracts.reader import Document
from contracts.retrieval import EmbeddingSpec, VectorManifest
from offline.vector_build import QdrantSnapshotWriter, build_vectors


class _NotFound(Exception):
    status_code = 404


class _HnswConfigDiff(SimpleNamespace):
    pass


class _Client:
    def __init__(self, applied=None, *, missing_config=False):
        self.applied = applied or {}
        self.missing_config = missing_config
        self.info = None
        self.created = []
        self.deleted = []
        self.points = {}

    def get_collection(self, collection_name):
        if self.info is None:
            raise _NotFound(collection_name)
        return self.info

    def create_collection(self, **kwargs):
        self.created.append(kwargs)
        self.info = SimpleNamespace(config=SimpleNamespace(
            metadata=copy.deepcopy(kwargs['metadata'])))
        if 'hnsw_config' in kwargs and not self.missing_config:
            actual = dict(vars(kwargs['hnsw_config']))
            actual.update(self.applied)
            self.info.config.hnsw_config = SimpleNamespace(**actual)

    def upsert(self, collection_name, points, wait):
        assert wait is True
        self.points.update({point.id: point for point in points})

    def count(self, collection_name, exact):
        assert exact is True
        return SimpleNamespace(count=len(self.points))

    def delete_collection(self, collection_name):
        self.deleted.append(collection_name)
        self.info = None


class _Reader:
    n_docs = 2

    def doc(self, docid):
        return Document(docid, f'd{docid}', 'title', f'text {docid}')


class _Encoder:
    spec = EmbeddingSpec('fixture-model', 'fixture-v1', 2)

    def encode_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


class HnswBuildConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sequence = 0

    def _sdk(self, client, *, hnsw_model=True):
        models = SimpleNamespace(VectorParams=SimpleNamespace, PointStruct=SimpleNamespace,
                                 Distance=SimpleNamespace(COSINE='Cosine'))
        if hnsw_model:
            models.HnswConfigDiff = _HnswConfigDiff
        self.client_factory = Mock(return_value=client)
        return patch.dict(sys.modules, {'qdrant_client': SimpleNamespace(
            QdrantClient=self.client_factory, models=models)})

    def _build(self, client, params, *, hnsw_model=True):
        self.sequence += 1
        self.output = self.root / str(self.sequence)
        self.writer = QdrantSnapshotWriter('http://fixture.invalid', self.output)

        def snapshot(_client, manifest):
            path = self.output / 'test.snapshot'
            path.write_bytes(b'fixture snapshot')
            return replace(manifest, snapshot_files={path.name: sha256_of(path)})

        self.writer._download_snapshot = Mock(side_effect=snapshot)
        with self._sdk(client, hnsw_model=hnsw_model):
            return build_vectors(_Reader(), _Encoder(), self.writer, self.output,
                                 'new-build', index_params=params)

    def test_nondefault_parameters_reach_sdk_and_match_response_before_snapshot(self):
        params = {'m': 24, 'ef_construct': 160, 'full_scan_threshold': 321,
                  'max_indexing_threads': 4, 'on_disk': True}
        client = _Client()
        manifest = self._build(client, params)
        request = client.created[0]
        self.assertIsInstance(request['hnsw_config'], _HnswConfigDiff)
        self.assertEqual(vars(request['hnsw_config']), params)
        self.assertEqual(manifest.index_params, params)
        self.writer._download_snapshot.assert_called_once()
        self.assertTrue((self.output / 'vector_manifest.json').is_file())
        self.assertEqual(client.deleted, [])

    def test_empty_parameters_omit_hnsw_option_and_need_no_hnsw_sdk_model(self):
        client = _Client()
        self._build(client, {}, hnsw_model=False)
        self.assertNotIn('hnsw_config', client.created[0])
        self.writer._download_snapshot.assert_called_once()

    def test_partial_parameters_and_zero_auto_values_are_preserved(self):
        for params in ({'m': 0}, {'ef_construct': 1}, {'full_scan_threshold': 0},
                       {'max_indexing_threads': 0}, {'on_disk': False}):
            with self.subTest(params=params):
                client = _Client()
                self._build(client, params)
                self.assertEqual(vars(client.created[0]['hnsw_config']), params)

    def test_invalid_parameters_fail_before_client_or_new_collection(self):
        invalid = [{'unknown': 1}, {'payload_m': 8}, {'inline_storage': True}]
        for name in ('m', 'ef_construct', 'full_scan_threshold', 'max_indexing_threads'):
            invalid += [{name: value} for value in (True, False, -1, 1.5, '16', None)]
        invalid += [{'ef_construct': 0}]
        invalid += [{'on_disk': value} for value in (0, 1, 'true', None)]
        for params in invalid:
            with self.subTest(params=params):
                client = _Client()
                with self.assertRaises(ValueError):
                    self._build(client, params)
                self.client_factory.assert_not_called()
                self.assertEqual(client.created, [])
                self.assertEqual(client.deleted, [])
                self.writer._download_snapshot.assert_not_called()
                self.assertFalse((self.output / 'vector_manifest.json').exists())

    def test_metadata_alone_cannot_admit_ignored_hnsw_parameters(self):
        params = {'m': 24, 'ef_construct': 160, 'full_scan_threshold': 321,
                  'max_indexing_threads': 4, 'on_disk': True}
        changes = {'m': 16, 'ef_construct': 100, 'full_scan_threshold': 10000,
                   'max_indexing_threads': 0, 'on_disk': False}
        for field, value in changes.items():
            with self.subTest(field=field):
                client = _Client({field: value})
                with self.assertRaisesRegex(ValueError, field):
                    self._build(client, params)
                self.writer._download_snapshot.assert_not_called()
                self.assertEqual(client.deleted, ['new-build'])
                self.assertFalse((self.output / 'vector_manifest.json').exists())

    def test_absent_or_coerced_response_does_not_publish(self):
        for client, params in ((_Client(missing_config=True), {'m': 24}),
                               (_Client({'m': 24.0}), {'m': 24}),
                               (_Client({'on_disk': 1}), {'on_disk': True})):
            with self.subTest(params=params, applied=client.applied):
                with self.assertRaises(ValueError):
                    self._build(client, params)
                self.writer._download_snapshot.assert_not_called()
                self.assertEqual(client.deleted, ['new-build'])

    def test_existing_collection_is_never_deleted(self):
        client = _Client()
        client.info = SimpleNamespace(config=SimpleNamespace())
        with self.assertRaisesRegex(ValueError, 'collection'):
            self._build(client, {'m': 24})
        self.assertEqual(client.created, [])
        self.assertEqual(client.deleted, [])
        self.writer._download_snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
