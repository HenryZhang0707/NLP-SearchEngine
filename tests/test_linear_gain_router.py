"""Fixtures for the optional BM25-first model and its trusted loader."""

import hashlib
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from contracts.api import SearchRequest
from contracts.retrieval import RetrievalConfig, corpus_fingerprint
from online.engine import SearchEngine
from online.linear_gain_router import FEATURE_VERSION, LinearGainRouter, feature_vector
from online.memory_index import MemoryIndex
from online.router import QueryFeatures, RouteDecision, load_router, router_identity


SELECTOR = "online.linear_gain_router:load_gain_router"
DIGEST = "ab" * 32
CORPUS = [{"doc_id": "a", "title": "fixture", "text": "phone water"}]


def _binding():
    return {"corpus_sha256": corpus_fingerprint(MemoryIndex.from_corpus(CORPUS)),
            "candidate_k": 200, "rrf_c": 60.0, "feature_version": FEATURE_VERSION}


def _model(**changes):
    return {
        "schema_version": 1,
        "feature_version": FEATURE_VERSION,
        "mean": [0, 0.25, 0, 0, 0],
        "scale": [1, 0.25, 1, 1, 1],
        "weights": [0, 2, 0, 0, -0.5],
        "bias": 0.25,
        "threshold": 1.75,
        "provenance": {"split": "fixture-dev", "binding": _binding()},
        **changes,
    }


