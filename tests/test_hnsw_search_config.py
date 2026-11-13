"""Search protocol wiring with SDK doubles; no database or ANN quality claim."""

from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from contracts.retrieval import (
    VECTOR_BUILD_METADATA_KEY, EmbeddingSpec, RetrievalUnavailable, VectorManifest,
)
from online import server
from online.vector_index import QdrantVectorIndex


class HNSWSearchConfigTests(unittest.TestCase):
    def setUp(self):
        self.manifest = VectorManifest(
            "a" * 64, 1, EmbeddingSpec("fixture", "v1", dimension=2), "fixture",
            snapshot_files={"fixture.snapshot": "b" * 64})

    @contextmanager
    def sdk(self):
        params = SimpleNamespace(size=2, distance="Cosine")
        config = SimpleNamespace(params=SimpleNamespace(vectors=params), metadata={
            VECTOR_BUILD_METADATA_KEY: self.manifest.build_metadata()})
        client = SimpleNamespace(
            get_collection=Mock(return_value=SimpleNamespace(config=config)),
            count=Mock(return_value=SimpleNamespace(count=1)),
            query_points=Mock(return_value=SimpleNamespace(points=[
                SimpleNamespace(id=0, score=0.5, payload={"doc_id": "d0"})])),
            search=Mock())
        search_params = Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs))
        module = SimpleNamespace(QdrantClient=Mock(return_value=client),
                                 models=SimpleNamespace(SearchParams=search_params))
        with patch.dict(sys.modules, {"qdrant_client": module}):
            yield client, module, search_params

    def test_explicit_beam_and_nonexact_search_reach_query_points(self):
        with self.sdk() as (client, _, params):
            reader = QdrantVectorIndex(self.manifest, "http://unused.invalid", hnsw_ef=256)
            hits = reader.search([1.0, 0.0], 200)
            params.assert_called_once_with(hnsw_ef=256, exact=False)
            call = client.query_points.call_args.kwargs
            self.assertEqual(call["search_params"].hnsw_ef, 256)
            self.assertIs(call["search_params"].exact, False)
            self.assertEqual((call["limit"], call["query"], call["with_payload"]),
                             (200, [1.0, 0.0], True))
            self.assertEqual(hits[0].doc_id, "d0")

    def test_default_omits_search_params_including_legacy_client(self):
        with self.sdk() as (client, _, params):
            reader = QdrantVectorIndex(self.manifest, "http://unused.invalid")
            reader.search([1.0, 0.0], 1)
            self.assertNotIn("search_params", client.query_points.call_args.kwargs)
            params.assert_not_called()
            del client.query_points
            client.search.return_value = []
            self.assertEqual(reader.search([1.0, 0.0], 1), [])
            self.assertNotIn("search_params", client.search.call_args.kwargs)

    def test_invalid_beam_rejected_before_optional_sdk_load(self):
        for value in (0, -1, True, False, 1.5, "256"):
            with self.subTest(value=value), patch.dict(sys.modules, {"qdrant_client": None}):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    QdrantVectorIndex(self.manifest, "http://unused.invalid", hnsw_ef=value)

    def test_configured_backend_failure_stays_typed_without_fallback(self):
        with self.sdk() as (client, _, _):
            reader = QdrantVectorIndex(self.manifest, "http://unused.invalid", hnsw_ef=256)
            client.query_points.side_effect = TimeoutError("offline")
            with self.assertRaises(RetrievalUnavailable):
                reader.search([1.0, 0.0], 200)
            client.search.assert_not_called()

    @contextmanager
    def cli(self):
        with tempfile.TemporaryDirectory(prefix="hnsw-cli-test-") as directory, ExitStack() as stack:
            path = Path(directory) / "vector_manifest.json"
            path.write_text(json.dumps(self.manifest.to_dict()), encoding="utf-8")
            mocks = {name: stack.enter_context(patch.object(server, name)) for name in (
                "DiskIndex", "SearchEngine", "QdrantVectorIndex", "ThreadingHTTPServer",
                "SentenceTransformerEncoder")}
            mocks["ThreadingHTTPServer"].return_value.server_address = ("127.0.0.1", 8000)
            mocks["SearchEngine"].return_value.info.return_value = {
                "index_identity": None, "vector_identity": None,
                "search_config": None, "policy": None}
            stack.enter_context(redirect_stdout(io.StringIO()))
            yield ["--index", "unused", "--vector-url", "http://unused.invalid",
                   "--vector-manifest", str(path)], mocks

    def test_cli_wires_explicit_beam_and_preserves_default(self):
        for options, kwargs in ((["--hnsw-ef", "256"], {"hnsw_ef": 256}), ([], {})):
            with self.subTest(options=options), self.cli() as (args, mocks):
                server.main([*args, *options])
                mocks["QdrantVectorIndex"].assert_called_once_with(
                    self.manifest, "http://unused.invalid", **kwargs)
                config = mocks["SearchEngine"].call_args.args[3]
                self.assertEqual(config.candidate_k, 200)

    def test_cli_rejects_inapplicable_or_invalid_beam_before_loading(self):
        for options in (["--hnsw-ef", "256"],
                        ["--vector-url", "http://unused.invalid", "--hnsw-ef", "256"],
                        *(["--vector-url", "http://unused.invalid", "--vector-manifest", "unused",
                           "--hnsw-ef", value] for value in ("0", "-1", "True", "1.5"))):
            with self.subTest(options=options), patch.object(server, "DiskIndex") as reader, \
                    patch.object(server, "QdrantVectorIndex") as vectors, \
                    patch.object(server, "SentenceTransformerEncoder") as encoder, \
                    redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    server.main(["--index", "unused", *options])
                self.assertEqual(error.exception.code, 2)
                reader.assert_not_called()
                vectors.assert_not_called()
                encoder.assert_not_called()

    def test_cli_reports_typed_backend_failure(self):
        with self.cli() as (args, mocks), redirect_stderr(io.StringIO()) as stderr:
            mocks["QdrantVectorIndex"].side_effect = RetrievalUnavailable("offline")
            with self.assertRaises(SystemExit) as error:
                server.main([*args, "--hnsw-ef", "256"])
            self.assertEqual(error.exception.code, 2)
            self.assertIn("offline", stderr.getvalue())
            mocks["DiskIndex"].assert_not_called()
            mocks["ThreadingHTTPServer"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
