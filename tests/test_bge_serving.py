"""Dependency-free BGE wiring checks; stubs do not establish model equivalence."""

import contextlib
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from adapters.embedding import BGEEncoder, bge_spec
from adapters import model_files
from contracts.retrieval import RetrievalUnavailable, VectorManifest
from online import server
from online.embeddings import SentenceTransformerEncoder


class SentenceTransformerServingTests(unittest.TestCase):
    def test_default_and_explicit_batch_sizes_reach_encoder(self):
        for kwargs, batch_size in (({}, 32), ({"batch_size": 3}, 3)):
            model = Mock()
            model.encode.side_effect = lambda texts, **options: [[1.0, 0.0] for _ in texts]
            factory = Mock(return_value=model)
            with self.subTest(batch_size=batch_size), patch.dict(
                sys.modules, {"sentence_transformers": SimpleNamespace(SentenceTransformer=factory)}
            ):
                encoder = SentenceTransformerEncoder(
                    "fixture", "rev-1", 2, query_prefix="q:", document_prefix="d:", **kwargs)
                self.assertEqual(encoder.encode_documents(["one", "two"]), [[1.0, 0.0]] * 2)
                self.assertEqual(encoder.encode_query("one"), [1.0, 0.0])
                self.assertEqual([call.args[0] for call in model.encode.call_args_list],
                                 [["d:one", "d:two"], ["q:one"]])
                self.assertEqual([call.kwargs["batch_size"] for call in model.encode.call_args_list],
                                 [batch_size, batch_size])

    def test_invalid_batch_sizes_are_rejected_before_loading_the_model(self):
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            for value in (False, 0, -1, 1.5, "8"):
                with self.subTest(batch_size=value), self.assertRaisesRegex(ValueError, "batch_size"):
                    SentenceTransformerEncoder("fixture", "rev-1", 2, batch_size=value)


class BGEServingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = VectorManifest("a" * 64, 1, bge_spec(), "test")
        self.path = self.root / "vector_manifest.json"
        self.path.write_text(json.dumps(self.manifest.to_dict()), encoding="utf-8")
        self.vector_args = ["--vector-url", "http://unused.invalid:6333",
                            "--vector-manifest", str(self.path)]

    def invoke_cli(self, args):
        # No server sockets, Qdrant connections, reader loads or model loads.
        with contextlib.ExitStack() as stack:
            mocks = {name: stack.enter_context(patch.object(server, name)) for name in
                     ("DiskIndex", "SearchEngine", "QdrantVectorIndex", "ThreadingHTTPServer",
                      "SentenceTransformerEncoder")}
            mocks["ThreadingHTTPServer"].return_value.server_address = ("127.0.0.1", 8000)
            mocks["SearchEngine"].return_value.info.return_value = {
                "index_identity": None, "vector_identity": None,
                "search_config": None, "policy": None}
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            server.main(["--index", "unused-index", *args])
            return mocks

    def test_cli_bge_passes_complete_spec_local_path_and_device(self):
        spec = replace(bge_spec(), max_tokens=123, query_prefix="query:", document_prefix="doc:")
        self.path.write_text(json.dumps(replace(self.manifest, embedding=spec).to_dict()), encoding="utf-8")
        for device_args, expected in (([], "cpu"), (["--encoder-device", "cuda:1"], "cuda:1")):
            with self.subTest(device=expected), patch("adapters.embedding.BGEEncoder") as bge:
                mocks = self.invoke_cli([*self.vector_args, "--bge-model-path", "verified-local", *device_args])
                bge.assert_called_once_with(spec=spec, model_path="verified-local", device=expected)
                mocks["SentenceTransformerEncoder"].assert_not_called()
                self.assertIs(mocks["SearchEngine"].call_args.args[1], bge.return_value)

    def test_cli_generic_default_remains_compatible(self):
        mocks = self.invoke_cli(self.vector_args)
        spec = self.manifest.embedding
        mocks["SentenceTransformerEncoder"].assert_called_once_with(
            spec.model_id, spec.revision, spec.dimension, spec.max_tokens,
            spec.query_prefix, spec.document_prefix)
        lexical = self.invoke_cli([])
        lexical["SentenceTransformerEncoder"].assert_not_called()
        lexical["QdrantVectorIndex"].assert_not_called()

    def test_cli_rejects_inapplicable_arguments_before_any_loading(self):
        invalid = [
            ["--bge-model-path", "local"],
            ["--encoder-device", "cpu"],
            [*self.vector_args, "--encoder-device", "cpu"],
            ["--vector-url", "http://unused.invalid", "--bge-model-path", "local"],
            [*self.vector_args, "--bge-model-path", ""],
            [*self.vector_args, "--bge-model-path", "local", "--encoder-device", ""],
        ]
        for args in invalid:
            with self.subTest(args=args), patch.object(server, "DiskIndex") as reader, \
                    patch("adapters.embedding.BGEEncoder") as bge, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    server.main(["--index", "unused", *args])
                self.assertEqual(error.exception.code, 2)
                reader.assert_not_called()
                bge.assert_not_called()

    def test_non_pinned_bge_rejected_without_importing_dependencies(self):
        for spec in (replace(bge_spec(), model_id="other/model"),
                     replace(bge_spec(), revision="other-revision"),
                     replace(bge_spec(), dimension=256),
                     replace(bge_spec(), max_tokens=513)):
            with self.subTest(spec=spec), patch.dict(sys.modules, {"torch": None, "transformers": None}):
                with self.assertRaises(ValueError):
                    BGEEncoder(spec=spec, model_path=self.root)
        bad = replace(self.manifest, embedding=replace(bge_spec(), model_id="other/model"))
        self.path.write_text(json.dumps(bad.to_dict()), encoding="utf-8")
        self.assert_cli_initialization_error("pinned")

    def assert_cli_initialization_error(self, message):
        stderr = io.StringIO()
        with patch.object(server, "DiskIndex") as reader, \
                patch.object(server, "QdrantVectorIndex") as vectors, \
                patch.dict(sys.modules, {"torch": None, "transformers": None}), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                server.main(["--index", "unused", *self.vector_args,
                             "--bge-model-path", str(self.root)])
            self.assertEqual(error.exception.code, 2)
            self.assertIn(message, stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            reader.assert_not_called()
            vectors.assert_not_called()

    def test_bad_inventory_fails_before_dependencies_and_cli_reports_error(self):
        inventories = [None, "{", "[]", json.dumps({"model_id": bge_spec().model_id,
                       "revision": bge_spec().revision, "files": []})]
        path = self.root / "model_inventory.json"
        for content in inventories:
            with self.subTest(content=content), patch.dict(sys.modules, {"torch": None, "transformers": None}):
                if content is not None:
                    path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(RetrievalUnavailable, "inventory verification"):
                    BGEEncoder(model_path=self.root)
                self.assert_cli_initialization_error("inventory verification")

    def test_missing_dependencies_are_typed_and_cli_reports_error(self):
        with patch.object(model_files, "verify_model") as verify, \
                patch.dict(sys.modules, {"torch": None, "transformers": None}):
            with self.assertRaisesRegex(RetrievalUnavailable, "requires torch and transformers"):
                BGEEncoder(model_path=self.root)
            verify.assert_called_once_with(self.root, bge_spec())
            self.assert_cli_initialization_error("requires torch and transformers")

    def test_verified_local_initialization_is_fp32_right_truncation_no_download(self):
        tokenizer = SimpleNamespace(truncation_side="left")
        model = Mock()
        model.float.return_value = model
        model.to.return_value = model
        model.eval.return_value = model
        auto_tokenizer = SimpleNamespace(from_pretrained=Mock(return_value=tokenizer))
        auto_model = SimpleNamespace(from_pretrained=Mock(return_value=model))
        modules = {"torch": SimpleNamespace(), "transformers": SimpleNamespace(
            AutoModel=auto_model, AutoTokenizer=auto_tokenizer)}
        with patch.object(model_files, "verify_model") as verify, patch.dict(sys.modules, modules):
            encoder = BGEEncoder(model_path=self.root, device="cpu")
        verify.assert_called_once_with(self.root, bge_spec())
        auto_tokenizer.from_pretrained.assert_called_once_with(self.root, local_files_only=True)
        auto_model.from_pretrained.assert_called_once_with(self.root, use_safetensors=True, local_files_only=True)
        self.assertEqual(encoder.tokenizer.truncation_side, "right")
        model.float.assert_called_once_with()
        model.to.assert_called_once_with("cpu")
        model.eval.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
