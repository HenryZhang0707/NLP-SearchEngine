"""Export pre-vector Router features from a checked index for supervised training."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts.api import SearchRequest
from contracts.index_format import sha256_of
from contracts.retrieval import RetrievalConfig, corpus_fingerprint
from evaluation.evaluate import load_queries, load_run
from offline.disk_index import DiskIndex
from online.engine import SearchEngine
from online.linear_gain_router import FEATURE_VERSION
from online.router import QueryFeatures, RouteDecision


class CaptureRouter:
    """Observe the normal engine features and keep every request lexical."""

    name = "feature-capture"
    policy_version = "feature-capture-v1"

    def __init__(self):
        self.features = None

    def decide(self, features):
        self.features = features
        return RouteDecision(False, "feature_capture")


def export_features(index, queries, lexical_run, output, *, expected_corpus_sha256,
                    candidate_k=200, depth=100):
    if not 1 <= depth <= candidate_k <= 200:
        raise ValueError("Expected 1 <= depth <= candidate_k <= 200")
    if output.exists():
        raise FileExistsError("Feature output already exists")
    records = load_queries(queries)
    ids = {record.qid for record in records}
    if not records or len(ids) != len(records):
        raise ValueError("Feature queries must have a unique ordered universe")
    reference = load_run(lexical_run)
    if reference.keys() - ids:
        raise ValueError("Lexical reference has query IDs outside the feature cohort")
    reader = DiskIndex(index)
    try:
        corpus_sha = corpus_fingerprint(reader)
        if corpus_sha != expected_corpus_sha256:
            raise ValueError("Feature index corpus fingerprint changed")
        router = CaptureRouter()
        engine = SearchEngine(reader, config=RetrievalConfig(candidate_k=candidate_k),
                              router=router)
        rows = []
        constrained = 0
        for record in records:
            router.features = None
            response = engine.search(SearchRequest(record.query, k=depth, mode="adaptive"))
            docs = [item.doc_id for item in response.results]
            expected = [doc_id for doc_id, _ in reference.get(record.qid, [])]
            if docs != expected:
                raise ValueError(f"Lexical rank order disagrees for query {record.qid!r}")
            features = router.features
            if response.parsed["explicit_constraints"]:
                if features is not None or response.routing.reason != "explicit_constraints":
                    raise ValueError("An explicit query unexpectedly reached the policy")
                # Explicit queries never enter the online policy. Preserve
                # their position in the cohort with a marked placeholder.
                features = QueryFeatures(True, 0, 0.0, 0, None)
                constrained += 1
            elif features is None or response.routing.reason != "feature_capture":
                raise ValueError("An eligible query did not yield online Router features")
            row = asdict(features)
            del row["statistics"]
            rows.append({"qid": record.qid, **row})
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
    return {"features": len(rows), "constrained": constrained, "rank_matches": len(rows),
            "corpus_sha256": corpus_sha, "queries_sha256": sha256_of(queries),
            "lexical_run_sha256": sha256_of(lexical_run), "features_sha256": sha256_of(output),
            "candidate_k": candidate_k, "rrf_c": engine.config.rrf_c,
            "feature_version": FEATURE_VERSION, "depth": depth}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("index", "queries", "lexical-run", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--expected-corpus-sha256", required=True)
    parser.add_argument("--candidate-k", type=int, default=200)
    parser.add_argument("--depth", type=int, default=100)
    args = parser.parse_args(argv)
    print(json.dumps(export_features(**vars(args)), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
