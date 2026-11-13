"""Synthetic offline cache checks: no model, network, processes or GPU required."""

import contextlib
import hashlib
import io
import json
import math
import os
import struct
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from adapters.embedding import bge_spec
from contracts.reader import Document
from contracts.retrieval import EmbeddingSpec, RetrievalUnavailable, corpus_fingerprint
from offline.corpus_documents import CorpusDocuments
from offline import vector_cache as cache_module
from offline.vector_build import build_vectors
from offline.vector_cache import CACHE_MANIFEST, CacheConfig, CachedEncoder, produce_chunks
from scripts import cache_vectors as cli


SPEC = EmbeddingSpec("fixture-only", "fixed-fp32-v1", dimension=8,
                     document_prefix="文档：", query_prefix="问题：")


class FakeEncoder:
    def __init__(self, spec=SPEC):
        self.spec = spec
        self.calls = []

    def encode_documents(self, texts):
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            # Prefix is the encoder's job. Hash exactly the complete public
            # document input, including Unicode/newlines; no manual truncation.
            digest = hashlib.sha256((self.spec.document_prefix + text).encode()).digest()
            vector = [0.0] * self.spec.dimension
            vector[digest[0] % self.spec.dimension] = -1.0 if digest[1] % 2 else 1.0
            vectors.append(vector)
        return vectors


class RecordingWriter:
    def replace(self, manifest, records):
        self.records = list(records)
        return manifest


class ChangedReader:
    def __init__(self, reader, *, changed_id=None, changed_text=None):
        self.reader, self.n_docs = reader, reader.n_docs
        self.changed_id, self.changed_text = changed_id, changed_text

    def doc(self, ordinal):
        doc = self.reader.doc(ordinal)
        if ordinal == 0:
            if self.changed_id is not None:
                return replace(doc, doc_id=self.changed_id)
            if self.changed_text is not None:
                return replace(doc, text=self.changed_text)
        return doc


class VectorCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.path = self.root / "source.jsonl"
        self.rows = [{"doc_id": f"external-{i}", "title": "标题\n" + str(i),
                      "text": "完整正文\n" + "x" * (1000 + i)} for i in range(11)]
        self.path.write_bytes(b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode()
                                      for row in self.rows))
        self.raw_sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.reader = CorpusDocuments(self.path, expected_sha256=self.raw_sha,
                                      expected_n_docs=len(self.rows))
        self.addCleanup(self.reader.close)
        self.config = CacheConfig(self.raw_sha, len(self.rows), SPEC, "fake-fp32-v1",
                                  chunk_size=4, batch_size=2, shards=2)
        self.texts = [row["title"] + "\n" + row["text"] for row in self.rows]

    def produce(self, shard=0, *, encoder=None, config=None, reader=None, cache=None):
        return produce_chunks(reader or self.reader, encoder or FakeEncoder(),
                              cache or self.cache, config or self.config, shard=shard,
                              verified_source_sha256=self.raw_sha)

    def complete(self):
        self.produce(1)
        self.produce(0)

    def consumer(self, **kwargs):
        return CachedEncoder(self.cache, self.reader, self.config, **kwargs)

    def test_interleaved_producers_preserve_batches_ids_inputs_and_exact_float32(self):
        odd, even = FakeEncoder(), FakeEncoder()
        self.assertEqual(self.produce(1, encoder=odd), {"completed": [1], "reused": []})
        self.assertEqual(self.produce(0, encoder=even), {"completed": [0, 2], "reused": []})
        self.assertEqual(odd.calls, [self.texts[4:6], self.texts[6:8]])
        self.assertEqual(even.calls, [self.texts[0:2], self.texts[2:4],
                                     self.texts[8:10], self.texts[10:11]])
        expected = FakeEncoder().encode_documents(self.texts)
        with self.consumer() as consumer:
            # Consumer batch boundaries may cross producer chunk boundaries.
            actual = consumer.encode_documents(self.texts[:3])
            actual += consumer.encode_documents(self.texts[3:9])
            actual += consumer.encode_documents(self.texts[9:])
            self.assertEqual(actual, expected)
            with self.assertRaises(ValueError):
                consumer.encode_documents(["extra"])
            with self.assertRaises(RetrievalUnavailable):
                consumer.encode_query("query")
        for chunk_id in range(self.config.n_chunks):
            prefix = f"chunk-{chunk_id:08d}"
            start, stop = self.config.bounds(chunk_id)
            rows = [json.loads(line) for line in (self.cache / (prefix + ".ids.jsonl")).read_bytes().splitlines()]
            self.assertEqual(rows, [[i, self.rows[i]["doc_id"],
                                     hashlib.sha256(self.texts[i].encode()).hexdigest()]
                                    for i in range(start, stop)])
            raw = (self.cache / (prefix + ".f32")).read_bytes()
            self.assertEqual(raw, b"".join(struct.pack("<8f", *v) for v in expected[start:stop]))
        self.assertFalse(list(self.cache.glob("*.claim")))

    def test_real_public_build_matches_direct_fake_records_and_semantic_fingerprint(self):
        self.complete()
        cached_writer, direct_writer = RecordingWriter(), RecordingWriter()
        with self.consumer() as encoder:
            cached = build_vectors(self.reader, encoder, cached_writer,
                                   self.root / "cached-public", "fixture", batch_size=3,
                                   backend="memory-exact")
        direct = build_vectors(self.reader, FakeEncoder(), direct_writer,
                               self.root / "direct-public", "fixture", batch_size=2,
                               backend="memory-exact")
        self.assertEqual(cached_writer.records, direct_writer.records)
        self.assertEqual(cached, direct)
        self.assertEqual(cached.corpus_sha256, corpus_fingerprint(self.reader))
        self.assertNotEqual(cached.corpus_sha256, self.config.raw_source_sha256)

    def test_verified_chunks_reuse_without_any_encoder_call(self):
        self.complete()
        encoder = FakeEncoder()
        encoder.encode_documents = lambda _: self.fail("inference on a completed valid chunk")
        before = {p.name: p.read_bytes() for p in self.cache.iterdir()}
        self.assertEqual(self.produce(0, encoder=encoder), {"completed": [], "reused": [0, 2]})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.cache.iterdir()})

    def test_missing_or_partial_chunk_never_counts_as_complete(self):
        self.produce(1)
        claim = self.cache / "chunk-00000000.claim"
        claim.mkdir()
        (claim / "vectors.part").write_bytes(b"incomplete")
        with self.consumer() as consumer, self.assertRaises(TimeoutError):
            consumer.encode_documents(self.texts[:1])
        encoder = FakeEncoder()
        with self.assertRaises(FileExistsError):
            self.produce(0, encoder=encoder)
        self.assertEqual(encoder.calls, [])
        self.assertEqual((claim / "vectors.part").read_bytes(), b"incomplete")

    def test_missing_wait_is_bounded_and_committed_later_chunk_is_consumed(self):
        self.produce(1)
        with self.consumer(wait_timeout=0.025, poll_interval=0.005) as consumer:
            before = time.monotonic()
            with self.assertRaises(TimeoutError):
                consumer.encode_documents(self.texts[:1])
            self.assertLess(time.monotonic() - before, 1.0)
        outcome = []

        def consume():
            with self.consumer(wait_timeout=2, poll_interval=0.005) as consumer:
                outcome.extend(consumer.encode_documents(self.texts))

        thread = threading.Thread(target=consume)
        thread.start()
        self.produce(0)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, FakeEncoder().encode_documents(self.texts))

    def test_config_source_spec_prefix_batch_and_assignment_mismatch_fail_closed(self):
        self.complete()
        for config in (replace(self.config, raw_source_sha256="0" * 64),
                       replace(self.config, embedding=replace(SPEC, document_prefix="different")),
                       replace(self.config, embedding=replace(SPEC, revision="different")),
                       replace(self.config, encoder_contract="different-fp32-v1"),
                       replace(self.config, batch_size=1),
                       replace(self.config, chunk_size=8),
                       replace(self.config, shards=3),
                       replace(self.config, source_kind="index-documents-jsonl")):
            with self.subTest(config=config), self.assertRaises(ValueError):
                CachedEncoder(self.cache, self.reader, config)
        encoder = FakeEncoder()
        with self.assertRaises(ValueError):
            self.produce(config=replace(self.config, encoder_contract="other"), encoder=encoder)
        self.assertEqual(encoder.calls, [])

    def test_wrong_same_ordinal_text_external_id_or_call_order_is_rejected(self):
        self.complete()
        for reader in (ChangedReader(self.reader, changed_id="wrong-external-id"),
                       ChangedReader(self.reader, changed_text="wrong-text")):
            with self.subTest(reader=reader), \
                    CachedEncoder(self.cache, reader, self.config) as consumer, \
                    self.assertRaisesRegex(ValueError, "doc_id/input"):
                consumer.encode_documents([reader.doc(0).title + "\n" + reader.doc(0).text])
        for texts in ([self.texts[1]], [self.texts[0] + "changed"]):
            with self.consumer() as consumer, self.assertRaisesRegex(ValueError, "input/doc_id/order"):
                consumer.encode_documents(texts)
        encoder = FakeEncoder()
        with self.assertRaises(ValueError):
            self.produce(0, reader=ChangedReader(self.reader, changed_id="wrong"), encoder=encoder)
        self.assertEqual(encoder.calls, [])

    def test_corrupted_hash_truncated_mapping_and_changed_open_chunk_are_rejected(self):
        self.complete()
        vector_path = self.cache / "chunk-00000000.f32"
        original = vector_path.read_bytes()
        vector_path.write_bytes(original[:-1])
        with self.consumer() as consumer, self.assertRaisesRegex(ValueError, "SHA256/size"):
            consumer.encode_documents(self.texts[:1])
        vector_path.write_bytes(original)
        with self.consumer() as consumer:
            consumer.encode_documents(self.texts[:1])
            with vector_path.open("ab") as handle:
                handle.write(b"x")
            with self.assertRaisesRegex(ValueError, "changed"):
                consumer.encode_documents(self.texts[1:2])

    def _update_entry_hash(self, chunk, key, path):
        marker = self.cache / f"chunk-{chunk:08d}.json"
        data = json.loads(marker.read_bytes())
        raw = path.read_bytes()
        data[key] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        marker.write_text(json.dumps(data), encoding="utf-8")

    def test_full_chunk_norm_finite_and_mapping_gate_runs_before_first_vector(self):
        self.complete()
        path = self.cache / "chunk-00000000.f32"
        original = path.read_bytes()
        for component in (0.0, math.nan, math.inf, 2.0):
            with self.subTest(component=component):
                path.write_bytes(original[:-32] + struct.pack("<8f", component, *([0.0] * 7)))
                self._update_entry_hash(0, "vectors", path)
                with self.consumer() as consumer, self.assertRaises(ValueError):
                    consumer.encode_documents(self.texts[:1])
                self.assertEqual(consumer.position, 0)
        path.write_bytes(original)
        self._update_entry_hash(0, "vectors", path)
        mapping = self.cache / "chunk-00000000.ids.jsonl"
        lines = mapping.read_bytes().splitlines(keepends=True)
        mapping.write_bytes(b"".join(lines[:-1]))
        self._update_entry_hash(0, "mapping", mapping)
        with self.consumer() as consumer, self.assertRaises(ValueError):
            consumer.encode_documents(self.texts[:1])

    def test_competing_producer_cannot_steal_claim_or_overwrite(self):
        entered, release = threading.Event(), threading.Event()
        encoder, result, failures = FakeEncoder(), [], []
        original_encode = encoder.encode_documents

        def block(texts):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test deadline")
            return original_encode(texts)

        encoder.encode_documents = block

        def produce():
            try:
                result.append(self.produce(0, encoder=encoder))
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=produce)
        thread.start()
        self.assertTrue(entered.wait(2))
        competitor = FakeEncoder()
        try:
            with self.assertRaises(FileExistsError):
                self.produce(0, encoder=competitor)
            self.assertEqual(competitor.calls, [])
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(result, [{"completed": [0, 2], "reused": []}])
        self.produce(1)
        with self.consumer() as consumer:
            self.assertEqual(consumer.encode_documents(self.texts), FakeEncoder().encode_documents(self.texts))

    def test_late_publication_failure_preserves_partials_and_never_overwrites(self):
        real_link = os.link

        def fail_marker(source, target):
            if Path(target).name == "chunk-00000000.json":
                raise OSError("synthetic late publication failure")
            return real_link(source, target)

        with patch("offline.vector_cache.os.link", side_effect=fail_marker), self.assertRaises(OSError):
            self.produce(0)
        self.assertFalse((self.cache / "chunk-00000000.json").exists())
        self.assertTrue((self.cache / "chunk-00000000.claim").exists())
        self.assertTrue((self.cache / "chunk-00000000.seal").exists())
        before = (self.cache / "chunk-00000000.f32").read_bytes()
        with self.consumer() as consumer, self.assertRaises(TimeoutError):
            consumer.encode_documents(self.texts[:1])
        with self.assertRaises(FileExistsError):
            self.produce(0)
        self.assertEqual((self.cache / "chunk-00000000.f32").read_bytes(), before)

    def _publication_visibility_race(self, *, simulated_ctime):
        entered, release = threading.Event(), threading.Event()
        failures = []
        real_link, real_stat = os.link, cache_module._stat

        def pause_after_visibility(source, target):
            result = real_link(source, target)
            if Path(target).name == "chunk-00000000.json":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test publication deadline")
            return result

        def linked_ctime(value):
            # Windows may not expose hard-link ctime changes. A link-count
            # surrogate deterministically models that POSIX ctime transition;
            # production still checks all five genuine identity fields.
            return real_stat(value)[:4] + (value.st_nlink,)

        def produce():
            try:
                self.produce(0)
            except Exception as error:
                failures.append(error)

        with patch("offline.vector_cache.os.link", side_effect=pause_after_visibility), \
                patch("offline.vector_cache._stat", side_effect=linked_ctime if simulated_ctime else real_stat):
            thread = threading.Thread(target=produce)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                visible = [self.cache / name for name in (
                    CACHE_MANIFEST, "chunk-00000000.f32", "chunk-00000000.ids.jsonl",
                    "chunk-00000000.json")]
                identities = [cache_module._stat(path.stat()) for path in visible]
                # Capture/validate backing inode identities while the producer
                # is paused immediately after making completion visible.
                with self.consumer() as consumer:
                    actual = consumer.encode_documents(self.texts[:1])
                    release.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(failures, [])
                    self.assertEqual(identities, [cache_module._stat(path.stat()) for path in visible])
                    self.produce(1)
                    actual += consumer.encode_documents(self.texts[1:])
                    self.assertEqual(actual, FakeEncoder().encode_documents(self.texts))
                encoder = FakeEncoder()
                encoder.encode_documents = lambda _: self.fail("reuse performed inference")
                self.assertEqual(self.produce(0, encoder=encoder), {"completed": [], "reused": [0, 2]})
            finally:
                release.set()
                thread.join(5)

    @unittest.skipUnless(os.name == "posix", "native POSIX ctime publication gate")
    def test_posix_marker_visible_consume_then_producer_cleanup_keeps_inode_identity(self):
        self._publication_visibility_race(simulated_ctime=False)

    def test_marker_visible_consume_then_cleanup_with_controlled_ctime(self):
        self._publication_visibility_race(simulated_ctime=True)

    def _config_visibility_race(self, *, simulated_ctime):
        entered, release = threading.Event(), threading.Event()
        failures = []
        real_link, real_stat = os.link, cache_module._stat

        def pause_after_visibility(source, target):
            result = real_link(source, target)
            if Path(target).name == CACHE_MANIFEST:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test config publication deadline")
            return result

        def identity(value):
            result = real_stat(value)
            return result[:4] + (value.st_nlink,) if simulated_ctime else result

        def publish():
            try:
                cache_module._publish_config(self.cache, self.config)
            except Exception as error:
                failures.append(error)

        with patch("offline.vector_cache.os.link", side_effect=pause_after_visibility):
            thread = threading.Thread(target=publish)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                path = self.cache / CACHE_MANIFEST
                with path.open("rb") as handle:
                    visible_identity = identity(os.fstat(handle.fileno()))
                    release.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(failures, [])
                    self.assertEqual(visible_identity, identity(os.fstat(handle.fileno())))
                    self.assertEqual(visible_identity[:4], identity(path.stat())[:4])
                # A losing config publisher removes only its own staging inode.
                cache_module._publish_config(self.cache, self.config)
                self.assertEqual(visible_identity, identity(path.stat()))
                self.assertEqual(len(list(self.cache.glob(".cache-config-*.seal"))), 1)
            finally:
                release.set()
                thread.join(5)

    @unittest.skipUnless(os.name == "posix", "native POSIX config ctime publication gate")
    def test_posix_shared_config_visible_inode_is_stable_after_publisher_returns(self):
        self._config_visibility_race(simulated_ctime=False)

    def test_shared_config_visible_inode_stability_with_controlled_ctime(self):
        self._config_visibility_race(simulated_ctime=True)

    def test_orphaned_published_files_refused_before_inference(self):
        self.cache.mkdir()
        target = self.cache / "chunk-00000000.f32"
        target.write_bytes(b"owned-by-an-earlier-attempt")
        encoder = FakeEncoder()
        with self.assertRaises(FileExistsError):
            self.produce(0, encoder=encoder)
        self.assertEqual(encoder.calls, [])
        self.assertEqual(target.read_bytes(), b"owned-by-an-earlier-attempt")
        self.assertFalse((self.cache / "chunk-00000000.json").exists())

    def test_raw_source_proof_and_encoder_spec_required_before_creating_cache(self):
        encoder = FakeEncoder()
        with self.assertRaisesRegex(ValueError, "verified raw source"):
            produce_chunks(self.reader, encoder, self.cache, self.config,
                           verified_source_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "encoder spec"):
            self.produce(encoder=FakeEncoder(replace(SPEC, query_prefix="changed")))
        self.assertEqual(encoder.calls, [])
        self.assertFalse(self.cache.exists())

    def test_committed_metadata_range_count_dimension_and_identity_are_bound(self):
        self.complete()
        marker = self.cache / "chunk-00000000.json"
        original = marker.read_bytes()
        for key, value in (("start", 1), ("stop", 5), ("count", 3),
                           ("dimension", 9), ("chunk_id", True),
                           ("cache_identity", "0" * 64), ("schema_version", 2)):
            metadata = json.loads(original)
            metadata[key] = value
            marker.write_text(json.dumps(metadata), encoding="utf-8")
            with self.subTest(key=key), self.consumer() as consumer, self.assertRaises(ValueError):
                consumer.encode_documents(self.texts[:1])
        marker.write_bytes(original)

    def test_bad_encoder_outputs_or_source_change_never_publish_completion(self):
        for kind in ("count", "dimension", "nan", "norm", "mutate", "source"):
            encoder = FakeEncoder()

            def bad(texts):
                values = FakeEncoder().encode_documents(texts)
                if kind == "count":
                    return values[:-1]
                if kind == "dimension":
                    return [[1.0]] * len(texts)
                if kind == "nan":
                    values[-1][0] = math.nan
                if kind == "norm":
                    values[-1] = [0.0] * SPEC.dimension
                if kind == "mutate":
                    texts[0] = "changed"
                if kind == "source":
                    with self.path.open("ab") as handle:
                        handle.write(b"\n")
                return values

            encoder.encode_documents = bad
            cache = self.root / kind
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.produce(0, encoder=encoder, cache=cache)
            self.assertFalse((cache / "chunk-00000000.json").exists())
            self.assertTrue((cache / "chunk-00000000.claim").exists())

    def test_manifest_contains_no_runtime_paths_and_strict_configuration(self):
        self.complete()
        contents = (self.cache / CACHE_MANIFEST).read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), contents)
        self.assertNotIn("model-path", contents)
        for kwargs in ({"chunk_size": 3}, {"batch_size": True}, {"shards": 0},
                       {"raw_source_sha256": "x" * 64},
                       {"encoder_contract": "C:/private/account/model"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(self.config, **kwargs)
        for kwargs in ({"wait_timeout": math.inf}, {"wait_timeout": -1},
                       {"poll_interval": 0}, {"poll_interval": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.consumer(**kwargs)


class CacheCliTests(unittest.TestCase):
    def test_real_invalid_source_is_rejected_before_model_and_error_output_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private-account-source.jsonl"
            source.write_text('{"doc_id":"a","title":"title","text":"text"}\n', encoding="utf-8")
            output = io.StringIO()
            with patch.object(cli, "BGEEncoder") as encoder, contextlib.redirect_stdout(output):
                status = cli.main(["produce", "--corpus", str(source),
                                   "--corpus-sha256", "0" * 64, "--corpus-n-docs", "1",
                                   "--cache-dir", str(root / "cache"), "--model-path", str(root / "private-model")])
            self.assertEqual(status, 1)
            encoder.assert_not_called()
            self.assertEqual(json.loads(output.getvalue()), {"status": "error", "code": "cache_operation_failed"})
            self.assertNotIn(directory, output.getvalue())

    def test_safe_argument_errors_do_not_echo_supplied_values(self):
        for argv in (["private-account-mode"], ["produce", "--batch-size", "private-account-value"]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = cli.main(argv)
            self.assertEqual(status, 2)
            self.assertEqual(json.loads(output.getvalue()), {"status": "error", "code": "invalid_arguments"})

    def test_actual_producer_and_public_consumer_cli_keep_original_writer_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "corpus.jsonl"
            rows = [{"doc_id": str(i), "title": "题" + str(i), "text": "正文"} for i in range(5)]
            source.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in rows))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            args = ["--corpus", str(source), "--corpus-sha256", digest,
                    "--corpus-n-docs", "5", "--cache-dir", str(root / "cache"),
                    "--batch-size", "2", "--chunk-size", "4", "--shards", "2"]
            output, fake = io.StringIO(), FakeEncoder(bge_spec())
            with patch.object(cli, "BGEEncoder", return_value=fake) as constructor, \
                    contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["produce", *args, "--model-path", "private-local-model", "--shard", "1"]), 0)
                self.assertEqual(cli.main(["produce", *args, "--model-path", "private-local-model", "--shard", "0"]), 0)
            self.assertEqual(constructor.call_args.kwargs, {"spec": bge_spec(), "model_path": "private-local-model",
                                                          "device": "cpu", "batch_size": 2})
            writer = RecordingWriter()
            original_build = cli.build_vectors

            def public_build(*args, **kwargs):
                self.assertEqual(kwargs["batch_size"], 2)
                self.assertEqual(kwargs["index_params"], {"m": 16, "ef_construct": 100})
                # Use the actual public implementation with its stdlib backend;
                # only the external database transport is replaced here.
                return original_build(*args, **kwargs, backend="memory-exact")

            with patch.object(cli, "BGEEncoder") as constructor, \
                    patch.object(cli, "QdrantSnapshotWriter", return_value=writer) as writer_factory, \
                    patch.object(cli, "build_vectors", side_effect=public_build), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(cli.main(["consume", *args, "--out", str(root / "vectors"),
                                           "--collection", "private-collection", "--url", "private-endpoint"]), 0)
            constructor.assert_not_called()
            writer_factory.assert_called_once_with("private-endpoint", root / "vectors", timeout=180)
            self.assertEqual([(r.docid, r.doc_id) for r in writer.records], [(i, str(i)) for i in range(5)])
            self.assertEqual([list(r.vector) for r in writer.records],
                             fake.encode_documents([r["title"] + "\n" + r["text"] for r in rows]))
            self.assertNotIn(directory, output.getvalue())
            self.assertNotIn("private", output.getvalue())


if __name__ == "__main__":
    unittest.main()
