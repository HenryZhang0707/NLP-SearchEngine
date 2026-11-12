"""Optional linear gain model for the existing BM25-first routing policy seam."""

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from contracts.retrieval import RetrievalConfig, corpus_fingerprint
from online.router import QueryFeatures, RouteDecision


FEATURE_VERSION = "bm25-prefix-v1"
_FEATURE_COUNT = 5
_MODEL_FIELDS = {
    "schema_version", "feature_version", "mean", "scale", "weights", "bias",
    "threshold", "provenance",
}


def feature_vector(features: QueryFeatures) -> tuple[float, ...]:
    gap = features.relative_gap
    return (
        math.log1p(features.n_terms),
        float(features.vocab_coverage),
        math.log1p(features.lexical_hits),
        0.0 if gap is None else float(gap),
        float(gap is None),
    )


def predict_gain(features: QueryFeatures, mean, scale, weights, bias) -> float:
    """Share score arithmetic between development calibration and serving."""
    try:
        gain = bias + math.fsum(
            weight * ((feature - center) / spread)
            for feature, center, spread, weight in zip(
                feature_vector(features), mean, scale, weights, strict=True
            )
        )
    except OverflowError as error:
        raise ValueError("gain model produced a non-finite prediction") from error
    if not math.isfinite(gain):
        raise ValueError("gain model produced a non-finite prediction")
    return gain


def _digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("gain model SHA256 must be a 64-digit hexadecimal digest")
    return value.lower()


def _finite_number(value, field: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"gain model {field} must contain finite numbers")
    try:
        number = float(value)
    except OverflowError as error:
        raise ValueError(f"gain model {field} must contain finite numbers") from error
    if not math.isfinite(number):
        raise ValueError(f"gain model {field} must contain finite numbers")
    return number


def _coefficients(value, field: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != _FEATURE_COUNT:
        raise ValueError(f"gain model {field} must be an array of {_FEATURE_COUNT} numbers")
    return tuple(_finite_number(entry, field) for entry in value)


@dataclass(frozen=True)
class LinearGainRouter:
    name = "linear-gain"
    requires_statistics = False

    mean: tuple[float, ...]
    scale: tuple[float, ...]
    weights: tuple[float, ...]
    bias: float
    threshold: float
    model_sha256: str
    binding_corpus_sha256: str
    binding_candidate_k: int
    binding_rrf_c: float

    @classmethod
    def from_dict(cls, value, model_sha256: str) -> "LinearGainRouter":
        digest = _digest(model_sha256)
        if not isinstance(value, dict) or value.keys() != _MODEL_FIELDS:
            raise ValueError("gain model fields do not match schema version 1")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError("unsupported gain model schema version")
        if value["feature_version"] != FEATURE_VERSION:
            raise ValueError("unsupported gain model feature version")
        if not isinstance(value["provenance"], dict):
            raise ValueError("gain model provenance must be a JSON object")
        binding = value["provenance"].get("binding")
        if (not isinstance(binding, dict) or binding.keys() != {
                "corpus_sha256", "candidate_k", "rrf_c", "feature_version"}):
            raise ValueError("gain model provenance.binding must specify corpus_sha256, "
                             "candidate_k, rrf_c and feature_version")
        corpus_sha256 = _digest(binding["corpus_sha256"])
        if type(binding["candidate_k"]) is not int or binding["candidate_k"] < 1:
            raise ValueError("gain model binding candidate_k must be a positive integer")
        rrf_c = _finite_number(binding["rrf_c"], "binding rrf_c")
        if rrf_c <= 0:
            raise ValueError("gain model binding rrf_c must be positive")
        if binding["feature_version"] != FEATURE_VERSION:
            raise ValueError("unsupported gain model binding feature version")

        scale = _coefficients(value["scale"], "scale")
        if any(entry <= 0 for entry in scale):
            raise ValueError("gain model scale must contain positive numbers")
        return cls(
            mean=_coefficients(value["mean"], "mean"),
            scale=scale,
            weights=_coefficients(value["weights"], "weights"),
            bias=_finite_number(value["bias"], "bias"),
            threshold=_finite_number(value["threshold"], "threshold"),
            model_sha256=digest,
            binding_corpus_sha256=corpus_sha256,
            binding_candidate_k=binding["candidate_k"],
            binding_rrf_c=rrf_c,
        )

    def check_binding(self, reader, config: RetrievalConfig) -> None:
        """Reject deployment outside the corpus/configuration used for calibration."""
        if config.candidate_k != self.binding_candidate_k:
            raise ValueError("gain model binding candidate_k does not match retrieval config")
        if config.rrf_c != self.binding_rrf_c:
            raise ValueError("gain model binding rrf_c does not match retrieval config")
        if corpus_fingerprint(reader) != self.binding_corpus_sha256:
            raise ValueError("gain model binding corpus_sha256 does not match actual corpus")

    @property
    def policy_version(self) -> str:
        return f"linear-gain-v1:{self.model_sha256}"

    def predict(self, features: QueryFeatures) -> float:
        return predict_gain(features, self.mean, self.scale, self.weights, self.bias)

    def decide(self, features: QueryFeatures) -> RouteDecision:
        if features.explicit_constraints:
            return RouteDecision(False, "explicit_constraints")
        if self.predict(features) >= self.threshold:
            return RouteDecision(True, "gain_at_or_above_threshold")
        return RouteDecision(False, "gain_below_threshold")


def _reject_json_constant(value):
    raise ValueError(f"non-standard JSON constant {value}")


def _unique_json_object(pairs):
    value = {}
    for key, entry in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = entry
    return value


def load_gain_router(config: RetrievalConfig) -> LinearGainRouter:
    """Load the locally configured, hash-pinned model through the router factory seam."""
    model_path = os.environ.get("NLP_ROUTER_GAIN_MODEL")
    expected_digest = os.environ.get("NLP_ROUTER_GAIN_SHA256")
    if not model_path or not expected_digest:
        raise ValueError("NLP_ROUTER_GAIN_MODEL and NLP_ROUTER_GAIN_SHA256 are required")
    digest = _digest(expected_digest)
    try:
        raw = Path(model_path).read_bytes()
    except (OSError, ValueError) as error:
        raise ValueError("unable to read NLP_ROUTER_GAIN_MODEL") from error
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("gain model SHA256 does not match NLP_ROUTER_GAIN_SHA256")
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant,
                           object_pairs_hook=_unique_json_object)
    except ValueError as error:
        raise ValueError("NLP_ROUTER_GAIN_MODEL must contain valid JSON") from error
    return LinearGainRouter.from_dict(value, digest)
