"""Compare rules-v1 routing quality against a uniform, matched vector budget.

The random policy assigns exactly B of N eligible queries to the recorded
forced-hybrid branch without replacement. Its expected metric is computed
from marginal inclusion B/N. The paired query bootstrap keeps B/N fixed and
quantifies uncertainty of adaptive minus expected random across queries.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
from statistics import fmean
import sys

from .evaluate import (
    EvaluationError,
    QueryRouting,
    _metric_for_query,
    _parse_run_row,
    _percentile,
    _unique_json_object,
    load_qrels,
    load_queries,
)


VECTOR_REASONS = {"no_lexical_hits", "low_vocab_coverage", "small_relative_gap"}
LEXICAL_REASONS = {"explicit_constraints", "lexical_sufficient"}


def require(condition, message):
    if not condition:
        raise EvaluationError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def parse_json(raw):
    return json.loads(raw, object_pairs_hook=_unique_json_object)


def read_rankings(path, universe):
    """Keep evaluator rank order while rejecting ambiguous ranks or duplicate IDs."""
    rows = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        qid, docid, rank, _ = _parse_run_row(line.split(), number, path)
        require(qid in universe, f"{path.name}: foreign qid {qid!r}")
        rows.setdefault(qid, []).append((rank, docid))
    rankings = {}
    for qid, ranking in rows.items():
        ranking.sort()
        ids = [docid for _, docid in ranking]
        require(len(set(ids)) == len(ids)
                and all(rank == expected for expected, (rank, _) in enumerate(ranking, 1)),
                f"{path.name}: duplicate document or noncontiguous/duplicate rank for {qid!r}")
        rankings[qid] = ids
    return rankings


def read_constraints(path, universe):
    if path is None:
        return set()
    ids = parse_json(path.read_bytes())
    require(isinstance(ids, list) and all(isinstance(qid, str) and qid in universe for qid in ids)
            and len(ids) == len(set(ids)), "Lexical-only constraint array has foreign or duplicate qids")
    return set(ids)


def check_journal(source, receipt, qids):
    reference = receipt["journal"]
    if reference is None:
        return None
    name = reference["file"]
    require(isinstance(name, str) and Path(name).name == name and name.endswith(".events.jsonl"),
            "Invalid adaptive request journal name")
    raw = source.with_name(name).read_bytes()
    sha = digest(raw)
    require(raw.endswith(b"\n") and sha == reference["sha256"],
            "Adaptive journal SHA256 or line termination disagrees with receipt")
    records = [parse_json(line) for line in raw.splitlines()]
    start = {"event": "START", "sequence": 0, "request": receipt["request"],
             "query_ids": qids, "query_universe_sha256": receipt["query_universe_sha256"], "policy": None}
    require(len(records) == 1 + 2 * len(qids) and records[0] == start,
            "Adaptive journal START or event count disagrees with receipt")
    for attempt, (qid, outcome) in enumerate(zip(qids, receipt["outcomes"]), 1):
        begin = {"qid": qid, "status": "INCOMPLETE", "http_status": None,
                 "latency_ms": None, "routing": None}
        require(records[2 * attempt - 1] == {
                    "event": "BEGIN", "sequence": 2 * attempt - 1, "attempt": attempt, "outcome": begin
                } and records[2 * attempt] == {
                    "event": "OUTCOME", "sequence": 2 * attempt, "attempt": attempt, "outcome": outcome
                }, f"Adaptive journal disagrees at request {attempt}")
    return sha


def read_adaptive_receipt(source, qids, constrained, adaptive, depth):
    payload = parse_json(source.read_bytes())
    require(isinstance(payload, dict), "Adaptive receipt must be an object")
    receipt = payload.get("requests", payload)
    require(isinstance(receipt, dict) and type(receipt["schema_version"]) is int
            and receipt["schema_version"] == 1 and receipt["status"] == "COMPLETE",
            "Adaptive request receipt must be finalized")
    request = receipt["request"]
    require(request["mode"] == "adaptive" and request["scorer"] == "bm25"
            and request["expander"] == "none" and receipt["policy"] is None
            and type(request["k"]) is int and request["k"] >= depth,
            "Adaptive request mode, unsupported policy or retrieval depth disagrees")
    universe_sha = digest(json.dumps(qids, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    require(receipt["query_ids"] == qids and receipt["query_universe_sha256"] == universe_sha,
            "Adaptive receipt ordered query universe disagrees with queries")
    n_queries = len(qids)
    expected = {"requested_queries": n_queries, "attempted_requests": n_queries, "http_200": n_queries,
                "routing_observed_queries": n_queries, "expected_unsupported": 0, "unexpected_errors": 0,
                "last_event_sequence": 2 * n_queries}
    require(all(type(receipt[key]) is int and receipt[key] == value for key, value in expected.items())
            and receipt["http_success_coverage"] == 1.0 and receipt["protocol_accounted_coverage"] == 1.0
            and receipt["inflight_outcome"] is None, "Adaptive receipt counters or coverage disagree")
    outcomes = receipt["outcomes"]
    require(isinstance(outcomes, list) and len(outcomes) == n_queries
            and receipt["last_outcome"] == outcomes[-1], "Adaptive receipt outcomes are incomplete")
    routing = {}
    for qid, outcome in zip(qids, outcomes):
        require(outcome["qid"] == qid and outcome["status"] == "SUCCESS"
                and type(outcome["http_status"]) is int and outcome["http_status"] == 200,
                f"Adaptive receipt outcome disagrees at {qid!r}")
        route = QueryRouting.from_response(outcome["routing"], "adaptive")
        require(not route.fallback, f"Adaptive fallback invalidates budget comparison at {qid!r}")
        reasons = VECTOR_REASONS if route.vector_used else LEXICAL_REASONS
        require(route.reason in reasons, f"Unknown or inconsistent rules-v1 reason at {qid!r}")
        require((route.reason == "explicit_constraints") == (qid in constrained),
                f"Predeclared lexical-only qids disagree with explicit_constraints at {qid!r}")
        routing[qid] = route
    if "per_query" in payload:
        per_query = payload["per_query"]
        require(isinstance(per_query, list) and len(per_query) == n_queries,
                "Adaptive metrics per_query coverage disagrees")
        checked = set()
        for row in per_query:
            qid = row["qid"]
            require(qid in routing and qid not in checked and type(row["retrieved"]) is int
                    and row["retrieved"] == len(adaptive.get(qid, []))
                    and QueryRouting.from_response(row["routing"], "adaptive") == routing[qid],
                    f"Adaptive metrics ranking count or routing disagrees at {qid!r}")
            checked.add(qid)
    return routing, universe_sha, check_journal(source, receipt, qids)


def compare_router_budget(queries, qrels, lexical_run, hybrid_run, adaptive_run, adaptive_receipt,
                          *, lexical_only=None, k=10, map_k=100, samples=10_000, seed=2026,
                          confidence=0.95):
    """Validate three rankings and estimate the quality of an exact-budget control."""
    require(all(type(value) is int and value > 0 for value in (k, map_k, samples)),
            "Cutoffs and bootstrap samples must be positive integers")
    require(type(seed) is int and math.isfinite(confidence) and 0 < confidence < 1,
            "Bootstrap seed must be an integer and confidence must be between zero and one")
    inputs = {key: Path(value) for key, value in {
        "queries": queries, "qrels": qrels, "lexical_run": lexical_run, "hybrid_run": hybrid_run,
        "adaptive_run": adaptive_run, "adaptive_receipt": adaptive_receipt, "lexical_only": lexical_only,
    }.items() if value is not None}
    input_hashes = {key: digest(path.read_bytes()) for key, path in inputs.items()}
    qids = [row.qid for row in load_queries(inputs["queries"])]
    universe = set(qids)
    require(qids and len(qids) == len(universe), "Query universe is empty or has duplicate qids")
    judgments = load_qrels(inputs["qrels"])
    require(set(judgments) == universe, "Qrels do not cover the exact query universe")
    constrained = read_constraints(inputs.get("lexical_only"), universe)
    runs = {mode: read_rankings(inputs[f"{mode}_run"], universe)
            for mode in ("lexical", "hybrid", "adaptive")}
    routing, universe_sha, journal_sha = read_adaptive_receipt(
        inputs["adaptive_receipt"], qids, constrained, runs["adaptive"], max(k, map_k))
    reasons = Counter()
    empty = Counter()
    budget = sum(route.vector_attempted for route in routing.values())
    eligible = len(qids) - len(constrained)
    probability = budget / eligible if eligible else 0.0
    metrics = (f"ndcg_at_{k}", f"recall_at_{k}", f"mrr_at_{k}", f"map_at_{map_k}")
    score_rows = []
    difference_rows = []
    for qid in qids:
        route = routing[qid]
        reasons[route.reason] += 1
        lexical, hybrid, adaptive = (runs[mode].get(qid, []) for mode in ("lexical", "hybrid", "adaptive"))
        selected = hybrid if route.vector_used else lexical
        require(adaptive == selected, f"Adaptive ranking differs from its selected branch for {qid!r}")
        for mode, ranking in (("lexical", lexical), ("hybrid", hybrid), ("adaptive", adaptive)):
            if not ranking:
                require(mode in ("adaptive", route.executed_mode)
                        or (mode == "lexical" and route.reason == "no_lexical_hits")
                        or qid in constrained, f"Uncorroborated empty {mode} ranking for {qid!r}")
                empty[mode] += 1
        lexical_scores = _metric_for_query(judgments[qid], lexical, k, map_k)[:4]
        hybrid_scores = (lexical_scores if qid in constrained
                         else _metric_for_query(judgments[qid], hybrid, k, map_k)[:4])
        adaptive_scores = hybrid_scores if route.vector_used else lexical_scores
        expected_scores = tuple((1 - probability) * left + probability * right
                                for left, right in zip(lexical_scores, hybrid_scores))
        score_rows.append((lexical_scores, hybrid_scores, adaptive_scores, expected_scores))
        difference_rows.append(tuple(actual - expected for actual, expected in zip(adaptive_scores, expected_scores)))

    rng = random.Random(seed)
    draws = [[] for _ in metrics]
    if all(all(value == 0.0 for value in row) for row in difference_rows):
        for metric_draws in draws:
            metric_draws.append(0.0)
    else:
        for _ in range(samples):
            paired = [difference_rows[rng.randrange(len(qids))] for _ in qids]
            for metric_draws, values in zip(draws, zip(*paired)):
                metric_draws.append(fmean(values))
    alpha = (1 - confidence) / 2
    averages = {}
    columns = tuple(zip(*score_rows))
    for index, metric in enumerate(metrics):
        averages[metric] = dict(zip(
            ("forced_lexical", "constraint_aware_always_hybrid", "observed_adaptive", "expected_uniform_fixed_budget"),
            (fmean(row[index] for row in column) for column in columns),
        ))
        averages[metric].update(
            adaptive_minus_expected_random=fmean(row[index] for row in difference_rows),
            paired_query_bootstrap_ci=[_percentile(draws[index], alpha), _percentile(draws[index], 1 - alpha)],
        )
    require(all(digest(path.read_bytes()) == input_hashes[key] for key, path in inputs.items()),
            "An input changed during the budget analysis")
    return {
        "schema_version": 1,
        "analysis_scope": "Offline quality control on this judged universe with recorded branches. "
                          "Latency and causal or cross-dataset conclusions are outside this analysis.",
        "routing_evidence": "Rules-v1 reason and routing consistency from evaluator outcomes. "
                            "The evaluator receipt does not identify the deployed router implementation.",
        "inputs_sha256": {**input_hashes, "adaptive_journal": journal_sha, "ordered_query_universe": universe_sha},
        "coverage": {"queries": len(qids), "judged_queries": len(judgments), "adaptive_http_200": len(routing),
                     "lexical_only_queries": len(constrained),
                     "empty_rankings": {mode: empty[mode] for mode in runs}, "routing_reasons": dict(reasons)},
        "vector_budget": {"eligible_queries": eligible, "observed_adaptive_calls": budget,
                          "forced_lexical_calls": 0, "constraint_aware_always_hybrid_calls": eligible,
                          "uniform_fixed_budget_calls": budget, "eligible_inclusion_probability": probability},
        "bootstrap": {"samples": samples, "seed": seed, "confidence": confidence,
                      "method": "Paired query percentile bootstrap with the original inclusion probability fixed."},
        "metrics": averages,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("queries", "qrels", "lexical-run", "hybrid-run", "adaptive-run", "adaptive-receipt"):
        parser.add_argument(f"--{flag}", type=Path, required=True)
    parser.add_argument("--lexical-only", type=Path, help="Predeclared JSON array of lexical-only qids")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--map-k", type=int, default=100)
    parser.add_argument("--samples", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--output-json", type=Path)
    args = vars(parser.parse_args(argv))
    output_path = args.pop("output_json")
    try:
        output = json.dumps(compare_router_budget(**args), indent=2, ensure_ascii=False, allow_nan=False)
        if output_path is not None:
            with output_path.open("x", encoding="utf-8") as handle:
                handle.write(output + "\n")
        print(output)
    except (ValueError, OSError, KeyError, TypeError, OverflowError) as error:
        print(f"router budget error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
