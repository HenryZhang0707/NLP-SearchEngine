"""Exact BGE reference checks with synthetic committed cache chunks."""

import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from adapters.embedding import bge_spec
from contracts.index_format import sha256_of
from offline.corpus_documents import CorpusDocuments
from offline.vector_cache import CacheConfig, produce_chunks
from scripts.cache_vectors import bge_cache_contract

from evaluation import dense_reference
from evaluation.external_run import validate_external_run


def _vector(x, y):
    return [x, y] + [0.0] * (bge_spec().dimension - 2)


class FixtureProducer:
    spec = bge_spec()

    def encode_documents(self, texts):
        return [{"same-a": _vector(1.0, 0.0), "near": _vector(0.60003, 0.80004),
                 "same-b": _vector(1.0, 0.0), "opposite": _vector(-1.0, 0.0),
                 "orthogonal": _vector(0.0, 1.0)}[text.split("\n")[0]] for text in texts]


@unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy required for exact ranking")
class ExactDenseReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.corpus = self.root / "corpus.jsonl"
        self.queries = self.root / "queries.tsv"
        self.qrels = self.root / "qrels.tsv"
        self.cache = self.root / "cache"
        self.out = self.root / "run"
        self.exceptions = self.root / "exceptions.json"
        self.docs = [("z", "same-a"), ("c", "near"), ("a", "same-b"), ("d", "opposite"),
                     ("e", "orthogonal")]
        self.queries.write_text("q2\tquery direction\nq-hard\tx AND y\nq1\tother direction\n",
                                encoding="utf-8")
        self.qrels.write_text("q2 0 z 1\nq-hard 0 c 1\n", encoding="utf-8")
        self.prepare_cache()
        self.write_exceptions({"q-hard": {"expected_count": 0, "reason": "explicit constraint"}})
        proc_patch = patch.object(dense_reference, "_encode_faiss_queries",
                                  side_effect=dense_reference._encode_queries)
        proc_patch.start()
        self.addCleanup(proc_patch.stop)
        encoder_patch = patch.object(dense_reference, "BGEEncoder")
        self.encoder = encoder_patch.start()
        self.addCleanup(encoder_patch.stop)
        self.encoder.return_value.encode_query.side_effect = [_vector(1.0, 0.0), _vector(0.0, 1.0)]
        version_patch = patch.object(dense_reference, "version", return_value="fixture")
        version_patch.start()
        self.addCleanup(version_patch.stop)

    def prepare_cache(self, encoder_contract=None):
        self.corpus.write_text("".join(json.dumps({"doc_id": doc_id, "title": title, "text": "body"}) + "\n"
                                       for doc_id, title in self.docs), encoding="utf-8")
        self.corpus_sha = sha256_of(self.corpus)
        config = CacheConfig(self.corpus_sha, len(self.docs), bge_spec(), encoder_contract or bge_cache_contract(),
                             chunk_size=2, batch_size=1, shards=2)
        with CorpusDocuments(self.corpus, expected_sha256=self.corpus_sha,
                             expected_n_docs=len(self.docs)) as reader:
            for shard in range(2):
                produce_chunks(reader, FixtureProducer(), self.cache, config, shard=shard,
                               verified_source_sha256=self.corpus_sha)

    def write_exceptions(self, entries, depth=2):
        self.exceptions.write_text(json.dumps({
            "schema_version": 1, "depth": depth, "corpus_sha256": self.corpus_sha,
            "queries_sha256": sha256_of(self.queries), "qrels_sha256": sha256_of(self.qrels),
            "short_queries": entries,
        }), encoding="utf-8")

    def generate(self, **kwargs):
        options = dict(corpus_sha256=self.corpus_sha, n_docs=len(self.docs), cache_dir=self.cache,
                       model_path=self.root / "model", depth=2, qrels=self.qrels,
                       exceptions=self.exceptions, chunk_size=2, batch_size=1, shards=2,
                       doc_block=2, query_block=1)
        options.update(kwargs)
        return dense_reference.generate_run(self.corpus, self.queries, self.out, **options)

    def test_cosine_cross_block_ties_and_frozen_zero_depth(self):
        manifest = self.generate()
        self.encoder.return_value.encode_documents.assert_not_called()
        self.assertEqual([call.args[0] for call in self.encoder.return_value.encode_query.call_args_list],
                         ["query direction", "other direction"])
        rows = [line.split() for line in (self.out / "run.trec").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(row[0], row[2], row[3]) for row in rows],
                         [("q2", "z", "1"), ("q2", "a", "2"), ("q1", "e", "1"), ("q1", "c", "2")])
        self.assertAlmostEqual(float(rows[-1][4]), 0.8, places=6)
        self.assertEqual(manifest["observed_short_queries"], {"q-hard": 0})
        self.assertEqual(manifest["run_sha256"], sha256_of(self.out / "run.trec"))
        self.assertEqual(manifest["document_vectors_sha256"], hashlib.sha256(b"".join(
            path.read_bytes() for path in sorted(self.cache.glob("chunk-*.f32")))).hexdigest())
        validated = validate_external_run(self.corpus, self.queries, self.qrels, self.out / "run.trec",
                                          depth=2, k=2, map_k=2, exceptions=self.exceptions)
        self.assertEqual(validated["counts"]["queries"], 3)
        self.assertEqual(validated["counts"]["short_queries"], 1)

    def test_boundary_ties_choose_earliest_ordinals(self):
        import numpy as np

        scores, ids = dense_reference._topk(np, np.array([1., 1., 2., 1., -1.]),
                                           np.array([9, 1, 4, 3, 0]), 3)
        self.assertEqual(scores.tolist(), [2., 1., 1.])
        self.assertEqual(ids.tolist(), [4, 1, 3])

    def test_faiss_boundary_tie_expands_only_the_affected_query(self):
        import numpy as np

        # Return later ordinals first for tied scores so the first search omits
        # the corpus-first document entirely at the cutoff.
        search_depths = []
        indexed_vectors = []

        class FlatIPFixture:
            def __init__(self, dimension):
                self.ntotal = 0
                self.dimension = dimension

            def add(self, vectors):
                self.ntotal += len(vectors)
                indexed_vectors.append(vectors)

            def search(self, queries, depth):
                search_depths.append((len(queries), depth))
                vectors = np.concatenate(indexed_vectors)
                scores = queries @ vectors.T
                ids = np.argsort(scores, axis=1, kind="stable")[:, ::-1][:, :depth]
                return np.take_along_axis(scores, ids, axis=1), ids

        self.docs[1] = ("c", "same-b")
        self.cache = self.root / "faiss-cache"
        previous_contract = "historical-fixture-contract"
        self.prepare_cache(encoder_contract=previous_contract)
        self.write_exceptions({"q-hard": {"expected_count": 0, "reason": "explicit constraint"}}, depth=1)
        with patch.object(dense_reference, "bge_cache_contract", return_value=previous_contract):
            reference = self.generate(depth=1)
        reference_path = self.out / "manifest.json"
        reference_sha = sha256_of(reference_path)
        self.out = self.root / "faiss-run"
        self.encoder.return_value.encode_query.reset_mock(
            side_effect=True
        )
        self.encoder.return_value.encode_query.side_effect = [_vector(1.0, 0.0), _vector(0.0, 1.0)]
        fake_faiss = SimpleNamespace(IndexFlatIP=FlatIPFixture,
                                    cvar=SimpleNamespace(distance_compute_blas_threshold=20))
        with patch.object(dense_reference, "_load_faiss", return_value=(fake_faiss, "1.10.0")):
            with self.assertRaisesRegex(ValueError, "reference manifest SHA256 mismatch"):
                self.generate(backend="faiss", depth=1, reference_manifest=(reference_path, "0" * 64))
            manifest = self.generate(backend="faiss", query_block=32, depth=1,
                                     reference_manifest=(reference_path, reference_sha))
        self.assertEqual(search_depths, [(2, 2), (1, 5)])
        self.assertTrue(all(vectors.dtype == np.float32 for vectors in indexed_vectors))
        self.encoder.return_value.encode_documents.assert_not_called()
        rows = [line.split() for line in (self.out / "run.trec").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(row[0], row[2]) for row in rows],
                         [("q2", "z"), ("q1", "e")])
        self.assertEqual({row[-1] for row in rows}, {dense_reference.FAISS_RUN_TAG})
        self.assertEqual(manifest["observed_short_queries"], {"q-hard": 0})
        self.assertEqual(manifest["scoring"]["method"], "faiss-index-flat-ip-float32")
        self.assertEqual(manifest["scoring"]["query_block"], 19)
        self.assertEqual(manifest["dependencies"]["faiss-cpu"], "1.10.0")
        self.assertEqual(manifest["reference_manifest_sha256"], reference_sha)
        self.assertEqual(manifest["cache_identity"], reference["cache_identity"])
        for field in ("query_vectors_sha256", "document_vectors_sha256"):
            self.assertEqual(manifest[field], reference[field])

        self.out = self.root / "invalid-reference-run"
        reference["query_vectors_sha256"] = "0" * 64
        altered = self.root / "altered-reference.json"
        altered.write_text(json.dumps(reference), encoding="utf-8")
        self.encoder.return_value.encode_query.side_effect = [_vector(1.0, 0.0), _vector(0.0, 1.0)]
        with patch.object(dense_reference, "_load_faiss", return_value=(fake_faiss, "1.10.0")):
            with self.assertRaisesRegex(ValueError, "query vector SHA256 differs"):
                self.generate(backend="faiss", depth=1, reference_manifest=(altered, sha256_of(altered)))
        self.assertFalse(self.out.exists())

    def test_missing_or_nonzero_policy_and_foreign_exclusion_fail_before_encoding(self):
        with self.assertRaisesRegex(ValueError, "exactly the explicit-constraint"):
            self.generate(qrels=None, exceptions=None)
        for entries in (
                {"q-hard": {"expected_count": 1, "reason": "test"}},
                {"q-hard": {"expected_count": 0, "reason": "test"},
                 "q1": {"expected_count": 0, "reason": "test"}}):
            self.write_exceptions(entries)
            with self.assertRaisesRegex(ValueError, "exactly the explicit-constraint"):
                self.generate()
        self.encoder.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_invalid_query_vector_and_corrupt_cache_fail_without_publication(self):
        self.encoder.return_value.encode_query.side_effect = [_vector(0.0, 0.0)]
        with self.assertRaises(ValueError):
            self.generate()
        self.assertFalse(self.out.exists())
        self.encoder.return_value.encode_query.side_effect = [_vector(1.0, 0.0), _vector(0.0, 1.0)]
        artifact = self.cache / "chunk-00000001.f32"
        content = artifact.read_bytes()
        artifact.write_bytes(b"\xff" + content[1:])
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.generate()
        self.assertFalse(self.out.exists())

    def test_negative_scores_are_retained_and_output_is_never_reused(self):
        self.queries.write_text("q2\tquery direction\n", encoding="utf-8")
        self.generate(depth=5, qrels=None, exceptions=None)
        rows = (self.out / "run.trec").read_text(encoding="utf-8").splitlines()
        self.assertEqual(float(rows[-1].split()[4]), -1.0)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.generate(depth=5, qrels=None, exceptions=None)


if __name__ == "__main__":
    unittest.main()