class LinearGainRouterTests(unittest.TestCase):
    def test_engine_accepts_bound_model_and_rejects_wrong_corpus_or_config(self):
        reader = MemoryIndex.from_corpus(CORPUS)
        router = LinearGainRouter.from_dict(_model(), DIGEST)
        engine = SearchEngine(reader, router=router)
        self.assertIs(engine.router, router)
        changed = MemoryIndex.from_corpus([
            {**CORPUS[0], "text": "phone water changed"}])
        for corpus, config, message in (
            (changed, RetrievalConfig(), "corpus_sha256"),
            (reader, RetrievalConfig(candidate_k=100), "candidate_k"),
            (reader, RetrievalConfig(rrf_c=61.0), "rrf_c"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                SearchEngine(corpus, config=config, router=router)

    def test_binding_is_required_and_feature_version_is_fixed(self):
        for binding in (None, {}, {**_binding(), "feature_version": "other"},
                        {**_binding(), "candidate_k": True},
                        {**_binding(), "rrf_c": True},
                        {**_binding(), "corpus_sha256": "invalid"}):
            with self.subTest(binding=binding), self.assertRaises(ValueError):
                LinearGainRouter.from_dict(_model(provenance={"binding": binding}), DIGEST)
        with self.assertRaisesRegex(ValueError, "provenance.binding"):
            LinearGainRouter.from_dict(_model(provenance={"split": "old"}), DIGEST)

    def test_engine_preserves_legacy_router_and_optional_callable_hook(self):
        class LegacyRouter:
            policy_version = "legacy"

            def decide(self, features):
                return RouteDecision(False, "legacy_lexical")

        reader = MemoryIndex.from_corpus(CORPUS)
        for index, router in enumerate((LegacyRouter(), LegacyRouter())):
            # An unrelated, noncallable field must not become a new requirement.
            if index == 1:
                router.check_binding = None
            engine = SearchEngine(reader, router=router)
            response = engine.search(SearchRequest(query="phone", mode="adaptive"))
            self.assertEqual(response.results[0].doc_id, "a")
        checked = LegacyRouter()
        checked.check_binding = Mock()
        config = RetrievalConfig()
        SearchEngine(reader, config=config, router=checked)
        checked.check_binding.assert_called_once_with(reader, config)

    def test_features_preserve_zero_gap_and_use_only_bm25_prefix(self):
        missing = QueryFeatures(False, 3, 0.5, 7, None, {"ignored": True})
        zero_gap = QueryFeatures(False, 3, 0.5, 7, 0)
        present = QueryFeatures(False, 3, 0.5, 7, 0.125)
        prefix = (math.log1p(3), 0.5, math.log1p(7))
        self.assertEqual(feature_vector(missing), (*prefix, 0.0, 1.0))
        self.assertEqual(feature_vector(zero_gap), (*prefix, 0.0, 0.0))
        self.assertEqual(feature_vector(present), (*prefix, 0.125, 0.0))
        self.assertTrue(all(type(value) is float for value in feature_vector(missing)))

    def test_prediction_scales_features_and_routes_at_inclusive_threshold(self):
        router = LinearGainRouter.from_dict(_model(), DIGEST)
        at_threshold = QueryFeatures(False, 3, 0.5, 7, None)
        below_threshold = QueryFeatures(False, 3, 0.375, 7, None)
        self.assertEqual(router.predict(at_threshold), 1.75)
        self.assertEqual(router.predict(below_threshold), 0.75)
        self.assertEqual(router.decide(at_threshold),
                         RouteDecision(True, "gain_at_or_above_threshold"))
        self.assertEqual(router.decide(below_threshold), RouteDecision(False, "gain_below_threshold"))

    def test_explicit_constraints_bypass_even_a_permissive_score(self):
        router = LinearGainRouter.from_dict(_model(threshold=-1), DIGEST)
        features = QueryFeatures(True, 3, 0.5, 7, None)
        self.assertGreater(router.predict(features), router.threshold)
        with patch.object(LinearGainRouter, "predict", side_effect=AssertionError("unexpected score")):
            self.assertEqual(router.decide(features), RouteDecision(False, "explicit_constraints"))

    def test_malformed_model_is_rejected(self):
        for changes in (
            {"schema_version": True}, {"schema_version": 2}, {"feature_version": "other"},
            {"weights": [0] * 4}, {"mean": ["0"] * 5}, {"scale": [1, 1, 1, 1, 0]},
            {"scale": [1, 1, 1, 1, -1]}, {"bias": True}, {"bias": 10**400},
            {"threshold": math.nan}, {"weights": [0, 0, 0, 0, math.inf]},
            {"provenance": None}, {"unexpected": 0},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                LinearGainRouter.from_dict(_model(**changes), DIGEST)
        missing = _model()
        missing.pop("threshold")
        with self.assertRaises(ValueError):
            LinearGainRouter.from_dict(missing, DIGEST)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            LinearGainRouter.from_dict(_model(), "not-a-digest")

    def test_trusted_factory_binds_identity_to_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            raw = json.dumps(_model()).encode("utf-8")
            path.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            settings = {"NLP_ROUTER_GAIN_MODEL": str(path), "NLP_ROUTER_GAIN_SHA256": digest.upper()}
            with patch.dict(os.environ, settings):
                router = load_router(SELECTOR, RetrievalConfig())
                self.assertEqual(router_identity(router), {
                    "name": "linear-gain",
                    "policy_version": f"linear-gain-v1:{digest}",
                    "requires_statistics": False,
                })
                path.write_bytes(raw + b"\n")
                with self.assertRaisesRegex(ValueError, "does not match"):
                    load_router(SELECTOR, RetrievalConfig())

    def test_trusted_factory_reports_missing_settings_and_invalid_json(self):
        with patch.dict(os.environ, {"NLP_ROUTER_GAIN_MODEL": "", "NLP_ROUTER_GAIN_SHA256": ""}):
            with self.assertRaisesRegex(ValueError, "NLP_ROUTER_GAIN_MODEL.*NLP_ROUTER_GAIN_SHA256"):
                load_router(SELECTOR, RetrievalConfig())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            raw = b'{"provenance":{"non_standard":NaN}}'
            settings = {
                "NLP_ROUTER_GAIN_MODEL": str(path),
                "NLP_ROUTER_GAIN_SHA256": hashlib.sha256(raw).hexdigest(),
            }
            with patch.dict(os.environ, settings):
                with self.assertRaisesRegex(ValueError, "unable to read NLP_ROUTER_GAIN_MODEL"):
                    load_router(SELECTOR, RetrievalConfig())
                path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "must contain valid JSON"):
                    load_router(SELECTOR, RetrievalConfig())

    def test_trusted_loader_rejects_duplicate_keys_at_any_depth(self):
        original = json.dumps(_model())
        raws = (
            original.replace('"bias": 0.25', '"bias": 0.25, "bias": 0.5'),
            original.replace('"candidate_k": 200',
                             '"candidate_k": 200, "candidate_k": 100'),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            for raw in raws:
                data = raw.encode("utf-8")
                path.write_bytes(data)
                settings = {"NLP_ROUTER_GAIN_MODEL": str(path),
                            "NLP_ROUTER_GAIN_SHA256": hashlib.sha256(data).hexdigest()}
                with self.subTest(raw=raw), patch.dict(os.environ, settings):
                    with self.assertRaisesRegex(ValueError, "must contain valid JSON"):
                        load_router(SELECTOR, RetrievalConfig())


if __name__ == "__main__":
    unittest.main()
